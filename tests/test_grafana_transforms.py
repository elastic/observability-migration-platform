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
