# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the parity-rig 5-tier panel verifier.

Covers the record schema, the per-tier collectors (against synthetic
artifacts), the pairwise comparator, the known-transform classifier,
and the round-trip ``to_jsonable`` / ``from_jsonable``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
VERIFIER_PARENT = ROOT / "parity-rig"
sys.path.insert(0, str(VERIFIER_PARENT))

from conftest import write_dashboard_ir_artifact  # noqa: E402
from verifier import cli as verifier_cli  # noqa: E402
from verifier import collectors, compare  # noqa: E402
from verifier.records import DRIFT_AXES, PanelRecord, Verdict  # noqa: E402

# --------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------- #


def _make_record(**overrides: Any) -> PanelRecord:
    defaults = dict(
        panel_id="p1",
        title="My Panel",
        dashboard_uid="dash-1",
        dashboard_title="My Dashboard",
        t0_source_promql="rate(http_requests_total[5m])",
        t1_translator_esql=(
            "TS metrics-* | STATS x = AVG(RATE(http_requests_total)) "
            "BY time_bucket = TBUCKET(5 minute)"
        ),
    )
    defaults.update(overrides)
    return PanelRecord(**defaults)


def _build_migration_report(panels: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "dashboards": [
            {
                "uid": "dash-1",
                "title": "My Dashboard",
                "panels": panels,
            }
        ]
    }


def test_scope_report_to_panels_keeps_only_sampled_panels() -> None:
    report = _build_migration_report(
        [{"title": "A"}, {"title": "B"}, {"title": "C"}]
    )
    scoped = verifier_cli._scope_report_to_panels(
        report, {("My Dashboard", "A"), ("My Dashboard", "C")}
    )
    titles = [p["title"] for p in scoped["dashboards"][0]["panels"]]
    assert titles == ["A", "C"]


def test_scope_report_to_panels_drops_dashboards_with_no_sampled_panels() -> None:
    report = _build_migration_report([{"title": "A"}, {"title": "B"}])
    scoped = verifier_cli._scope_report_to_panels(report, {("Other", "A")})
    assert scoped["dashboards"] == []


def test_load_compiled_panels_reads_compiled_dashboards_ndjson(tmp_path) -> None:
    compiled = tmp_path / "my_dash"
    compiled.mkdir(parents=True)
    panels_json = json.dumps([
        {
            "panelIndex": "1",
            "type": "lens",
            "embeddableConfig": {
                "attributes": {
                    "title": "A",
                    "state": {"query": {"esql": "FROM metrics-* | LIMIT 1"}},
                }
            },
        }
    ])
    (compiled / "compiled_dashboards.ndjson").write_text(
        json.dumps({"type": "dashboard", "attributes": {"panelsJSON": panels_json}})
        + "\n"
    )
    # The index is keyed by dashboard, then panel title. This saved object
    # carries no ``attributes.title``, so it lands under the empty key and
    # stays reachable through the single-dashboard fallback.
    assert verifier_cli._load_compiled_panels(tmp_path) == {
        "": {"A": "FROM metrics-* | LIMIT 1"}
    }


def test_load_compiled_panels_reads_every_compiled_dashboard(tmp_path) -> None:
    """T3 must not come from whichever dashboard happens to sort first.

    ``_load_compiled_panels`` used to ``return`` on the first
    ``compiled/<slug>/`` subdirectory it found, so in a multi-dashboard run
    every panel's T3 was read out of one arbitrary dashboard.
    """
    def _write(slug: str, dashboard_title: str, esql: str) -> None:
        sub = tmp_path / slug
        sub.mkdir(parents=True)
        panels_json = json.dumps([
            {
                "panelIndex": "1",
                "type": "lens",
                "embeddableConfig": {
                    "attributes": {"title": "Error Logs", "state": {"query": {"esql": esql}}}
                },
            }
        ])
        (sub / "compiled_dashboards.ndjson").write_text(
            json.dumps({
                "type": "dashboard",
                "attributes": {"title": dashboard_title, "panelsJSON": panels_json},
            })
            + "\n"
        )

    _write("aaa_kafka", "Kafka", "FROM logs-* | WHERE service.name == \"kafka\"")
    _write("zzz_redis", "Redis", "FROM logs-* | WHERE service.name == \"redis\"")

    index = verifier_cli._load_compiled_panels(tmp_path)

    assert sorted(index) == ["Kafka", "Redis"]
    assert index["Redis"]["Error Logs"] == 'FROM logs-* | WHERE service.name == "redis"'


def test_load_compiled_panels_ignores_legacy_yaml_ndjson(tmp_path) -> None:
    """``yaml.ndjson`` is no longer a compiled-NDJSON source.

    It was the last reader of that filename anywhere in the repo, and nothing
    writes it, so the branch could only ever mask a missing
    ``compiled_dashboards.ndjson`` with stale input.
    """
    compiled = tmp_path / "my_dash"
    compiled.mkdir(parents=True)
    (compiled / "yaml.ndjson").write_text(
        json.dumps({"type": "dashboard", "attributes": {"panelsJSON": "[]"}}) + "\n"
    )
    assert verifier_cli._load_compiled_panels(tmp_path) == {}


# --------------------------------------------------------------------- #
# PanelRecord schema
# --------------------------------------------------------------------- #


class TestPanelRecord:
    def test_to_and_from_jsonable_roundtrip(self):
        record = _make_record(
            t1_warnings=["w1"],
            drift_axes=["T1=T2"],
            drift_details={"T1=T2": "added EVAL legend"},
            verdict=Verdict.PASS,
        )
        blob = record.to_jsonable()
        restored = PanelRecord.from_jsonable(blob)
        assert restored.title == record.title
        assert restored.t1_warnings == ["w1"]
        assert restored.drift_axes == ["T1=T2"]
        assert restored.verdict == Verdict.PASS

    def test_jsonable_is_json_serialisable(self):
        record = _make_record()
        blob = record.to_jsonable()
        # Must round-trip through JSON without TypeError.
        text = json.dumps(blob)
        assert "My Panel" in text

    def test_default_verdict_is_skip(self):
        record = _make_record(t1_translator_esql="")
        assert record.verdict == Verdict.SKIP


# --------------------------------------------------------------------- #
# Collectors — migration_report.json
# --------------------------------------------------------------------- #


class TestMigrationReportCollector:
    def test_panels_from_migration_report_extracts_promql_and_esql(self, tmp_path):
        report = _build_migration_report(
            [
                {
                    "source_panel_id": "panel-7",
                    "title": "HTTP Requests",
                    "status": "migrated",
                    "readiness": "feasible",
                    "promql": "rate(foo[5m])",
                    "esql": "TS metrics-* | STATS x = AVG(RATE(foo))",
                    "grafana_type": "timeseries",
                    "kibana_type": "lens",
                }
            ]
        )
        records = list(collectors.panels_from_migration_report(report))
        assert len(records) == 1
        r = records[0]
        assert r.panel_id == "panel-7"
        assert r.t0_source_promql == "rate(foo[5m])"
        assert r.t1_translator_esql.startswith("TS metrics-*")
        assert r.dashboard_uid == "dash-1"
        assert r.dashboard_title == "My Dashboard"
        assert r.grafana_type == "timeseries"
        assert r.kibana_type == "lens"

    def test_panels_from_migration_report_falls_back_to_query_ir(self):
        report = _build_migration_report(
            [
                {
                    "title": "Fallback panel",
                    "query_ir": {
                        "source_expression": "node_load1{instance=~\".*\"}"
                    },
                    "esql": "",
                }
            ]
        )
        records = list(collectors.panels_from_migration_report(report))
        assert records[0].t0_source_promql == 'node_load1{instance=~".*"}'

    def test_datadog_report_key_is_read_for_t1(self):
        """Datadog writes ``esql_query``; Grafana writes ``esql``.

        Reading only ``esql`` left T1 empty for every Datadog panel, and
        ``compare_panel_record`` short-circuits an empty T1 to SKIP -- so a
        Datadog run verified as "all SKIP, zero drift on every axis"
        regardless of what the translator emitted.
        """
        report = _build_migration_report(
            [
                {
                    "widget_id": "8013519185925578",
                    "title": "Redis commands",
                    "status": "ok",
                    "dd_widget_type": "timeseries",
                    "kibana_type": "lens",
                    "esql_query": "TS metrics-* | STATS x = AVG(RATE(redis.net.commands))",
                    "query_ir": {"source_expression": "avg:redis.net.commands{*}"},
                }
            ]
        )

        records = list(collectors.panels_from_migration_report(report))

        assert len(records) == 1
        r = records[0]
        assert r.t1_translator_esql.startswith("TS metrics-*")
        assert r.t1_index == "metrics-*"
        assert r.t0_source_promql == "avg:redis.net.commands{*}"
        # The record's source-panel-type slot is filled from the Datadog key.
        assert r.grafana_type == "timeseries"
        assert r.panel_id == "8013519185925578"
        assert compare.compare_panel_record(r) is not Verdict.SKIP

    def test_native_promql_detection(self):
        report = _build_migration_report(
            [
                {
                    "title": "Native",
                    "promql": "up",
                    "esql": "PROMQL index=metrics-* step=1m value=(up)",
                }
            ]
        )
        records = list(collectors.panels_from_migration_report(report))
        assert records[0].t1_native_promql is True
        assert records[0].t1_index == "metrics-*"


# --------------------------------------------------------------------- #
# Collectors — IR export (T2)
# --------------------------------------------------------------------- #


class TestIrCollector:
    """T2 is sourced from ``ir/*.ir.json``, not the dashboard YAML.

    The IR's ``visual.presentation.config.query`` is the same string the YAML
    export carried in ``esql.query`` (the YAML is derived from the IR), so
    these assertions are the pre-port ones with the artifact swapped.
    """

    def test_load_ir_panels_extracts_esql_query(self, tmp_path):
        write_dashboard_ir_artifact(
            tmp_path,
            {
                "name": "Dash",
                "panels": [
                    {
                        "title": "section-1",
                        "section": {
                            "panels": [
                                {
                                    "title": "A",
                                    "esql": {
                                        "query": "FROM metrics-* | STATS x = COUNT(*)"
                                    },
                                },
                                {
                                    "title": "B",
                                    "markdown": {"content": "Migration Required"},
                                },
                            ]
                        },
                    }
                ],
            },
        )
        out = collectors.load_ir_panels(tmp_path / "ir")
        assert out["A"].startswith("FROM metrics-*")
        # markdown panels yield an empty query (still mapped, so we know
        # they exist).
        assert out["B"] == ""

    def test_load_ir_panels_handles_nested_sections(self, tmp_path):
        write_dashboard_ir_artifact(
            tmp_path,
            {
                "name": "D",
                "panels": [
                    {
                        "title": "outer",
                        "section": {
                            "panels": [
                                {
                                    "title": "inner-section",
                                    "section": {
                                        "panels": [
                                            {
                                                "title": "deep",
                                                "esql": {"query": "TS metrics-*"},
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                    }
                ],
            },
            stem="nested",
        )
        out = collectors.load_ir_panels(tmp_path / "ir")
        assert "deep" in out
        assert out["deep"] == "TS metrics-*"

    def test_load_ir_panels_returns_empty_when_no_artifacts(self, tmp_path):
        assert collectors.load_ir_panels(tmp_path / "ir") == {}


class TestIrCollectorDashboardScoping:
    """T2 must be joined per dashboard, never by panel title alone.

    ``load_ir_panels`` folds every dashboard in the directory into one
    title-keyed dict. A multi-dashboard run (what ``--input-dir`` produces)
    then silently gives one dashboard's panel another dashboard's query
    whenever the titles match, which fabricates T1=T2 drift findings.
    """

    def _two_dashboards_sharing_a_panel_title(self, tmp_path):
        for stem, name, esql in (
            ("kafka", "Kafka", 'FROM logs-* | WHERE service.name == "kafka"'),
            ("redis", "Redis", 'FROM logs-* | WHERE service.name == "redis"'),
        ):
            write_dashboard_ir_artifact(
                tmp_path,
                {
                    "name": name,
                    "panels": [{"title": "Error Logs", "esql": {"query": esql}}],
                },
                stem=stem,
            )
        return tmp_path / "ir"

    def test_flat_reader_collapses_shared_titles(self, tmp_path):
        """Documents the collapse the scoped reader exists to avoid."""
        ir_dir = self._two_dashboards_sharing_a_panel_title(tmp_path)

        flat = collectors.load_ir_panels(ir_dir)

        assert list(flat) == ["Error Logs"]
        # Last writer wins by sorted filename: Kafka's query is simply gone.
        assert "redis" in flat["Error Logs"]

    def test_scoped_reader_keeps_each_dashboard_separate(self, tmp_path):
        ir_dir = self._two_dashboards_sharing_a_panel_title(tmp_path)

        scoped = collectors.load_ir_panels_by_dashboard(ir_dir)

        assert scoped["Kafka"]["Error Logs"] == 'FROM logs-* | WHERE service.name == "kafka"'
        assert scoped["Redis"]["Error Logs"] == 'FROM logs-* | WHERE service.name == "redis"'

    def test_scoped_reader_drops_keys_two_dashboards_claim(self, tmp_path):
        """A duplicated dashboard title is unjoinable, so refuse to guess."""
        for stem in ("copy_a", "copy_b"):
            write_dashboard_ir_artifact(
                tmp_path,
                {
                    "name": "Same Title",
                    "panels": [{"title": "P", "esql": {"query": f"FROM {stem}"}}],
                },
                stem=stem,
            )

        scoped = collectors.load_ir_panels_by_dashboard(tmp_path / "ir")

        assert "Same Title" not in scoped

    def test_scoped_reader_returns_empty_when_no_artifacts(self, tmp_path):
        assert collectors.load_ir_panels_by_dashboard(tmp_path / "ir") == {}


class TestScopedTierLookup:
    """``_scoped_panel_query`` resolves a tier within the record's dashboard."""

    def test_prefers_the_records_own_dashboard(self):
        index = {
            "Kafka": {"Error Logs": "kafka-query"},
            "Redis": {"Error Logs": "redis-query"},
        }
        record = _make_record(title="Error Logs", dashboard_title="Kafka", dashboard_uid="")

        assert verifier_cli._scoped_panel_query(index, record, "T2", "IR export") == "kafka-query"
        assert record.notes == []

    def test_matches_on_uid_when_present(self):
        index = {"dash-uid-1": {"P": "by-uid"}, "Other": {"P": "by-title"}}
        record = _make_record(title="P", dashboard_uid="dash-uid-1", dashboard_title="Other")

        assert verifier_cli._scoped_panel_query(index, record, "T2", "IR export") == "by-uid"

    def test_single_dashboard_index_needs_no_key_match(self):
        """The documented per-dashboard shape stays joinable without a title."""
        index = {"": {"P": "only-dashboard"}}
        record = _make_record(title="P", dashboard_title="Whatever", dashboard_uid="uid")

        assert verifier_cli._scoped_panel_query(index, record, "T3", "compiled NDJSON") == "only-dashboard"
        assert record.notes == []

    def test_unresolvable_dashboard_reports_rather_than_guesses(self):
        index = {"Kafka": {"P": "kafka"}, "Redis": {"P": "redis"}}
        record = _make_record(title="P", dashboard_title="Postgres", dashboard_uid="")

        assert verifier_cli._scoped_panel_query(index, record, "T2", "IR export") == ""
        assert any("T2 unavailable" in note for note in record.notes)

    def test_absent_artifacts_are_silent(self):
        """No artifacts at all is already reported as NOT_UPLOADED."""
        record = _make_record(title="P", dashboard_title="Postgres")

        assert verifier_cli._scoped_panel_query({}, record, "T3", "compiled NDJSON") == ""
        assert record.notes == []


# --------------------------------------------------------------------- #
# Collectors — compiled NDJSON / cluster
# --------------------------------------------------------------------- #


class TestNdjsonCollector:
    def _ndjson_with_panel(self, title: str, esql: str) -> str:
        panel_obj = {
            "panelIndex": "1",
            "type": "lens",
            "embeddableConfig": {
                "attributes": {
                    "title": title,
                    "state": {"query": {"esql": esql}},
                }
            },
            "gridData": {"x": 0, "y": 0, "w": 24, "h": 12, "i": "1"},
        }
        dashboard_obj = {
            "id": "dash-id-1",
            "type": "dashboard",
            "attributes": {
                "title": "Dash",
                "panelsJSON": json.dumps([panel_obj]),
            },
        }
        return json.dumps(dashboard_obj)

    def test_load_ndjson_panels(self, tmp_path):
        ndjson = tmp_path / "compiled_dashboards.ndjson"
        ndjson.write_text(self._ndjson_with_panel("A", "FROM metrics-*"))
        out = collectors.load_ndjson_panels(ndjson)
        assert out == {"A": "FROM metrics-*"}

    def test_load_ndjson_panels_handles_missing_file(self, tmp_path):
        assert collectors.load_ndjson_panels(tmp_path / "nope.ndjson") == {}

    def test_cluster_dashboard_panels_uses_same_parser(self):
        saved_object = {
            "id": "x",
            "attributes": {
                "panelsJSON": json.dumps(
                    [
                        {
                            "embeddableConfig": {
                                "attributes": {
                                    "title": "Z",
                                    "state": {
                                        "query": {"esql": "TS metrics-*"}
                                    },
                                }
                            }
                        }
                    ]
                )
            },
        }
        assert collectors.cluster_dashboard_panels(saved_object) == {
            "Z": "TS metrics-*"
        }


# --------------------------------------------------------------------- #
# Collectors — live query auto-params
# --------------------------------------------------------------------- #


class TestRunClusterQueryAutoparams:
    def test_autoparams_supplies_tstart_tend_when_referenced(self):
        params = collectors._autoparams_for_esql(
            "FROM metrics-* | WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend"
        )
        keys = [next(iter(p.keys())) for p in params]
        assert keys == ["_tend", "_tstart"]

    def test_autoparams_empty_when_no_named_refs(self):
        assert collectors._autoparams_for_esql("FROM metrics-* | LIMIT 1") == []

    def test_autoparams_provides_empty_string_for_unknown_named_params(self):
        params = collectors._autoparams_for_esql(
            "FROM metrics-* | WHERE host == ?host"
        )
        assert params == [{"host": ""}]

    def test_autoparams_does_not_treat_identifier_as_value_param(self):
        params = collectors._autoparams_for_esql(
            "TS metrics-* | STATS value = SUM(metric) BY grouping = ??grouping"
        )
        assert params == []

    def test_run_cluster_query_omits_authorization_for_empty_key(self, monkeypatch):
        seen = {}

        class Response:
            status_code = 200

            def json(self):
                return {"columns": [], "values": []}

        def fake_post(url, *, headers, json, timeout):
            seen["headers"] = headers
            return Response()

        monkeypatch.setattr(collectors.requests, "post", fake_post)

        status, _body = collectors.run_cluster_query(
            "http://localhost:9200", "", "FROM metrics-* | LIMIT 1"
        )

        assert status == 200
        assert "Authorization" not in seen["headers"]
        assert seen["headers"]["Content-Type"] == "application/json"

    def test_fetch_cluster_dashboard_omits_authorization_for_empty_key(self, monkeypatch):
        seen = {}

        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"id": "dash-1", "attributes": {"panelsJSON": "[]"}}

        def fake_get(url, *, headers, timeout):
            seen["headers"] = headers
            return Response()

        monkeypatch.setattr(collectors.requests, "get", fake_get)

        body = collectors.fetch_cluster_dashboard(
            "http://localhost:5601", "", "dash-1"
        )

        assert body["id"] == "dash-1"
        assert "Authorization" not in seen["headers"]
        assert seen["headers"]["kbn-xsrf"] == "verifier"


# --------------------------------------------------------------------- #
# Comparator
# --------------------------------------------------------------------- #


class TestCompare:
    def test_canonicalise_collapses_whitespace(self):
        assert compare.canonicalise("FROM\n  metrics-*\n   | LIMIT  1") == (
            "FROM metrics-* | LIMIT 1"
        )

    def test_identical_tiers_yield_pass(self):
        esql = "FROM metrics-* | LIMIT 1"
        record = _make_record(
            t1_translator_esql=esql,
            t2_ir_esql=esql,
            t3_ndjson_esql=esql,
            t4_cluster_esql=esql,
            t5_live_query_body=esql,
        )
        verdict = compare.compare_panel_record(record)
        assert verdict == Verdict.PASS
        assert record.drift_axes == []

    def test_t1_t2_gauge_constants_splice_classified_as_known_transform(self):
        t1 = "PROMQL index=metrics-* step=1m value=(100 * (1 - avg(rate(up[5m]))))"
        t2 = (
            t1
            + "\n| EVAL _gauge_min = 0, _gauge_max = 100, _gauge_goal = 85"
        )
        record = _make_record(
            t1_translator_esql=t1, t2_ir_esql=t2, t3_ndjson_esql=t2, t4_cluster_esql=t2,
        )
        compare.compare_panel_record(record)
        assert "T1=T2" not in record.drift_axes, (
            f"gauge constants splice should NOT count as drift; got: {record.drift_axes}"
        )

    def test_t1_t2_legend_splice_classified_as_known_transform(self):
        t1 = (
            "PROMQL index=metrics-* step=1m value=(up)\n"
            "| EVAL method = \"GET\"\n"
            "| KEEP step, value, method"
        )
        t2 = (
            "PROMQL index=metrics-* step=1m value=(up)\n"
            "| EVAL method = \"GET\"\n"
            "| EVAL legend = CONCAT(COALESCE(method, \"\"), \" - \", COALESCE(status, \"\"))\n"
            "| KEEP step, value, method, legend"
        )
        record = _make_record(t1_translator_esql=t1, t2_ir_esql=t2, t3_ndjson_esql=t2, t4_cluster_esql=t2)
        compare.compare_panel_record(record)
        assert "T1=T2" not in record.drift_axes, (
            f"composite-legend splice should NOT count as drift; got: {record.drift_axes}"
        )

    def test_real_canonical_mismatch_reported_as_drift(self):
        record = _make_record(
            t1_translator_esql="FROM metrics-* | LIMIT 1",
            t2_ir_esql="FROM other-index | LIMIT 1",
            t3_ndjson_esql="FROM other-index | LIMIT 1",
            t4_cluster_esql="FROM other-index | LIMIT 1",
        )
        verdict = compare.compare_panel_record(record)
        assert verdict == Verdict.DRIFT
        assert "T1=T2" in record.drift_axes
        assert "canonical-mismatch" in record.drift_details["T1=T2"]

    def test_not_feasible_status_short_circuits_to_not_feasible_verdict(self):
        record = _make_record(status="not_feasible", t1_translator_esql="")
        verdict = compare.compare_panel_record(record)
        assert verdict == Verdict.NOT_FEASIBLE

    def test_missing_translator_output_yields_skip(self):
        record = _make_record(t1_translator_esql="")
        verdict = compare.compare_panel_record(record)
        assert verdict == Verdict.SKIP

    def test_live_query_error_yields_fail(self):
        esql = "FROM metrics-* | LIMIT 1"
        record = _make_record(
            t1_translator_esql=esql,
            t2_ir_esql=esql,
            t3_ndjson_esql=esql,
            t4_cluster_esql=esql,
            t5_live_query_body=esql,
            t5_response_status=400,
            t5_response_error="parsing_exception",
        )
        verdict = compare.compare_panel_record(record)
        assert verdict == Verdict.FAIL

    def test_missing_cluster_tier_yields_not_uploaded(self):
        esql = "FROM metrics-* | LIMIT 1"
        record = _make_record(
            t1_translator_esql=esql,
            t2_ir_esql=esql,
            t3_ndjson_esql="",
            t4_cluster_esql="",
        )
        verdict = compare.compare_panel_record(record)
        assert verdict == Verdict.NOT_UPLOADED

    def test_t0_t1_difference_does_not_register_as_drift_axis(self):
        record = _make_record(
            t0_source_promql="rate(foo[5m])",
            t1_translator_esql="TS metrics-* | STATS x = AVG(RATE(foo))",
            t2_ir_esql="TS metrics-* | STATS x = AVG(RATE(foo))",
            t3_ndjson_esql="TS metrics-* | STATS x = AVG(RATE(foo))",
            t4_cluster_esql="TS metrics-* | STATS x = AVG(RATE(foo))",
        )
        compare.compare_panel_record(record)
        assert "T0=T1" not in record.drift_axes


# --------------------------------------------------------------------- #
# _is_known_t1_t2_drift — middle-splice false-negative regression
# --------------------------------------------------------------------- #


def test_middle_splice_different_metric_is_not_known():
    left = "TS metrics-* | STATS val = AVG(foo)"
    right = "TS metrics-* | STATS val = AVG(bar) | WHERE region = 'us-west'"
    assert not compare._is_known_t1_t2_drift(left, right)


def test_middle_splice_gauge_min_constant_is_known():
    left = "TS metrics-* | STATS val = AVG(foo)"
    right = left + " | EVAL _gauge_min = 0"
    assert compare._is_known_t1_t2_drift(left, right)


def test_suffix_only_legend_splice_is_known():
    left = "TS metrics-* | STATS val = AVG(foo)"
    right = left + " | EVAL legend = CONCAT(val, '')"
    assert compare._is_known_t1_t2_drift(left, right)


def test_identical_queries_are_known():
    q = "TS metrics-* | STATS val = AVG(foo)"
    assert compare._is_known_t1_t2_drift(q, q)


# --------------------------------------------------------------------- #
# Aggregation helpers
# --------------------------------------------------------------------- #


class TestAggregates:
    def test_aggregate_verdicts_counts_each_bucket(self):
        records = [
            _make_record(verdict=Verdict.PASS),
            _make_record(verdict=Verdict.PASS),
            _make_record(verdict=Verdict.DRIFT),
            _make_record(verdict=Verdict.NOT_FEASIBLE),
        ]
        counts = compare.aggregate_verdicts(records)
        assert counts[Verdict.PASS.value] == 2
        assert counts[Verdict.DRIFT.value] == 1
        assert counts[Verdict.NOT_FEASIBLE.value] == 1

    def test_aggregate_drift_axes_counts_each_axis(self):
        records = [
            _make_record(drift_axes=["T1=T2", "T3=T4"]),
            _make_record(drift_axes=["T3=T4"]),
        ]
        counts = compare.aggregate_drift_axes(records)
        assert counts["T1=T2"] == 1
        assert counts["T3=T4"] == 2
        assert counts["T2=T3"] == 0
        assert set(counts.keys()) == set(DRIFT_AXES)
