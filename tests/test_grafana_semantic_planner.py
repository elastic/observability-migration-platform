# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import unittest

import pytest

from observability_migration.adapters.source.grafana.promql import (
    _gauge_fallback_for_counter_range_func,
)
from observability_migration.adapters.source.grafana.semantic_planner import (
    RuntimeCapabilities,
    plan_grafana_metric_contract,
)
from observability_migration.core.assets.query import QueryIR


class TestGrafanaSemanticPlanner(unittest.TestCase):
    def test_rate_prefers_native_promql_when_runtime_supports_it(self):
        query_ir = QueryIR(
            source_language=" PromQL ",
            source_expression="rate(http_requests_total[5m])",
            metric="http_requests_total",
            range_function="rate",
            panel_type="timeseries",
        )

        contract = plan_grafana_metric_contract(
            query_ir,
            runtime_capabilities=RuntimeCapabilities(promql=True),
        )

        self.assertEqual(contract.canonical_target, "promql")
        self.assertEqual(contract.exactness_class, "exact_if_contract_met")

    def test_gauge_prefers_ts_when_promql_runtime_is_unavailable(self):
        query_ir = QueryIR(
            source_language="promql",
            source_expression="node_systemd_units",
            metric="node_systemd_units",
            panel_type="timeseries",
        )

        contract = plan_grafana_metric_contract(
            query_ir,
            runtime_capabilities=RuntimeCapabilities(promql=False),
        )

        self.assertEqual(contract.canonical_target, "ts")
        self.assertEqual(contract.target_shape["target_mode"], "all_tsds")
        self.assertEqual(contract.runtime_requirements["source_command"], "TS")
        self.assertIs(contract.fulfillment_hints.get("allow_index_narrowing"), True)

    def test_document_shaped_metric_query_can_fall_through_to_from(self):
        query_ir = QueryIR(
            source_language="promql",
            source_expression="count(node_systemd_units) by (state)",
            metric="node_systemd_units",
            outer_agg="count",
            group_labels=["state"],
            panel_type="table",
        )

        contract = plan_grafana_metric_contract(
            query_ir,
            runtime_capabilities=RuntimeCapabilities(promql=False),
        )

        self.assertEqual(contract.canonical_target, "from")

    def test_non_promql_ir_does_not_prefer_promql_target(self):
        query_ir = QueryIR(
            source_language="esql",
            source_expression="FROM metrics-* | STATS AVG(cpu)",
            metric="cpu",
            panel_type="timeseries",
        )

        contract = plan_grafana_metric_contract(
            query_ir,
            runtime_capabilities=RuntimeCapabilities(promql=True),
        )

        self.assertNotEqual(contract.canonical_target, "promql")

    def test_document_shaped_metric_query_normalizes_case_for_from_fallback(self):
        query_ir = QueryIR(
            source_language="promql",
            source_expression="count(node_systemd_units) by (state)",
            metric="node_systemd_units",
            outer_agg=" Count ",
            group_labels=["state"],
            panel_type=" Table ",
        )

        contract = plan_grafana_metric_contract(
            query_ir,
            runtime_capabilities=RuntimeCapabilities(promql=False),
        )

        self.assertEqual(contract.canonical_target, "from")


def test_gauge_fallback_known_funcs():
    for fn in ("rate", "irate", "increase"):
        result = _gauge_fallback_for_counter_range_func(fn)
        assert isinstance(result, tuple) and len(result) == 2


def test_gauge_fallback_unknown_func_raises_value_error():
    with pytest.raises(ValueError, match="no gauge fallback"):
        _gauge_fallback_for_counter_range_func("delta")
