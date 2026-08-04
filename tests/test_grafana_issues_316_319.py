# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression tests for GitHub issues #316, #317, #318, #319."""

from __future__ import annotations

import unittest

from observability_migration.adapters.source.grafana import panels, rules, schema
from observability_migration.adapters.source.grafana.runtime_features import (
    KIBANA_PROMQL_CONTROL_PARAMS,
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
    def test_auto_panel_emits_adaptive_window_not_frozen_step(self):
        """#318 / #272: an auto panel must not freeze resolution with ``step=``.

        It stays adaptive by binding the window to the dashboard time picker via
        the ``?_tstart`` / ``?_tend`` placeholders. It must NOT be emitted bare:
        Elasticsearch rejects a stepless PROMQL command at plan time, and Kibana
        only substitutes placeholders — it does not synthesise absent command
        arguments, so there is nothing for it to inject into a bare command.
        """
        yaml_panel, _ = _translate(_make_panel("node_disk_read_bytes_total", legend=""))
        query = yaml_panel["esql"]["query"]
        self.assertTrue(query.startswith("PROMQL index="), query)
        self.assertNotIn("step=", query)
        self.assertIn("start=?_tstart", query)
        self.assertIn("end=?_tend", query)
        self.assertIn("buckets=", query)

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

    def test_user_opt_in_keeps_native_when_both_es_and_kibana_support_are_present(self):
        features = {
            PROMQL_LABEL_MATCHER_PARAMS: {
                "supported": True,
                "source": "probe",
                "confidence": "verified",
            },
            KIBANA_PROMQL_CONTROL_PARAMS: {
                "supported": True,
                "source": "user",
                "confidence": "assumed",
            },
        }
        yaml_panel, _ = _translate(
            _make_panel(
                'rate(redis_commands_processed_total{instance=~"$instance"}[1m])',
                legend="",
            ),
            runtime_features=features,
            regex_default_params={"instance"},
        )
        self.assertIsNotNone(yaml_panel)
        query = yaml_panel["esql"]["query"]
        self.assertTrue(query.startswith("PROMQL"), query)
        self.assertIn("{instance=~?instance}", query)

    def test_user_opt_in_keeps_grouped_native_series_when_controls_are_bound(self):
        features = {
            PROMQL_LABEL_MATCHER_PARAMS: {
                "supported": True,
                "source": "probe",
                "confidence": "verified",
            },
            KIBANA_PROMQL_CONTROL_PARAMS: {
                "supported": True,
                "source": "user",
                "confidence": "assumed",
            },
        }
        yaml_panel, _ = _translate(
            _make_panel(
                'sum(rate(redis_commands_total{instance=~"$instance"}[1m])) by (cmd)',
                legend="{{cmd}}",
            ),
            runtime_features=features,
            regex_default_params={"instance"},
        )
        self.assertIsNotNone(yaml_panel)
        esql = yaml_panel["esql"]
        query = esql["query"]
        self.assertTrue(query.startswith("PROMQL"), query)
        self.assertIn("{instance=~?instance}", query)
        self.assertEqual((esql.get("breakdown") or {}).get("field"), "cmd")

    def test_user_opt_in_still_falls_back_for_multi_target_overlay(self):
        features = {
            PROMQL_LABEL_MATCHER_PARAMS: {
                "supported": True,
                "source": "probe",
                "confidence": "verified",
            },
            KIBANA_PROMQL_CONTROL_PARAMS: {
                "supported": True,
                "source": "user",
                "confidence": "assumed",
            },
        }
        panel = {
            "id": 2,
            "type": "timeseries",
            "title": "Hits / Misses per Sec",
            "targets": [
                {
                    "expr": 'irate(redis_keyspace_hits_total{instance=~"$instance"}[5m])',
                    "refId": "A",
                    "legendFormat": "Hits",
                    "datasource": {"type": "prometheus"},
                },
                {
                    "expr": 'irate(redis_keyspace_misses_total{instance=~"$instance"}[5m])',
                    "refId": "B",
                    "legendFormat": "Misses",
                    "datasource": {"type": "prometheus"},
                },
            ],
            "fieldConfig": {"defaults": {}, "overrides": []},
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
        }
        yaml_panel, _ = _translate(
            panel,
            runtime_features=features,
            regex_default_params={"instance"},
        )
        self.assertIsNotNone(yaml_panel)
        query = yaml_panel["esql"]["query"]
        self.assertTrue(query.startswith("TS "), query)
        self.assertIn("redis_keyspace_hits_total", query)
        self.assertIn("redis_keyspace_misses_total", query)


if __name__ == "__main__":
    unittest.main()


class TestNativePromqlAdaptiveSelectorMustCarryTiming(unittest.TestCase):
    """A bare stepless ``PROMQL index=...`` is rejected by Elasticsearch.

    Elasticsearch fails such a command at plan time with "unable to create a
    bucket; provide either [step] or all of [start], [end], and [buckets]".
    Kibana does NOT rescue it: it substitutes ``?name`` placeholders, it does
    not synthesise missing command arguments, so a bare command gives it
    nothing to fill.

    A previous change dropped the timing args on the theory that Kibana
    supplied them at render time. It did not, and 8 corpus panels across 4
    dashboards began failing with exactly that plan-time error. These tests
    pin the selector so the same theory cannot be re-applied silently.
    """

    def test_adaptive_selector_is_not_empty(self):
        from observability_migration.adapters.source.grafana import panels

        self.assertTrue(
            panels._NATIVE_PROMQL_ADAPTIVE_SELECTOR.strip(),
            "bare stepless PROMQL is rejected by Elasticsearch at plan time",
        )

    def test_adaptive_selector_binds_the_dashboard_time_picker(self):
        from observability_migration.adapters.source.grafana import panels

        selector = panels._NATIVE_PROMQL_ADAPTIVE_SELECTOR
        # ``?_tstart`` / ``?_tend`` are the placeholders Kibana actually binds;
        # they are what keeps the range adaptive without a frozen ``step=``.
        self.assertIn("?_tstart", selector)
        self.assertIn("?_tend", selector)
        self.assertIn("buckets=", selector)
        self.assertNotIn("step=", selector)

    def test_emitted_range_panel_query_carries_timing_args(self):
        from observability_migration.adapters.source.grafana.panels import (
            build_native_promql_query,
        )

        query = build_native_promql_query(
            "rate(node_cpu_seconds_total[5m])",
            index="metrics-*",
            adaptive_step=True,
        )
        self.assertTrue(query.startswith("PROMQL "), query)
        has_step = "step=" in query
        has_window = all(tok in query for tok in ("start=", "end=", "buckets="))
        self.assertTrue(
            has_step or has_window,
            f"PROMQL command must carry step= or start/end/buckets: {query}",
        )


class TestMultiSelectControlsUseMvContains(unittest.TestCase):
    """A Grafana multi-select variable must stay multi-select in Kibana.

    ``RLIKE ?var`` is a scalar parameter position, so it can only ever bind one
    value. Kibana's supported multi-value mechanism is ``MV_CONTAINS(?var,
    field)`` with ``single_select: false`` — verified against Kibana/ES 9.5.
    The ``.*`` sentinel preserves Grafana's All option, because the control
    query already offers ``.*`` via MV_APPEND:

        WHERE MV_CONTAINS(?v, ".*") OR MV_CONTAINS(?v, field)

        [".*"]                -> every series (All)
        ["a"]                 -> just a
        ["a", "b"]            -> a and b

    Exact matching (not regex) is forced by the platform: ES|QL ``RLIKE``
    requires a literal pattern and rejects a computed one, so
    ``RLIKE MV_CONCAT(?v, "|")`` — which would have rebuilt Grafana's own
    ``(a|b)`` alternation — is not expressible.
    """

    def _dash(self, multi):
        return {
            "title": "multi-select repro",
            "uid": "ms-repro",
            "panels": [{
                "id": 1, "type": "timeseries", "title": "P",
                "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
                "datasource": {"type": "prometheus", "uid": "p"},
                "targets": [{"expr": 'sum(up{instance=~"$instance"})', "refId": "A"}],
            }],
            "templating": {"list": [{
                "name": "instance", "type": "query", "multi": multi,
                "includeAll": True,
                "definition": "label_values(up, instance)",
                "current": {"text": "All", "value": "$__all"},
                "options": [],
            }]},
        }

    def _translate(self, multi):
        from observability_migration.adapters.source.grafana.runtime_features import (
            ESQL_NAMED_PARAM_BINDING,
            set_runtime_feature,
        )

        rule_pack = rules.RulePackConfig()
        set_runtime_feature(
            rule_pack, ESQL_NAMED_PARAM_BINDING,
            supported=True, source="test", confidence="assumed",
        )
        resolver = schema.SchemaResolver(rule_pack)
        result = panels.translate_dashboard(
            self._dash(multi), datasource_index="metrics-*",
            esql_index="metrics-*", rule_pack=rule_pack, resolver=resolver,
        )
        return result.dashboard_ir.to_yaml_dict()

    def test_multi_select_emits_mv_contains_with_all_sentinel(self):
        doc = self._translate(multi=True)
        query = doc["panels"][0]["esql"]["query"]
        self.assertIn("MV_COUNT(?instance) == 0", query)
        self.assertIn("MV_CONTAINS(?instance", query)
        self.assertIn('MV_CONTAINS(?instance, ".*")', query)
        self.assertNotIn("RLIKE ?instance", query)

    def test_multi_select_control_is_not_single_select(self):
        doc = self._translate(multi=True)
        control = next(c for c in doc["controls"] if c.get("variable_name") == "instance")
        self.assertTrue(control.get("multiple"), control)

    def test_single_select_keeps_rlike_binding(self):
        """Non-multi variables must be untouched: RLIKE keeps regex semantics."""
        doc = self._translate(multi=False)
        query = doc["panels"][0]["esql"]["query"]
        self.assertIn("RLIKE ?instance", query)
        self.assertNotIn("MV_CONTAINS", query)
        control = next(c for c in doc["controls"] if c.get("variable_name") == "instance")
        self.assertFalse(control.get("multiple"))
