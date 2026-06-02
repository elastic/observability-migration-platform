# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import importlib.util
import pathlib
import unittest


def _load():
    p = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "lens_reconstruct.py"
    spec = importlib.util.spec_from_file_location("lens_reconstruct", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lr = _load()


class AggExprTests(unittest.TestCase):
    def test_sum(self):
        self.assertEqual(lr._agg_expr({"aggregation": "sum", "field": "x"}),
                         ("SUM(x)", "x_sum", False))

    def test_average(self):
        self.assertEqual(lr._agg_expr({"aggregation": "average", "field": "x"}),
                         ("AVG(x)", "x_average", False))

    def test_min_max(self):
        self.assertEqual(lr._agg_expr({"aggregation": "min", "field": "x"}),
                         ("MIN(x)", "x_min", False))
        self.assertEqual(lr._agg_expr({"aggregation": "max", "field": "x"}),
                         ("MAX(x)", "x_max", False))

    def test_count_star_no_field(self):
        self.assertEqual(lr._agg_expr({"aggregation": "count"}),
                         ("COUNT(*)", "count", False))

    def test_unique_count(self):
        self.assertEqual(lr._agg_expr({"aggregation": "unique_count", "field": "x"}),
                         ("COUNT_DISTINCT(x)", "x_unique_count", False))

    def test_median(self):
        self.assertEqual(lr._agg_expr({"aggregation": "median", "field": "x"}),
                         ("MEDIAN(x)", "x_median", False))

    def test_standard_deviation_maps_to_std_dev(self):
        self.assertEqual(lr._agg_expr({"aggregation": "standard_deviation", "field": "x"}),
                         ("STD_DEV(x)", "x_standard_deviation", False))

    def test_percentile_uses_p(self):
        self.assertEqual(lr._agg_expr({"aggregation": "percentile", "field": "x", "percentile": 95}),
                         ("PERCENTILE(x, 95)", "x_pct95", False))

    def test_last_value_needs_ts(self):
        self.assertEqual(lr._agg_expr({"aggregation": "last_value", "field": "x"}),
                         ("LAST_OVER_TIME(x)", "x_last_value", True))

    def test_unknown_aggregation_returns_none(self):
        self.assertEqual(lr._agg_expr({"aggregation": "bogus", "field": "x"}), None)


class QuoteTests(unittest.TestCase):
    def test_plain_field_unquoted(self):
        self.assertEqual(lr._quote("docker_image"), "docker_image")

    def test_dotted_field_unquoted(self):
        # dotted ECS fields are valid bare identifiers in ES|QL
        self.assertEqual(lr._quote("host.name"), "host.name")

    def test_hyphenated_field_backticked(self):
        self.assertEqual(lr._quote("client-id"), "`client-id`")

    def test_field_with_space_backticked(self):
        self.assertEqual(lr._quote("my field"), "`my field`")

    def test_already_backticked_left_alone(self):
        self.assertEqual(lr._quote("`client-id`"), "`client-id`")


class BreakdownTests(unittest.TestCase):
    def test_no_breakdown(self):
        self.assertEqual(lr._breakdown_fields({"type": "line"}), [])

    def test_singular_breakdown(self):
        self.assertEqual(
            lr._breakdown_fields({"breakdown": {"type": "values", "field": "docker_image"}}),
            ["docker_image"],
        )

    def test_plural_breakdowns(self):
        self.assertEqual(
            lr._breakdown_fields({"breakdowns": [
                {"type": "values", "field": "pod"},
                {"type": "values", "field": "ns"},
            ]}),
            ["pod", "ns"],
        )

    def test_breakdown_without_field_ignored(self):
        self.assertEqual(lr._breakdown_fields({"breakdown": {"type": "values"}}), [])


class LineReconstructTests(unittest.TestCase):
    def test_line_sum_no_breakdown(self):
        q, cols, reason = lr.lens_to_esql({
            "type": "line", "data_view": "metrics-*",
            "dimension": {"type": "date_histogram", "field": "@timestamp"},
            "metrics": [{"aggregation": "sum", "field": "apache_net_request_per_s"}],
        })
        self.assertIsNone(reason)
        self.assertEqual(
            q,
            "FROM metrics-*\n"
            "| STATS apache_net_request_per_s_sum = SUM(apache_net_request_per_s)"
            " BY tbucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)",
        )
        self.assertEqual(cols, ["apache_net_request_per_s_sum", "tbucket"])

    def test_line_average_with_breakdown(self):
        q, cols, reason = lr.lens_to_esql({
            "type": "line", "data_view": "metrics-*",
            "dimension": {"type": "date_histogram", "field": "@timestamp"},
            "metrics": [{"aggregation": "average", "field": "docker_cpu_user"}],
            "breakdown": {"type": "values", "field": "docker_image"},
        })
        self.assertIsNone(reason)
        self.assertEqual(
            q,
            "FROM metrics-*\n"
            "| STATS docker_cpu_user_average = AVG(docker_cpu_user)"
            " BY docker_image, tbucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)",
        )
        self.assertEqual(cols, ["docker_cpu_user_average", "docker_image", "tbucket"])

    def test_line_hyphenated_breakdown_quoted_in_query_bare_in_cols(self):
        q, cols, reason = lr.lens_to_esql({
            "type": "line", "data_view": "metrics-*",
            "dimension": {"type": "date_histogram", "field": "@timestamp"},
            "metrics": [{"aggregation": "sum", "field": "x"}],
            "breakdown": {"type": "values", "field": "client-id"},
        })
        self.assertIsNone(reason)
        self.assertIn("BY `client-id`, tbucket =", q)
        self.assertEqual(cols, ["x_sum", "client-id", "tbucket"])

    def test_line_missing_date_histogram_unsupported(self):
        q, cols, reason = lr.lens_to_esql({
            "type": "line", "data_view": "metrics-*",
            "dimension": {"type": "terms", "field": "host"},
            "metrics": [{"aggregation": "sum", "field": "x"}],
        })
        self.assertIsNone(q)
        self.assertEqual(cols, [])
        self.assertIn("dimension", reason)


class OtherChartTests(unittest.TestCase):
    def test_metric_chart_primary_no_bucket(self):
        q, cols, reason = lr.lens_to_esql({
            "type": "metric", "data_view": "metrics-*",
            "primary": {"aggregation": "average", "field": "cpu"},
        })
        self.assertIsNone(reason)
        self.assertEqual(q, "FROM metrics-*\n| STATS cpu_average = AVG(cpu)")
        self.assertEqual(cols, ["cpu_average"])

    def test_pie_with_plural_breakdowns_no_bucket(self):
        q, cols, reason = lr.lens_to_esql({
            "type": "pie", "data_view": "metrics-*",
            "metrics": [{"aggregation": "sum", "field": "v"}],
            "breakdowns": [{"type": "values", "field": "pod"}],
        })
        self.assertIsNone(reason)
        self.assertEqual(q, "FROM metrics-*\n| STATS v_sum = SUM(v) BY pod")
        self.assertEqual(cols, ["v_sum", "pod"])

    def test_datatable_no_breakdown(self):
        q, cols, reason = lr.lens_to_esql({
            "type": "datatable", "data_view": "metrics-*",
            "metrics": [{"aggregation": "max", "field": "v"}],
        })
        self.assertIsNone(reason)
        self.assertEqual(q, "FROM metrics-*\n| STATS v_max = MAX(v)")
        self.assertEqual(cols, ["v_max"])

    def test_last_value_line_uses_ts_source(self):
        q, _cols, reason = lr.lens_to_esql({
            "type": "line", "data_view": "metrics-*",
            "dimension": {"type": "date_histogram", "field": "@timestamp"},
            "metrics": [{"aggregation": "last_value", "field": "queue_depth"}],
        })
        self.assertIsNone(reason)
        self.assertTrue(q.startswith("TS metrics-*\n"))
        self.assertIn("queue_depth_last_value = LAST_OVER_TIME(queue_depth)", q)

    def test_unknown_chart_type_unsupported(self):
        q, cols, reason = lr.lens_to_esql({
            "type": "treemap", "data_view": "metrics-*",
            "metrics": [{"aggregation": "sum", "field": "v"}],
        })
        self.assertIsNone(q)
        self.assertEqual(cols, [])
        self.assertIn("treemap", reason)

    def test_metric_chart_unknown_agg_unsupported(self):
        q, _cols, reason = lr.lens_to_esql({
            "type": "metric", "data_view": "metrics-*",
            "primary": {"aggregation": "bogus", "field": "v"},
        })
        self.assertIsNone(q)
        self.assertIn("aggregation", reason)


class CounterFieldTests(unittest.TestCase):
    """Counter-typed metrics cannot be aggregated on FROM with bare SUM/AVG;
    ES|QL requires the TS command with a counter-aware inner function. The
    caller supplies the set of counter fields (a runtime field_caps property,
    not present in the Lens YAML)."""

    def test_agg_expr_wraps_counter_in_last_over_time_on_ts(self):
        self.assertEqual(
            lr._agg_expr({"aggregation": "sum", "field": "c"}, counter_fields={"c"}),
            ("SUM(LAST_OVER_TIME(c))", "c_sum", True),
        )

    def test_agg_expr_gauge_unchanged_when_counter_set_present(self):
        self.assertEqual(
            lr._agg_expr({"aggregation": "sum", "field": "g"}, counter_fields={"c"}),
            ("SUM(g)", "g_sum", False),
        )

    def test_agg_expr_count_star_unaffected_by_counter_set(self):
        # COUNT(*) has no field; counter typing is irrelevant.
        self.assertEqual(
            lr._agg_expr({"aggregation": "count"}, counter_fields={"c"}),
            ("COUNT(*)", "count", False),
        )

    def test_agg_expr_percentile_counter_wrapped(self):
        self.assertEqual(
            lr._agg_expr({"aggregation": "percentile", "field": "c", "percentile": 90},
                         counter_fields={"c"}),
            ("PERCENTILE(LAST_OVER_TIME(c), 90)", "c_pct90", True),
        )

    def test_line_counter_metric_uses_ts_source(self):
        q, cols, reason = lr.lens_to_esql(
            {
                "type": "line", "data_view": "metrics-*",
                "dimension": {"type": "date_histogram", "field": "@timestamp"},
                "metrics": [{"aggregation": "sum", "field": "apache_conns_total"}],
            },
            counter_fields={"apache_conns_total"},
        )
        self.assertIsNone(reason)
        self.assertEqual(
            q,
            "TS metrics-*\n"
            "| STATS apache_conns_total_sum = SUM(LAST_OVER_TIME(apache_conns_total))"
            " BY tbucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)",
        )
        self.assertEqual(cols, ["apache_conns_total_sum", "tbucket"])

    def test_line_counter_metric_with_breakdown_uses_ts(self):
        q, cols, reason = lr.lens_to_esql(
            {
                "type": "line", "data_view": "metrics-*",
                "dimension": {"type": "date_histogram", "field": "@timestamp"},
                "metrics": [{"aggregation": "average", "field": "docker_net_bytes_rcvd"}],
                "breakdown": {"type": "values", "field": "docker_image"},
            },
            counter_fields={"docker_net_bytes_rcvd"},
        )
        self.assertIsNone(reason)
        self.assertEqual(
            q,
            "TS metrics-*\n"
            "| STATS docker_net_bytes_rcvd_average = AVG(LAST_OVER_TIME(docker_net_bytes_rcvd))"
            " BY docker_image, tbucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)",
        )
        self.assertEqual(cols, ["docker_net_bytes_rcvd_average", "docker_image", "tbucket"])

    def test_no_counter_set_defaults_to_from(self):
        # Backward-compatible: omitting counter_fields keeps FROM + bare agg.
        q, _, reason = lr.lens_to_esql({
            "type": "line", "data_view": "metrics-*",
            "dimension": {"type": "date_histogram", "field": "@timestamp"},
            "metrics": [{"aggregation": "sum", "field": "apache_conns_total"}],
        })
        self.assertIsNone(reason)
        self.assertTrue(q.startswith("FROM metrics-*\n"))
        self.assertIn("SUM(apache_conns_total)", q)


if __name__ == "__main__":
    unittest.main()
