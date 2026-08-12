# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression tests for Datadog's adaptive FROM/TS bucket-width unification.

See docs/design/datadog-esql-time-bucketing-adaptivity.md.
"""

from __future__ import annotations

import unittest
from copy import deepcopy

from observability_migration.adapters.source.datadog import translate
from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.models import NormalizedWidget, WidgetFormula, WidgetQuery
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.query_parser import parse_formula, parse_metric_query
from observability_migration.adapters.source.datadog.translate import translate_widget
from observability_migration.core.verification.field_capabilities import FieldCapability


class TestTimeBucketExprHelper(unittest.TestCase):
    def test_default_time_bucket_expr_uses_75(self):
        self.assertEqual(
            translate.TIME_BUCKET_EXPR,
            "BUCKET(@timestamp, 75, ?_tstart, ?_tend)",
        )

    def test_rate_safe_time_bucket_expr_uses_20(self):
        self.assertEqual(
            translate._time_bucket_expr(True),
            "BUCKET(@timestamp, 20, ?_tstart, ?_tend)",
        )

    def test_non_rate_safe_time_bucket_expr_matches_default(self):
        self.assertEqual(
            translate._time_bucket_expr(False),
            translate.TIME_BUCKET_EXPR,
        )

class TestSingleQueryFromPathBucketSplit(unittest.TestCase):
    def _widget(
        self,
        query: str,
        widget_type: str = "timeseries",
        *,
        aggregator: str = "",
    ) -> NormalizedWidget:
        mq = parse_metric_query(query)
        wq = WidgetQuery(
            name="query1",
            data_source="metrics",
            raw_query=query,
            metric_query=mq,
            query_type="metric",
            aggregator=aggregator,
        )
        return NormalizedWidget(id="1", widget_type=widget_type, title="w", queries=[wq])

    def test_plain_gauge_timeseries_uses_75_bucket(self):
        widget = self._widget("avg:system.cpu.user{*}")
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertIn("BUCKET(@timestamp, 75, ?_tstart, ?_tend)", result.esql_query)
        self.assertNotIn("BUCKET(@timestamp, 20", result.esql_query)

    def test_as_rate_timeseries_uses_20_bucket(self):
        widget = self._widget("sum:http.requests{*}.as_rate()")
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertIn("BUCKET(@timestamp, 20, ?_tstart, ?_tend)", result.esql_query)
        self.assertNotIn("BUCKET(@timestamp, 75", result.esql_query)

    def test_as_rate_query_value_uses_20_bucket(self):
        # Grouped query_value collapses to a ranked table via
        # _build_categorical_esql(reducer=...) -- still must stay rate-safe.
        widget = self._widget("sum:http.requests{*} by {host}.as_rate()", widget_type="query_value")
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertIn("BUCKET(@timestamp, 20, ?_tstart, ?_tend)", result.esql_query)

    def test_plain_gauge_toplist_uses_75_bucket(self):
        # Toplist widgets with a request reducer bucket over time before ranking.
        widget = self._widget(
            "avg:system.cpu.user{*} by {host}",
            widget_type="toplist",
            aggregator="last",
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertIn("BUCKET(@timestamp, 75, ?_tstart, ?_tend)", result.esql_query)


class TestFormulaFromPathBucketSplit(unittest.TestCase):
    def _formula_widget(self, queries: list[tuple[str, str]], formula_raw: str) -> NormalizedWidget:
        wqs = []
        for name, query in queries:
            mq = parse_metric_query(query)
            wqs.append(
                WidgetQuery(
                    name=name,
                    data_source="metrics",
                    raw_query=query,
                    metric_query=mq,
                    query_type="metric",
                )
            )
        wf = WidgetFormula(raw=formula_raw)
        wf.expression = parse_formula(formula_raw)
        return NormalizedWidget(id="1", widget_type="timeseries", title="w", queries=wqs, formulas=[wf])

    def test_plain_ratio_formula_uses_75_bucket(self):
        widget = self._formula_widget(
            [("query1", "sum:haproxy.backend.response.2xx{*}"), ("query2", "sum:haproxy.backend.response.4xx{*}")],
            "query1 / (query1 + query2) * 100",
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertIn("BUCKET(@timestamp, 75, ?_tstart, ?_tend)", result.esql_query)

    def test_rate_formula_on_gauge_uses_20_bucket(self):
        widget = self._formula_widget([("query1", "avg:mysql.performance.user_time{*}")], "rate(query1)")
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertIn("BUCKET(@timestamp, 20, ?_tstart, ?_tend)", result.esql_query)

    def test_diff_formula_on_gauge_uses_20_bucket(self):
        widget = self._formula_widget([("query1", "sum:redis.keyspace.hits{*}")], "diff(query1)")
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertIn("BUCKET(@timestamp, 20, ?_tstart, ?_tend)", result.esql_query)

    def test_as_rate_query_used_in_formula_uses_20_bucket(self):
        widget = self._formula_widget(
            [("query1", "sum:http.requests{*}.as_rate()"), ("query2", "sum:http.errors{*}.as_rate()")],
            "query1 - query2",
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertIn("BUCKET(@timestamp, 20, ?_tstart, ?_tend)", result.esql_query)

class TestTsRatePathAdaptiveWindowless(unittest.TestCase):
    def _counter_profile(self, es_field: str, metric_type: str = "counter_long"):
        profile = deepcopy(OTEL_PROFILE)
        profile.field_caps[es_field] = FieldCapability(
            name=es_field, type=metric_type, time_series_metric_kind="counter",
        )
        return profile

    def test_ts_rate_uses_adaptive_20_bucket_no_window(self):
        profile = self._counter_profile("parity_counter")
        query = "sum:parity.counter{host:h1}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        wf = WidgetFormula(raw="rate(query1)")
        wf.expression = parse_formula("rate(query1)")
        widget = NormalizedWidget(id="1", widget_type="timeseries", title="w", queries=[wq], formulas=[wf])
        result = translate_widget(widget, plan_widget(widget), profile)
        self.assertIn("TBUCKET(20, ?_tstart, ?_tend)", result.esql_query)
        self.assertIn("RATE(parity_counter)", result.esql_query)
        self.assertNotIn("RATE(parity_counter, 5 minute)", result.esql_query)
        self.assertNotIn("TBUCKET(5 minute)", result.esql_query)

    def test_ts_increase_uses_adaptive_20_bucket_no_window(self):
        profile = self._counter_profile("parity_counter", "counter_double")
        query = "sum:parity.counter{host:h1}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        wf = WidgetFormula(raw="diff(query1)")
        wf.expression = parse_formula("diff(query1)")
        widget = NormalizedWidget(id="1", widget_type="timeseries", title="w", queries=[wq], formulas=[wf])
        result = translate_widget(widget, plan_widget(widget), profile)
        self.assertIn("TBUCKET(20, ?_tstart, ?_tend)", result.esql_query)
        self.assertIn("INCREASE(parity_counter)", result.esql_query)
        self.assertNotIn("INCREASE(parity_counter, 5 minute)", result.esql_query)
