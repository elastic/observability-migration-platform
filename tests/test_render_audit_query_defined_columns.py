# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""An unknown column the query defines itself is a bug, not a target field gap.

The render audit downgrades ``Unknown column [x]`` to the warn-level
``field_gap`` once it confirms ``x`` is absent from the index the panel reads.
That is right for a column the panel expects to *find* in the target, and wrong
for one the translator *creates*: a synthetic ``EVAL``/``STATS`` output is never
supposed to exist in the index, so its absence proves nothing and the error can
only be a construction bug.

A real migrated Datadog dashboard emitted
``EVAL series_group = CONCAT(..., TO_STRING(series_group))`` -- an EVAL reading
the column it defines. Elasticsearch rejected it with
``Unknown column [series_group]``, and because ``series_group`` is (correctly)
absent from the target, the audit filed it as a data-readiness warning. The
gate that exists to catch panels that cannot render passed it.
"""
from __future__ import annotations

from observability_migration.targets.kibana.render_audit import (
    classify_panel,
    query_defined_columns_from_query,
)

_SELF_REFERENCING = (
    "FROM metrics-*\n"
    "| STATS value = SUM(spans) BY time_bucket = BUCKET(@timestamp, 75), service.name\n"
    '| EVAL series_group = CONCAT(COALESCE(TO_STRING(service.name), ""), '
    '" / ", COALESCE(TO_STRING(series_group), ""))'
)

_ERROR_TEXT = (
    "An error occurred\n"
    "verification_exception: Found 1 problem\nline 4:151: Unknown column [series_group]"
)


def test_a_column_the_query_defines_is_a_render_error_not_a_field_gap():
    result = classify_panel(
        "Spans per env and service",
        _ERROR_TEXT,
        available_fields=["service.name", "@timestamp", "spans"],
        query_defined_columns=["series_group", "value", "time_bucket"],
    )
    assert result.error_class == "render_error"


def test_the_verdict_says_why_the_absence_proves_nothing():
    result = classify_panel(
        "Spans per env and service",
        _ERROR_TEXT,
        available_fields=["service.name"],
        query_defined_columns=["series_group"],
    )
    assert "series_group" in result.detail
    assert "defined by this panel's own query" in result.detail


def test_a_genuinely_absent_target_column_is_still_a_field_gap():
    """The downgrade must survive: this is the case it was written for."""
    result = classify_panel(
        "Spans by pod",
        "An error occurred\nverification_exception: Found 1 problem\n"
        "line 2:20: Unknown column [kubernetes.pod.name]",
        available_fields=["service.name"],
        query_defined_columns=["value"],
    )
    assert result.error_class == "field_gap"


def test_no_query_columns_supplied_keeps_the_previous_verdict():
    result = classify_panel(
        "Spans by pod",
        "An error occurred\nverification_exception: Found 1 problem\n"
        "line 2:20: Unknown column [kubernetes.pod.name]",
        available_fields=["service.name"],
    )
    assert result.error_class == "field_gap"


def test_eval_and_stats_aliases_are_both_collected():
    columns = set(query_defined_columns_from_query(_SELF_REFERENCING))
    assert {"series_group", "value", "time_bucket"} <= columns


def test_plain_index_columns_are_not_reported_as_query_defined():
    columns = set(query_defined_columns_from_query(_SELF_REFERENCING))
    assert "service.name" not in columns
    assert "spans" not in columns


def test_a_query_with_no_aliases_defines_nothing():
    assert query_defined_columns_from_query("FROM metrics-* | KEEP a, b") == []
