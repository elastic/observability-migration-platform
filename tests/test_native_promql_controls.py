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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from observability_migration.adapters.source.grafana import cli, panels, rules, schema


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
# FEATURE 2 — --translation-mode {auto,native,esql}
# =========================================================================


class TestTranslationMode(unittest.TestCase):
    def _args(self, **overrides):
        ns = argparse.Namespace(
            es_url="",
            es_api_key="",
            dataset_filter="",
            kibana_promql_control_params=False,
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

    def test_grafana_cli_rejects_invalid_translation_mode(self):
        with self.assertRaises(SystemExit):
            cli.parse_args(["--translation-mode", "bogus"])

    def test_grafana_cli_accepts_kibana_promql_control_param_opt_in(self):
        args = cli.parse_args(["--kibana-promql-control-params"])
        self.assertTrue(args.kibana_promql_control_params)

    def test_apply_native_promql_records_kibana_control_param_override(self):
        from observability_migration.adapters.source.grafana.runtime_features import (
            KIBANA_PROMQL_CONTROL_PARAMS,
            get_runtime_features,
        )

        rp = rules.RulePackConfig()
        cli._apply_native_promql_to_rule_pack(
            rp,
            self._args(kibana_promql_control_params=True),
        )
        self.assertTrue(
            get_runtime_features(rp)[KIBANA_PROMQL_CONTROL_PARAMS]["supported"]
        )


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
