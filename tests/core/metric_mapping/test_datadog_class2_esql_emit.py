# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Class-2 metric_map fields must appear in emitted Datadog ES|QL."""

from __future__ import annotations

import unittest

from observability_migration.adapters.source.datadog.field_map import FieldMapProfile
from observability_migration.adapters.source.datadog.models import MetricQuery, NormalizedWidget, WidgetQuery
from observability_migration.adapters.source.datadog.monitor_translate import (
    translate_monitor_to_alert_query,
)
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.translate import translate_widget
from observability_migration.core.metric_mapping import normalize_metric_map


class DatadogClass2EsqlEmitTests(unittest.TestCase):
    def test_attribute_filter_emits_where_clause(self) -> None:
        profile = FieldMapProfile(
            name="test",
            metric_index="metrics-*",
            metric_map=normalize_metric_map(
                {
                    "system.net.bytes_rcvd": {
                        "target": "system.network.in.bytes",
                        "attribute_filter": {"network.direction": "receive"},
                    }
                }
            ),
        )
        mq = MetricQuery(metric="system.net.bytes_rcvd", space_agg="avg")
        wq = WidgetQuery(
            name="q1",
            data_source="metrics",
            raw_query="avg:system.net.bytes_rcvd{*}",
            metric_query=mq,
            query_type="metric",
        )
        widget = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="Network",
            queries=[wq],
        )
        plan = plan_widget(widget)
        result = translate_widget(widget, plan, profile)
        self.assertNotIn(result.status, ("not_feasible", "blocked"))
        assert result.esql_query is not None
        self.assertIn('network.direction == "receive"', result.esql_query)
        self.assertIn("system.network.in.bytes", result.esql_query)

    def test_variants_select_from_scope_tags(self) -> None:
        from observability_migration.adapters.source.datadog.models import TagFilter

        profile = FieldMapProfile(
            name="test",
            metric_index="metrics-*",
            metric_map=normalize_metric_map(
                {
                    "system.net.bytes": {
                        "variants": [
                            {
                                "source_filter": {"direction": "in"},
                                "target": "system.network.in.bytes",
                                "attribute_filter": {"network.direction": "receive"},
                            },
                            {
                                "source_filter": {"direction": "out"},
                                "target": "system.network.out.bytes",
                                "attribute_filter": {"network.direction": "transmit"},
                            },
                        ]
                    }
                }
            ),
        )
        mq = MetricQuery(
            metric="system.net.bytes",
            space_agg="avg",
            scope=[TagFilter(key="direction", value="in")],
        )
        wq = WidgetQuery(
            name="q1",
            data_source="metrics",
            raw_query="avg:system.net.bytes{direction:in}",
            metric_query=mq,
            query_type="metric",
        )
        widget = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="Network",
            queries=[wq],
        )
        plan = plan_widget(widget)
        result = translate_widget(widget, plan, profile)
        self.assertNotIn(result.status, ("not_feasible", "blocked"))
        assert result.esql_query is not None
        self.assertIn("system.network.in.bytes", result.esql_query)
        self.assertIn('network.direction == "receive"', result.esql_query)
        self.assertNotIn("system.network.out.bytes", result.esql_query)
        self.assertNotIn('direction == "in"', result.esql_query)

    def test_target_index_overrides_from_clause(self) -> None:
        profile = FieldMapProfile(
            name="test",
            metric_index="metrics-*",
            metric_map=normalize_metric_map(
                {
                    "system.cpu.user": {
                        "target": "system.cpu.user",
                        "target_index": "metrics-host-*",
                    }
                }
            ),
        )
        mq = MetricQuery(metric="system.cpu.user", space_agg="avg")
        wq = WidgetQuery(
            name="q1",
            data_source="metrics",
            raw_query="avg:system.cpu.user{*}",
            metric_query=mq,
            query_type="metric",
        )
        widget = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="CPU",
            queries=[wq],
        )
        plan = plan_widget(widget)
        result = translate_widget(widget, plan, profile)
        self.assertNotIn(result.status, ("not_feasible", "blocked"))
        assert result.esql_query is not None
        self.assertIn("FROM metrics-host-*", result.esql_query)

    def test_monitor_exact_rollup_applies_class2_options(self) -> None:
        from observability_migration.adapters.source.datadog.models import TagFilter

        profile = FieldMapProfile(
            name="test",
            metric_index="metrics-*",
            metric_map=normalize_metric_map(
                {
                    "system.net.bytes": {
                        "variants": [
                            {
                                "source_filter": {"direction": "in"},
                                "target": "system.network.in.kbytes",
                                "attribute_filter": {"network.direction": "receive"},
                                "unit_scale": 0.001,
                                "target_index": "metrics-network-*",
                            }
                        ]
                    }
                }
            ),
        )
        # Sanity-check parser shape used by the monitor query below.
        mq = MetricQuery(
            metric="system.net.bytes",
            space_agg="avg",
            scope=[TagFilter(key="direction", value="in")],
        )
        self.assertEqual(mq.scope_tags, {"direction": "in"})

        result = translate_monitor_to_alert_query(
            {
                "type": "metric alert",
                "query": "avg(last_5m):avg:system.net.bytes{direction:in}.rollup(avg, 60) > 10",
            },
            profile,
        )
        self.assertIn("FROM metrics-network-*", result.translated_query)
        self.assertIn("system.network.in.kbytes", result.translated_query)
        self.assertIn('network.direction == "receive"', result.translated_query)
        self.assertIn("* 0.001", result.translated_query)
        self.assertNotIn('direction == "in"', result.translated_query)

    def test_monitor_exact_rate_applies_unit_scale(self) -> None:
        profile = FieldMapProfile(
            name="test",
            metric_index="metrics-*",
            metric_map=normalize_metric_map(
                {
                    "source.bytes": {
                        "target": "target.kbytes",
                        "unit_scale": 0.001,
                    }
                }
            ),
        )
        result = translate_monitor_to_alert_query(
            {
                "type": "metric alert",
                "query": "sum(last_5m):sum:source.bytes{*}.as_rate() > 10",
            },
            profile,
        )
        self.assertIn("target.kbytes", result.translated_query)
        self.assertIn("* 0.001", result.translated_query)

    def _widget_result(self, metric: str, raw_query: str, metric_map: dict, *, field_caps=None):
        profile = FieldMapProfile(
            name="test",
            metric_index="metrics-*",
            metric_map=normalize_metric_map(metric_map),
            field_caps=field_caps or {},
            metric_field_caps=field_caps or {},
        )
        mq = MetricQuery(metric=metric, space_agg="avg")
        if ".as_rate()" in raw_query:
            mq.as_rate = True
        wq = WidgetQuery(
            name="q1",
            data_source="metrics",
            raw_query=raw_query,
            metric_query=mq,
            query_type="metric",
        )
        widget = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="Metric",
            queries=[wq],
        )
        return translate_widget(widget, plan_widget(widget), profile)

    def test_variant_mismatch_surfaces_widget_warning(self) -> None:
        from observability_migration.adapters.source.datadog.models import TagFilter

        profile = FieldMapProfile(
            name="test",
            metric_index="metrics-*",
            metric_map=normalize_metric_map(
                {
                    "system.net.bytes": {
                        "variants": [
                            {
                                "source_filter": {"direction": "in"},
                                "target": "system.network.in.bytes",
                            }
                        ]
                    }
                }
            ),
        )
        mq = MetricQuery(
            metric="system.net.bytes",
            space_agg="avg",
            scope=[TagFilter(key="direction", value="out")],
        )
        wq = WidgetQuery(
            name="q1",
            data_source="metrics",
            raw_query="avg:system.net.bytes{direction:out}",
            metric_query=mq,
            query_type="metric",
        )
        widget = NormalizedWidget(
            id="1",
            widget_type="timeseries",
            title="Network",
            queries=[wq],
        )
        result = translate_widget(widget, plan_widget(widget), profile)
        self.assertNotIn(result.status, ("not_feasible", "blocked"))
        self.assertTrue(
            any("none matched" in str(w) for w in result.warnings),
            result.warnings,
        )
        assert result.esql_query is not None
        self.assertNotIn("system.network.in.bytes", result.esql_query)

    def test_to_rate_emits_rate_when_target_is_counter(self) -> None:
        from observability_migration.core.verification.field_capabilities import FieldCapability

        caps = {
            "target.bytes": FieldCapability(
                name="target.bytes",
                type="double",
                time_series_metric_kind="counter",
            )
        }
        result = self._widget_result(
            "source.bytes",
            "avg:source.bytes{*}",
            {
                "source.bytes": {
                    "target": "target.bytes",
                    "transform": "to_rate",
                }
            },
            field_caps=caps,
        )
        self.assertNotIn(result.status, ("not_feasible", "blocked"))
        assert result.esql_query is not None
        self.assertIn("target.bytes", result.esql_query)
        self.assertTrue(
            "DATE_DIFF" in result.esql_query or "RATE(" in result.esql_query,
            result.esql_query,
        )

    def test_drop_rate_strips_as_rate_when_target_is_gauge(self) -> None:
        from observability_migration.core.verification.field_capabilities import FieldCapability

        caps = {
            "target.bytes": FieldCapability(
                name="target.bytes",
                type="double",
                time_series_metric_kind="gauge",
            )
        }
        result = self._widget_result(
            "source.bytes",
            "avg:source.bytes{*}.as_rate()",
            {
                "source.bytes": {
                    "target": "target.bytes",
                    "transform": "drop_rate",
                }
            },
            field_caps=caps,
        )
        self.assertNotIn(result.status, ("not_feasible", "blocked"))
        assert result.esql_query is not None
        self.assertIn("target.bytes", result.esql_query)
        self.assertNotIn("DATE_DIFF", result.esql_query)

    def test_to_rate_unknown_kind_warns_and_does_not_invent_rate(self) -> None:
        result = self._widget_result(
            "source.bytes",
            "avg:source.bytes{*}",
            {
                "source.bytes": {
                    "target": "target.bytes",
                    "transform": "to_rate",
                }
            },
        )
        self.assertTrue(
            any("to_rate requires known" in str(w) for w in result.warnings),
            result.warnings,
        )
        assert result.esql_query is not None
        self.assertNotIn("DATE_DIFF", result.esql_query)


if __name__ == "__main__":
    unittest.main()
