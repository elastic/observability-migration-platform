# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from observability_migration.adapters.source.grafana.panels import (
    _strip_scalar_last_time_bucket_keep,
)


def test_strip_scalar_last_time_bucket_keep_removes_stale_time_bucket():
    query = (
        "TS metrics-*\n"
        "| STATS computed_value = LAST(computed_value, time_bucket)\n"
        "| KEEP time_bucket, computed_value\n"
        "| SORT label ASC"
    )

    normalized = _strip_scalar_last_time_bucket_keep(query)

    assert "| KEEP computed_value" in normalized
    assert "| KEEP time_bucket, computed_value" not in normalized
