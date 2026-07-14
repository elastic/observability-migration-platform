# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the live ES|QL execution oracle (error classification + driver).

The executor is injected, so these run with no cluster. The error strings are
real Elasticsearch responses captured from a live 9.5 Serverless cluster."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parity-rig"))

from verifier import live_validate  # noqa: E402
from verifier.live_validate import classify_error  # noqa: E402

# Real captured error bodies.
_PERCENTILE_BUG = (
    '{"error":{"root_cause":[{"type":"ql_illegal_argument_exception",'
    '"reason":"expects exactly two arguments"}],"type":"parsing_exception",'
    '"reason":"line 3:31: error building [percentile]: expects exactly two arguments"},"status":400}'
)
_UNKNOWN_COLUMN = (
    '{"error":{"type":"verification_exception",'
    '"reason":"Found 1 problem\\nline 2:9: Unknown column [service.instance.id]"},"status":400}'
)
_UNKNOWN_INDEX = (
    '{"error":{"type":"verification_exception","reason":"Unknown index [metrics-*]"},"status":400}'
)
_UNKNOWN_FUNCTION_WRAPPED = (
    '{"error":{"type":"verification_exception",'
    '"reason":"Found 1 problem\\nline 2:9: Unknown function [FOO]"},"status":400}'
)
_TYPE_ERROR = (
    '{"error":{"type":"verification_exception",'
    '"reason":"line 1:20: first argument of [a + b] must be [numeric], found value [b] type [keyword]"},"status":400}'
)
_FOUND_PROBLEM_TYPE_ERROR = (
    '{"error":{"type":"verification_exception",'
    '"reason":"Found 1 problem\\nline 1:20: first argument of [RATE(x)] must be [counter], found [x] type [double]"},'
    '"status":400}'
)


class TestClassifyError:
    def test_percentile_arg_count_is_real_bug(self) -> None:
        assert classify_error(_PERCENTILE_BUG) == "real_bug"

    def test_unknown_column_is_data_gap(self) -> None:
        assert classify_error(_UNKNOWN_COLUMN) == "data_gap"

    def test_unknown_index_is_data_gap(self) -> None:
        assert classify_error(_UNKNOWN_INDEX) == "data_gap"

    def test_type_error_is_real_bug(self) -> None:
        assert classify_error(_TYPE_ERROR) == "real_bug"

    def test_found_problem_type_error_is_real_bug(self) -> None:
        assert classify_error(_FOUND_PROBLEM_TYPE_ERROR) == "real_bug"

    def test_unknown_function_is_real_bug(self) -> None:
        assert classify_error('{"reason":"Unknown function [FOO]"}') == "real_bug"

    def test_wrapped_unknown_function_is_real_bug(self) -> None:
        assert classify_error(_UNKNOWN_FUNCTION_WRAPPED) == "real_bug"

    def test_empty_is_other(self) -> None:
        assert classify_error("") == "other"

    def test_data_gap_wins_over_bug_signal(self) -> None:
        # A body that mentions both an unknown column and a parse-ish word must
        # be a data_gap (the unknown column is the dominant cause).
        mixed = '{"reason":"line 2:9: Unknown column [x]; expected something"}'
        assert classify_error(mixed) == "data_gap"


class TestValidateQuery:
    def _runner(self, status, body):
        def _r(_es, _key, _q):
            return status, body
        return _r

    def test_ok_status_captures_columns(self) -> None:
        body = {"columns": [{"name": "a"}, {"name": "b"}], "values": [[1, 2], [3, 4]]}
        r = live_validate.validate_query("es", "k", "FROM x", runner=self._runner(200, body))
        assert r.classification == "ok"
        assert r.columns == ["a", "b"]
        assert r.row_count == 2

    def test_real_bug_classified(self) -> None:
        r = live_validate.validate_query("es", "k", "FROM x | STATS PERCENTILE(y)", runner=self._runner(400, _PERCENTILE_BUG))
        assert r.classification == "real_bug"
        assert "percentile" in r.error.lower()

    def test_data_gap_classified(self) -> None:
        r = live_validate.validate_query("es", "k", "FROM x", runner=self._runner(400, _UNKNOWN_COLUMN))
        assert r.classification == "data_gap"

    def test_default_runner_binds_control_params(self, monkeypatch) -> None:
        # Without an injected runner, dashboard control params must be bound the
        # same way the smoke path binds them (``RLIKE ?var`` -> ``.*``), not left
        # to default to empty strings, so a valid query is not mis-run/-classified.
        from verifier import collectors

        captured: dict = {}

        def fake_run(es, key, q, params=None, timeout=0):
            captured["params"] = params
            return 200, {"columns": [], "values": []}

        monkeypatch.setattr(collectors, "run_cluster_query", fake_run)

        query = 'FROM metrics-* | WHERE host RLIKE ?var | STATS c = COUNT(*)'
        r = live_validate.validate_query("es", "k", query)
        assert r.classification == "ok"
        params = {k: v for entry in captured["params"] for k, v in entry.items()}
        assert params == {"var": ".*"}

    def test_default_runner_binds_time_and_control_params_together(self, monkeypatch) -> None:
        # A query that mixes dashboard control params with the Lens-injected
        # ``?_tstart``/``?_tend`` time placeholders must bind BOTH: the control
        # param via the smoke binding and the time placeholders via the autobind,
        # otherwise the time params are left unbound and the query fails at runtime.
        from verifier import collectors

        captured: dict = {}

        def fake_run(es, key, q, params=None, timeout=0):
            captured["params"] = params
            return 200, {"columns": [], "values": []}

        monkeypatch.setattr(collectors, "run_cluster_query", fake_run)

        query = (
            "FROM metrics-* | WHERE host RLIKE ?var "
            "| STATS c = COUNT(*) BY b = BUCKET(@timestamp, ?_tstart, ?_tend)"
        )
        r = live_validate.validate_query("es", "k", query)
        assert r.classification == "ok"
        params = {k: v for entry in (captured["params"] or []) for k, v in entry.items()}
        assert params.get("var") == ".*"
        # Time placeholders must be bound (to concrete date strings), not dropped.
        assert params.get("_tstart")
        assert params.get("_tend")

    def test_default_runner_preserves_time_alias_date_binds(self, monkeypatch) -> None:
        # The collector auto-binds every time-alias spelling (``?_t_start``/
        # ``?_t_end``/``?tstart``/``?tend``) to an ISO date, but the smoke helper
        # only date-binds ``_tstart``/``_tend`` and wildcards the rest to ``.*``.
        # The smoke overlay must NOT clobber the alias date binds, or those
        # queries fail at runtime and get mis-classified as real_bug.
        from verifier import collectors

        captured: dict = {}

        def fake_run(es, key, q, params=None, timeout=0):
            captured["params"] = params
            return 200, {"columns": [], "values": []}

        monkeypatch.setattr(collectors, "run_cluster_query", fake_run)

        query = (
            "FROM metrics-* | STATS c = COUNT(*) "
            "BY b = BUCKET(@timestamp, ?_t_start, ?_t_end)"
        )
        r = live_validate.validate_query("es", "k", query)
        assert r.classification == "ok"
        params = {k: v for entry in (captured["params"] or []) for k, v in entry.items()}
        # Alias time params must keep their concrete date binds, not ``.*``.
        assert params.get("_t_start") and params["_t_start"] != ".*"
        assert params.get("_t_end") and params["_t_end"] != ".*"

    def test_default_runner_binds_identifier_control_default(self, monkeypatch) -> None:
        from verifier import collectors

        captured: dict = {}

        def fake_run(es, key, q, params=None, timeout=0):
            captured["params"] = params
            return 200, {"columns": [], "values": []}

        monkeypatch.setattr(collectors, "run_cluster_query", fake_run)
        query = "TS metrics-* | STATS value = SUM(metric) BY grouping = ??grouping"

        result = live_validate.validate_query(
            "es",
            "k",
            query,
            identifier_params={"grouping": "exporter"},
        )

        assert result.classification == "ok"
        assert captured["params"] == [{"grouping": "exporter"}]


class TestDriverAndExtraction:
    def test_validate_queries_dedups(self) -> None:
        calls = []

        def runner(_es, _key, q):
            calls.append(q)
            return 200, {"columns": [], "values": []}

        items = [("d", "p1", "FROM x"), ("d", "p2", "FROM x"), ("d", "p3", "FROM y")]
        results = live_validate.validate_queries("es", "k", items, runner=runner)
        assert len(results) == 2  # deduped
        assert calls == ["FROM x", "FROM y"]

    def test_summarize_counts(self) -> None:
        def runner(_es, _key, q):
            if "BAD" in q:
                return 400, _PERCENTILE_BUG
            if "GAP" in q:
                return 400, _UNKNOWN_COLUMN
            return 200, {"columns": [], "values": []}

        items = [("d", "ok", "FROM ok"), ("d", "bug", "FROM BAD"), ("d", "gap", "FROM GAP")]
        results = live_validate.validate_queries("es", "k", items, runner=runner)
        s = live_validate.summarize(results)
        assert s["total"] == 3
        assert s["real_bugs"] == 1
        assert s["by_classification"]["data_gap"] == 1
        assert s["by_classification"]["ok"] == 1

    def test_queries_from_report_prefers_visual_ir(self) -> None:
        report = {
            "dashboards": [
                {
                    "title": "D",
                    "panels": [
                        {
                            "title": "P",
                            "esql": "BARE QUERY",
                            "visual_ir": {
                                "presentation": {
                                    "kind": "esql",
                                    "config": {"query": "EMITTED QUERY"},
                                }
                            },
                        },
                        {"title": "markdown", "visual_ir": {"presentation": {"kind": "markdown", "config": {}}}},
                    ],
                }
            ]
        }
        items = live_validate.queries_from_report(report)
        assert items == [("D", "P", "EMITTED QUERY")]

    def test_query_specs_from_report_include_identifier_defaults(self) -> None:
        query = "TS metrics-* | STATS value = SUM(metric) BY grouping = ??grouping"
        report = {
            "dashboards": [{
                "title": "D",
                "panels": [{
                    "title": "P",
                    "visual_ir": {
                        "presentation": {
                            "kind": "esql",
                            "config": {"query": query},
                        }
                    },
                    "query_ir": {
                        "metadata": {
                            "esql_identifier_param_defaults": {
                                "grouping": "exporter",
                            }
                        }
                    },
                }],
            }]
        }

        assert live_validate.query_specs_from_report(report) == [
            ("D", "P", query, {"grouping": "exporter"})
        ]

    def test_queries_from_report_falls_back_to_bare_esql(self) -> None:
        report = {"dashboards": [{"title": "D", "panels": [{"title": "P", "esql": "BARE"}]}]}
        items = live_validate.queries_from_report(report)
        assert items == [("D", "P", "BARE")]

    def test_queries_from_report_reads_datadog_esql_query(self) -> None:
        report = {
            "dashboards": [
                {
                    "title": "Datadog",
                    "panels": [
                        {
                            "title": "Widget",
                            "kibana_type": "xy",
                            "esql_query": "FROM metrics-* | STATS value = AVG(metric)",
                        }
                    ],
                }
            ]
        }
        items = live_validate.queries_from_report(report)
        assert items == [("Datadog", "Widget", "FROM metrics-* | STATS value = AVG(metric)")]

    def test_queries_from_report_does_not_execute_explicit_markdown_stale_query(self) -> None:
        report = {
            "dashboards": [
                {
                    "title": "D",
                    "panels": [
                        {
                            "title": "Manual",
                            "esql_query": "FROM stale-* | LIMIT 1",
                            "visual_ir": {
                                "presentation": {
                                    "kind": "markdown",
                                    "config": {"content": "Migration Required"},
                                }
                            },
                        }
                    ],
                }
            ]
        }
        assert live_validate.queries_from_report(report) == []
