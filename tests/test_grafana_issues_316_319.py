# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression tests for GitHub issues #316, #317, #318, #319."""

from __future__ import annotations

import unittest

from observability_migration.adapters.source.grafana import panels, rules, schema
from observability_migration.adapters.source.grafana.runtime_features import (
    PROMQL_LABEL_MATCHER_PARAMS,
)


def _make_panel(expr, *, panel_type="timeseries", legend="__auto", interval=None, **extra):
    panel = {
        "id": 1,
        "type": panel_type,
        "title": extra.pop("title", "Panel"),
        "targets": [
            {
                "expr": expr,
                "refId": "A",
                "legendFormat": legend,
                "range": True,
                "datasource": {"type": "prometheus"},
            }
        ],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
    }
    if interval is not None:
        panel["interval"] = interval
    panel.update(extra)
    return panel


def _translate(panel, *, runtime_features=None, native_promql=True, regex_default_params=None):
    rp = rules.RulePackConfig(native_promql=native_promql)
    if runtime_features is not None:
        rp.runtime_features = runtime_features
    if regex_default_params is not None:
        rp._regex_default_param_names = frozenset(regex_default_params)
    resolver = schema.SchemaResolver(rp)
    return panels.translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rp,
        resolver=resolver,
    )


class TestIssue317AutoLegendBreakdown(unittest.TestCase):
    def test_auto_legend_breaks_down_on_timeseries_not_missing_label(self):
        """#317: legendFormat __auto must not point Lens at a missing ``label`` column."""
        yaml_panel, result = _translate(_make_panel("node_disk_read_bytes_total"))
        self.assertIsNotNone(yaml_panel)
        self.assertIn(result.status, {"migrated", "migrated_with_warnings"})
        esql = yaml_panel["esql"]
        query = esql["query"]
        self.assertTrue(query.startswith("PROMQL"), query)
        self.assertNotIn("| EVAL label =", query)
        breakdown = (esql.get("breakdown") or {}).get("field")
        self.assertEqual(breakdown, "_timeseries")


class TestIssue318PanelIntervalStep(unittest.TestCase):
    def test_auto_panel_emits_bare_promql(self):
        """#318 / #272: auto panels emit bare PROMQL; Kibana injects time range."""
        yaml_panel, _ = _translate(_make_panel("node_disk_read_bytes_total", legend=""))
        query = yaml_panel["esql"]["query"]
        self.assertTrue(query.startswith("PROMQL index="), query)
        self.assertNotIn("step=", query)
        self.assertNotIn("start=", query)
        self.assertNotIn("buckets=", query)

    def test_panel_interval_emits_step(self):
        """#318: Grafana panel ``interval`` must become PROMQL ``step=``."""
        yaml_panel, _ = _translate(
            _make_panel("node_disk_read_bytes_total", legend="", interval="1h")
        )
        query = yaml_panel["esql"]["query"]
        self.assertIn("step=1h", query)
        self.assertNotIn("buckets=50", query)

    def test_rate_interval_stays_windowless_with_panel_step(self):
        yaml_panel, _ = _translate(
            _make_panel(
                "rate(node_disk_read_bytes_total[$__rate_interval])",
                legend="",
                interval="1h",
            )
        )
        query = yaml_panel["esql"]["query"]
        self.assertIn("step=1h", query)
        self.assertIn("value=(rate(node_disk_read_bytes_total))", query)
        self.assertNotIn("[5m]", query)


class TestIssue316EsqlAdaptiveTbucket(unittest.TestCase):
    def test_esql_xy_uses_adaptive_tbucket_by_default(self):
        """#316: ES|QL TS path must not hardcode TBUCKET(5 minute) for auto panels."""
        yaml_panel, _ = _translate(
            _make_panel("rate(node_disk_read_bytes_total[5m])", legend=""),
            native_promql=False,
        )
        self.assertIsNotNone(yaml_panel)
        query = yaml_panel["esql"]["query"]
        self.assertTrue(query.startswith("TS "), query)
        self.assertIn("TBUCKET(100, ?_tstart, ?_tend)", query)
        self.assertNotIn("TBUCKET(5 minute)", query)

    def test_esql_xy_uses_panel_interval_tbucket(self):
        yaml_panel, _ = _translate(
            _make_panel(
                "rate(node_disk_read_bytes_total[5m])",
                legend="",
                interval="1h",
            ),
            native_promql=False,
        )
        query = yaml_panel["esql"]["query"]
        self.assertIn("TBUCKET(1 hour)", query)


class TestIssue319PromqlLabelMatcherParams(unittest.TestCase):
    def test_falls_through_to_esql_even_when_target_supports_label_matcher_params(self):
        """ES supports ?param in PromQL label matchers, but Kibana does not forward
        dashboard control values as named params inside PROMQL command expressions.
        The ES-side probe marks the feature supported, yet panels with control-bound
        label matchers must still fall through to ES|QL so ?instance lands in a
        WHERE clause that Kibana DOES bind. (#230 / #319)"""
        features = {
            PROMQL_LABEL_MATCHER_PARAMS: {
                "supported": True,
                "source": "probe",
                "confidence": "verified",
            }
        }
        yaml_panel, _ = _translate(
            _make_panel(
                'node_disk_read_bytes_total{device="$device_filtered"}',
                legend="",
            ),
            runtime_features=features,
            regex_default_params={"device_filtered"},
        )
        self.assertIsNotNone(yaml_panel)
        query = yaml_panel["esql"]["query"]
        # Must fall through to ES|QL regardless of PROMQL_LABEL_MATCHER_PARAMS,
        # because Kibana does not inject control values into PROMQL expressions.
        self.assertFalse(query.startswith("PROMQL"), query)
        self.assertTrue(query.startswith("TS "), query)

    def test_still_falls_through_when_feature_unsupported(self):
        yaml_panel, _ = _translate(
            _make_panel(
                'node_disk_read_bytes_total{device="$device_filtered"}',
                legend="",
            ),
            runtime_features={
                PROMQL_LABEL_MATCHER_PARAMS: {
                    "supported": False,
                    "source": "probe",
                    "confidence": "verified",
                }
            },
        )
        query = yaml_panel["esql"]["query"]
        # Without a live control registry the matcher may widen to .*, but the
        # important bit is we did NOT keep an opaque PROMQL ?param binding.
        self.assertTrue(query.startswith("TS "), query)
        self.assertFalse(query.startswith("PROMQL"))


if __name__ == "__main__":
    unittest.main()
