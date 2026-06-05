# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import json
import unittest

from observability_migration.core.verification import parity_oracle as po


class VerdictTests(unittest.TestCase):
    def test_strict_pass_under_1pct(self):
        c = po.Comparison(expr="x", esql="TS ...", common_series=1, compared_points=5, max_relative_error=0.005)
        self.assertEqual(c.verdict(), "STRICT_PASS")

    def test_fuzzy_pass_under_5pct(self):
        c = po.Comparison(expr="x", esql="TS ...", common_series=1, compared_points=5, max_relative_error=0.03)
        self.assertEqual(c.verdict(), "FUZZY_PASS")

    def test_no_common_series_is_fail(self):
        c = po.Comparison(expr="x", esql="TS ...", common_series=0, compared_points=0)
        self.assertEqual(c.verdict(), "FAIL")

    def test_skip_reason_wins(self):
        c = po.Comparison(expr="x", skipped_reason="translator marked not_feasible")
        self.assertEqual(c.verdict(), "SKIP")

    def test_shape_pass_when_values_diverge_but_series_overlap(self):
        c = po.Comparison(expr="x", esql="TS ...", common_series=1, compared_points=5, max_relative_error=0.2)
        self.assertEqual(c.verdict(), "SHAPE_PASS")

    def test_translated_error_is_error(self):
        c = po.Comparison(expr="x", esql="TS ...", translated_error="boom")
        self.assertEqual(c.verdict(), "ERROR")


class NormalizeAndDiffTests(unittest.TestCase):
    def test_compute_diff_identical_series_zero_error(self):
        a = {po.SeriesKey((("host", "a"),)): [(0.0, 10.0), (60.0, 20.0), (120.0, 30.0), (180.0, 40.0)]}
        b = {po.SeriesKey((("host", "a"),)): [(0.0, 10.0), (60.0, 20.0), (120.0, 30.0), (180.0, 40.0)]}
        points, rmax, _rmean = po.compute_diff(a, b, 60)
        self.assertGreater(points, 0)
        self.assertEqual(rmax, 0.0)

    def test_normalize_native_parses_value_step_columns(self):
        data = {
            "columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"}, {"name": "host", "type": "keyword"}],
            "values": [[10.0, "2026-01-01T00:00:00Z", "a"], [20.0, "2026-01-01T00:05:00Z", "a"]],
        }
        out = po.normalize_native(data)
        self.assertEqual(len(out), 1)

    def test_normalize_native_decodes_timeseries_label_column(self):
        # Native PROMQL packs series labels into a ``_timeseries`` JSON column
        # rather than broken-out columns. Ignoring it collapses every grouped
        # series into one empty-key series, which can never match the translated
        # side (which already decodes ``_timeseries``). Decode it symmetrically.
        def ts(state):
            return json.dumps({"labels": {"state": state, "job": "job_1", "instance": "1:1"}})
        data = {
            "columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"},
                        {"name": "_timeseries", "type": "keyword"}],
            "values": [
                [10.0, "2026-01-01T00:00:00Z", ts("busy")],
                [11.0, "2026-01-01T00:05:00Z", ts("busy")],
                [20.0, "2026-01-01T00:00:00Z", ts("idle")],
                [21.0, "2026-01-01T00:05:00Z", ts("idle")],
            ],
        }
        out = po.normalize_native(data)
        # Two distinct ``state`` values -> two series (job/instance are scrubbed
        # as PROMETHEUS_ONLY_LABELS, leaving ``state`` as the distinguishing key).
        self.assertEqual(len(out), 2)
        states = sorted(dict(k.labels).get("state") for k in out)
        self.assertEqual(states, ["busy", "idle"])

    def test_normalize_native_matches_translated_series_for_grouped_panel(self):
        # End-to-end symmetry: the same label set on both sides must yield the
        # same SeriesKeys so common-series intersection is non-empty.
        def native_ts(state):
            return json.dumps({"labels": {"state": state, "job": "job_1"}})
        native = po.normalize_native({
            "columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"},
                        {"name": "_timeseries", "type": "keyword"}],
            "values": [[1.0, "2026-01-01T00:00:00Z", native_ts("busy")],
                       [2.0, "2026-01-01T00:05:00Z", native_ts("busy")]],
        })
        translated = po.normalize_translated({
            "columns": [{"name": "computed_value", "type": "double"}, {"name": "time_bucket", "type": "date"},
                        {"name": "state", "type": "keyword"}],
            "values": [[1.0, "2026-01-01T00:00:00Z", "busy"],
                       [2.0, "2026-01-01T00:05:00Z", "busy"]],
        })
        self.assertTrue(set(native) & set(translated))

    def test_project_to_subset_sum_aligns_to_translated_dims(self):
        native = {
            po.SeriesKey((("dc", "x"), ("host", "a"))): [(0.0, 10.0)],
            po.SeriesKey((("dc", "y"), ("host", "a"))): [(0.0, 5.0)],
        }
        translated = {po.SeriesKey((("host", "a"),)): [(0.0, 15.0)]}
        projected = po._project_to_subset(native, translated)
        self.assertEqual(list(projected.keys()), [po.SeriesKey((("host", "a"),))])
        self.assertEqual(projected[po.SeriesKey((("host", "a"),))], [(0.0, 15.0)])

    def test_project_to_subset_averages_when_reducer_is_avg(self):
        # When the translated panel AVG()s by a label, collapsing native series
        # onto that label must AVERAGE, not SUM -- otherwise N native series
        # summed read N times the translated mean (rel err ~= (N-1)/N).
        native = {
            po.SeriesKey((("dc", "x"), ("host", "a"))): [(0.0, 10.0)],
            po.SeriesKey((("dc", "y"), ("host", "a"))): [(0.0, 20.0)],
            po.SeriesKey((("dc", "z"), ("host", "a"))): [(0.0, 30.0)],
        }
        translated = {po.SeriesKey((("host", "a"),)): [(0.0, 20.0)]}
        projected = po._project_to_subset(native, translated, reducer="avg")
        self.assertEqual(projected[po.SeriesKey((("host", "a"),))], [(0.0, 20.0)])

    def test_project_to_subset_takes_max_when_reducer_is_max(self):
        native = {
            po.SeriesKey((("dc", "x"), ("host", "a"))): [(0.0, 10.0)],
            po.SeriesKey((("dc", "y"), ("host", "a"))): [(0.0, 30.0)],
        }
        translated = {po.SeriesKey((("host", "a"),)): [(0.0, 30.0)]}
        projected = po._project_to_subset(native, translated, reducer="max")
        self.assertEqual(projected[po.SeriesKey((("host", "a"),))], [(0.0, 30.0)])

    def test_translated_reducer_detects_outer_aggregation(self):
        self.assertEqual(
            po._translated_reducer("TS m | STATS x = AVG(x) BY time_bucket = TBUCKET(5 minute), state"),
            "avg",
        )
        self.assertEqual(
            po._translated_reducer("TS m | STATS x = SUM(RATE(x, 5 minute)) BY time_bucket, dc"),
            "sum",
        )
        self.assertEqual(
            po._translated_reducer("TS m | STATS x = MAX(x) BY time_bucket"),
            "max",
        )

    def test_normalize_translated_canonicalizes_otel_labels(self):
        data = {
            "columns": [
                {"name": "computed_value", "type": "double"},
                {"name": "time_bucket", "type": "date"},
                {"name": "k8s.namespace.name", "type": "keyword"},
            ],
            "values": [
                [10.0, "2026-01-01T00:00:00Z", "ns1"],
                [20.0, "2026-01-01T00:00:00Z", "ns2"],
            ],
        }
        out = po.normalize_translated(data)
        self.assertEqual(len(out), 2)
        namespaces = sorted(dict(k.labels)["namespace"] for k in out)
        self.assertEqual(namespaces, ["ns1", "ns2"])


class SingleValueReductionTests(unittest.TestCase):
    def test_terminal_time_bucket_collapse_is_single_value(self):
        # Existing form: final STATS folds the per-bucket series to one row.
        esql = ("TS metrics-* | STATS m = MAX(LAST_OVER_TIME(m)) BY time_bucket = TBUCKET(5 minute) "
                "| SORT time_bucket ASC | STATS time_bucket = MAX(time_bucket), m = MAX(m) | KEEP time_bucket, m")
        self.assertTrue(po.is_single_value_reduction(esql))

    def test_count_distinct_only_is_single_value(self):
        # Grafana ``count(count(node_cpu_seconds_total) by (cpu))`` -> a scalar cardinality.
        esql = "FROM metrics-* | WHERE node_cpu_seconds_total IS NOT NULL | STATS node_cpu_seconds_total_count = COUNT_DISTINCT(cpu)"
        self.assertTrue(po.is_single_value_reduction(esql))

    def test_uptime_date_diff_scalar_is_single_value(self):
        # ``time() - haproxy_process_start_time_seconds`` -> one scalar uptime value.
        esql = ('FROM metrics-* | WHERE haproxy_process_start_time_seconds IS NOT NULL '
                '| STATS start_time_ms = MAX(haproxy_process_start_time_seconds * 1000) '
                '| EVAL uptime_seconds = DATE_DIFF("seconds", TO_DATETIME(start_time_ms), NOW()) | KEEP uptime_seconds')
        self.assertTrue(po.is_single_value_reduction(esql))

    def test_trailing_stats_without_time_bucket_is_single_value(self):
        # MTU/Speed stat panels: per-bucket STATS then a terminal STATS with no BY time_bucket.
        esql = ("TS metrics-* | WHERE node_network_mtu_bytes IS NOT NULL "
                "| STATS node_network_mtu_bytes = AVG(node_network_mtu_bytes) BY time_bucket = TBUCKET(5 minute), device "
                "| SORT time_bucket ASC | STATS node_network_mtu_bytes = MAX(node_network_mtu_bytes) BY device")
        self.assertTrue(po.is_single_value_reduction(esql))

    def test_multi_bucket_series_is_not_single_value(self):
        # A genuine time series whose terminal STATS still groups BY time_bucket must NOT be flagged.
        esql = ("TS metrics-* | WHERE m IS NOT NULL "
                "| STATS m = AVG(RATE(m, 5m)) BY time_bucket = TBUCKET(5 minute), device | SORT time_bucket ASC")
        self.assertFalse(po.is_single_value_reduction(esql))

    def test_multi_bucket_with_eval_after_is_not_single_value(self):
        # Trailing EVAL/KEEP that preserves time_bucket is still a series.
        esql = ("TS metrics-* | STATS v = AVG(AVG_OVER_TIME(v, 5m)) BY time_bucket = TBUCKET(5 minute) "
                "| EVAL computed_value = v * 8 | KEEP time_bucket, computed_value | SORT time_bucket ASC")
        self.assertFalse(po.is_single_value_reduction(esql))

    def test_from_aggregation_grouped_by_dimension_only_is_single_value(self):
        # FROM ... STATS ... BY <dimension> (no time bucket anywhere) is a single snapshot per dim, not a range series.
        esql = "FROM metrics-* | WHERE up IS NOT NULL | STATS up = MAX(up) BY instance"
        self.assertTrue(po.is_single_value_reduction(esql))


class SanitizeSourceForOracleTests(unittest.TestCase):
    """The native-PROMQL oracle must not be fed unexpanded Grafana template
    variables. ``{job="$job"}`` matches no seeded series, so every templated
    panel would FAIL with 0 comparable points even when the translation is
    correct. We drop variable-valued matchers (so the source side spans the
    same series as the no-filter translated side) and concretize duration
    macros so ``rate(x[$__rate_interval])`` is runnable.
    """

    def test_drops_simple_variable_matchers(self):
        out = po.sanitize_source_for_oracle(
            'apache_uptime_seconds_total{job="$job", instance="$instance"}', step=300
        )
        self.assertNotIn("$", out)
        # Both matchers were variable-valued -> selector collapses to the bare metric.
        self.assertEqual(out.replace(" ", ""), "apache_uptime_seconds_total")

    def test_drops_regex_and_composite_variable_matchers_and_resolves_rate_interval(self):
        out = po.sanitize_source_for_oracle(
            'rate(node_nfs_connections_total{instance=~"$node:$port",job=~"$job"}[$__rate_interval])',
            step=300,
        )
        self.assertNotIn("$", out)
        self.assertIn("rate(node_nfs_connections_total[", out)
        # The range macro became a concrete duration.
        self.assertRegex(out, r"\[\d+[smhdw]\]")
        # No surviving label matchers (all were variable-valued).
        self.assertNotIn("{", out)

    def test_keeps_static_matchers_drops_only_variable_ones(self):
        out = po.sanitize_source_for_oracle(
            'http_requests_total{status="200", job="$job"}', step=300
        )
        self.assertNotIn("$", out)
        self.assertIn('status="200"', out)
        self.assertNotIn("job=", out)

    def test_passthrough_when_no_variables(self):
        expr = "sum(rate(go_gc_duration_seconds_count[5m]))"
        self.assertEqual(po.sanitize_source_for_oracle(expr, step=300), expr)

    def test_compare_panel_sanitizes_before_native(self):
        # A templated source must reach native PROMQL with the $vars removed.
        seen = {}

        def request(method, path, body=None, content_type="application/json"):
            q = body.get("query", "") if isinstance(body, dict) else ""
            if q.startswith("PROMQL"):
                seen["native"] = q
                return {"columns": [{"name": "value", "type": "double"},
                                    {"name": "step", "type": "date"}],
                        "values": [[1.0, "2026-01-01T00:00:00Z"]]}
            return {"columns": [], "values": []}

        po.compare_panel(
            request,
            source_query='apache_uptime_seconds_total{job="$job"}',
            translated_query="TS metrics-* | STATS computed_value = MAX(x) BY time_bucket = TBUCKET(5m)",
            index="metrics-*", step=300,
            start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z",
        )
        self.assertIn("native", seen)
        self.assertNotIn("$job", seen["native"])


class ExecutionTests(unittest.TestCase):
    def _fake_request(self, native_data, translated_data, *, native_error=None):
        calls = []

        def request(method, path, body=None, content_type="application/json"):
            q = body.get("query", "") if isinstance(body, dict) else ""
            calls.append(q)
            if q.startswith("PROMQL"):
                if native_error:
                    return {"error": {"reason": native_error}}
                return native_data
            return translated_data

        request.calls = calls  # type: ignore[attr-defined]
        return request

    def test_compare_panel_strict_pass(self):
        # Space points one full step (5 min) apart so they land in DISTINCT buckets;
        # compute_diff trims the first/last bucket, so >=3 points are needed to leave
        # >=1 comparable bucket. Identical native/translated values => max err 0 => STRICT_PASS.
        stamps = [f"2026-01-01T00:{m:02d}:00Z" for m in (0, 5, 10, 15, 20)]
        vals = [10.0, 20.0, 30.0, 40.0, 50.0]
        series = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"}, {"name": "host", "type": "keyword"}],
                  "values": [[v, t, "a"] for t, v in zip(stamps, vals)]}
        translated = {"columns": [{"name": "computed_value", "type": "double"}, {"name": "time_bucket", "type": "date"}, {"name": "host", "type": "keyword"}],
                      "values": [[v, t, "a"] for t, v in zip(stamps, vals)]}
        req = self._fake_request(series, translated)
        result = po.compare_panel(req, source_query="go_goroutines",
                                  translated_query="TS metrics-* | STATS computed_value = AVG(x) BY time_bucket = TBUCKET(5m), host",
                                  index="metrics-*", step=300, start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.verdict(), "STRICT_PASS")
        self.assertEqual(result.max_relative_error, 0.0)
        self.assertGreaterEqual(result.compared_points, 1)

    def test_compare_panel_avg_panel_projection_uses_mean_not_sum(self):
        # Real corpus shape: source is a bare gauge (many native series via the
        # _timeseries label blob), translated AVG()s by one label. Native carries
        # extra phantom labels so no key matches directly -> projection runs. The
        # projection must AVERAGE native onto the translated label subset to match
        # AVG(); summing N series reads N* too high (the SHAPE_PASS-at-0.99 bug).
        stamps = [f"2026-01-01T00:{m:02d}:00Z" for m in (0, 5, 10, 15, 20)]
        # Three native series all collapse to state="busy"; each = 20 -> mean 20, sum 60.
        nvals = []
        for dc in ("x", "y", "z"):
            for t in stamps:
                nvals.append([20.0, t, json.dumps({"labels": {"state": "busy", "dc": dc}})])
        native = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"},
                              {"name": "_timeseries", "type": "keyword"}],
                  "values": nvals}
        translated = {"columns": [{"name": "computed_value", "type": "double"}, {"name": "time_bucket", "type": "date"},
                                  {"name": "state", "type": "keyword"}],
                      "values": [[20.0, t, "busy"] for t in stamps]}
        req = self._fake_request(native, translated)
        result = po.compare_panel(
            req, source_query="apache_workers",
            translated_query="TS metrics-* | STATS computed_value = AVG(apache_workers) BY time_bucket = TBUCKET(5 minute), state",
            index="metrics-*", step=300, start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertGreaterEqual(result.common_series, 1)
        # mean(20,20,20) == 20 == translated -> near-zero error, a real pass.
        self.assertLess(result.max_relative_error, 0.05)
        self.assertIn(result.verdict(), {"STRICT_PASS", "FUZZY_PASS"})

    def test_compare_panel_native_unparseable_is_skip(self):
        req = self._fake_request({}, {}, native_error="could not parse")
        result = po.compare_panel(req, source_query="weird_expr", translated_query="TS metrics-*",
                                  index="metrics-*", step=300, start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.verdict(), "SKIP")

    def test_compare_panel_no_esql_is_skip(self):
        req = self._fake_request({}, {})
        result = po.compare_panel(req, source_query="go_goroutines", translated_query="",
                                  index="metrics-*", step=300, start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.verdict(), "SKIP")

    def test_native_promql_available_true_false(self):
        ok = self._fake_request({"columns": [{"name": "value"}], "values": [[1.0]]}, {})
        self.assertTrue(po.native_promql_available(ok, "metrics-*"))
        bad = self._fake_request({}, {}, native_error="unknown command [PROMQL]")
        self.assertFalse(po.native_promql_available(bad, "metrics-*"))

    def test_single_value_reduction_is_skip_not_fail(self):
        # A Grafana stat/single-value panel translates to ES|QL that collapses the
        # per-bucket series to ONE row (final ``STATS time_bucket = MAX(time_bucket),
        # m = MAX(m)``). There is no time series to diff against the native range
        # vector, so point-wise comparison is meaningless -> SKIP, not a false FAIL.
        series = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"}],
                  "values": [[1.0, "2026-01-01T00:00:00Z"], [2.0, "2026-01-01T00:05:00Z"], [3.0, "2026-01-01T00:10:00Z"]]}
        single_row = {"columns": [{"name": "time_bucket", "type": "date"}, {"name": "m", "type": "double"}],
                      "values": [["2026-01-01T00:10:00Z", 3.0]]}
        req = self._fake_request(series, single_row)
        esql = ("TS metrics-* | WHERE m IS NOT NULL "
                "| STATS m = MAX(LAST_OVER_TIME(m)) BY time_bucket = TBUCKET(5 minute) "
                "| SORT time_bucket ASC "
                "| STATS time_bucket = MAX(time_bucket), m = MAX(m) "
                "| KEEP time_bucket, m | SORT time_bucket ASC")
        result = po.compare_panel(req, source_query="m", translated_query=esql,
                                  index="metrics-*", step=300, start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.verdict(), "SKIP")
        self.assertIn("single", result.skipped_reason.lower())

    def test_compare_panel_translated_error_is_error(self):
        # native side returns data; translated (ES|QL) side returns an ES error body
        series = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"}],
                  "values": [[1.0, "2026-01-01T00:00:00Z"]]}
        req = self._fake_request(series, {"error": {"type": "parsing_exception", "reason": "bad ES|QL"}})
        result = po.compare_panel(req, source_query="go_goroutines", translated_query="TS metrics-* | BROKEN",
                                  index="metrics-*", step=300, start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.verdict(), "ERROR")
        self.assertIn("bad ES|QL", result.translated_error)


class PromqlPassthroughOracleTests(unittest.TestCase):
    """A translated query that is itself a native ``PROMQL ...`` command (the
    'native passthrough' degrade path) emits native-shaped ``step``/``value``/
    ``_timeseries`` columns, NOT ES|QL ``time_bucket``. ``normalize_translated``
    can't parse those (no time_bucket -> 0 series), turning every passthrough
    panel into a false ``cmp=0`` FAIL. ``compare_panel`` must normalize a
    passthrough translated query with the native parser instead."""

    def _passthrough_request(self, native_data, translated_data):
        """Native call has no ``params``; translated (run_translated) carries
        ``params=[{_tstart},{_tend}]`` -- route on that so a PROMQL-passthrough
        *translated* query is still served the translated payload."""
        def request(method, path, body=None, content_type="application/json"):
            is_translated = isinstance(body, dict) and "params" in body
            return translated_data if is_translated else native_data
        return request

    def test_passthrough_translated_is_parsed_with_native_normalizer(self):
        stamps = [f"2026-01-01T00:{m:02d}:00Z" for m in (0, 5, 10, 15, 20)]
        vals = [10.0, 20.0, 30.0, 40.0, 50.0]
        # Native side: one series via _timeseries label blob.
        native = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"},
                              {"name": "_timeseries", "type": "keyword"}],
                  "values": [[v, t, json.dumps({"labels": {"view": "default"}})] for t, v in zip(stamps, vals)]}
        # Translated side is a PROMQL passthrough: identical native-shaped columns.
        translated = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"},
                                  {"name": "_timeseries", "type": "keyword"}],
                      "values": [[v, t, json.dumps({"labels": {"view": "default"}})] for t, v in zip(stamps, vals)]}
        req = self._passthrough_request(native, translated)
        esql = ("PROMQL index=metrics-* step=1m value=(rate(bind_responses_total{instance=\"1:1\"}[5m]))\n"
                "| GROK _timeseries \"\\\"view\\\":\\\"%{DATA:view}\\\"\"\n"
                "| KEEP step, value, view")
        result = po.compare_panel(req, source_query="rate(bind_responses_total[5m])",
                                  translated_query=esql, index="metrics-*", step=300,
                                  start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        # Before the fix: translated parsed by normalize_translated -> 0 series -> cmp=0 FAIL.
        self.assertGreater(result.translated_series, 0,
                           "passthrough translated query must yield series via the native parser")
        self.assertGreaterEqual(result.compared_points, 1)
        self.assertEqual(result.verdict(), "STRICT_PASS")

    def test_non_passthrough_still_uses_translated_normalizer(self):
        # Guard: a normal TS/ES|QL translated query must STILL be parsed by
        # normalize_translated (time_bucket column), unchanged by the fix.
        stamps = [f"2026-01-01T00:{m:02d}:00Z" for m in (0, 5, 10, 15, 20)]
        vals = [10.0, 20.0, 30.0, 40.0, 50.0]
        native = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"}, {"name": "host", "type": "keyword"}],
                  "values": [[v, t, "a"] for t, v in zip(stamps, vals)]}
        translated = {"columns": [{"name": "computed_value", "type": "double"}, {"name": "time_bucket", "type": "date"}, {"name": "host", "type": "keyword"}],
                      "values": [[v, t, "a"] for t, v in zip(stamps, vals)]}
        req = self._passthrough_request(native, translated)
        result = po.compare_panel(req, source_query="go_goroutines",
                                  translated_query="TS metrics-* | STATS computed_value = AVG(x) BY time_bucket = TBUCKET(5m), host",
                                  index="metrics-*", step=300, start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.verdict(), "STRICT_PASS")
        self.assertGreaterEqual(result.compared_points, 1)
