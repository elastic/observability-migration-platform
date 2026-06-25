# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Combinatorial panel-matrix (Layer 11) over the real translation pipeline.

Instead of hand-picking fixtures, this enumerates the cartesian product of

    {panel type} x {query family} x {legend shape} x {by-arity}

builds a real Grafana panel for each cell, runs it through the *actual*
``panels.translate_panel`` pipeline, and applies the Layer-9 invariant linter to
the emitted ``(yaml_panel, panel_result)``. Every cell must translate without an
ERROR-severity fidelity finding (broken accessor, silent series merge, silent
placeholder). When a cell trips an ERROR finding, that is a genuine pipeline bug
the matrix has surfaced.

Run standalone to print a triage table::

    python -m tests.test_panel_matrix
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "parity-rig"))

from verifier import invariants
from verifier.invariants import Severity

from observability_migration.adapters.source.grafana import panels, rules, schema

_LABEL_POOL = ("job", "instance", "namespace", "pod", "method")

# panel type -> Grafana panel "type" string
_PANEL_TYPES = ("timeseries", "barchart", "gauge", "stat", "table", "piechart", "heatmap")

# query families: name -> expr template using {m} metric and {by} clause
_FAMILIES: dict[str, str] = {
    "simple_metric": "{m}{sel}",
    "rate": "rate({m}{sel}[5m])",
    "sum_rate_by": "sum(rate({m}{sel}[5m])){by}",
    "avg_by": "avg({m}{sel}){by}",
    "histogram_quantile": "histogram_quantile(0.95, sum(rate({m}_bucket{sel}[5m])) by (le{extra_by}))",
}

_LEGEND_SHAPES: dict[str, str] = {
    "none": "",
    "single": "{{" + "{l0}" + "}}",
    "multi": "{{" + "{l0}" + "}} {{" + "{l1}" + "}}",
}


def _by_clause(arity: int) -> tuple[str, str, list[str]]:
    labels = list(_LABEL_POOL[:arity])
    if not labels:
        return "", "", labels
    by = f" by ({', '.join(labels)})"
    extra = ", " + ", ".join(labels) if labels else ""
    return by, extra, labels


def _legend_format(shape: str, labels: list[str]) -> str:
    l0 = labels[0] if labels else "job"
    l1 = labels[1] if len(labels) > 1 else "instance"
    template = _LEGEND_SHAPES[shape]
    return template.format(l0=l0, l1=l1)


def generate_cases() -> list[tuple[str, dict[str, Any]]]:
    """Yield ``(case_id, grafana_panel)`` across the matrix."""
    cases: list[tuple[str, dict[str, Any]]] = []
    metric = "http_requests_total"
    idx = 0
    for panel_type in _PANEL_TYPES:
        for family, template in _FAMILIES.items():
            for arity in (0, 1, 2, 3):
                by, extra_by, labels = _by_clause(arity)
                # families that require a by() clause skip arity 0
                if family in ("sum_rate_by", "avg_by") and arity == 0:
                    continue
                expr = template.format(m=metric, sel="", by=by, extra_by=extra_by)
                for shape in _LEGEND_SHAPES:
                    if shape != "none" and arity == 0 and family != "histogram_quantile":
                        # no labels to put in a legend
                        continue
                    idx += 1
                    legend = _legend_format(shape, labels)
                    target: dict[str, Any] = {
                        "expr": expr,
                        "refId": "A",
                        "datasource": {"type": "prometheus"},
                    }
                    if legend:
                        target["legendFormat"] = legend
                    panel = {
                        "id": idx,
                        "type": panel_type,
                        "title": f"{panel_type}|{family}|by{arity}|{shape}",
                        "targets": [target],
                        "fieldConfig": {"defaults": {}, "overrides": []},
                        "gridPos": {"x": 0, "y": idx * 8, "w": 24, "h": 8},
                    }
                    case_id = f"{panel_type}::{family}::by{arity}::legend_{shape}"
                    cases.append((case_id, panel))
    return cases


def run_matrix() -> list[tuple[str, list[invariants.Finding]]]:
    rp = rules.RulePackConfig()
    resolver = schema.SchemaResolver(rp)
    results: list[tuple[str, list[invariants.Finding]]] = []
    for case_id, panel in generate_cases():
        try:
            yaml_panel, panel_result = panels.translate_panel(
                panel,
                datasource_index="metrics-*",
                esql_index="metrics-*",
                rule_pack=rp,
                resolver=resolver,
            )
        except Exception as exc:  # a crash is itself a finding
            results.append(
                (
                    case_id,
                    [
                        invariants.Finding(
                            invariants.InvariantCategory.ACCESSOR_BROKEN,
                            Severity.ERROR,
                            case_id,
                            "matrix",
                            f"translate_panel raised {type(exc).__name__}: {exc}",
                        )
                    ],
                )
            )
            continue
        findings = invariants.lint_translation(yaml_panel, panel_result, "matrix")
        results.append((case_id, findings))
    return results


def _errors(results: list[tuple[str, list[invariants.Finding]]]) -> list[tuple[str, invariants.Finding]]:
    out: list[tuple[str, invariants.Finding]] = []
    for case_id, findings in results:
        for f in findings:
            if f.severity is Severity.ERROR:
                out.append((case_id, f))
    return out


def test_matrix_has_no_error_findings() -> None:
    results = run_matrix()
    errors = _errors(results)
    detail = "\n".join(f"  {cid}: {f.category.value} - {f.message}" for cid, f in errors)
    assert not errors, (
        f"{len(errors)} panel-matrix cell(s) produced ERROR fidelity findings:\n{detail}"
    )


def _main() -> int:
    results = run_matrix()
    total = len(results)
    errors = _errors(results)
    by_cat: dict[str, int] = {}
    for _cid, f in errors:
        by_cat[f.category.value] = by_cat.get(f.category.value, 0) + 1
    print(f"panel-matrix: {total} cells, {len(errors)} ERROR findings")
    for cat, count in sorted(by_cat.items()):
        print(f"  {cat}: {count}")
    print("-" * 70)
    for cid, f in errors:
        print(f"{cid}\n    {f.category.value} [{f.severity.value}] {f.message}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(_main())
