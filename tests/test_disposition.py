# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Unit tests for live ES|QL validation disposition helpers."""

from __future__ import annotations

from observability_migration.core.verification.disposition import (
    unknown_column_looks_like_alias_bug,
    validation_failure_self_heals,
)

_ALIAS_BUG_QUERY = (
    "TS metrics-*\n"
    "| WHERE node_cpu_scaling_frequency_hertz IS NOT NULL\n"
    "| STATS node_cpu_scaling_frequency_hertz_B = MAX(LAST_OVER_TIME(node_cpu_scaling_frequency_hertz)) "
    "BY time_bucket = TBUCKET(5 minute)\n"
    "| EVAL CPU = node_cpu_scaling_frequency_hertz\n"
)

_CORRECT_RENAMED_ALIAS_QUERY = (
    "TS metrics-*\n"
    "| WHERE node_cpu_scaling_frequency_hertz IS NOT NULL\n"
    "| STATS node_cpu_scaling_frequency_hertz_B = MAX(LAST_OVER_TIME(node_cpu_scaling_frequency_hertz)) "
    "BY time_bucket = TBUCKET(5 minute)\n"
    "| EVAL CPU = node_cpu_scaling_frequency_hertz_B\n"
)


def test_unknown_column_matching_pre_rename_stats_alias_is_not_self_heal():
    assert (
        unknown_column_looks_like_alias_bug(
            "node_cpu_scaling_frequency_hertz", _ALIAS_BUG_QUERY
        )
        is True
    )


def test_unknown_column_renamed_stats_alias_is_not_flagged_as_alias_bug():
    assert (
        unknown_column_looks_like_alias_bug(
            "node_cpu_scaling_frequency_hertz_B", _CORRECT_RENAMED_ALIAS_QUERY
        )
        is False
    )


def test_unknown_metric_not_in_query_still_self_heals():
    query = (
        "TS metrics-*\n"
        "| STATS x = AVG(RATE(http_requests_total)) BY time_bucket = TBUCKET(5 minute)\n"
    )
    assert unknown_column_looks_like_alias_bug("http_requests_total", query) is False


def test_validation_failure_self_heals_false_for_alias_bug_when_esql_query_provided():
    assert (
        validation_failure_self_heals(
            {
                "status": "fail",
                "esql_query": _ALIAS_BUG_QUERY,
                "analysis": {
                    "unknown_columns": [
                        {"name": "node_cpu_scaling_frequency_hertz", "role": "metric"}
                    ]
                },
            }
        )
        is False
    )


def test_validation_failure_self_heals_false_for_alias_bug_when_legacy_query_key():
    assert (
        validation_failure_self_heals(
            {
                "status": "fail",
                "query": _ALIAS_BUG_QUERY,
                "analysis": {
                    "unknown_columns": [
                        {"name": "node_cpu_scaling_frequency_hertz", "role": "metric"}
                    ]
                },
            }
        )
        is False
    )


def test_validation_failure_self_heals_true_for_missing_metric_when_query_provided():
    query = (
        "TS metrics-*\n"
        "| STATS x = AVG(RATE(http_requests_total)) BY time_bucket = TBUCKET(5 minute)\n"
    )
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


def test_validation_failure_self_heals_true_when_renamed_alias_query_and_missing_metric():
    assert (
        validation_failure_self_heals(
            {
                "status": "fail",
                "esql_query": _CORRECT_RENAMED_ALIAS_QUERY,
                "analysis": {
                    "unknown_columns": [
                        {"name": "node_cpu_scaling_frequency_hertz", "role": "metric"}
                    ]
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
