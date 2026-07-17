# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Unit tests for live ES|QL validation disposition helpers."""

from __future__ import annotations

from observability_migration.core.verification.disposition import (
    unknown_column_looks_like_alias_bug,
    validation_failure_self_heals,
)


def test_unknown_column_matching_pre_rename_stats_alias_is_not_self_heal():
    query = (
        "| STATS freq_B = MAX(LAST_OVER_TIME(freq)) BY time_bucket = TBUCKET(5 minute)\n"
        "| EVAL CPU = freq\n"
    )
    assert unknown_column_looks_like_alias_bug("freq", query) is True


def test_unknown_metric_not_in_query_still_self_heals():
    query = "| STATS x = AVG(RATE(http_requests_total, 5m)) BY time_bucket = TBUCKET(5 minute)\n"
    assert unknown_column_looks_like_alias_bug("http_requests_total", query) is False


def test_validation_failure_self_heals_false_for_alias_bug_when_query_provided():
    query = (
        "| STATS freq_B = MAX(LAST_OVER_TIME(freq)) BY time_bucket = TBUCKET(5 minute)\n"
        "| EVAL CPU = freq\n"
    )
    assert (
        validation_failure_self_heals(
            {
                "status": "fail",
                "esql_query": query,
                "analysis": {"unknown_columns": [{"name": "freq", "role": "metric"}]},
            }
        )
        is False
    )


def test_validation_failure_self_heals_true_for_missing_metric_when_query_provided():
    query = "| STATS x = AVG(RATE(http_requests_total, 5m)) BY time_bucket = TBUCKET(5 minute)\n"
    assert (
        validation_failure_self_heals(
            {
                "status": "fail",
                "esql_query": query,
                "analysis": {
                    "unknown_columns": [{"name": "http_requests_total", "role": "metric"}]
                },
            }
        )
        is True
    )


def test_validation_failure_self_heals_unchanged_when_esql_query_omitted():
    assert (
        validation_failure_self_heals(
            {
                "status": "fail",
                "analysis": {"unknown_columns": [{"name": "freq", "role": "metric"}]},
            }
        )
        is True
    )
