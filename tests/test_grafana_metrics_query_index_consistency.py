# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Consistency: native PROMQL and ES|QL must share one metrics query target.

When operators set ``--esql-index`` to a concrete Prometheus stream (and leave
``--data-view`` as a broader UI pattern), every emitted *query* — native
``PROMQL index=…`` and ES|QL ``TS``/``FROM`` — must read that same stream.
Schema discovery already probes ``esql_index or data_view``; query emission
must not silently diverge.
"""

from __future__ import annotations

import re
import unittest

from observability_migration.adapters.source.grafana import panels, rules, schema

CONCRETE = "metrics-alloy.prometheus-default"
BROAD = "metrics-*"


class MetricsQueryIndexConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.rp = rules.RulePackConfig(native_promql=True)
        self.resolver = schema.SchemaResolver(self.rp)

    def translate(self, panel, *, datasource_index=BROAD, esql_index=CONCRETE):
        return panels.translate_panel(
            panel,
            datasource_index=datasource_index,
            esql_index=esql_index,
            rule_pack=self.rp,
            resolver=self.resolver,
        )

    def test_helper_prefers_esql_index(self):
        self.assertEqual(
            panels.metrics_query_index(BROAD, CONCRETE),
            CONCRETE,
        )
        self.assertEqual(
            panels.metrics_query_index(BROAD, None),
            BROAD,
        )
        self.assertEqual(
            panels.metrics_query_index(BROAD, ""),
            BROAD,
        )

    def test_native_promql_uses_esql_index_not_data_view(self):
        """Scalar/counter panel that stays native must not ignore --esql-index."""
        panel = {
            "id": 1,
            "type": "timeseries",
            "title": "HTTP rate",
            "targets": [
                {
                    "refId": "A",
                    "expr": "sum(rate(http_requests_total[5m]))",
                    "legendFormat": "requests",
                }
            ],
            "fieldConfig": {"defaults": {}, "overrides": []},
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
        }
        yaml_panel, _result = self.translate(panel)
        query = (yaml_panel.get("esql") or {}).get("query") or ""
        self.assertTrue(
            query.startswith("PROMQL"),
            f"expected native PROMQL for simple rate(), got: {query[:160]}",
        )
        self.assertIn(
            f"PROMQL index={CONCRETE}",
            query,
            f"native PROMQL must use esql_index={CONCRETE}, not data-view={BROAD}:\n{query}",
        )
        self.assertNotIn(f"PROMQL index={BROAD}", query)

    def test_esql_fallback_uses_same_esql_index(self):
        """Panel that falls through to ES|QL must TS/FROM the same concrete index."""
        panel = {
            "id": 2,
            "type": "timeseries",
            "title": "Grouped rate",
            "targets": [
                {
                    "refId": "A",
                    "expr": "sum by (instance) (rate(http_requests_total[5m]))",
                    "legendFormat": "{{instance}}",
                }
            ],
            "fieldConfig": {"defaults": {}, "overrides": []},
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
        }
        yaml_panel, _result = self.translate(panel)
        query = (yaml_panel.get("esql") or {}).get("query") or ""
        self.assertFalse(
            query.startswith("PROMQL"),
            f"expected ES|QL fallback for by(instance), got: {query[:160]}",
        )
        self.assertRegex(
            query,
            rf"^(TS|FROM) {re.escape(CONCRETE)}\b",
            f"ES|QL must target esql_index={CONCRETE}:\n{query}",
        )
        self.assertNotRegex(query, rf"^(TS|FROM) {re.escape(BROAD)}\b")

    def test_same_target_when_only_data_view_set(self):
        panel = {
            "id": 3,
            "type": "stat",
            "title": "Up",
            "targets": [{"refId": "A", "expr": "up", "legendFormat": "up"}],
            "fieldConfig": {"defaults": {}, "overrides": []},
            "gridPos": {"x": 0, "y": 0, "w": 4, "h": 4},
        }
        yaml_panel, _ = self.translate(panel, datasource_index=CONCRETE, esql_index=None)
        query = (yaml_panel.get("esql") or {}).get("query") or ""
        self.assertIn(CONCRETE, query)


if __name__ == "__main__":
    unittest.main()
