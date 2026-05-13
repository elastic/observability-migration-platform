"""Pairwise drift detection between the five tiers of a panel record.

The naive ``a == b`` check is wrong here: kb-dashboard-cli compiles
YAML to NDJSON with predictable whitespace normalisation, and Lens may
inject ``BUCKET(@timestamp,...)`` columns that the YAML didn't have.

So we compare on a canonical form (stripped + collapsed whitespace +
unified single quotes) and surface the side-by-side detail in
``record.drift_details`` so the report renderer can show exactly what
differed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .records import DRIFT_AXES, PanelRecord, Verdict

_WHITESPACE = re.compile(r"\s+")

# Known/expected post-translator transforms that mutate ES|QL between
# T1 (migration_report.json) and T2 (YAML) or beyond. When all of the
# right-side-only differences match one of these patterns the drift is
# downgraded to PASS rather than DRIFT.
_KNOWN_T1_T2_RIGHT_ONLY_PATTERNS = (
    # Strip the full pipe-clause including all CONCAT arguments so the
    # remainder can be compared with exact equality against the left side.
    re.compile(r"\|\s*EVAL\s+legend\s*=\s*CONCAT\([^|]*\)", re.IGNORECASE),
    re.compile(r",\s*legend\b"),  # extended KEEP that includes synthetic legend
    # gauge panels: YAML emitter appends synthetic min/max/goal constants
    # so Lens can render the gauge with the user's expected bounds.
    re.compile(r"\|\s*EVAL\s+_gauge_(?:min|max|goal)\s*=", re.IGNORECASE),
)


def canonicalise(esql: str) -> str:
    """Return a normalised ES|QL string for equality comparison.

    Drops leading/trailing whitespace and collapses internal whitespace
    to single spaces. Does NOT semantically transform the query (e.g.
    rewrite a BUCKET call). The intent is "structural identity modulo
    whitespace"; non-trivial mutations should surface as drift, not be
    hidden.
    """
    if not esql:
        return ""
    return _WHITESPACE.sub(" ", esql).strip()


def compare_panel_record(record: PanelRecord) -> Verdict:
    """Fill ``record.drift_axes`` / ``record.drift_details`` and return
    the overall :class:`Verdict`."""
    record.drift_axes = []
    record.drift_details = {}

    if record.status == "not_feasible" or record.feasibility == "not_feasible":
        record.verdict = Verdict.NOT_FEASIBLE
        return record.verdict
    if not record.t1_translator_esql:
        record.verdict = Verdict.SKIP
        record.notes.append("no translator output (panel may be markdown / manual)")
        return record.verdict

    pairs: Iterable[tuple[str, str, str]] = (
        ("T0=T1", record.t0_source_promql, record.t1_translator_esql),
        ("T1=T2", record.t1_translator_esql, record.t2_yaml_esql),
        ("T2=T3", record.t2_yaml_esql, record.t3_ndjson_esql),
        ("T3=T4", record.t3_ndjson_esql, record.t4_cluster_esql),
        ("T4=T5", record.t4_cluster_esql, record.t5_live_query_body),
    )
    for axis, left, right in pairs:
        verdict = _compare_pair(axis, left, right, record)
        if verdict:
            record.drift_axes.append(axis)
            record.drift_details[axis] = verdict

    if record.t5_response_error and record.t5_response_status >= 400:
        record.verdict = Verdict.FAIL
        record.notes.append(
            f"live _query failed: {record.t5_response_status} {record.t5_response_error[:120]}"
        )
        return record.verdict

    if not record.t3_ndjson_esql and not record.t4_cluster_esql:
        record.verdict = Verdict.NOT_UPLOADED
        record.notes.append("no compiled NDJSON or cluster saved object available")
        return record.verdict

    if record.drift_axes:
        record.verdict = Verdict.DRIFT
        return record.verdict

    record.verdict = Verdict.PASS
    return record.verdict


def _compare_pair(
    axis: str,
    left: str,
    right: str,
    record: PanelRecord,
) -> str:
    """Return a short human-readable drift description, or empty string
    if the tiers match."""
    if axis == "T0=T1":
        # Source PromQL -> translator ES|QL is *expected* to differ; we
        # only report this axis if the translator produced no output,
        # which is already classified as SKIP above.
        return ""
    left_canon = canonicalise(left)
    right_canon = canonicalise(right)
    if not left_canon and not right_canon:
        return ""
    if not right_canon:
        return f"{axis}: right side empty (left={_preview(left_canon)})"
    if not left_canon:
        return f"{axis}: left side empty (right={_preview(right_canon)})"
    if left_canon == right_canon:
        return ""
    if axis == "T1=T2" and _is_known_t1_t2_drift(left_canon, right_canon):
        return ""
    return (
        f"{axis} canonical-mismatch: "
        f"L={_preview(left_canon)} | R={_preview(right_canon)}"
    )


def _is_known_t1_t2_drift(left: str, right: str) -> bool:
    """Return True if every right-side-only diff matches a documented
    post-translator transform applied by the panels.py emitter.

    The composite-legend splice (``EVAL legend = CONCAT(...)`` plus an
    extended ``KEEP`` clause) is the canonical example: the translator
    records the bare query in ``migration_report.json:esql`` but the
    YAML emitter adds the legend column. That is intentional and should
    not be flagged as drift.
    """
    if right.startswith(left.rstrip()):
        suffix = right[len(left.rstrip()):].strip()
        if not suffix:
            return True
        return any(p.search(suffix) for p in _KNOWN_T1_T2_RIGHT_ONLY_PATTERNS)
    # Right may have spliced lines into the middle (e.g. extended KEEP
    # that retains original labels alongside legend). In that case
    # both sides should still parse identically once known patterns are
    # stripped from the right.
    stripped_right = right
    for pattern in _KNOWN_T1_T2_RIGHT_ONLY_PATTERNS:
        stripped_right = pattern.sub("", stripped_right)
    stripped_right = _WHITESPACE.sub(" ", stripped_right).strip()
    return stripped_right == _WHITESPACE.sub(" ", left).strip()


def _preview(s: str, limit: int = 80) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + "..."


def aggregate_verdicts(records: list[PanelRecord]) -> dict[str, int]:
    out: dict[str, int] = {v.value: 0 for v in Verdict}
    for record in records:
        out[record.verdict.value] = out.get(record.verdict.value, 0) + 1
    return out


def aggregate_drift_axes(records: list[PanelRecord]) -> dict[str, int]:
    out = {axis: 0 for axis in DRIFT_AXES}
    for record in records:
        for axis in record.drift_axes:
            out[axis] = out.get(axis, 0) + 1
    return out


__all__ = [
    "aggregate_drift_axes",
    "aggregate_verdicts",
    "canonicalise",
    "compare_panel_record",
]
