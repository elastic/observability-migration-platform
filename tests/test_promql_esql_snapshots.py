# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Snapshot tests for the PromQL → ES|QL translation pipeline.

Each test case translates a canonical PromQL expression and compares the result
(source PromQL, feasibility, warnings, and ES|QL query text) against a stored
snapshot file in ``tests/snapshots/promql_to_esql/``. The leading ``source:``
line records the original PromQL so each snapshot documents the full
``from this -> to this`` translation in one place.

Updating snapshots
------------------
Set the environment variable ``UPDATE_SNAPSHOTS=1`` before running pytest to
regenerate all snapshot files from the current output:

    UPDATE_SNAPSHOTS=1 python -m pytest tests/test_promql_esql_snapshots.py -v

Review the diffs with ``git diff tests/snapshots/`` before committing.
"""

from __future__ import annotations

import difflib
import os
import unittest
from pathlib import Path

from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.adapters.source.grafana.translate import translate_promql_to_esql

SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "promql_to_esql"
UPDATE_SNAPSHOTS = os.environ.get("UPDATE_SNAPSHOTS") == "1"
INDEX = "metrics-*"

# ---------------------------------------------------------------------------
# Canonical test cases — one per important translation path.
# Name → (PromQL expression, panel_type)
# ---------------------------------------------------------------------------
CASES: list[tuple[str, str, str]] = [
    # --- range_agg (rate / irate / increase / avg_over_time) ---------------
    (
        "range_agg_rate_sum_by",
        'sum(rate(http_requests_total{job="web"}[5m])) by (job)',
        "timeseries",
    ),
    (
        "range_agg_rate_no_outer_agg",
        'rate(node_cpu_seconds_total{mode="idle"}[5m])',
        "timeseries",
    ),
    (
        "range_agg_avg_over_time",
        'avg(avg_over_time(up{job="prom"}[5m])) by (job)',
        "timeseries",
    ),
    # --- simple_agg / simple_metric ----------------------------------------
    (
        "simple_agg_sum_by",
        "sum(kube_pod_info) by (namespace, pod)",
        "timeseries",
    ),
    (
        "simple_metric_gauge",
        "node_memory_MemAvailable_bytes",
        "timeseries",
    ),
    # --- binary_expr: ratio (same metric, divergent static filters) ---------
    # This is the NGINX success-rate pattern: after macro preprocessing the
    # two operands share variable-driven matchers but differ in status filter.
    (
        "binary_ratio_divergent_static_filter",
        (
            'sum(rate(nginx_ingress_controller_requests{'
            'controller_pod=~"$controller",'
            'status!~"[4-5].*"}[5m])) by (controller)'
            ' / sum(rate(nginx_ingress_controller_requests{'
            'controller_pod=~"$controller"}[5m])) by (controller)'
        ),
        "timeseries",
    ),
    # --- binary_expr: ratio across different metrics (TS ÷ gauge FROM) ------
    (
        "binary_ratio_ts_over_from_not_feasible",
        (
            "sum(rate(jvm_gc_pause_seconds_sum[1m])) by (application)"
            " / on(application) system_cpu_count"
        ),
        "timeseries",
    ),
    # --- binary_expr: sum(A ± B) → linearity rewrite ----------------------
    (
        "binary_sum_linearity_rewrite",
        "sum(node_memory_MemFree_bytes + node_memory_Cached_bytes) by (instance)",
        "timeseries",
    ),
    # --- scalar hoisting: agg(X * k) = agg(X) * k -------------------------
    (
        "scalar_hoist_avg_times_100",
        "avg(avg_over_time(up[5m]) * 100)",
        "timeseries",
    ),
    (
        "scalar_hoist_max_rate_times_8",
        "max(rate(node_network_receive_bytes_total[5m])*8) by (instance)",
        "timeseries",
    ),
    (
        "scalar_hoist_sum_rate_div_1000",
        "sum(rate(http_requests_total[5m]) / 1000) by (job)",
        "timeseries",
    ),
    # --- outer agg over vector-matching join (unknown + vector_matching) ----
    (
        "outer_agg_over_join_strips_rhs",
        (
            "max(rate(node_network_receive_bytes_total[5m])"
            " * on(instance) group_left(nodename) node_uname_info) by (instance)"
        ),
        "timeseries",
    ),
    # --- bare join (family='join', no outer agg) ---------------------------
    (
        "bare_join_strips_rhs",
        "node_hwmon_temp_celsius * on(chip) group_left(chip_name) node_hwmon_chip_names",
        "timeseries",
    ),
    # --- or fallback: A or vector(0) ---------------------------------------
    (
        "or_vector0_fallback",
        "up{job='prom'} or vector(0)",
        "timeseries",
    ),
    # --- uptime: time() - boot_time ----------------------------------------
    (
        "uptime_expression",
        "time() - node_boot_time_seconds{job='node'}",
        "timeseries",
    ),
    # --- two-vector multiplication: correctly not_feasible ------------------
    (
        "two_series_ratio_not_feasible",
        "max(node_filesystem_avail_bytes / node_filesystem_size_bytes)",
        "timeseries",
    ),
    # --- stat / singlestat panel (summary mode) ----------------------------
    (
        "stat_panel_rate_sum",
        "sum(rate(http_requests_total[5m]))",
        "stat",
    ),
    # --- unary minus over binary_expr (butterfly-chart pattern) ------------
    (
        "unary_minus_over_binary_expr",
        (
            "-(irate(node_network_transmit_errs_total[5m])"
            " + irate(node_network_transmit_drop_total[5m]))"
        ),
        "timeseries",
    ),
    # --- binary_expr LHS of a group_left join (memory used * label enrichment) --
    (
        "binary_expr_join_lhs",
        (
            "(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes)"
            " * ON(instance) GROUP_LEFT(nodename) node_uname_info"
        ),
        "timeseries",
    ),
    # --- Grafana $var phantom metric stripping (rate * $trends) ---------------
    (
        "phantom_var_rate_times_dollar_trends",
        'rate(node_network_receive_bytes_total{instance=~"$instance"}[5m]) * $trends',
        "timeseries",
    ),
    # --- join + outer agg + scalar division (Podman pattern) -----------------
    # sum(A * group_right B / k / k) strips the join RHS, pushes sum down to A,
    # then hoists the scalar divisions out: sum(A)/k/k.
    (
        "join_agg_scalar_div",
        (
            "sum by(name)(podman_container_info"
            " * on(id) group_right(name) podman_container_memory_bytes"
            " / 1024 / 1024)"
        ),
        "timeseries",
    ),
    # --- correctness fix A: anchored template-var matcher parameterized -------
    # namespace=~"^$Namespace$" is equivalent to an exact full-value regex in
    # PromQL. ES|QL RLIKE already matches the whole value and treats ^/$ as
    # literals, so the anchors must be stripped before parameterization.
    (
        "anchored_variable_matcher_param",
        'kube_pod_status_phase{namespace=~"^$Namespace$",phase="Running"} > 0',
        "timeseries",
    ),
    # --- correctness fix C: PromQL end anchor normalized for ES|QL RLIKE ------
    # status!~".*cam(era)?$" — ES|QL RLIKE treats "$" as a literal dollar, so
    # strip the PromQL anchor instead of preserving it.
    (
        "real_regex_anchor_kept",
        'http_requests_total{service="web",status!~".*cam(era)?$"}',
        "timeseries",
    ),
    # --- correctness fix B: by($Var) preprocessed label dropped from BY -------
    # sum(M) by (namespace, label_Env) — label_Env is a preprocessed $Env
    # template variable and must be silently dropped from the STATS BY clause.
    (
        "group_by_var_dropped",
        "sum(kube_pod_info) by (namespace, label_Env)",
        "timeseries",
    ),
    # --- feasibility expansion: exact 1:1 ES|QL function maps ---------------
    # clamp_max(v, hi) == LEAST(v, hi)
    (
        "clamp_max_least",
        "clamp_max(node_filesystem_avail_bytes, 100)",
        "timeseries",
    ),
    # clamp(v, lo, hi) == GREATEST(LEAST(v, hi), lo)
    (
        "clamp_greatest_least",
        "clamp(node_filesystem_avail_bytes, 0, 100)",
        "timeseries",
    ),
    # sgn(v) == SIGNUM(v)
    (
        "sgn_signum",
        "sgn(node_filesystem_avail_bytes)",
        "timeseries",
    ),
    # quantile(phi, m) by (..) == PERCENTILE(m, phi*100)
    (
        "quantile_by_percentile",
        "quantile(0.95, node_filesystem_avail_bytes) by (job)",
        "timeseries",
    ),
    # --- elementwise math / trig wrappers: exact ES|QL function maps -------
    (
        "math_abs",
        "abs(node_memory_usage)",
        "timeseries",
    ),
    (
        "math_sqrt",
        "sqrt(node_memory_usage)",
        "timeseries",
    ),
    # ln(v) -> natural LOG(v)
    (
        "math_ln_natural_log",
        "ln(node_memory_usage)",
        "timeseries",
    ),
    # log2(v) -> LOG(2, v)
    (
        "math_log2",
        "log2(node_memory_usage)",
        "timeseries",
    ),
    # deg(v) -> v * 180 / PI()
    (
        "math_deg_radians_to_degrees",
        "deg(node_memory_usage)",
        "timeseries",
    ),
    # nested wrappers apply innermost-first: ABS then SQRT
    (
        "math_nested_sqrt_abs",
        "sqrt(abs(node_memory_usage))",
        "timeseries",
    ),
    # --- set operators -------------------------------------------------------
    (
        "or_same_metric_static_filters",
        'http_requests_total{instance="i",status=~"4.."} or http_requests_total{instance="i",status=~"5.."}',
        "timeseries",
    ),
    (
        "or_cross_metric_left_fallback",
        "rate(http_requests_total[5m]) or rate(http_errors_total[5m])",
        "timeseries",
    ),
    (
        "and_operator_not_feasible",
        "http_requests_total and http_other_total",
        "timeseries",
    ),
    # --- explicit hard blockers ---------------------------------------------
    (
        "histogram_quantile_not_feasible",
        "histogram_quantile(0.9, rate(alertmanager_notification_latency_seconds_bucket[5m]))",
        "timeseries",
    ),
    (
        "subquery_not_feasible",
        "max_over_time(rate(foo_total[5m])[1h:])",
        "timeseries",
    ),
    (
        "offset_not_feasible",
        "rate(foo_total[5m] offset 1h)",
        "timeseries",
    ),
    # --- ranking -------------------------------------------------------------
    (
        "topk_rate_limit",
        "topk(5, rate(http_requests_total[5m]))",
        "timeseries",
    ),
    (
        "topk_grouped_sum_rate",
        "topk(10, sum(rate(http_requests_total[5m])) by (handler))",
        "timeseries",
    ),
    (
        "bottomk_grouped_not_feasible",
        "bottomk(3, sum by (job) (rate(foo_total[5m])))",
        "timeseries",
    ),
    # --- real dashboard arithmetic and semantic boundaries -------------------
    (
        "memory_percent_formula",
        "(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100",
        "timeseries",
    ),
    (
        "sum_subtraction_linearity",
        'sum(node_memory_MemTotal_bytes{cluster="$cluster",job="$job"} - node_memory_MemAvailable_bytes{cluster="$cluster",job="$job"})',
        "timeseries",
    ),
    (
        "per_element_ratio_not_feasible",
        'sum(increase(prometheus_tsdb_compaction_duration_sum{instance="$instance"}[30m]) / increase(prometheus_tsdb_compaction_duration_count{instance="$instance"}[30m])) by (instance)',
        "timeseries",
    ),
    # --- schema and label handling ------------------------------------------
    (
        "otel_dotted_labels",
        'sum by (service.name) (rate(http_requests_total{http.response.status_code=~"5..",http.request.method="POST"}[5m]))',
        "timeseries",
    ),
    (
        "recording_rule_colon_metric",
        "sum(node_namespace_pod_container:container_cpu_usage_seconds_total:sum_irate{namespace='default'})",
        "timeseries",
    ),
    (
        "label_replace_copy_label",
        'label_replace(rate(http_requests_total[5m]), "host", "$1", "instance", "(.*)")',
        "timeseries",
    ),
    (
        "label_replace_extract_label",
        'label_replace(rate(http_requests_total[5m]), "short", "$1", "job", "prefix-(.*)")',
        "timeseries",
    ),
    # --- nested aggregations and uptime joins --------------------------------
    (
        "nested_agg_avg_sum_rate",
        'avg(sum by (cpu) (rate(node_cpu_seconds_total{mode!~"idle|iowait|steal"}[5m])))',
        "timeseries",
    ),
    (
        "uptime_join",
        'time() - (alertmanager_build_info{instance=~"$instance"} * on (instance, cluster) group_left process_start_time_seconds{instance=~"$instance"})',
        "timeseries",
    ),
    # --- dashboard arithmetic and scalar wrappers ---------------------------
    (
        "scalar_rate_over_nested_count",
        'sum(irate(node_cpu_seconds_total{instance="$node",job="$job", mode="system"}[5m])) / scalar(count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu)))',
        "stat",
    ),
    (
        "cpu_idle_percent_by_instance",
        '100 - (avg by(instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
        "timeseries",
    ),
    (
        "cross_metric_sum_ratio",
        'sum(kube_pod_container_resource_requests{resource="cpu", cluster="$cluster"}) / sum(machine_cpu_cores{cluster="$cluster"})',
        "timeseries",
    ),
    (
        "disk_used_percent_divergent_filter",
        '100 - ((node_filesystem_avail_bytes{mountpoint!~".*pods.*"} / node_filesystem_size_bytes) * 100)',
        "gauge",
    ),
    (
        "unary_minus_sum_rate_by_device",
        '- sum(rate(node_network_transmit_bytes_total{device!~"(veth|azv|lxc).*"}[5m])) by (device)',
        "timeseries",
    ),
    # --- additional range functions and comparison filters -------------------
    (
        "increase_sum_by_instance",
        "sum(increase(http_requests_total[5m])) by (instance)",
        "timeseries",
    ),
    (
        "irate_scalar_multiply_bits",
        "irate(node_network_receive_bytes_total[5m]) * 8",
        "timeseries",
    ),
    (
        "post_agg_increase_gt_zero",
        "sum(increase(prometheus_rule_evaluation_failures_total[5m])) by (instance) > 0",
        "timeseries",
    ),
    # --- PromQL ``bool`` modifier: numeric 0/1 indicator (not a filter) -------
    # Node Exporter "SWAP Used": the ``> bool 0`` guard reads 1 when swap is
    # configured, 0 otherwise, so the percentage is zeroed (not NaN/garbage)
    # on swap-less hosts. Must render CASE(cond, 1, 0), never the bare metric.
    (
        "bool_indicator_swap_used_percent",
        (
            "((node_memory_SwapTotal_bytes - node_memory_SwapFree_bytes)"
            " / (node_memory_SwapTotal_bytes)) * (node_memory_SwapTotal_bytes > bool 0) * 100"
        ),
        "stat",
    ),
    # A ``bool`` indicator used as a divisor is NULL-guarded so it never
    # divides by zero (PromQL yields no sample when the comparison is false).
    (
        "bool_indicator_divisor_null_guarded",
        "node_memory_SwapFree_bytes / (node_memory_SwapTotal_bytes > bool 0)",
        "timeseries",
    ),
    (
        "histogram_bucket_rate_by_le",
        "sum(rate(http_request_duration_seconds_bucket[5m])) by (le)",
        "timeseries",
    ),
    # --- count/comparison, set operators, joins, and blockers ----------------
    (
        "count_up_equals_one",
        "count(up == 1)",
        "stat",
    ),
    (
        "nested_count_distinct_cpu",
        'count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu))',
        "stat",
    ),
    (
        "unless_operator_not_feasible",
        "rate(http_requests_total[5m]) unless rate(http_errors_total[5m])",
        "timeseries",
    ),
    (
        "join_ratio_group_left_denominator",
        'sum by(instance) (irate(node_cpu_guest_seconds_total{mode="user"}[1m])) / on(instance) group_left sum by(instance)(irate(node_cpu_seconds_total[1m]))',
        "timeseries",
    ),
    (
        "__name__introspection_not_feasible",
        'topk(10, count by (__name__)({__name__=~".+"}))',
        "bargauge",
    ),
    (
        "changes_not_feasible",
        "changes(prometheus_config_last_reload_success_timestamp_seconds[10m])",
        "timeseries",
    ),
    # --- additional range functions over gauges ------------------------------
    (
        "max_over_time_gauge",
        "max_over_time(node_memory_MemAvailable_bytes[1h])",
        "timeseries",
    ),
    (
        "min_over_time_grouped",
        "min by (instance) (min_over_time(up[5m]))",
        "timeseries",
    ),
    (
        "delta_gauge_range",
        "delta(node_filesystem_avail_bytes[1h])",
        "timeseries",
    ),
    (
        "deriv_gauge_range",
        "deriv(node_filesystem_avail_bytes[1h])",
        "timeseries",
    ),
    # --- aggregation operators: STDDEV maps, others must not silently AVG -----
    (
        "stddev_by_job",
        "stddev by (job) (http_request_duration_seconds)",
        "timeseries",
    ),
    (
        "stdvar_aggregation_not_feasible",
        "stdvar(node_cpu_seconds_total) by (instance)",
        "timeseries",
    ),
    (
        "group_aggregation_not_feasible",
        "group(up) by (job)",
        "timeseries",
    ),
    # --- scalar/time arithmetic and rounding ---------------------------------
    (
        "round_rate",
        "round(rate(http_requests_total[5m]))",
        "timeseries",
    ),
    (
        "time_modulo_seconds",
        "time() % 86400",
        "stat",
    ),
    (
        "avg_idle_percent_by_instance",
        'avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100',
        "timeseries",
    ),
    # --- additional hard blockers (degrade gracefully) -----------------------
    (
        "quantile_over_time_not_feasible",
        "quantile_over_time(0.95, node_cpu_seconds_total[10m])",
        "timeseries",
    ),
    (
        "predict_linear_not_feasible",
        "predict_linear(node_filesystem_avail_bytes[1h], 4*3600)",
        "timeseries",
    ),
    (
        "absent_not_feasible",
        'absent(up{job="prometheus"})',
        "stat",
    ),
    (
        "count_values_not_feasible",
        'count_values("version", build_info)',
        "timeseries",
    ),
    (
        "vector_scalar_not_feasible",
        "vector(1)",
        "stat",
    ),
]


def _render_snapshot(
    source: str, feasibility: str, warnings: list[str], query: str | None
) -> str:
    """Render a snapshot to a canonical text form.

    The ``source`` line records the original PromQL expression so each snapshot
    documents the full ``from this -> to this`` translation in one place.
    """
    lines = [f"source: {source}", f"feasibility: {feasibility}"]
    for w in warnings:
        lines.append(f"warning: {w}")
    lines.append("---")
    lines.append(query or "")
    return "\n".join(lines) + "\n"


def _diff(expected: str, actual: str) -> str:
    return "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile="expected",
            tofile="actual",
        )
    )


class TestPromQLESQLSnapshots(unittest.TestCase):
    """Each test method corresponds to one entry in CASES."""

    _rule_pack: RulePackConfig
    _resolver: SchemaResolver

    @classmethod
    def setUpClass(cls):
        cls._rule_pack = RulePackConfig()
        cls._resolver = SchemaResolver(cls._rule_pack)

    def _run_case(self, name: str, expr: str, panel_type: str) -> None:
        result = translate_promql_to_esql(
            expr,
            datasource_index=INDEX,
            panel_type=panel_type,
            rule_pack=self._rule_pack,
            resolver=self._resolver,
        )
        actual = _render_snapshot(
            expr, result.feasibility, result.warnings, result.esql_query
        )
        snapshot_path = SNAPSHOT_DIR / f"{name}.txt"

        if UPDATE_SNAPSHOTS or not snapshot_path.exists():
            snapshot_path.write_text(actual, encoding="utf-8")
            if not UPDATE_SNAPSHOTS:
                self.fail(
                    f"Created new snapshot '{name}'. "
                    "Run again (or with UPDATE_SNAPSHOTS=1) to pass."
                )
            return

        expected = snapshot_path.read_text(encoding="utf-8")
        if actual != expected:
            diff = _diff(expected, actual)
            self.fail(
                f"Snapshot mismatch for '{name}'.\n"
                f"To update: UPDATE_SNAPSHOTS=1 pytest tests/test_promql_esql_snapshots.py\n"
                f"\n{diff}"
            )


# Generate individual test methods dynamically so pytest reports them by name.
def _make_test(name, expr, panel_type):
    def test_method(self):
        self._run_case(name, expr, panel_type)

    test_method.__name__ = f"test_{name}"
    test_method.__doc__ = expr[:80]
    return test_method


for _name, _expr, _ptype in CASES:
    setattr(TestPromQLESQLSnapshots, f"test_{_name}", _make_test(_name, _expr, _ptype))
