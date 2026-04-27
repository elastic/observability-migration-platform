"""Source/target comparison logic.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from observability_migration.adapters.source.grafana.esql_validate import DEFAULT_TEND_EXPR, DEFAULT_TSTART_EXPR

ROW_COUNT_TOLERANCE = 0.20
SCALAR_RELATIVE_TOLERANCE = 0.05
SCALAR_ABSOLUTE_TOLERANCE = 1e-6
COLUMN_OVERLAP_THRESHOLD = 0.5


@dataclass
class ComparisonWindow:
    mode: str = "dashboard_time"
    time_from: str = DEFAULT_TSTART_EXPR
    time_to: str = DEFAULT_TEND_EXPR
    bucket_alignment: str = ""
    range_window: str = ""
    variables_expanded: bool = False
    fixture_selection: str = "not_configured"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ComparisonResult:
    status: str = "not_attempted"
    comparator_family: str = ""
    reason: str = ""
    diff_summary: str = ""
    tolerance_used: dict[str, Any] = field(default_factory=dict)
    counterexamples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_sample_window(
    query_ir: dict[str, Any] | None,
    validation_record: dict[str, Any] | None = None,
) -> ComparisonWindow:
    query_ir = query_ir or {}
    validation_record = validation_record or {}
    has_validation = bool(validation_record.get("status"))
    analysis = validation_record.get("analysis") or {}
    runtime_window = analysis.get("sample_window") or {}
    source_expression = str(query_ir.get("source_expression", "") or "")
    target_query = str(query_ir.get("target_query", "") or "")
    range_window = str(query_ir.get("range_window", "") or "")
    has_unexpanded_vars = any(
        token in expr
        for expr in (source_expression, target_query)
        for token in ("$", "[[", "{{")
    )
    return ComparisonWindow(
        mode=str(runtime_window.get("mode", "dashboard_time") or "dashboard_time"),
        time_from=str(runtime_window.get("time_from", DEFAULT_TSTART_EXPR) or DEFAULT_TSTART_EXPR),
        time_to=str(runtime_window.get("time_to", DEFAULT_TEND_EXPR) or DEFAULT_TEND_EXPR),
        bucket_alignment=(f"range_window:{range_window}" if range_window else "query_defined"),
        range_window=range_window,
        variables_expanded=not has_unexpanded_vars,
        fixture_selection="live_target_validation" if has_validation else "not_configured",
    )


def _compare_row_counts(source_rows: int, target_rows: int) -> tuple[str, str]:
    if source_rows == 0 and target_rows == 0:
        return "within_tolerance", "Both sides returned 0 rows"
    if source_rows == 0:
        return "drift", f"Source returned 0 rows but target returned {target_rows}"
    if target_rows == 0:
        return "drift", f"Target returned 0 rows but source returned {source_rows}"
    ratio = abs(target_rows - source_rows) / max(source_rows, 1)
    if ratio <= ROW_COUNT_TOLERANCE:
        return "within_tolerance", f"Row counts within tolerance: source={source_rows}, target={target_rows} (ratio={ratio:.2%})"
    return "drift", f"Row count drift: source={source_rows}, target={target_rows} (ratio={ratio:.2%})"


def _compare_column_overlap(source_cols: list[str], target_cols: list[str]) -> tuple[str, list[str]]:
    source_set = {c.lower() for c in source_cols if c and not c.startswith("__")}
    target_set = {c.lower() for c in target_cols if c and not c.startswith("__")}
    if not source_set or not target_set:
        return "within_tolerance", []
    missing = sorted(source_set - target_set)
    if not missing:
        return "within_tolerance", []
    overlap = len(source_set & target_set) / len(source_set) if source_set else 1.0
    if overlap >= COLUMN_OVERLAP_THRESHOLD:
        return "within_tolerance", missing
    return "drift", missing


def _compare_scalar(source_value: Any, target_value: Any) -> tuple[str, str]:
    try:
        sv = float(source_value)
        tv = float(target_value)
    except (TypeError, ValueError):
        return "drift", f"Cannot compare non-numeric scalar values: source={source_value!r}, target={target_value!r}"
    if math.isnan(sv) and math.isnan(tv):
        return "within_tolerance", "Both sides returned NaN"
    if math.isnan(sv) or math.isnan(tv):
        return "drift", f"NaN mismatch: source={sv}, target={tv}"
    if abs(sv) < SCALAR_ABSOLUTE_TOLERANCE and abs(tv) < SCALAR_ABSOLUTE_TOLERANCE:
        return "within_tolerance", f"Both values near zero: source={sv}, target={tv}"
    denom = max(abs(sv), abs(tv))
    relative = abs(sv - tv) / denom if denom > 0 else 0.0
    if relative <= SCALAR_RELATIVE_TOLERANCE:
        return "within_tolerance", f"Values within tolerance: source={sv}, target={tv} (relative={relative:.2%})"
    if relative > 0.5:
        return "material_drift", f"Large value divergence: source={sv}, target={tv} (relative={relative:.2%})"
    return "drift", f"Value drift: source={sv}, target={tv} (relative={relative:.2%})"


def _coerce_float(value: Any) -> float | None:
    try:
        if isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_scalar_candidate(summary: dict[str, Any]) -> Any:
    values = list(summary.get("values") or [])
    for row in values:
        if isinstance(row, (list, tuple)):
            for item in reversed(row):
                numeric = _coerce_float(item)
                if numeric is not None:
                    return item
        else:
            numeric = _coerce_float(row)
            if numeric is not None:
                return row
    metadata = summary.get("metadata") or {}
    for key in ("latest_value", "value"):
        numeric = _coerce_float(metadata.get(key))
        if numeric is not None:
            return metadata.get(key)
    return None


def _run_value_comparison(
    comparator_family: str,
    source_summary: dict[str, Any],
    target_summary: dict[str, Any],
) -> ComparisonResult:
    source_rows = source_summary.get("rows") or 0
    target_rows = target_summary.get("rows") or 0
    source_cols = list(source_summary.get("columns") or [])
    target_cols = list(target_summary.get("columns") or [])
    counterexamples: list[str] = []

    row_status, row_detail = _compare_row_counts(source_rows, target_rows)
    col_status, col_missing = _compare_column_overlap(source_cols, target_cols)
    if col_missing:
        counterexamples.append(f"Source columns not found in target: {', '.join(col_missing)}")

    worst = "within_tolerance"
    for status in (row_status, col_status):
        if status == "material_drift":
            worst = "material_drift"
            break
        if status == "drift" and worst != "material_drift":
            worst = "drift"

    if comparator_family in {"single_value", "scalar"}:
        source_value = _extract_scalar_candidate(source_summary)
        target_value = _extract_scalar_candidate(target_summary)
        if source_value is not None and target_value is not None:
            scalar_status, scalar_detail = _compare_scalar(source_value, target_value)
            if scalar_status == "material_drift":
                worst = "material_drift"
            elif scalar_status == "drift" and worst != "material_drift":
                worst = "drift"
            counterexamples.append(scalar_detail)

    tolerance_used = {
        "row_count_tolerance": ROW_COUNT_TOLERANCE,
        "column_overlap_threshold": COLUMN_OVERLAP_THRESHOLD,
        "scalar_relative_tolerance": SCALAR_RELATIVE_TOLERANCE,
    }

    return ComparisonResult(
        status=worst,
        comparator_family=comparator_family,
        reason=row_detail,
        diff_summary=f"Row comparison: {row_status}. Column comparison: {col_status}.",
        tolerance_used=tolerance_used,
        counterexamples=counterexamples,
    )


def build_comparison_result(
    source_execution: dict[str, Any] | None,
    target_execution: dict[str, Any] | None,
    query_ir: dict[str, Any] | None,
    validation_record: dict[str, Any] | None = None,
) -> ComparisonResult:
    source_execution = source_execution or {}
    target_execution = target_execution or {}
    query_ir = query_ir or {}
    validation_record = validation_record or {}
    comparator_family = str(query_ir.get("output_shape", "") or "unknown")
    source_status = str(source_execution.get("status", "not_attempted") or "not_attempted")
    target_status = str(target_execution.get("status", "not_run") or "not_run")
    target_error = str(target_execution.get("error", "") or validation_record.get("error", "") or "")
    source_reason = str(source_execution.get("reason", "") or "")

    if target_status in {"fail", "fixed_empty"}:
        return ComparisonResult(
            status="target_broken",
            comparator_family=comparator_family,
            reason="Target runtime validation did not produce a trustworthy runnable result",
            diff_summary="Target-side execution failed before a source-vs-target comparison could run",
            tolerance_used={"mode": "unconfigured"},
            counterexamples=[target_error] if target_error else [],
        )

    if source_status == "pass" and target_status in {"pass", "fixed"}:
        source_summary = source_execution.get("result_summary") or {}
        target_summary = target_execution.get("result_summary") or {}
        if source_summary and target_summary:
            return _run_value_comparison(comparator_family, source_summary, target_summary)
        return ComparisonResult(
            status="summary_only",
            comparator_family=comparator_family,
            reason="Source and target both executed, but one side returned no result summary",
            diff_summary="Execution evidence exists on both sides; result summaries insufficient for comparison",
            tolerance_used={"mode": "pending_result_data"},
        )

    if target_status in {"pass", "fixed"}:
        return ComparisonResult(
            status="target_only",
            comparator_family=comparator_family,
            reason=source_reason or "Target runtime evidence is available, but source execution is not configured",
            diff_summary="Target execution evidence captured; source-vs-target drift is still unmeasured",
            tolerance_used={"mode": "pending_source_adapter"},
        )

    if target_status == "skip":
        return ComparisonResult(
            status="not_attempted",
            comparator_family=comparator_family,
            reason=target_error or "Target validation was skipped",
            diff_summary="No live target execution evidence was recorded",
        )

    return ComparisonResult(
        status="not_attempted",
        comparator_family=comparator_family,
        reason="Live comparison was not requested",
        diff_summary="Verification fell back to static and runtime heuristics only",
    )


def comparison_gate_override(comparison: dict[str, Any] | None) -> str | None:
    comparison = comparison or {}
    status = str(comparison.get("status", "") or "")
    if status in {"target_broken", "material_drift"}:
        return "Red"
    if status == "drift":
        return "Yellow"
    if status == "within_tolerance":
        return "Green"
    return None
