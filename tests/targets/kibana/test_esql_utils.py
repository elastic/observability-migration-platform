# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for shared ES|QL shape parsing helpers."""

from observability_migration.targets.kibana.emit.esql_utils import extract_esql_shape


def test_extract_esql_shape_uses_final_stats_shape():
    query = (
        "FROM metrics-* "
        "| STATS query1 = AVG(celery_flower_worker_online) "
        "BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), worker "
        "| EVAL online = query1 "
        "| STATS online = AVG(online) BY worker "
        "| KEEP worker, online "
        "| SORT online DESC "
        "| LIMIT 500"
    )

    shape = extract_esql_shape(query)

    assert shape.metric_fields == ["online"]
    assert shape.group_fields == ["worker"]
    assert shape.projected_fields == ["worker", "online"]


def test_extract_esql_shape_ignores_by_inside_quoted_strings():
    query = (
        "FROM metrics-* "
        "| STATS value = AVG(system_cpu_user) "
        "BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), host.name "
        '| EVAL note = "sent by host" '
        "| KEEP time_bucket, host.name, value"
    )

    shape = extract_esql_shape(query)

    assert shape.metric_fields == ["value"]
    assert shape.group_fields == ["time_bucket", "host.name"]
    assert shape.time_fields == ["time_bucket"]
    assert shape.projected_fields == ["time_bucket", "host.name", "value"]


def test_extract_esql_shape_reclassifies_metric_after_drop():
    query = (
        "FROM metrics-* "
        "| STATS value = AVG(system_cpu_user) "
        "BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), host.name "
        "| KEEP time_bucket, host.name, value "
        "| EVAL ratio = value / 100 "
        "| DROP value"
    )

    shape = extract_esql_shape(query)

    assert shape.metric_fields == ["ratio"]
    assert shape.group_fields == ["time_bucket", "host.name"]
    assert shape.time_fields == ["time_bucket"]
    assert shape.projected_fields == ["time_bucket", "host.name", "ratio"]


def test_extract_esql_shape_drop_removes_group_field():
    query = "FROM metrics-* | STATS value = AVG(system_cpu_user) BY host.name | KEEP host.name, value | DROP host.name"

    shape = extract_esql_shape(query)

    assert shape.metric_fields == ["value"]
    assert shape.group_fields == []
    assert shape.projected_fields == ["value"]


def test_extract_esql_shape_keep_includes_eval_aliases_alongside_stats():
    """STATS intermediates + EVAL derived column + KEEP of both.

    Curated MySQL CPU / Memory / Query Cache overrides emit this shape.
    Lens Y accessors must follow KEEP, not only the surviving STATS names.
    """
    query = (
        "TS metrics-* "
        "| STATS Load = MAX(LAST_OVER_TIME(metrics.node_load1)), "
        "non_idle = SUM(RATE(metrics.node_cpu_seconds_total)), "
        "cpu_cores = COUNT_DISTINCT(labels.cpu) "
        "BY time_bucket = TBUCKET(20, ?_tstart, ?_tend) "
        "| EVAL CPU_busy_pct = CASE(cpu_cores > 0, ((non_idle * 100) / cpu_cores), NULL) "
        "| KEEP time_bucket, CPU_busy_pct, Load "
        "| SORT time_bucket ASC"
    )

    shape = extract_esql_shape(query)

    assert shape.metric_fields == ["CPU_busy_pct", "Load"]
    assert shape.time_fields == ["time_bucket"]
    assert "non_idle" not in shape.metric_fields
    assert "cpu_cores" not in shape.metric_fields


def test_extract_esql_shape_eval_is_a_metric_when_stats_intermediates_are_dropped():
    query = (
        "TS metrics-* "
        "| STATS lhs = AVG(metrics.page_size), rhs = AVG(metrics.pages), "
        "log_buffer = AVG(metrics.log_buffer) "
        "BY time_bucket = TBUCKET(20, ?_tstart, ?_tend) "
        "| EVAL pool_data = lhs * rhs "
        "| DROP lhs, rhs"
    )

    shape = extract_esql_shape(query)

    assert shape.metric_fields == ["log_buffer", "pool_data"]
    assert shape.time_fields == ["time_bucket"]

