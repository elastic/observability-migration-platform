# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana transformation → ES|QL rewrites."""

from __future__ import annotations

from types import SimpleNamespace

from observability_migration.adapters.source.grafana.transforms import (
    apply_transformations_to_esql,
)

_QUERY = (
    "TS metrics-*\n"
    "| STATS policy_report_result = SUM(policy_report_result) "
    "BY time_bucket = TBUCKET(5 minute), namespace, category\n"
    "| KEEP time_bucket, namespace, category, policy_report_result"
)


def _translation():
    return SimpleNamespace(
        esql_query=_QUERY,
        metadata={
            "metric_fields": ["policy_report_result"],
            "output_group_fields": ["namespace", "category"],
        },
        warnings=[],
    )


def test_organize_identity_rename_keeps_the_column():
    """`renameByName: {"namespace": "namespace"}` must not drop the column.

    Real dashboards ship identity entries in organize.renameByName -- PolicyReport
    Details does. The rename path emitted `EVAL namespace = namespace` and then
    dropped `resolved`, which for an identity rename is that same column, so the
    field vanished from KEEP even though excludeByName did not exclude it and
    indexByName positioned it for display. Silent column loss, not just noise.
    """
    panel = {
        "transformations": [
            {"id": "organize", "options": {
                "excludeByName": {},
                "renameByName": {"namespace": "namespace"},
            }}
        ]
    }
    query, _result = apply_transformations_to_esql(panel, _translation())

    assert "EVAL namespace = namespace" not in query
    keep = [ln for ln in query.splitlines() if ln.strip().startswith("| KEEP")]
    assert keep, query
    assert "namespace" in keep[-1], keep[-1]


def test_organize_real_rename_still_renames():
    """The identity short-circuit must not disable genuine renames."""
    panel = {
        "transformations": [
            {"id": "organize", "options": {
                "excludeByName": {},
                "renameByName": {"namespace": "ns"},
            }}
        ]
    }
    query, _result = apply_transformations_to_esql(panel, _translation())
    assert "EVAL ns = namespace" in query
    keep = [ln for ln in query.splitlines() if ln.strip().startswith("| KEEP")]
    assert keep and "ns" in keep[-1]


def test_counter_suffix_alias_resolves_against_live_caps():
    """A dashboard naming a counter without `_total` must still find it.

    Exporters adopted the OpenMetrics `_total` suffix at different times, so a
    dashboard written against one version names a metric the current exporter no
    longer exposes. Real case: postgres-overview "Buffers" queries
    `pg_stat_bgwriter_buffers_alloc` while postgres_exporter v0.15 emits
    `pg_stat_bgwriter_buffers_alloc_total` -- the panel was dead against a
    perfectly healthy target.
    """
    from observability_migration.adapters.source.grafana.schema import SchemaResolver

    r = SchemaResolver.__new__(SchemaResolver)
    r._field_cache = {"metrics.x_total": {}, "metrics.y": {}}
    r._metric_map_applied = {}
    r._metric_map_warnings = []

    # Dashboard says `x`, target has `x_total`.
    assert r._counter_suffix_alias("metrics.x", "x") == "metrics.x_total"
    assert r._metric_map_applied["x"] == "metrics.x_total"
    assert r._metric_map_warnings, "the substitution must be reported"

    # Dashboard says `y_total`, target has `y`.
    assert r._counter_suffix_alias("metrics.y_total", "y_total") == "metrics.y"

    # Present as asked: untouched.
    assert r._counter_suffix_alias("metrics.y", "y") == "metrics.y"

    # Neither spelling present: keep the source name so preflight reports the gap.
    assert r._counter_suffix_alias("metrics.z", "z") == "metrics.z"


def test_counter_suffix_alias_never_guesses_offline():
    """With no live caps there is no evidence, so nothing may be substituted."""
    from observability_migration.adapters.source.grafana.schema import SchemaResolver

    r = SchemaResolver.__new__(SchemaResolver)
    r._field_cache = {}
    r._metric_map_applied = {}
    r._metric_map_warnings = []
    assert r._counter_suffix_alias("metrics.x", "x") == "metrics.x"
    assert r._metric_map_applied == {}
