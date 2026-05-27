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
