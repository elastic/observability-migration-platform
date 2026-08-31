# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for two related native-PROMQL control features.

FEATURE 1 — observable native live-validation:
  * The native validator gate records per-panel decisions (checked / degraded /
    kept) on ``rule_pack.native_validation_stats`` so the migrate CLI can print
    a clear summary line instead of running silently.
  * A panel only degrades to ES|QL on a *parse* rejection from the target, never
    on a data/field gap (which is a data-readiness condition, not a translator
    bug).

FEATURE 2 — ``--translation-mode {auto,native,esql}``:
  * ``esql`` forces ``rule_pack.native_promql = False`` regardless of the cluster
    probe, so every panel goes through the ES|QL translator.
  * ``native`` forces ``rule_pack.native_promql = True``.
  * ``auto`` (default) preserves the existing probe behavior.
"""

import argparse
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from observability_migration.adapters.source.grafana import cli, panels, rules, schema
from observability_migration.adapters.source.grafana.verification import (
    panel_notes_imply_warning,
)


def _make_panel(idx, expr="rate(http_requests_total[5m])", panel_type="timeseries"):
    return {
        "id": idx,
        "type": panel_type,
        "title": f"Panel {idx}",
        "targets": [
            {
                "expr": expr,
                "refId": "A",
                "datasource": {"type": "prometheus"},
            }
        ],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "gridPos": {"x": 0, "y": idx * 8, "w": 24, "h": 8},
    }


def _translate_panel(panel, rule_pack):
    res = schema.SchemaResolver(rule_pack)
    return panels.translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rule_pack,
        resolver=res,
    )


# =========================================================================
# FEATURE 1 — native live-validation stats + parse-only degrade
# =========================================================================


class TestNativeValidationStats(unittest.TestCase):
    def _native_rule_pack(self):
        rp = rules.RulePackConfig()
        rp.native_promql = True
        return rp

    def test_validator_accepts_query_counts_checked_and_kept(self):
        rp = self._native_rule_pack()
        seen = []

        def validator(query):
            seen.append(query)
            return True, ""

        rp.native_promql_validator = validator
        panel = _make_panel(1, "rate(http_requests_total[5m])")

        yaml_panel, _result = _translate_panel(panel, rp)

        # Native path was taken (PROMQL command emitted).
        self.assertIn("esql", yaml_panel)
        self.assertTrue(yaml_panel["esql"]["query"].startswith("PROMQL"))
        # The validator was actually called with the built native query.
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].startswith("PROMQL"))
        stats = rp.native_validation_stats
        self.assertEqual(stats["checked"], 1)
        self.assertEqual(stats["kept"], 1)
        self.assertEqual(stats["degraded"], 0)

    def test_parse_rejection_degrades_to_esql_and_counts_degraded(self):
        rp = self._native_rule_pack()

        def validator(query):
            # Simulate the target parser rejecting the native query at parse time.
            return False, "line 1:23: mismatched input '(' expecting STRING"

        rp.native_promql_validator = validator
        panel = _make_panel(2, "rate(http_requests_total[5m])")

        yaml_panel, result = _translate_panel(panel, rp)

        query = yaml_panel["esql"]["query"] if yaml_panel and "esql" in yaml_panel else (result.esql_query or "")
        self.assertFalse(
            query.startswith("PROMQL"),
            f"parse rejection must degrade to ES|QL, got: {query[:80]}",
        )
        stats = rp.native_validation_stats
        self.assertEqual(stats["checked"], 1)
        self.assertEqual(stats["degraded"], 1)
        self.assertEqual(stats["kept"], 0)

    def test_data_gap_does_not_degrade_native(self):
        """A field/index gap (data-readiness) must NOT degrade native PROMQL —
        only a true parse rejection does (the panel self-heals when data lands)."""
        rp = self._native_rule_pack()

        def validator(query):
            # "Unknown column"/"verification_exception" = data gap, not a parse error.
            return False, "verification_exception: Found 1 problem\nline 1:8: Unknown column [foo]"

        rp.native_promql_validator = validator
        panel = _make_panel(3, "rate(http_requests_total[5m])")

        yaml_panel, _result = _translate_panel(panel, rp)

        self.assertIn("esql", yaml_panel)
        self.assertTrue(
            yaml_panel["esql"]["query"].startswith("PROMQL"),
            "data gap must keep native PROMQL, not degrade to ES|QL",
        )
        stats = rp.native_validation_stats
        self.assertEqual(stats["checked"], 1)
        self.assertEqual(stats["kept"], 1)
        self.assertEqual(stats["degraded"], 0)

    def test_absent_validator_keeps_native_without_stats_increment(self):
        """Offline runs (no validator attached) keep native and do not pretend a
        live check happened."""
        rp = self._native_rule_pack()
        self.assertIsNone(getattr(rp, "native_promql_validator", None))
        panel = _make_panel(4, "rate(http_requests_total[5m])")

        yaml_panel, _result = _translate_panel(panel, rp)

        self.assertIn("esql", yaml_panel)
        self.assertTrue(yaml_panel["esql"]["query"].startswith("PROMQL"))
        # No validator => no checked panels recorded.
        self.assertEqual(rp.native_validation_stats.get("checked", 0), 0)

    def test_parse_rejection_classifier(self):
        rejected = panels._native_query_parse_rejected
        self.assertTrue(rejected("line 1:23: mismatched input '(' expecting STRING"))
        self.assertTrue(rejected("parsing_exception: line 1:5: extraneous input"))
        self.assertTrue(rejected("Invalid date format [NOW]"))
        # Data/field gaps are not parse rejections.
        self.assertFalse(rejected("verification_exception: Unknown column [foo]"))
        self.assertFalse(rejected("Unknown index [metrics-nope]"))
        self.assertFalse(rejected(""))


# =========================================================================
# ISSUE #379 — a reachable target must not change the translation strategy
# =========================================================================


class TestRangeVectorPanelsStayNativeWithLiveFieldCaps(unittest.TestCase):
    """``--es-url`` supplies live field caps, and it used to reroute every
    range-vector panel whose metric the target typed as a gauge onto the ES|QL
    ``TS`` path. That path drops the range-vector ``[window]`` and — because the
    shared counter policy trusts a source ``rate()`` over live caps (#119) —
    still emits ``RATE()`` on the gauge, which Elasticsearch rejects with
    ``first argument of [RATE(...)] must be [counter_long, counter_integer or
    counter_double]``. So merely pointing the tool at a cluster turned working,
    window-preserving panels into broken ones (issue #379).
    """

    GAUGE_CAPS = {
        "metrics.kubelet_volume_stats_used_bytes": {
            "double": {
                "type": "double",
                "searchable": True,
                "aggregatable": True,
                "time_series_metric": "gauge",
            },
        },
    }

    def _resolver(self, rule_pack, caps):
        res = schema.SchemaResolver(rule_pack)
        res._discovery_attempted = True
        res._field_cache = dict(caps)
        res._discovered_mappings = {}
        res._schema_profile_cache_id = None
        return res

    def _translate(self, expr, caps=None, metric_kinds=None):
        rp = rules.RulePackConfig()
        rp.native_promql = True
        if metric_kinds:
            rp.metric_kinds.update(metric_kinds)
        resolver = self._resolver(rp, caps if caps is not None else self.GAUGE_CAPS)
        yaml_panel, result = panels.translate_panel(
            _make_panel(1, expr),
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=rp,
            resolver=resolver,
        )
        query = ""
        if yaml_panel and "esql" in yaml_panel:
            query = yaml_panel["esql"]["query"]
        elif result is not None:
            query = result.esql_query or ""
        return query, result

    def test_gauge_typed_metric_keeps_native_promql_and_its_window(self):
        query, _result = self._translate("rate(kubelet_volume_stats_used_bytes[1d])")

        self.assertTrue(
            query.startswith("PROMQL "),
            f"live-caps gauge typing must not divert to ES|QL: {query[:120]}",
        )
        # The window survives, and no bare ES|QL RATE() is emitted on the gauge.
        self.assertIn("rate(metrics.kubelet_volume_stats_used_bytes[1d])", query)
        self.assertNotIn("RATE(metrics.kubelet_volume_stats_used_bytes)", query)

    def test_panels_differing_only_by_window_stay_distinct(self):
        """The issue's Hourly/Daily/Weekly panels emitted byte-identical ES|QL."""
        queries = {
            window: self._translate(
                f"max by (namespace) (rate(kubelet_volume_stats_used_bytes[{window}]))"
            )[0]
            for window in ("1h", "1d", "1w")
        }
        for window, query in queries.items():
            self.assertIn(f"[{window}]", query, f"[{window}] dropped: {query[:120]}")
        self.assertEqual(
            len(set(queries.values())), 3,
            f"windows collapsed to the same query: {queries}",
        )

    def test_target_gauge_typing_is_reported_as_a_render_risk(self):
        """Native is the right call, but Elasticsearch only evaluates a gauge
        rate while the window stays large relative to the bucket step, so the
        panel must carry the ingest fix rather than fail silently later."""
        _query, result = self._translate("rate(kubelet_volume_stats_used_bytes[1d])")

        notes = list(getattr(result, "notes", []) or []) + list(result.reasons or [])
        gauge_notes = [n for n in notes if "does not type this field as a counter" in n]
        self.assertEqual(len(gauge_notes), 1, f"expected one gauge note, got: {notes}")
        self.assertIn("kubelet_volume_stats_used_bytes", gauge_notes[0])

    def test_target_gauge_typing_lands_in_the_with_warnings_scorecard(self):
        """A panel that renders at 1h and errors at 3d is not clean. Verified in
        Kibana on ES 9.5: ``rate(gauge[1h])`` draws over a 24h view and throws
        ``class_cast_exception`` over a 3d view, so the note has to promote the
        panel rather than leave the summary reporting zero warnings."""
        _query, result = self._translate("rate(kubelet_volume_stats_used_bytes[1d])")

        self.assertEqual(result.status, "migrated_with_warnings", result.status)
        self.assertTrue(
            panel_notes_imply_warning(getattr(result, "notes", []) or []),
            f"gauge note must imply a warning: {getattr(result, 'notes', None)}",
        )
        self.assertLessEqual(result.confidence, 0.85, result.confidence)

    def test_gauge_note_is_absent_when_the_panel_falls_back_to_esql(self):
        """The note claims native PROMQL *kept* the window, so it must only
        attach once the native panel is actually committed. Several gates after
        the type check can still reject native, and on the ES|QL fallback the
        window is dropped — the opposite of what the note says."""
        rp = rules.RulePackConfig()
        rp.native_promql = True
        rp.native_promql_validator = lambda _query: (
            False, "line 1:23: mismatched input '(' expecting STRING"
        )
        resolver = self._resolver(rp, self.GAUGE_CAPS)
        yaml_panel, result = panels.translate_panel(
            _make_panel(1, "rate(kubelet_volume_stats_used_bytes[1d])"),
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=rp,
            resolver=resolver,
        )

        query = (
            yaml_panel["esql"]["query"]
            if yaml_panel and "esql" in yaml_panel
            else (result.esql_query or "")
        )
        self.assertFalse(query.startswith("PROMQL"), query[:90])
        notes = list(getattr(result, "notes", []) or []) + list(result.reasons or [])
        self.assertEqual(
            [n for n in notes if "Native PROMQL keeps the source" in n], [],
            f"ES|QL fallback must not claim the window was kept: {notes}",
        )

    def test_counter_typed_metric_stays_clean(self):
        """The counter control is the other half of the promotion check: a
        correctly typed field renders at every range, so it must stay clean."""
        caps = {
            "metrics.http_requests_total": {
                "double": {
                    "type": "double",
                    "searchable": True,
                    "aggregatable": True,
                    "time_series_metric": "counter",
                },
            },
        }
        _query, result = self._translate("rate(http_requests_total[5m])", caps=caps)

        self.assertFalse(
            panel_notes_imply_warning(getattr(result, "notes", []) or []),
            f"counter-typed metric must not be promoted: {getattr(result, 'notes', None)}",
        )

    def test_counter_typed_metric_is_not_flagged(self):
        caps = {
            "metrics.http_requests_total": {
                "double": {
                    "type": "double",
                    "searchable": True,
                    "aggregatable": True,
                    "time_series_metric": "counter",
                },
            },
        }
        query, result = self._translate("rate(http_requests_total[5m])", caps=caps)

        self.assertTrue(query.startswith("PROMQL "), query[:120])
        notes = list(getattr(result, "notes", []) or []) + list(result.reasons or [])
        self.assertEqual(
            [n for n in notes if "does not type this field as a counter" in n], [],
            f"counter-typed metric must not get the gauge note: {notes}",
        )

    def test_rule_pack_gauge_pin_still_degrades_via_esql(self):
        """A ``metric_kinds`` pin is the operator asserting the source is wrong.
        It is also the one signal the ES|QL path degrades on, so the panel must
        keep falling through to the honest gauge analogue."""
        query, result = self._translate(
            "rate(kubelet_volume_stats_used_bytes[1d])",
            metric_kinds={"kubelet_volume_stats_used_bytes": "gauge"},
        )

        self.assertFalse(
            query.startswith("PROMQL "),
            f"a rule-pack gauge pin must leave the native path: {query[:120]}",
        )
        self.assertIn("AVG_OVER_TIME", query)
        self.assertNotIn("RATE(", query)
        notes = list(getattr(result, "notes", []) or []) + list(result.reasons or [])
        self.assertTrue(
            any("rule pack pins" in n for n in notes),
            f"expected a note naming the pin, got: {notes}",
        )

    def test_gate_helper_only_matches_the_rule_pack_pin(self):
        rp = rules.RulePackConfig()
        resolver = self._resolver(rp, self.GAUGE_CAPS)
        gate = panels._native_promql_counter_func_on_declared_gauge

        # Live-caps gauge typing alone keeps native PROMQL.
        self.assertIsNone(gate("rate(kubelet_volume_stats_used_bytes[1d])", resolver))
        # An explicit pin diverts to the ES|QL degrade.
        rp.metric_kinds["kubelet_volume_stats_used_bytes"] = "gauge"
        self.assertEqual(
            gate("rate(kubelet_volume_stats_used_bytes[1d])", resolver),
            "kubelet_volume_stats_used_bytes",
        )
        # A pinned gauge outside the counter family is untouched: *_over_time
        # takes its window as a genuine lookback and needs no degrade.
        self.assertIsNone(
            gate("avg_over_time(kubelet_volume_stats_used_bytes[1d])", resolver)
        )


# =========================================================================
# FEATURE 2 — --translation-mode {auto,native,esql}
# =========================================================================


class TestTranslationMode(unittest.TestCase):
    def _args(self, **overrides):
        ns = argparse.Namespace(
            es_url="",
            es_api_key="",
            dataset_filter="",
            kibana_url="",
            kibana_api_key="",
            ca_cert="",
            insecure=False,
            translation_mode="auto",
        )
        for key, value in overrides.items():
            setattr(ns, key, value)
        return ns

    def test_auto_offline_defaults_to_native(self):
        rp = rules.RulePackConfig()
        cli._apply_native_promql_to_rule_pack(rp, self._args(translation_mode="auto"))
        self.assertTrue(rp.native_promql)

    def test_esql_mode_forces_native_off(self):
        rp = rules.RulePackConfig()
        cli._apply_native_promql_to_rule_pack(rp, self._args(translation_mode="esql"))
        self.assertFalse(rp.native_promql)

    def test_native_mode_forces_native_on(self):
        rp = rules.RulePackConfig()
        cli._apply_native_promql_to_rule_pack(rp, self._args(translation_mode="native"))
        self.assertTrue(rp.native_promql)

    def test_esql_mode_panel_emits_esql_not_promql(self):
        """End-to-end: a panel that would otherwise be native PROMQL must emit an
        ES|QL query (not a ``PROMQL …`` command) under --translation-mode esql."""
        rp = rules.RulePackConfig()
        cli._apply_native_promql_to_rule_pack(rp, self._args(translation_mode="esql"))
        panel = _make_panel(1, "rate(http_requests_total[5m])")

        yaml_panel, result = _translate_panel(panel, rp)

        query = ""
        if yaml_panel and "esql" in yaml_panel:
            query = yaml_panel["esql"]["query"]
        elif result is not None:
            query = result.esql_query or ""
        self.assertFalse(
            query.startswith("PROMQL "),
            f"--translation-mode esql must not emit native PROMQL: {query[:80]}",
        )

    def test_native_mode_panel_emits_promql(self):
        rp = rules.RulePackConfig()
        cli._apply_native_promql_to_rule_pack(rp, self._args(translation_mode="native"))
        panel = _make_panel(1, "rate(http_requests_total[5m])")

        yaml_panel, _result = _translate_panel(panel, rp)

        self.assertIn("esql", yaml_panel)
        self.assertTrue(yaml_panel["esql"]["query"].startswith("PROMQL"))

    def test_grafana_cli_parses_translation_mode(self):
        args = cli.parse_args(["--translation-mode", "esql"])
        self.assertEqual(args.translation_mode, "esql")

    def test_grafana_cli_translation_mode_default_auto(self):
        args = cli.parse_args([])
        self.assertEqual(args.translation_mode, "auto")

    def test_grafana_cli_has_no_implicit_kibana_target(self):
        args = cli.parse_args([])
        self.assertEqual(args.kibana_url, "")

    def test_grafana_cli_rejects_invalid_translation_mode(self):
        with self.assertRaises(SystemExit):
            cli.parse_args(["--translation-mode", "bogus"])

    def test_grafana_cli_rejects_removed_kibana_promql_control_params_flag(self):
        with self.assertRaises(SystemExit) as raised:
            cli.parse_args(["--kibana-promql-control-params"])
        self.assertEqual(raised.exception.code, 2)

    def test_kibana_95_enables_promql_control_params(self):
        from observability_migration.adapters.source.grafana.runtime_features import (
            KIBANA_PROMQL_CONTROL_PARAMS,
            get_runtime_features,
        )

        rp = rules.RulePackConfig()
        with mock.patch.object(cli, "_detect_kibana_version", return_value=(9, 5)):
            cli._apply_native_promql_to_rule_pack(
                rp,
                self._args(kibana_url="http://localhost:5601"),
            )
        state = get_runtime_features(rp)[KIBANA_PROMQL_CONTROL_PARAMS]
        self.assertTrue(state["supported"])
        self.assertEqual(state["source"], "probe")
        self.assertEqual(state["confidence"], "verified")

    def test_kibana_94_keeps_promql_control_params_off(self):
        from observability_migration.adapters.source.grafana.runtime_features import (
            KIBANA_PROMQL_CONTROL_PARAMS,
            get_runtime_features,
        )

        rp = rules.RulePackConfig()
        with mock.patch.object(cli, "_detect_kibana_version", return_value=(9, 4)):
            cli._apply_native_promql_to_rule_pack(
                rp,
                self._args(kibana_url="http://localhost:5601"),
            )
        state = get_runtime_features(rp)[KIBANA_PROMQL_CONTROL_PARAMS]
        self.assertFalse(state["supported"])
        self.assertEqual(state["source"], "probe")
        self.assertEqual(state["confidence"], "verified")

    def test_missing_kibana_url_prefers_promql_control_params(self):
        from observability_migration.adapters.source.grafana.runtime_features import (
            KIBANA_PROMQL_CONTROL_PARAMS,
            get_runtime_features,
        )

        rp = rules.RulePackConfig()
        cli._apply_native_promql_to_rule_pack(rp, self._args())
        state = get_runtime_features(rp)[KIBANA_PROMQL_CONTROL_PARAMS]
        self.assertTrue(state["supported"])
        self.assertEqual(state["source"], "default")
        self.assertEqual(state["confidence"], "unverified")

    def test_missing_target_urls_keep_control_bound_panel_on_promql(self):
        rp = rules.RulePackConfig()
        cli._apply_native_promql_to_rule_pack(rp, self._args())
        setattr(rp, "_regex_default_param_names", frozenset({"instance"}))
        panel = _make_panel(
            1,
            'rate(redis_commands_processed_total{instance=~"$instance"}[1m])',
        )

        yaml_panel, _result = _translate_panel(panel, rp)

        query = yaml_panel["esql"]["query"]
        self.assertTrue(query.startswith("PROMQL"), query)
        self.assertIn("{instance=~?instance}", query)

    def test_inconclusive_kibana_version_prefers_promql_control_params(self):
        from observability_migration.adapters.source.grafana.runtime_features import (
            KIBANA_PROMQL_CONTROL_PARAMS,
            get_runtime_features,
        )

        rp = rules.RulePackConfig()
        with mock.patch.object(cli, "_detect_kibana_version", return_value=None):
            cli._apply_native_promql_to_rule_pack(
                rp,
                self._args(kibana_url="http://localhost:5601"),
            )
        state = get_runtime_features(rp)[KIBANA_PROMQL_CONTROL_PARAMS]
        self.assertTrue(state["supported"])
        self.assertEqual(state["source"], "probe")
        self.assertEqual(state["confidence"], "inconclusive")


class TestDetectKibanaVersion(unittest.TestCase):
    def test_prefers_api_status_version_number(self):
        class _Resp:
            status_code = 200

            def json(self):
                return {"version": {"number": "9.5.0-SNAPSHOT"}}

        with mock.patch.object(cli.requests, "get", return_value=_Resp()):
            self.assertEqual(cli._detect_kibana_version("http://kibana:5601"), (9, 5))

    def test_falls_back_to_api_stats(self):
        class _Status:
            status_code = 200

            def json(self):
                return {"status": {"overall": {"level": "available"}}}

        class _Stats:
            status_code = 200

            def json(self):
                return {"kibana": {"version": "9.4.2"}}

        def _get(url, **_kwargs):
            if url.endswith("/api/status"):
                return _Status()
            if url.endswith("/api/stats"):
                return _Stats()
            raise AssertionError(url)

        with mock.patch.object(cli.requests, "get", side_effect=_get):
            self.assertEqual(cli._detect_kibana_version("http://kibana:5601"), (9, 4))


class TestDatadogTranslationModeNoOp(unittest.TestCase):
    def test_datadog_cli_accepts_translation_mode(self):
        from observability_migration.adapters.source.datadog import cli as dd_cli

        args = dd_cli.parse_args(["--translation-mode", "esql"])
        self.assertEqual(args.translation_mode, "esql")

    def test_datadog_cli_translation_mode_default_auto(self):
        from observability_migration.adapters.source.datadog import cli as dd_cli

        args = dd_cli.parse_args([])
        self.assertEqual(args.translation_mode, "auto")

    def test_datadog_cli_rejects_invalid_translation_mode(self):
        from observability_migration.adapters.source.datadog import cli as dd_cli

        with self.assertRaises(SystemExit):
            dd_cli.parse_args(["--translation-mode", "bogus"])


if __name__ == "__main__":
    unittest.main()
