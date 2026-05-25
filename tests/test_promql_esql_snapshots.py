# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Snapshot tests for the PromQL → ES|QL translation pipeline.

Each test case translates a canonical PromQL expression and compares the result
(feasibility, warnings, and ES|QL query text) against a stored snapshot file in
``tests/snapshots/promql_to_esql/``.

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
]


def _render_snapshot(feasibility: str, warnings: list[str], query: str | None) -> str:
    """Render a snapshot to a canonical text form."""
    lines = [f"feasibility: {feasibility}"]
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
        actual = _render_snapshot(result.feasibility, result.warnings, result.esql_query)
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
