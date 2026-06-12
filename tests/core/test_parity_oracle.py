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

    def test_decode_timeseries_labels_flattens_nested_objects(self):
        # ``_timeseries`` can carry nested label objects (OTel resource attrs).
        # They must flatten to dotted paths, not stringified Python dicts, so
        # keys can align with the translator's flattened label fallback.
        out = po._decode_timeseries_labels(json.dumps({
            "__name__": "m",
            "job": "j",
            "k8s": {"cluster": {"name": "c1"}},
            "state": "busy",
        }))
        self.assertEqual(out, {"k8s.cluster.name": "c1", "state": "busy"})

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

    def test_flattened_label_blob_series_align_with_native(self):
        # Real corpus shape (Node Exporter Full "ARP Entries"): the panel groups
        # by a label (``device``) absent from the data, so the translated
        # passthrough's label-extraction fallback flattens the whole
        # ``_timeseries`` JSON into one opaque string per series
        # ('__name__:m, instance:host:9100, k8s:cluster:name:c1'). The
        # comparator must decode that blob back into label pairs - resolving
        # the colon ambiguity (instance values contain ':') against the label
        # names observed on the native side - so per-series keys align instead
        # of producing a false compared_points=0 FAIL.
        stamps = [f"2026-01-01T00:{m:02d}:00Z" for m in (0, 5, 10, 15, 20)]
        series_vals = {"c1": [10.0, 20.0, 30.0, 40.0, 50.0],
                       "c2": [100.0, 200.0, 300.0, 400.0, 500.0]}

        def native_ts(cluster, host):
            return json.dumps({"__name__": "node_arp_entries", "instance": f"{host}:9100",
                               "job": "node", "k8s": {"cluster": {"name": cluster}}})

        def blob(cluster, host):
            return (f"__name__:node_arp_entries, instance:{host}:9100, "
                    f"job:node, k8s:cluster:name:{cluster}")

        native_rows = []
        translated_rows = []
        for cluster, host in (("c1", "hosta"), ("c2", "hostb")):
            for t, v in zip(stamps, series_vals[cluster]):
                native_rows.append([v, t, native_ts(cluster, host)])
                translated_rows.append([t, v, blob(cluster, host)])
        native = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"},
                              {"name": "_timeseries", "type": "keyword"}],
                  "values": native_rows}
        translated = {"columns": [{"name": "step", "type": "date"}, {"name": "value", "type": "double"},
                                  {"name": "device", "type": "keyword"}],
                      "values": translated_rows}
        esql = ('PROMQL index=metrics-* step=5m value=(node_arp_entries)\n'
                '| EVAL _ts = COALESCE(_timeseries, "")\n'
                '| KEEP step, value, device')

        # The translated query is itself a PROMQL passthrough, so the shared
        # _fake_request (which routes on the PROMQL prefix) cannot tell the two
        # sides apart; route on the exact translated text instead.
        def req(method, path, body=None, content_type="application/json"):
            q = body.get("query", "") if isinstance(body, dict) else ""
            return translated if q == esql else native
        result = po.compare_panel(req, source_query="node_arp_entries",
                                  translated_query=esql,
                                  index="metrics-*", step=300,
                                  start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.common_series, 2)
        self.assertEqual(result.verdict(), "STRICT_PASS")
        self.assertEqual(result.max_relative_error, 0.0)
        self.assertTrue(any("label fallback blob" in n for n in result.notes),
                        f"expected a blob-decode note, got: {result.notes}")

    def test_normalize_translated_selects_named_value_column(self):
        # A merged multi-target response carries one numeric column per
        # target. Selecting a specific value column must use exactly that
        # column and treat the sibling value columns as ignorable, not as
        # series labels.
        data = {
            "columns": [{"name": "time_bucket", "type": "date"},
                        {"name": "cpu_usage", "type": "double"},
                        {"name": "memory_usage", "type": "double"},
                        {"name": "host", "type": "keyword"}],
            "values": [["2026-01-01T00:00:00Z", 1.0, 9.0, "a"],
                       ["2026-01-01T00:05:00Z", 2.0, 8.0, "a"],
                       ["2026-01-01T00:00:00Z", 3.0, 7.0, "b"],
                       ["2026-01-01T00:05:00Z", 4.0, 6.0, "b"]],
        }
        out = po.normalize_translated(
            data, value_column="memory_usage", ignore_columns=frozenset({"cpu_usage"}))
        self.assertEqual(len(out), 2)
        by_host = {dict(key.labels).get("host"): [v for _, v in points]
                   for key, points in out.items()}
        self.assertEqual(by_host, {"a": [9.0, 8.0], "b": [7.0, 6.0]})

    def test_translated_grouped_series_project_onto_global_native_sum(self):
        # Source ``sum(metric{cluster="$cluster"})`` collapses to ONE global
        # series once the oracle strips the variable matcher, while the
        # translated panel keeps per-cluster grouping for its Kibana control.
        # The translated side must be re-aggregated onto the native (empty)
        # label subset - sum of the partition sums IS the global sum - instead
        # of failing with "series keys did not align (native 1, translated 3)".
        stamps = [f"2026-01-01T00:{m:02d}:00Z" for m in (0, 5, 10, 15, 20)]
        per_cluster = {"c1": 10.0, "c2": 20.0, "c3": 30.0}
        native = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"}],
                  "values": [[sum(per_cluster.values()) + i, t] for i, t in enumerate(stamps)]}
        trows = []
        for cluster, base in per_cluster.items():
            for i, t in enumerate(stamps):
                trows.append([t, base + i / 3.0, cluster])
        translated = {"columns": [{"name": "time_bucket", "type": "date"},
                                  {"name": "computed_value", "type": "double"},
                                  {"name": "cluster_name", "type": "keyword"}],
                      "values": trows}
        req = self._fake_request(native, translated)
        result = po.compare_panel(
            req, source_query='sum(kube_service_info{cluster="$cluster"})',
            translated_query=("FROM metrics-* | STATS computed_value = SUM(kube_service_info) "
                              "BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), cluster_name"),
            index="metrics-*", step=300,
            start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.common_series, 1)
        self.assertEqual(result.verdict(), "STRICT_PASS")
        self.assertTrue(any("re-aggregated" in n and "translated" in n for n in result.notes),
                        f"expected a translated-side projection note, got: {result.notes}")

    def test_native_empty_translated_nonempty_is_skip(self):
        # The native oracle ran without error but returned no series (e.g.
        # instant-vector arithmetic that matches nothing on this data set).
        # An oracle with no reference data cannot prove the translation
        # wrong - SKIP with the asymmetry spelled out, not FAIL.
        stamps = [f"2026-01-01T00:{m:02d}:00Z" for m in (0, 5, 10)]
        empty = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"}],
                 "values": []}
        translated = {"columns": [{"name": "time_bucket", "type": "date"},
                                  {"name": "computed_value", "type": "double"}],
                      "values": [[t, 1.0] for t in stamps]}
        req = self._fake_request(empty, translated)
        result = po.compare_panel(
            req, source_query="a_bytes - b_bytes",
            translated_query="FROM metrics-* | STATS computed_value = AVG(x) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)",
            index="metrics-*", step=300,
            start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.verdict(), "SKIP")
        self.assertIn("native oracle returned no series", result.skipped_reason)

    def test_compare_panel_per_target_value_column(self):
        # Per-target comparison of a merged multi-target panel: target B's
        # sub-query is verified against its own output column while target
        # A's column is ignored.
        stamps = [f"2026-01-01T00:{m:02d}:00Z" for m in (0, 5, 10, 15, 20)]
        b_vals = [10.0, 20.0, 30.0, 40.0, 50.0]
        native = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"}],
                  "values": [[v, t] for t, v in zip(stamps, b_vals)]}
        translated = {"columns": [{"name": "time_bucket", "type": "date"},
                                  {"name": "cpu_usage", "type": "double"},
                                  {"name": "memory_usage", "type": "double"}],
                      "values": [[t, 999.0, v] for t, v in zip(stamps, b_vals)]}
        req = self._fake_request(native, translated)
        result = po.compare_panel(
            req, source_query="memory_usage",
            translated_query=("FROM metrics-* | STATS cpu_usage = AVG(cpu_usage), "
                              "memory_usage = AVG(memory_usage) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)"),
            index="metrics-*", step=300,
            start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z",
            translated_value_column="memory_usage",
            translated_ignore_columns=frozenset({"cpu_usage"}))
        self.assertEqual(result.verdict(), "STRICT_PASS")
        self.assertEqual(result.max_relative_error, 0.0)

    def test_terminal_scalar_reduction_parses_supported_shapes(self):
        self.assertEqual(
            po._terminal_scalar_reduction(
                "TS m-* | STATS m = AVG(x) BY time_bucket = TBUCKET(5 minute) "
                "| STATS time_bucket = MAX(time_bucket), m = MAX(m)"),
            {"time_bucket": "max", "m": "max"},
        )
        self.assertEqual(
            po._terminal_scalar_reduction(
                "TS m-* | STATS v = AVG(x) BY time_bucket = TBUCKET(5 minute) "
                "| STATS v = LAST(v, time_bucket)"),
            {"v": "last"},
        )
        # Grouped tables, counts, and arithmetic are NOT scalar-comparable.
        self.assertIsNone(po._terminal_scalar_reduction(
            "TS m-* | STATS m = MAX(m) BY handler"))
        self.assertIsNone(po._terminal_scalar_reduction(
            "FROM m-* | STATS up_count = COUNT(up)"))
        self.assertIsNone(po._terminal_scalar_reduction(
            "FROM m-* | STATS start_time_ms = MAX(node_boot_time_seconds * 1000)"))

    def test_stat_panel_single_value_comparison_passes(self):
        # The dominant stat-panel shape reduces the bucketed series to its
        # window MAX. The oracle can compare scalars: reduce the native series
        # the same way instead of SKIPping ("no time series to compare").
        stamps = [f"2026-01-01T00:{m:02d}:00Z" for m in (0, 5, 10, 15, 20)]
        vals = [10.0, 20.0, 50.0, 40.0, 30.0]
        native = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"}],
                  "values": [[v, t] for t, v in zip(stamps, vals)]}
        translated = {"columns": [{"name": "time_bucket", "type": "date"},
                                  {"name": "m", "type": "double"}],
                      "values": [["2026-01-01T00:20:00Z", 50.0]]}
        req = self._fake_request(native, translated)
        result = po.compare_panel(
            req, source_query="m_metric",
            translated_query=("TS metrics-* | WHERE m IS NOT NULL "
                              "| STATS m = MAX(LAST_OVER_TIME(m)) BY time_bucket = TBUCKET(5 minute) "
                              "| SORT time_bucket ASC "
                              "| STATS time_bucket = MAX(time_bucket), m = MAX(m)"),
            index="metrics-*", step=300,
            start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.verdict(), "STRICT_PASS")
        self.assertEqual(result.compared_points, 1)
        self.assertEqual(result.max_relative_error, 0.0)
        self.assertTrue(any("single-value" in n for n in result.notes),
                        f"expected a single-value note, got: {result.notes}")

    def test_stat_panel_single_value_mismatch_reports_error(self):
        stamps = [f"2026-01-01T00:{m:02d}:00Z" for m in (0, 5, 10)]
        native = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"}],
                  "values": [[v, t] for t, v in zip(stamps, (10.0, 20.0, 30.0))]}
        translated = {"columns": [{"name": "time_bucket", "type": "date"},
                                  {"name": "m", "type": "double"}],
                      "values": [["2026-01-01T00:10:00Z", 90.0]]}
        req = self._fake_request(native, translated)
        result = po.compare_panel(
            req, source_query="m_metric",
            translated_query=("TS metrics-* | STATS m = AVG(m) BY time_bucket = TBUCKET(5 minute) "
                              "| STATS time_bucket = MAX(time_bucket), m = MAX(m)"),
            index="metrics-*", step=300,
            start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.compared_points, 1)
        self.assertGreater(result.max_relative_error, 0.5)
        self.assertEqual(result.verdict(), "SHAPE_PASS")

    def test_unsupported_stat_shape_still_skips(self):
        # COUNT-style reductions cannot be mirrored faithfully; the honest
        # SKIP must survive.
        native = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"}],
                  "values": [[1.0, "2026-01-01T00:00:00Z"]]}
        translated = {"columns": [{"name": "up_count", "type": "long"}], "values": [[3]]}
        req = self._fake_request(native, translated)
        result = po.compare_panel(
            req, source_query="count(up)",
            translated_query="FROM metrics-* | STATS up_count = COUNT(up)",
            index="metrics-*", step=300,
            start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.verdict(), "SKIP")
        self.assertIn("single value", result.skipped_reason)

    def test_compare_panel_translated_label_filter_scopes_one_target(self):
        # Same-metric collapse: targets map to VALUES of one BY column.
        # Verifying target A means comparing its sub-query against only the
        # translated rows whose key carries state="active" (the label is then
        # dropped from the key so it can align with the native side).
        stamps = [f"2026-01-01T00:{m:02d}:00Z" for m in (0, 5, 10, 15, 20)]
        active = [10.0, 20.0, 30.0, 40.0, 50.0]
        failed = [7.0, 6.0, 5.0, 4.0, 3.0]
        native = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"}],
                  "values": [[v, t] for t, v in zip(stamps, active)]}
        trows = []
        for state, vals in (("active", active), ("failed", failed)):
            for t, v in zip(stamps, vals):
                trows.append([t, v, state])
        translated = {"columns": [{"name": "time_bucket", "type": "date"},
                                  {"name": "computed_value", "type": "double"},
                                  {"name": "state", "type": "keyword"}],
                      "values": trows}
        req = self._fake_request(native, translated)
        result = po.compare_panel(
            req, source_query='node_systemd_units{state="active"}',
            translated_query=("TS metrics-* | STATS computed_value = AVG(node_systemd_units) "
                              "BY time_bucket = TBUCKET(5 minute), state"),
            index="metrics-*", step=300,
            start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z",
            translated_label_filter=("state", "active"))
        self.assertEqual(result.common_series, 1)
        self.assertEqual(result.verdict(), "STRICT_PASS")
        self.assertEqual(result.max_relative_error, 0.0)

    def test_multi_query_panel_is_skip_with_clear_reason(self):
        # Multi-target panels join their PromQL sub-queries with " ||| "
        # (panels.py) and translate to ONE merged ES|QL; sub-queries can be
        # reordered or dropped, so there is no per-target mapping the oracle
        # could compare against. Feeding the joined text to native PROMQL
        # produces a cryptic parse error; the verdict must be a SKIP that
        # says exactly why the panel is not verifiable.
        req = self._fake_request({}, {})
        result = po.compare_panel(
            req,
            source_query='sum(rate(a_total[5m])) ||| sum(rate(b_total[5m])) ||| up',
            translated_query="FROM metrics-* | STATS q0 = SUM(a_total), q1 = SUM(b_total) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)",
            index="metrics-*", step=300,
            start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.verdict(), "SKIP")
        self.assertIn("multi-query panel (3 sub-queries", result.skipped_reason)
        # No queries should have been sent to the cluster at all.
        self.assertEqual(req.calls, [])

    def test_both_sides_empty_is_skip_with_reason(self):
        # Neither the oracle nor the translated query returned any data:
        # nothing was compared, so FAIL (which implies a translation defect)
        # would be dishonest. SKIP and say why.
        empty = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"}],
                 "values": []}
        req = self._fake_request(empty, {"columns": [], "values": []})
        result = po.compare_panel(req, source_query="up",
                                  translated_query="TS metrics-* | STATS v = AVG(up) BY time_bucket = TBUCKET(5m)",
                                  index="metrics-*", step=300,
                                  start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.verdict(), "SKIP")
        self.assertIn("no data on either side", result.skipped_reason)

    def test_translated_empty_native_nonempty_fail_carries_reason(self):
        stamps = [f"2026-01-01T00:{m:02d}:00Z" for m in (0, 5, 10)]
        native = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"}],
                  "values": [[v, t] for v, t in zip((1.0, 2.0, 3.0), stamps)]}
        req = self._fake_request(native, {"columns": [], "values": []})
        result = po.compare_panel(req, source_query="m",
                                  translated_query="TS metrics-* | STATS v = AVG(m) BY time_bucket = TBUCKET(5m)",
                                  index="metrics-*", step=300,
                                  start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.verdict(), "FAIL")
        self.assertIn("translated query returned no series", result.fail_reason)

    def test_unalignable_series_keys_fail_carries_reason(self):
        stamps = [f"2026-01-01T00:{m:02d}:00Z" for m in (0, 5, 10)]
        native = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"},
                              {"name": "zone", "type": "keyword"}],
                  "values": [[v, t, z] for z in ("a", "b") for v, t in zip((1.0, 2.0, 3.0), stamps)]}
        translated = {"columns": [{"name": "computed_value", "type": "double"},
                                  {"name": "time_bucket", "type": "date"},
                                  {"name": "shard", "type": "keyword"}],
                      "values": [[v, t, s] for s in ("x", "y") for v, t in zip((9.0, 8.0, 7.0), stamps)]}
        req = self._fake_request(native, translated)
        result = po.compare_panel(req, source_query="m",
                                  translated_query="TS metrics-* | STATS computed_value = AVG(m) BY time_bucket = TBUCKET(5m), shard",
                                  index="metrics-*", step=300,
                                  start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.verdict(), "FAIL")
        self.assertIn("series keys did not align", result.fail_reason)
        self.assertIn("native 2", result.fail_reason)
        self.assertIn("translated 2", result.fail_reason)

    def test_row_constant_translation_is_skip(self):
        # A constant PromQL panel ("2") translates to ``ROW constant_value = 2.0``:
        # a single row with no time series. Point-wise comparison is meaningless;
        # this must SKIP like other single-value reductions, not FAIL.
        native = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"}],
                  "values": [[2.0, "2026-01-01T00:00:00Z"], [2.0, "2026-01-01T00:05:00Z"]]}
        translated = {"columns": [{"name": "constant_value", "type": "double"}], "values": [[2.0]]}
        req = self._fake_request(native, translated)
        result = po.compare_panel(req, source_query="2",
                                  translated_query="ROW constant_value = 2.0",
                                  index="metrics-*", step=300,
                                  start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.verdict(), "SKIP")
        self.assertIn("single value", result.skipped_reason)

    def test_label_names_align_by_value_sets_when_keys_disagree(self):
        # Real corpus shape (Node Exporter Full "Systemd Sockets"): the
        # translated legend extraction names its column after the panel label
        # (``name``) but the regex grabbed values that natively live under
        # ``k8s.cluster.name``. Same series, same values, different label
        # name -> 0 common keys -> false FAIL. When every translated label
        # value set matches exactly one native label's value set, rename and
        # compare per-series.
        stamps = [f"2026-01-01T00:{m:02d}:00Z" for m in (0, 5, 10, 15, 20)]
        series_vals = {"c1": [10.0, 20.0, 30.0, 40.0, 50.0],
                       "c2": [100.0, 200.0, 300.0, 400.0, 500.0]}

        def native_ts(cluster):
            return json.dumps({"__name__": "m", "job": "node",
                               "k8s": {"cluster": {"name": cluster}}})

        native_rows = []
        translated_rows = []
        for cluster in ("c1", "c2"):
            for t, v in zip(stamps, series_vals[cluster]):
                native_rows.append([v, t, native_ts(cluster)])
                translated_rows.append([t, v, cluster])
        native = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"},
                              {"name": "_timeseries", "type": "keyword"}],
                  "values": native_rows}
        translated = {"columns": [{"name": "step", "type": "date"}, {"name": "value", "type": "double"},
                                  {"name": "name", "type": "keyword"}],
                      "values": translated_rows}
        esql = ('PROMQL index=metrics-* step=5m value=(m)\n'
                '| EVAL _ts = COALESCE(_timeseries, "")\n'
                '| KEEP step, value, name')

        def req(method, path, body=None, content_type="application/json"):
            q = body.get("query", "") if isinstance(body, dict) else ""
            return translated if q == esql else native

        result = po.compare_panel(req, source_query="m", translated_query=esql,
                                  index="metrics-*", step=300,
                                  start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.common_series, 2)
        self.assertEqual(result.verdict(), "STRICT_PASS")
        self.assertTrue(any("label name" in n for n in result.notes),
                        f"expected a label-name alignment note, got: {result.notes}")

    def test_flattened_blob_without_name_prefix_aligns(self):
        # Real corpus shape (Node Exporter Full "File Nodes Free"): the
        # _timeseries JSON for these series does not start with __name__
        # (e.g. '{"device":"sda","instance":...}'), so the flatten fallback
        # blob is 'device:sda, instance:..., job:...'. Blob detection must not
        # depend on a __name__ prefix; any all-pairs blob whose anchors match
        # native label names decodes.
        stamps = [f"2026-01-01T00:{m:02d}:00Z" for m in (0, 5, 10, 15, 20)]
        series_vals = {"sda": [10.0, 20.0, 30.0, 40.0, 50.0],
                       "sdb": [100.0, 200.0, 300.0, 400.0, 500.0]}

        def native_ts(device):
            return json.dumps({"device": device, "instance": "host:9100", "job": "node"})

        def blob(device):
            return f"device:{device}, instance:host:9100, job:node"

        native_rows = []
        translated_rows = []
        for device in ("sda", "sdb"):
            for t, v in zip(stamps, series_vals[device]):
                native_rows.append([v, t, native_ts(device)])
                translated_rows.append([t, v, blob(device)])
        native = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"},
                              {"name": "_timeseries", "type": "keyword"}],
                  "values": native_rows}
        translated = {"columns": [{"name": "step", "type": "date"}, {"name": "value", "type": "double"},
                                  {"name": "mountpoint", "type": "keyword"}],
                      "values": translated_rows}
        esql = ('PROMQL index=metrics-* step=5m value=(node_filesystem_files_free)\n'
                '| EVAL _ts = COALESCE(_timeseries, "")\n'
                '| KEEP step, value, mountpoint')

        def req(method, path, body=None, content_type="application/json"):
            q = body.get("query", "") if isinstance(body, dict) else ""
            return translated if q == esql else native

        result = po.compare_panel(req, source_query="node_filesystem_files_free",
                                  translated_query=esql,
                                  index="metrics-*", step=300,
                                  start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.common_series, 2)
        self.assertEqual(result.verdict(), "STRICT_PASS")

    def test_translated_label_matching_scrubbed_native_label_aligns(self):
        # Real corpus shape (Node Exporter Full "Systemd Sockets"): the legend
        # extraction regex (.*"name":"(...)".*) greedily matches the LAST
        # "name": occurrence in the _timeseries JSON - service.name - so the
        # translated label carries values of a native label the comparator
        # normally scrubs (service.name -> job). The values still map 1:1 to
        # series, so the comparator must key BOTH sides by that pre-scrub
        # label and compare per-series instead of emitting a false
        # "series keys did not align" FAIL.
        stamps = [f"2026-01-01T00:{m:02d}:00Z" for m in (0, 5, 10, 15, 20)]
        series_vals = {"backend": [10.0, 20.0, 30.0, 40.0, 50.0],
                       "frontend": [100.0, 200.0, 300.0, 400.0, 500.0]}
        clusters = {"backend": "c1", "frontend": "c2"}

        def native_ts(svc):
            return json.dumps({"__name__": "m", "job": "node",
                               "k8s": {"cluster": {"name": clusters[svc]}},
                               "service": {"name": svc}})

        native_rows = []
        translated_rows = []
        for svc in ("backend", "frontend"):
            for t, v in zip(stamps, series_vals[svc]):
                native_rows.append([v, t, native_ts(svc)])
                translated_rows.append([t, v, svc])
        native = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"},
                              {"name": "_timeseries", "type": "keyword"}],
                  "values": native_rows}
        translated = {"columns": [{"name": "step", "type": "date"}, {"name": "value", "type": "double"},
                                  {"name": "name", "type": "keyword"}],
                      "values": translated_rows}
        esql = ('PROMQL index=metrics-* step=5m value=(m)\n'
                '| EVAL _ts = COALESCE(_timeseries, "")\n'
                '| KEEP step, value, name')

        def req(method, path, body=None, content_type="application/json"):
            q = body.get("query", "") if isinstance(body, dict) else ""
            return translated if q == esql else native

        result = po.compare_panel(req, source_query="m", translated_query=esql,
                                  index="metrics-*", step=300,
                                  start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.common_series, 2)
        self.assertEqual(result.verdict(), "STRICT_PASS")
        self.assertTrue(any("scrub" in n for n in result.notes),
                        f"expected a scrubbed-label alignment note, got: {result.notes}")

    def test_static_legend_collapse_on_multi_series_data_is_skip(self):
        # Real corpus shape (Node Exporter Full "Memory Bounce"): the panel has
        # a static legend, so the translated query emits a constant label and
        # the response collapses every underlying series into one interleaved
        # stream. On multi-series seeded data (template variables resolved to
        # match-all) per-series comparison is impossible; reporting a 0.889
        # "max relative error" against a sum-projection is a comparator
        # artifact, not a measured translation error -> SKIP and say why.
        stamps = [f"2026-01-01T00:{m:02d}:00Z" for m in (0, 5, 10, 15, 20)]

        def native_ts(host):
            return json.dumps({"__name__": "node_memory_Bounce_bytes", "instance": host,
                               "k8s": {"cluster": {"name": host}}})

        native_rows = []
        translated_rows = []
        for host, base in (("a", 10.0), ("b", 100.0), ("c", 1000.0)):
            for i, t in enumerate(stamps):
                native_rows.append([base + i, t, native_ts(host)])
                translated_rows.append([t, base + i, "Bounce - Memory used"])
        native = {"columns": [{"name": "value", "type": "double"}, {"name": "step", "type": "date"},
                              {"name": "_timeseries", "type": "keyword"}],
                  "values": native_rows}
        translated = {"columns": [{"name": "step", "type": "date"}, {"name": "value", "type": "double"},
                                  {"name": "label", "type": "keyword"}],
                      "values": translated_rows}
        esql = ('PROMQL index=metrics-* step=5m value=(node_memory_Bounce_bytes)\n'
                '| EVAL label = "Bounce - Memory used"\n'
                '| KEEP step, value, label')

        def req(method, path, body=None, content_type="application/json"):
            q = body.get("query", "") if isinstance(body, dict) else ""
            return translated if q == esql else native

        result = po.compare_panel(req, source_query="node_memory_Bounce_bytes",
                                  translated_query=esql,
                                  index="metrics-*", step=300,
                                  start_iso="2026-01-01T00:00:00Z", end_iso="2026-01-01T00:30:00Z")
        self.assertEqual(result.verdict(), "SKIP")
        self.assertIn("static legend", result.skipped_reason)

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

    def test_single_value_reduction_with_mirrorable_reducer_is_compared(self):
        # A Grafana stat/single-value panel collapses the per-bucket series to
        # ONE row (final ``STATS time_bucket = MAX(time_bucket), m = MAX(m)``).
        # Point-wise comparison is meaningless, but the window-MAX reduction
        # can be mirrored on the native side, so the scalar IS verified
        # (previously a blanket SKIP; unsupported shapes still SKIP - see
        # test_unsupported_stat_shape_still_skips).
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
        self.assertEqual(result.verdict(), "STRICT_PASS")
        self.assertEqual(result.compared_points, 1)

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
