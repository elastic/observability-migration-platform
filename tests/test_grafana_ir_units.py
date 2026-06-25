# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Direct unit tests for core Grafana translation IR.

CodeGraph flagged ``PromQLFragment``, ``MeasureSpec``, ``PanelContext``,
``TranslationContext`` and ``DashboardLineage`` as having no *direct* covering
tests — they were only exercised indirectly through snapshot suites, so an IR
regression showed up as opaque snapshot churn instead of a precise failure.
These tests pin the parser → IR contract and the lineage state machine directly.
"""

from __future__ import annotations

import pytest

from observability_migration.adapters.source.grafana.panels import PanelContext
from observability_migration.adapters.source.grafana.promql import (
    MeasureSpec,
    PromQLFragment,
    _parse_fragment,
)
from observability_migration.adapters.source.grafana.rollout import (
    ROLLOUT_STATES,
    DashboardLineage,
)
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.translate import TranslationContext


class TestParseFragment:
    def test_sum_rate_by_extracts_full_ir(self):
        frag = _parse_fragment('sum(rate(http_requests_total{job="api"}[5m])) by (method)')
        assert frag.metric == "http_requests_total"
        assert frag.outer_agg == "sum"
        assert frag.range_func == "rate"
        assert frag.range_window == "5m"
        assert frag.group_labels == ["method"]
        assert frag.family == "range_agg"

    def test_bare_metric_is_simple_family(self):
        frag = _parse_fragment("node_memory_MemAvailable_bytes")
        assert frag.metric == "node_memory_MemAvailable_bytes"
        assert frag.outer_agg == ""
        assert frag.range_func == ""
        assert frag.group_labels == []
        assert frag.family == "simple_metric"

    def test_histogram_quantile_strips_bucket_suffix(self):
        frag = _parse_fragment("histogram_quantile(0.95, sum(rate(http_bucket[5m])) by (le))")
        assert frag.metric == "http"
        assert frag.range_window == "5m"
        assert frag.family == "histogram_quantile"


class TestIRDataclassContracts:
    def test_promql_fragment_defaults(self):
        frag = PromQLFragment()
        assert frag.metric == ""
        assert frag.matchers == []
        assert frag.group_mode == "by"
        assert frag.is_scalar is False
        assert frag.extra == {}

    def test_measure_spec_requires_core_fields_and_defaults_warnings(self):
        spec = MeasureSpec(
            source_type="metric",
            time_filter="",
            bucket_expr="",
            group_fields=[],
            filters=[],
            alias="a",
            stats_expr="AVG(value)",
            final_alias="a",
        )
        assert spec.warnings == []
        assert spec.eval_expr == ""

    def test_translation_context_defaults(self):
        ctx = TranslationContext(
            promql_expr="up",
            data_view="metrics-*",
            index="metrics-*",
            rule_pack=RulePackConfig(),
        )
        assert ctx.feasibility == "feasible"
        assert ctx.translation_complete is False
        assert ctx.warnings == []
        assert ctx.group_labels == []

    def test_panel_context_defaults(self):
        ctx = TranslationContext(
            promql_expr="up",
            data_view="metrics-*",
            index="metrics-*",
            rule_pack=RulePackConfig(),
        )
        pctx = PanelContext(
            panel={"type": "timeseries"},
            panel_type="timeseries",
            title="CPU",
            kibana_type="line",
            yaml_panel={},
            translation=ctx,
        )
        assert pctx.handled is False
        assert pctx.extra_translations == []
        assert pctx.trace == []


class TestDashboardLineage:
    def test_valid_transition_updates_state_and_records_history(self):
        lineage = DashboardLineage(grafana_uid="abc", grafana_title="Test")
        assert lineage.rollout_state == "report_only"
        lineage.transition("shadow_imported", reason="uploaded to scratch space")
        assert lineage.rollout_state == "shadow_imported"
        assert len(lineage.state_history) == 1
        entry = lineage.state_history[0]
        assert entry["from"] == "report_only"
        assert entry["to"] == "shadow_imported"
        assert entry["reason"] == "uploaded to scratch space"

    def test_invalid_transition_raises_and_leaves_state_unchanged(self):
        lineage = DashboardLineage(grafana_uid="abc")
        with pytest.raises(ValueError, match="Invalid rollout state"):
            lineage.transition("not_a_real_state")
        assert lineage.rollout_state == "report_only"
        assert lineage.state_history == []

    def test_all_declared_states_are_reachable_targets(self):
        # Every state in ROLLOUT_STATES must be an accepted transition target,
        # so the validator and the declared set never drift apart.
        for state in ROLLOUT_STATES:
            lineage = DashboardLineage()
            lineage.transition(state)
            assert lineage.rollout_state == state


class TestHeatmapEmitter:
    def _heatmap_panel(self, expr):
        return {
            "id": 1, "type": "heatmap", "title": "H",
            "targets": [{"expr": expr, "refId": "A"}],
            "fieldConfig": {"defaults": {}, "overrides": []},
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
        }

    def _translate(self, expr):
        rp = RulePackConfig()
        from observability_migration.adapters.source.grafana import panels
        from observability_migration.adapters.source.grafana import schema as gschema
        resolver = gschema.SchemaResolver(rp)
        return panels.translate_panel(self._heatmap_panel(expr), datasource_index="metrics-*",
                                      esql_index="metrics-*", rule_pack=rp, resolver=resolver)

    def test_histogram_bucket_heatmap_emits_heatmap_type(self):
        yaml_panel, result = self._translate("sum(rate(http_request_duration_seconds_bucket[5m])) by (le)")
        esql = yaml_panel["esql"]
        assert esql["type"] == "heatmap"
        assert esql["y_axis"]["field"] == "le"
        assert "x_axis" in esql and "metric" in esql
        assert result.status in ("migrated", "migrated_with_warnings")

    def test_heatmap_without_breakdown_degrades_to_line(self):
        # No second dimension -> cannot be a heatmap -> line/metric fallback, with a warning.
        yaml_panel, _ = self._translate("sum(rate(http_requests_total[5m]))")
        assert yaml_panel["esql"]["type"] in ("line", "metric")
