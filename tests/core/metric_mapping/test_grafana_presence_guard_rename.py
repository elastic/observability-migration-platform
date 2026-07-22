# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression: metric_map renames must reach the fused-measure presence guard.

A nested ``count(count(<metric>) by (label))`` sub-expression collapses to
``COUNT_DISTINCT(label)`` but still emits a ``<metric> IS NOT NULL`` presence
guard in the shared measure pipeline. The metric_map / profile rename has to be
applied to that guard too, otherwise it references the raw source metric name
and empties the panel against a remapped target.
"""

from __future__ import annotations

import unittest

from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.adapters.source.grafana.translate import (
    translate_promql_to_esql,
)
from observability_migration.core.metric_mapping import normalize_metric_map

# node_exporter "CPU Busy" gauge: the count(count(...) by (cpu)) core count is
# fused with node_load1 into one measure pipeline with an OR presence guard.
_EXPR = "scalar(node_load1) * 100 / count(count(node_cpu_seconds_total) by (cpu))"


class GrafanaPresenceGuardRenameTests(unittest.TestCase):
    def _translate(self, metric_map: dict[str, str]) -> str:
        rule_pack = RulePackConfig()
        rule_pack.metric_map.update(normalize_metric_map(metric_map))
        resolver = SchemaResolver(rule_pack)
        result = translate_promql_to_esql(
            _EXPR,
            datasource_index="metrics-*",
            panel_type="timeseries",
            rule_pack=rule_pack,
            resolver=resolver,
        )
        self.assertEqual(result.feasibility, "feasible", result.warnings)
        return result.esql_query

    def test_metric_map_rename_reaches_presence_guard(self) -> None:
        esql = self._translate({"node_cpu_seconds_total": "system.cpu.time"})
        self.assertIn("system.cpu.time IS NOT NULL", esql)
        self.assertNotIn("node_cpu_seconds_total IS NOT NULL", esql)

    def test_without_map_guard_uses_source_name(self) -> None:
        esql = self._translate({})
        self.assertIn("node_cpu_seconds_total IS NOT NULL", esql)


if __name__ == "__main__":
    unittest.main()
