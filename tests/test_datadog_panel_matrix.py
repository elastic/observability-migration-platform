# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Combinatorial Datadog widget matrix over the real translation pipeline.

Mirror of ``tests/test_panel_matrix.py`` (Grafana). Enumerates the cartesian
product of

    {widget type} x {space aggregation} x {group-by arity}

builds a real Datadog widget for each cell, runs it through the *actual*
``normalize_dashboard -> plan_widget -> translate_widget`` pipeline, and applies
the Layer-9 invariant linter to the emitted ``(yaml_panel, TranslationResult)``.

Every cell must translate without an ERROR-severity fidelity finding. A cell
that the planner honestly marks ``not_feasible``/``requires_manual``/``skipped``
is excluded from the ERROR gate (the translator refusing is correct behaviour,
not a bug) but is still counted so silent coverage loss is visible.

Run standalone for a triage table::

    python -m tests.test_datadog_panel_matrix
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "parity-rig"))

from verifier import invariants
from verifier.invariants import Severity

from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.translate import translate_widget

# Chart-bearing widget types (text/group/stream types carry no query and are
# matrix-exempt — see tests/core/coverage/test_supported_types.py).
_WIDGETS = (
    "timeseries", "query_value", "toplist", "table", "heatmap", "pie",
    "bar_chart", "query_table", "distribution", "change",
    "treemap", "sunburst", "scatterplot", "geomap",
)
_AGGS = ("avg", "sum", "min", "max")
_BY: tuple[tuple[str, ...], ...] = ((), ("host",), ("host", "service"))

# Explicitly deferred chart-bearing shapes. These are not counted as translated
# support; keeping them enumerated prevents the matrix from silently treating
# broad ``requires_manual`` / ``not_feasible`` statuses as covered.
_DEFERRED_CELLS = {
    # Geomap has no native automatic map translation yet.
    *(f"geomap::{agg}::by{arity}" for agg in _AGGS for arity in (0, 1, 2)),
    # Partition-style widgets need at least one grouping dimension.
    *(f"{widget}::{agg}::by0" for widget in ("pie", "treemap", "sunburst") for agg in _AGGS),
    # Heatmap needs a bucket/category dimension.
    *(f"heatmap::{agg}::by0" for agg in _AGGS),
}
_NON_TRANSLATED_STATUSES = {"not_feasible", "requires_manual", "skipped"}


def _widget(wtype: str, agg: str, by: tuple[str, ...], idx: int) -> dict[str, Any]:
    by_clause = (" by {" + ", ".join(by) + "}") if by else ""
    query = f"{agg}:system.cpu.user{{*}}{by_clause}"
    return {
        "id": idx,
        "definition": {
            "type": wtype,
            "title": f"{wtype}|{agg}|by{len(by)}",
            "requests": [{"q": query, "display_type": "line"}],
        },
        "layout": {"x": 0, "y": idx * 4, "width": 6, "height": 4},
    }


def generate_cases() -> list[tuple[str, dict[str, Any]]]:
    cases: list[tuple[str, dict[str, Any]]] = []
    idx = 0
    for wtype in _WIDGETS:
        for agg in _AGGS:
            for by in _BY:
                idx += 1
                case_id = f"{wtype}::{agg}::by{len(by)}"
                cases.append((case_id, _widget(wtype, agg, by, idx)))
    return cases


def run_matrix() -> list[tuple[str, str, list[invariants.Finding]]]:
    """Return ``(case_id, status, findings)`` per cell."""
    results: list[tuple[str, str, list[invariants.Finding]]] = []
    for case_id, widget_json in generate_cases():
        dash = {"title": "dd-matrix", "widgets": [widget_json]}
        try:
            normalized = normalize_dashboard(dash)
            status = "ok"
            findings: list[invariants.Finding] = []
            for widget in normalized.widgets:
                plan = plan_widget(widget)
                result = translate_widget(widget, plan, OTEL_PROFILE)
                status = result.status
                findings += invariants.lint_translation(
                    result.yaml_panel, result, "dd-matrix"
                )
        except Exception as exc:  # a crash is itself an ERROR finding
            results.append(
                (
                    case_id,
                    "crash",
                    [
                        invariants.Finding(
                            invariants.InvariantCategory.ACCESSOR_BROKEN,
                            Severity.ERROR,
                            case_id,
                            "dd-matrix",
                            f"pipeline raised {type(exc).__name__}: {exc}",
                        )
                    ],
                )
            )
            continue
        results.append((case_id, status, findings))
    return results


def _gated_errors(
    results: list[tuple[str, str, list[invariants.Finding]]],
) -> list[tuple[str, invariants.Finding]]:
    out: list[tuple[str, invariants.Finding]] = []
    for case_id, status, findings in results:
        if status in _NON_TRANSLATED_STATUSES:
            if case_id in _DEFERRED_CELLS:
                continue
            out.append(
                (
                    case_id,
                    invariants.Finding(
                        invariants.InvariantCategory.VISUAL_SEMANTIC_DRIFT,
                        Severity.ERROR,
                        case_id,
                        "dd-matrix",
                        f"supported matrix cell returned non-translated status {status}",
                    ),
                )
            )
            continue
        for finding in findings:
            if finding.severity is Severity.ERROR:
                out.append((case_id, finding))
    return out


def test_datadog_matrix_has_no_error_findings() -> None:
    results = run_matrix()
    errors = _gated_errors(results)
    detail = "\n".join(
        f"  {cid}: {f.category.value} - {f.message}" for cid, f in errors
    )
    assert not errors, (
        f"{len(errors)} datadog panel-matrix cell(s) produced ERROR fidelity "
        f"findings:\n{detail}"
    )


def _main() -> int:
    results = run_matrix()
    errors = _gated_errors(results)
    by_status: dict[str, int] = {}
    for _cid, status, _f in results:
        by_status[status] = by_status.get(status, 0) + 1
    print(f"datadog matrix: {len(results)} cells | status={by_status} | gated_errors={len(errors)}")
    for cid, finding in errors:
        print(f"  ERROR {cid}: {finding.category.value} - {finding.message}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(_main())
