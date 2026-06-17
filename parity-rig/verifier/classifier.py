"""Rule-based root-cause classification for 5-tier verifier output.

Inputs a :class:`PanelRecord` and emits a :class:`Classification` that
assigns the panel to one of a fixed set of failure categories. The
intent is to compress a 1500-panel verifier report into a triage queue
where each row already says *what* class of fix is needed, not just
that the panel is broken.

Categories are tied to documented mig-to-kbn failure modes. Each rule
matches on cheap structural signals (regex on T5 errors, presence of
notes, drift axes, visual diff score) so the classifier is fast and
deterministic.

LLM hook
--------

The module exposes a single module-level ``LLM_HOOK`` variable. When
``None`` (default) the rule-based verdict is returned as-is. When set
to a callable with signature::

    LLM_HOOK(record: PanelRecord, rule: Classification) -> Classification

it is invoked AFTER the rule classifier and its return value WINS over
the rule verdict. This is the single point of integration for any
future LLM-based classifier; everything else in this module stays
LLM-free, which keeps the module trivially testable and reproducible.

The hook is allowed to raise. Exceptions are logged at WARN and the
rule-based verdict is kept (graceful degradation: a flaky LLM endpoint
must never break a verifier run).

CLI
---

``python -m verifier.classifier --verifier-report <path> --output <path>``
reads a verifier JSON report (the format produced by
``parity-rig/verifier/cli.py``), classifies every panel, and writes:

  * ``<output>``      — the merged JSON (verifier payload + classifications)
  * ``<output>.md``   — a Markdown triage doc grouped by category

The ``--output`` directory is created if necessary; we deliberately do
not default to ``/tmp``.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .records import PanelRecord, Verdict

LOG = logging.getLogger(__name__)


# --------------------------------------------------------------------- #
# Public types
# --------------------------------------------------------------------- #


CATEGORY_TRANSLATOR_BUG = "translator_bug"
CATEGORY_DATA_GAP = "data_gap"
CATEGORY_KIBANA_CACHE_STALE = "kibana_cache_stale"
CATEGORY_LENS_VISUAL_MISMATCH = "lens_visual_mismatch"
CATEGORY_SCHEMA_RESOLUTION = "schema_resolution"
CATEGORY_FEASIBILITY_GAP = "feasibility_gap"
CATEGORY_TRANSIENT_CLUSTER = "transient_cluster"
CATEGORY_UNKNOWN = "unknown"

ALL_CATEGORIES = (
    CATEGORY_TRANSLATOR_BUG,
    CATEGORY_DATA_GAP,
    CATEGORY_KIBANA_CACHE_STALE,
    CATEGORY_LENS_VISUAL_MISMATCH,
    CATEGORY_SCHEMA_RESOLUTION,
    CATEGORY_FEASIBILITY_GAP,
    CATEGORY_TRANSIENT_CLUSTER,
    CATEGORY_UNKNOWN,
)


@dataclass
class Classification:
    """Root-cause verdict for a single :class:`PanelRecord`."""

    category: str = CATEGORY_UNKNOWN
    confidence: float = 0.0  # 0.0..1.0
    rationale: str = ""
    suggested_action: str = ""
    evidence: list[str] = field(default_factory=list)

    def to_jsonable(self) -> dict:
        return asdict(self)


#: Optional LLM override.  See module docstring.
LLM_HOOK: Callable[[PanelRecord, Classification], Classification] | None = None


# --------------------------------------------------------------------- #
# Regex tables
# --------------------------------------------------------------------- #


_UNKNOWN_COLUMN_RE = re.compile(r"unknown column \[([^\]]+)\]", re.IGNORECASE)
_BINARY_OPERATOR_RE = re.compile(r"binary operator", re.IGNORECASE)
_LABEL_SET_RE = re.compile(r"cannot infer label set", re.IGNORECASE)
_COUNTER_REQUIRED_RE = re.compile(r"requires a counter metric", re.IGNORECASE)
_CIRCUIT_BREAKER_RES = (
    re.compile(r"data too large", re.IGNORECASE),
    re.compile(r"circuit_breaker_exception", re.IGNORECASE),
)

# Documented mig-to-kbn feasibility gaps for PROMQL primitives we cannot
# express in ES|QL today; matching one of these in a not_feasible note
# escalates the verdict from "unknown" to "feasibility_gap".
_FEASIBILITY_NOTE_TOKENS = (
    "histogram_quantile",
    "topk",
    "label_replace",
    "vector",
    "predict_linear",
)


# --------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------- #


def classify(
    record: PanelRecord,
    *,
    yaml_mtime: datetime | None = None,
) -> Classification:
    """Classify a single panel record.

    ``yaml_mtime`` is the mtime of the source YAML for the panel's
    dashboard, used by the ``kibana_cache_stale`` rule to decide whether
    the cluster's saved object is older than the on-disk YAML. When not
    provided we still report the rule but with reduced confidence
    because we can't ground-truth the timestamp comparison.
    """
    rule = _classify_rules(record, yaml_mtime=yaml_mtime)

    if LLM_HOOK is None:
        return rule

    try:
        override = LLM_HOOK(record, rule)
    except Exception as exc:
        LOG.warning(
            "LLM_HOOK raised for panel %r (%r): %s; keeping rule verdict %r",
            record.title,
            record.panel_id,
            exc,
            rule.category,
        )
        return rule
    if not isinstance(override, Classification):
        LOG.warning(
            "LLM_HOOK returned non-Classification (%r) for panel %r; "
            "keeping rule verdict %r",
            type(override).__name__,
            record.title,
            rule.category,
        )
        return rule
    return override


# --------------------------------------------------------------------- #
# Rule engine — order matters; first match wins
# --------------------------------------------------------------------- #


def _classify_rules(
    record: PanelRecord,
    *,
    yaml_mtime: datetime | None,
) -> Classification:
    error = record.t5_response_error or ""

    # 1. Schema resolution: ES|QL knows the index but the field is
    # missing. The PromQL label probably needs aliasing (handled in
    # observability_migration.adapters.source.grafana.schema).
    m = _UNKNOWN_COLUMN_RE.search(error)
    if m:
        column = m.group(1)
        return Classification(
            category=CATEGORY_SCHEMA_RESOLUTION,
            confidence=0.9,
            rationale=(
                f"T5 _query rejected with `unknown column [{column}]`, meaning "
                "the field name our ES|QL references is not present on the "
                "live index. Most often this is a label-name mismatch the "
                "schema resolver did not catch."
            ),
            suggested_action=(
                f"add a SchemaResolver alias for `{column}` (or extend the "
                "translator to emit the alias automatically) and re-run the "
                "panel through the pipeline."
            ),
            evidence=[f"t5_response_error: unknown column [{column}]", f"field={column}"],
        )

    # 2. Transient cluster failures: NOT translator bugs.  We surface
    # them so the operator does not waste time chasing a code change.
    for pattern in _CIRCUIT_BREAKER_RES:
        if pattern.search(error):
            token = pattern.pattern.strip("\\").lower()
            return Classification(
                category=CATEGORY_TRANSIENT_CLUSTER,
                confidence=0.95,
                rationale=(
                    f"T5 _query rejected with `{token}` — this is an "
                    "Elasticsearch resource limit (heap pressure / circuit "
                    "breaker), not a defect in the translator output. The "
                    "same query usually succeeds after the cluster recovers."
                ),
                suggested_action=(
                    "retry the panel after the cluster cools down; if it "
                    "persists, increase indices.breaker.* limits or shrink "
                    "the time window."
                ),
                evidence=[f"t5_response_error matches `{token}`"],
            )

    # 3. Translator-bug gates that should have downgraded a hard query
    # but didn't. We split into two distinct subcategories so the fix
    # location is unambiguous in the suggested action.
    if _LABEL_SET_RE.search(error) or _BINARY_OPERATOR_RE.search(error):
        which = "cannot infer label set" if _LABEL_SET_RE.search(error) else "binary operator"
        return Classification(
            category=CATEGORY_TRANSLATOR_BUG,
            confidence=0.9,
            rationale=(
                f"T5 _query rejected with `{which}` — Elastic's PROMQL "
                "command could not evaluate this specific expression shape. "
                "The common implicit-match ratio between two distinct metrics "
                "is evaluated natively (see #138), so this is a narrower "
                "unsupported shape that the native gate currently lets through."
            ),
            suggested_action=(
                "narrow `can_use_native_promql` to reject this specific PROMQL "
                "shape so the panel degrades to ES|QL translation instead of "
                "emitting a PROMQL command the cluster rejects."
            ),
            evidence=[f"t5_response_error contains `{which}`"],
        )

    if _COUNTER_REQUIRED_RE.search(error):
        return Classification(
            category=CATEGORY_TRANSLATOR_BUG,
            confidence=0.9,
            rationale=(
                "T5 _query rejected with `requires a counter metric` — the "
                "rate-on-gauge gate did not catch a metric the cluster "
                "treats as a gauge but the translator emitted RATE() against."
            ),
            suggested_action=(
                "extend `_COUNTER_TO_GAUGE_FALLBACK` (or the equivalent "
                "metric-type override table) to cover this metric so the "
                "translator switches to a delta/derivative form."
            ),
            evidence=["t5_response_error: requires a counter metric"],
        )

    # 4. Feasibility gap — the translator already declined, and the
    # note explains which PROMQL primitive is unsupported. This is
    # *expected* output, not a defect; we surface it so the operator
    # knows whether to wait for the feature or rebuild the panel.
    notes_blob = " ".join(record.t1_notes).lower()
    if (record.status == "not_feasible" or record.feasibility == "not_feasible"):
        gap_token = next(
            (tok for tok in _FEASIBILITY_NOTE_TOKENS if tok in notes_blob),
            "",
        )
        if gap_token:
            return Classification(
                category=CATEGORY_FEASIBILITY_GAP,
                confidence=0.95,
                rationale=(
                    f"Translator declined this panel because PROMQL `{gap_token}` "
                    "has no documented ES|QL equivalent today. This is a "
                    "tracked feature-gap, not a translator regression."
                ),
                suggested_action=(
                    f"track the PROMQL `{gap_token}` feature gap and surface "
                    "the panel to the user as a known limitation rather than "
                    "an error."
                ),
                evidence=[
                    f"status={record.status or record.feasibility}",
                    f"t1_notes mentions `{gap_token}`",
                ],
            )

    # 5. Stale Kibana cache — drift on T3=T4 is the canonical signal
    # that the on-disk NDJSON disagrees with the live saved object,
    # and a stale upload is by far the most common cause.
    if "T3=T4" in record.drift_axes:
        cluster_ts = _parse_iso(record.t4_saved_object_updated_at)
        if yaml_mtime is not None and cluster_ts is not None and cluster_ts < yaml_mtime:
            return Classification(
                category=CATEGORY_KIBANA_CACHE_STALE,
                confidence=0.9,
                rationale=(
                    "T3=T4 drift detected and the cluster saved object "
                    f"({cluster_ts.isoformat()}) is older than the local "
                    f"YAML ({yaml_mtime.isoformat()}). The most likely cause "
                    "is that the dashboard wasn't re-uploaded after the YAML "
                    "was regenerated."
                ),
                suggested_action=(
                    "re-run `obs-migrate --upload` (or `parity-rig/upload-all.sh`) "
                    "to refresh the saved object."
                ),
                evidence=[
                    "drift_axes contains T3=T4",
                    f"cluster_updated_at={record.t4_saved_object_updated_at}",
                    f"yaml_mtime={yaml_mtime.isoformat()}",
                ],
            )
        # Without yaml_mtime we still tag it but with lower confidence.
        return Classification(
            category=CATEGORY_KIBANA_CACHE_STALE,
            confidence=0.6,
            rationale=(
                "T3=T4 drift detected: the on-disk NDJSON disagrees with "
                "the cluster saved object. The most likely cause is a stale "
                "upload, but we do not have a YAML mtime to confirm."
            ),
            suggested_action=(
                "re-run `obs-migrate --upload` to refresh the saved object "
                "and re-run the verifier."
            ),
            evidence=[
                "drift_axes contains T3=T4",
                f"cluster_updated_at={record.t4_saved_object_updated_at or '(unknown)'}",
            ],
        )

    # 6. Lens visual mismatch — every structural tier matches and the
    # query returned data, but the rendered chart still drifted. The
    # suspect is Lens dimension/breakdown wiring, not the query.
    above_threshold = (
        record.visual_diff_threshold > 0.0
        and record.visual_diff_score > record.visual_diff_threshold
    )
    all_pass = record.verdict == Verdict.PASS and not record.drift_axes
    if above_threshold and all_pass:
        return Classification(
            category=CATEGORY_LENS_VISUAL_MISMATCH,
            confidence=0.85,
            rationale=(
                "Every structural tier (T0..T5) matches and the live query "
                f"returned data, yet the visual diff "
                f"({record.visual_diff_score:.4f}) exceeds the threshold "
                f"({record.visual_diff_threshold:.4f}). Lens is rendering "
                "the same data differently from Grafana — typically a "
                "dimension or breakdown binding mismatch."
            ),
            suggested_action=(
                "verify Lens dimension/breakdown bindings against the panel "
                "YAML (color, x-axis, breakdown columns), and check the "
                "`splitDimensions`/`xAccessor` keys in the compiled NDJSON."
            ),
            evidence=[
                f"visual_diff_score={record.visual_diff_score:.4f} > "
                f"threshold={record.visual_diff_threshold:.4f}",
                "verdict=PASS, no drift_axes",
            ],
        )

    # 7. Data gap — query parsed and ran, but returned zero rows even
    # though the PromQL side did. Almost always a misconfigured data
    # view, a too-narrow filter, or a missing index pattern.
    promql_appears_to_work = bool(record.t0_source_promql) and not record.t5_response_error
    if (
        promql_appears_to_work
        and record.t5_response_status
        and record.t5_response_status < 400
        and record.t5_response_row_count == 0
    ):
        return Classification(
            category=CATEGORY_DATA_GAP,
            confidence=0.75,
            rationale=(
                "The PromQL source query exists and the live ES|QL parsed "
                f"successfully (status={record.t5_response_status}) but "
                "returned 0 rows. The query is well-formed; the data the "
                "query is looking at is not."
            ),
            suggested_action=(
                "run a `WHERE @timestamp IS NOT NULL | LIMIT 5` against the "
                "same index; check the data view's index pattern matches "
                "the index where the metrics actually land."
            ),
            evidence=[
                f"t0_source_promql present (len={len(record.t0_source_promql)})",
                f"t5_response_status={record.t5_response_status}",
                "t5_response_row_count=0",
            ],
        )

    # 8. Fall through — we have nothing strong to say, so report low
    # confidence and let the LLM hook (if any) take a crack at it.
    return Classification(
        category=CATEGORY_UNKNOWN,
        confidence=0.1,
        rationale=(
            "No rule matched; this panel will need manual triage. "
            "Common reasons we land here: unfamiliar T5 error string, "
            "drift axes other than T3=T4, or a feasibility gap whose note "
            "is phrased differently from the documented set."
        ),
        suggested_action=(
            "open the panel's verifier-row Markdown section, look at "
            "drift_details and t5_response_error verbatim, and add a new "
            "rule to the classifier if this becomes a recurring pattern."
        ),
        evidence=[
            f"verdict={record.verdict.value}",
            f"drift_axes={list(record.drift_axes)}",
        ],
    )


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        # Kibana's saved-object updated_at is RFC3339 / ISO 8601 ending
        # in ``Z`` (UTC). datetime.fromisoformat accepts the ``+00:00``
        # form natively.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="verifier.classifier",
        description=(
            "Root-cause classification for a 5-tier verifier report. "
            "Reads the JSON report produced by `verifier.cli`, classifies "
            "each panel, and writes a merged JSON + a Markdown triage doc."
        ),
    )
    p.add_argument(
        "--verifier-report",
        type=Path,
        required=True,
        help="Path to the verifier JSON report.",
    )
    p.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Where to write the classified JSON. The Markdown triage "
             "doc is written alongside (`<output>.md`).",
    )
    p.add_argument(
        "--yaml-dir",
        type=Path,
        default=None,
        help="Directory containing the source YAML for the dashboard. "
             "When provided, used to derive the YAML mtime for the "
             "kibana_cache_stale rule.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose logging.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    payload = json.loads(args.verifier_report.read_text())
    yaml_mtime = _yaml_mtime_for(args.yaml_dir) if args.yaml_dir else None

    classified_panels: list[dict] = []
    for panel_blob in payload.get("panels", []):
        record = PanelRecord.from_jsonable(panel_blob)
        classification = classify(record, yaml_mtime=yaml_mtime)
        merged = dict(panel_blob)
        merged["classification"] = classification.to_jsonable()
        classified_panels.append(merged)

    out_payload = dict(payload)
    out_payload["panels"] = classified_panels
    out_payload["classification_summary"] = _summarise(classified_panels)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out_payload, indent=2, default=str))
    LOG.info("wrote %s", args.output)

    md_path = args.output.with_suffix(args.output.suffix + ".md")
    md_path.write_text(_render_markdown(out_payload, classified_panels))
    LOG.info("wrote %s", md_path)
    return 0


def _yaml_mtime_for(yaml_dir: Path) -> datetime | None:
    if not yaml_dir.is_dir():
        return None
    mtimes: list[float] = []
    for entry in yaml_dir.rglob("*.yaml"):
        try:
            mtimes.append(entry.stat().st_mtime)
        except OSError:
            continue
    if not mtimes:
        return None
    return datetime.fromtimestamp(max(mtimes), tz=UTC)


def _summarise(panels: list[dict]) -> dict:
    counts: dict[str, int] = {c: 0 for c in ALL_CATEGORIES}
    for panel in panels:
        cat = panel.get("classification", {}).get("category", CATEGORY_UNKNOWN)
        counts[cat] = counts.get(cat, 0) + 1
    return counts


def _render_markdown(payload: dict, panels: list[dict]) -> str:
    title = payload.get("dashboard_title") or "(unknown)"
    lines = [
        f"# Triage: {title}",
        "",
        f"- panels classified: **{len(panels)}**",
        "",
        "## counts by category",
        "",
        "| category | count |",
        "| --- | --- |",
    ]
    summary = payload.get("classification_summary", _summarise(panels))
    for cat in ALL_CATEGORIES:
        lines.append(f"| `{cat}` | {summary.get(cat, 0)} |")
    grouped: dict[str, list[dict]] = defaultdict(list)
    for panel in panels:
        grouped[panel["classification"]["category"]].append(panel)
    for cat in ALL_CATEGORIES:
        rows = grouped.get(cat, [])
        if not rows:
            continue
        lines += ["", f"## {cat} ({len(rows)})", ""]
        lines += [
            "| panel | confidence | suggested action |",
            "| --- | --- | --- |",
        ]
        for panel in rows:
            cl = panel["classification"]
            lines.append(
                f"| {_md_escape(panel.get('title', ''))} "
                f"| {cl['confidence']:.2f} "
                f"| {_md_escape(cl['suggested_action'])} |"
            )
    return "\n".join(lines) + "\n"


def _md_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


__all__ = [
    "ALL_CATEGORIES",
    "CATEGORY_DATA_GAP",
    "CATEGORY_FEASIBILITY_GAP",
    "CATEGORY_KIBANA_CACHE_STALE",
    "CATEGORY_LENS_VISUAL_MISMATCH",
    "CATEGORY_SCHEMA_RESOLUTION",
    "CATEGORY_TRANSIENT_CLUSTER",
    "CATEGORY_TRANSLATOR_BUG",
    "CATEGORY_UNKNOWN",
    "LLM_HOOK",
    "Classification",
    "classify",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
