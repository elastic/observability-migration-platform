"""Layer 9 - deterministic, offline panel invariant linter.

The 5-tier verifier (``compare.py``) proves a panel's ES|QL *text* did not
mutate between translator, YAML, NDJSON, saved object and live query. It says
nothing about whether the *visualization* wiring is internally consistent:

* every Lens dimension/breakdown/metric references a column the query actually
  produces (otherwise Kibana renders "Provided column name or index is
  invalid");
* multi-dimension grouping that an XY chart cannot show as separate series is
  either folded into a synthetic composite ``legend``/``series_group`` column
  or honestly disclosed as a "visually merged" warning - never silently
  dropped;
* a panel that degraded to a markdown placeholder carries a reason, instead of
  silently disappearing as a chart.

These checks are *deterministic and offline*: they read the artifacts a normal
``obs-migrate`` run already writes (``migration_report.json`` - which carries
both the intended ``query_ir`` and the emitted ``visual_ir``). No cluster is
required. When a live Elasticsearch is available, :func:`make_es_columns_oracle`
swaps the embedded column inference for the authoritative ``columns`` block that
``POST /_query`` returns (Oracle 1), eliminating any parser drift.

This module deliberately keeps the verifier package's "zero dependency on
``observability_migration``" contract: the column parser is embedded, and the
package's richer parser is used only opportunistically when it happens to be
importable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

# A column oracle maps an ES|QL query to the set of column names it outputs, or
# ``None`` when that set cannot be determined (in which case field-existence
# checks are skipped rather than guessed - we never false-positive).
ColumnsOracle = Callable[[str], "set[str] | None"]

_TIME_LIKE_FIELDS = {"time_bucket", "timestamp_bucket", "step", "@timestamp"}

_COMPOSITE_LEGEND_RE = re.compile(r"EVAL\s+legend\s*=\s*CONCAT\(", re.IGNORECASE)
_COMPOSITE_SERIES_GROUP_RE = re.compile(r"EVAL\s+series_group\s*=\s*CONCAT\(", re.IGNORECASE)
_MERGE_DISCLOSURE_RE = re.compile(r"merg(e|ed)|collaps|composit", re.IGNORECASE)

# Opportunistically use the package's battle-tested ES|QL shape parser when it
# is importable; otherwise fall back to the embedded parser below. Either way
# the verifier package remains runnable standalone.
try:  # pragma: no cover - exercised implicitly when the package is installed
    from observability_migration.targets.kibana.emit.esql_utils import (
        extract_esql_shape as _pkg_extract_esql_shape,
    )
    from observability_migration.targets.kibana.emit.esql_utils import (
        split_esql_pipeline as _pkg_split_pipeline,
    )
except Exception:  # pragma: no cover
    _pkg_extract_esql_shape = None
    _pkg_split_pipeline = None


class InvariantCategory(str, Enum):
    """What kind of fidelity defect a :class:`Finding` represents."""

    ACCESSOR_BROKEN = "ACCESSOR_BROKEN"
    BREAKDOWN_LEGEND_MISMATCH = "BREAKDOWN_LEGEND_MISMATCH"
    VISUAL_SEMANTIC_DRIFT = "VISUAL_SEMANTIC_DRIFT"
    PLACEHOLDER_DROPPED = "PLACEHOLDER_DROPPED"


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class Finding:
    """A single invariant violation on one panel."""

    category: InvariantCategory
    severity: Severity
    panel_title: str
    dashboard_title: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "severity": self.severity.value,
            "panel_title": self.panel_title,
            "dashboard_title": self.dashboard_title,
            "message": self.message,
            "evidence": dict(self.evidence),
        }


# --------------------------------------------------------------------- #
# Embedded ES|QL column inference (offline oracle)
# --------------------------------------------------------------------- #


def _split_top_level(text: str, sep: str) -> list[str]:
    """Split on ``sep`` at paren depth 0, ignoring quoted regions."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    for ch in text:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth = max(depth - 1, 0)
            current.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())
    return [p for p in parts if p]


def _split_pipeline(esql: str) -> list[str]:
    if _pkg_split_pipeline is not None:
        try:
            return list(_pkg_split_pipeline(esql))
        except Exception:  # pragma: no cover - defensive
            pass
    return _split_top_level(str(esql or ""), "|")


def _lhs_alias(assignment: str) -> str:
    """Return the output column name an assignment defines.

    ``foo = AVG(bar)`` -> ``foo``;  ``namespace`` -> ``namespace``;
    ``BUCKET(@timestamp, ...)`` -> the whole expression (Lens would name the
    column after it, but our emitter always aliases so this is a fallback).
    """
    for idx, ch in enumerate(assignment):
        if ch == "=" and assignment[idx : idx + 2] != "==":
            # ignore ``==`` / ``>=`` / ``<=`` / ``!=`` operators
            prev = assignment[idx - 1] if idx else ""
            if prev not in ("=", "!", "<", ">"):
                return assignment[:idx].strip()
    return assignment.strip()


def _columns_via_embedded_parser(esql: str) -> set[str] | None:
    parts = _split_pipeline(esql)
    if not parts:
        return None
    is_native = parts[0].strip().upper().startswith("PROMQL")
    cols: set[str] = set()
    has_projection = False
    for cmd in parts:
        low = cmd.strip().lower()
        if low.startswith("stats "):
            has_projection = True
            body = cmd.strip()[6:]
            assigns, by = _split_keyword(body, "by")
            cols = set()
            for piece in _split_top_level(assigns, ",") + _split_top_level(by, ","):
                name = _lhs_alias(piece)
                if name:
                    cols.add(name)
        elif low.startswith("keep "):
            has_projection = True
            cols = {p.strip() for p in _split_top_level(cmd.strip()[5:], ",") if p.strip()}
        elif low.startswith("row "):
            has_projection = True
            cols = set()
            for piece in _split_top_level(cmd.strip()[4:], ","):
                name = _lhs_alias(piece)
                if name:
                    cols.add(name)
        elif low.startswith("eval "):
            for piece in _split_top_level(cmd.strip()[5:], ","):
                name = _lhs_alias(piece)
                if name:
                    cols.add(name)
        elif low.startswith("drop "):
            for piece in _split_top_level(cmd.strip()[5:], ","):
                cols.discard(piece.strip())
        elif low.startswith("rename "):
            for piece in _split_top_level(cmd.strip()[7:], ","):
                # ``old AS new`` (ES|QL) -> new is the output column
                m = re.match(r"(.+?)\s+as\s+(.+)", piece.strip(), re.IGNORECASE)
                if m:
                    cols.discard(m.group(1).strip())
                    cols.add(m.group(2).strip())
    # Only a STATS / KEEP / ROW stage fully bounds the output column set. A bare
    # ``FROM``/``TS``/native ``PROMQL`` (optionally with WHERE/EVAL) leaves the
    # base columns unknown, so we decline to judge rather than risk a false
    # "broken accessor".
    if not has_projection:
        return None
    if is_native and not any(p.strip().lower().startswith("keep ") for p in parts):
        # native PROMQL base output columns are not statically knowable unless a
        # trailing KEEP pins them.
        return None
    return {c for c in cols if c}


def _split_keyword(text: str, keyword: str) -> tuple[str, str]:
    """Split ``STATS ... BY ...`` style text on a top-level keyword."""
    depth = 0
    quote: str | None = None
    kw = f" {keyword.lower()} "
    lower = text.lower()
    for idx, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        elif depth == 0 and lower[idx:].startswith(kw):
            return text[:idx].strip(), text[idx + len(kw) :].strip()
    return text.strip(), ""


def static_query_columns(esql: str) -> set[str] | None:
    """Best-effort offline inference of a query's output column names.

    Returns ``None`` when the column set cannot be reliably determined, which
    callers treat as "skip the field-existence check for this panel".
    """
    if not esql or not esql.strip():
        return None
    if _pkg_extract_esql_shape is not None:
        try:
            embedded = _columns_via_embedded_parser(esql)
            if embedded is None:
                return None
            shape = _pkg_extract_esql_shape(esql)
            cols = (
                set(shape.projected_fields)
                | set(shape.metric_fields)
                | set(shape.group_fields)
                | set(shape.time_fields)
            )
            cols = {c for c in cols if c}
            # Union with the embedded parser so EVAL-added columns the package
            # parser may not surface (it tracks projected_fields conservatively)
            # are still considered present.
            return {_strip_ident(c) for c in (cols | embedded)}
        except Exception:  # pragma: no cover - defensive
            pass
    parsed = _columns_via_embedded_parser(esql)
    if parsed is None:
        return None
    return {_strip_ident(c) for c in parsed}


def _strip_ident(name: str) -> str:
    """Normalize an ES|QL identifier for comparison.

    ES|QL escapes field names containing dots/special chars with backticks
    (the column ``service.instance.id`` is written wrapped in backticks). The
    column a query *produces* is the unescaped name, so accessor comparison must
    strip the backticks on both sides to avoid false ACCESSOR_BROKEN findings.
    """
    return str(name or "").strip().strip("`")


def is_time_like(field_name: str) -> bool:
    return _strip_ident(field_name) in _TIME_LIKE_FIELDS


# --------------------------------------------------------------------- #
# Live oracle (Oracle 1)
# --------------------------------------------------------------------- #


def make_es_columns_oracle(es_url: str, api_key: str) -> ColumnsOracle:
    """Return a :data:`ColumnsOracle` backed by Elasticsearch ``POST /_query``.

    The authoritative column set the cluster reports replaces the embedded
    parser. On any transport / query error the oracle returns ``None`` so an
    un-seeded or transiently-failing query degrades to "not checked" rather than
    a false accessor error.
    """
    from . import collectors

    def _oracle(esql: str) -> set[str] | None:
        if not esql or not esql.strip():
            return None
        status, body = collectors.run_cluster_query(es_url, api_key, esql)
        if status >= 400 or isinstance(body, str):
            return None
        columns = body.get("columns") or []
        names = {c.get("name", "") for c in columns if c.get("name")}
        return names or None

    return _oracle


# --------------------------------------------------------------------- #
# Field references on an emitted esql panel config
# --------------------------------------------------------------------- #


def _field_of(container: Any) -> str:
    if isinstance(container, dict):
        return _strip_ident(container.get("field") or "")
    return ""


def referenced_fields(esql_config: dict[str, Any]) -> list[tuple[str, str]]:
    """Yield ``(role, field_name)`` for every column an esql panel references."""
    refs: list[tuple[str, str]] = []
    for key in ("dimension", "primary", "metric", "breakdown"):
        fname = _field_of(esql_config.get(key))
        if fname:
            refs.append((key, fname))
    metrics = esql_config.get("metrics")
    if isinstance(metrics, list):
        for item in metrics:
            fname = _field_of(item)
            if fname:
                refs.append(("metrics", fname))
    breakdowns = esql_config.get("breakdowns")
    if isinstance(breakdowns, list):
        for item in breakdowns:
            fname = _field_of(item)
            if fname:
                refs.append(("breakdowns", fname))
    for key in ("minimum", "maximum", "goal"):
        fname = _field_of(esql_config.get(key))
        if fname:
            refs.append((key, fname))
    return refs


# --------------------------------------------------------------------- #
# Per-panel checks
# --------------------------------------------------------------------- #

_XY_CHART_TYPES = {"line", "area", "bar"}

# Grafana panel types that are not data charts; rendering them as markdown/text
# in Kibana is the correct outcome, not a silent placeholder drop.
_NON_DATA_GRAFANA_TYPES = {
    "text",
    "row",
    "dashlist",
    "news",
    "welcome",
    "alertlist",
    "annolist",
    "pluginlist",
    "getting-started",
}


def _fallback_esql_config(panel: dict[str, Any]) -> dict[str, Any]:
    yaml_panel = panel.get("yaml_panel") if isinstance(panel.get("yaml_panel"), dict) else {}
    yaml_esql = yaml_panel.get("esql") if isinstance(yaml_panel.get("esql"), dict) else {}
    if yaml_esql:
        return dict(yaml_esql)
    query = str(panel.get("esql_query") or panel.get("esql") or "").strip()
    if not query:
        return {}
    kibana_type = str(panel.get("kibana_type") or "").lower()
    chart_type = {
        "xy": "line",
        "metric": "metric",
        "table": "datatable",
        "partition": "pie",
        "treemap": "treemap",
        "heatmap": "heatmap",
    }.get(kibana_type, kibana_type)
    config: dict[str, Any] = {"query": query}
    if chart_type:
        config["type"] = chart_type
    return config


def lint_report_panel(
    panel: dict[str, Any],
    dashboard_title: str,
    *,
    columns_oracle: ColumnsOracle | None = None,
) -> list[Finding]:
    """Run every deterministic invariant on one ``migration_report`` panel."""
    title = panel.get("title") or "(untitled)"
    status = str(panel.get("status") or "").lower()
    if status == "not_feasible":
        # Honestly-not-feasible panels are expected; placeholder honesty for
        # them is covered by the markdown/placeholder rule below.
        pass

    query_ir = panel.get("query_ir") if isinstance(panel.get("query_ir"), dict) else {}
    visual_ir = panel.get("visual_ir") if isinstance(panel.get("visual_ir"), dict) else {}
    presentation = visual_ir.get("presentation") if isinstance(visual_ir, dict) else {}
    presentation = presentation if isinstance(presentation, dict) else {}
    kind = str(presentation.get("kind") or "")
    config = presentation.get("config") if isinstance(presentation.get("config"), dict) else {}
    if not kind and (fallback_config := _fallback_esql_config(panel)):
        kind = "esql"
        config = fallback_config
    reasons = [str(r) for r in (panel.get("reasons") or [])]
    top_level_warnings = [str(w) for w in (panel.get("warnings") or [])]
    top_level_losses = [str(w) for w in (panel.get("semantic_losses") or [])]
    pva = str(panel.get("post_validation_action") or "")

    findings: list[Finding] = []

    # --- Placeholder honesty -------------------------------------------------
    # Grafana panel types that are *inherently* non-data: migrating them to a
    # markdown/text panel is correct, not a silent chart drop.
    grafana_type = str(panel.get("grafana_type") or "").lower()
    non_data_source = grafana_type in _NON_DATA_GRAFANA_TYPES
    if kind == "markdown" and status == "migrated" and not pva and not reasons and not non_data_source:
        findings.append(
            Finding(
                InvariantCategory.PLACEHOLDER_DROPPED,
                Severity.ERROR,
                title,
                dashboard_title,
                "panel migrated to a markdown placeholder but carries no reason "
                "and no post_validation_action - a chart silently became text",
                evidence={"status": status},
            )
        )

    if kind != "esql" or not config:
        return findings

    query = str(config.get("query") or panel.get("esql_query") or panel.get("esql") or "").strip()

    findings.extend(_check_accessor_fields(title, dashboard_title, config, query, columns_oracle))
    findings.extend(
        _check_merged_series(
            title,
            dashboard_title,
            query_ir,
            config,
            query,
            reasons + top_level_warnings + top_level_losses,
        )
    )
    return findings


def _check_accessor_fields(
    title: str,
    dashboard_title: str,
    config: dict[str, Any],
    query: str,
    columns_oracle: ColumnsOracle | None,
) -> list[Finding]:
    cols = columns_oracle(query) if columns_oracle is not None else static_query_columns(query)
    if cols is None:
        return []
    findings: list[Finding] = []
    for role, fname in referenced_fields(config):
        if is_time_like(fname):
            continue
        if fname in cols:
            continue
        category = (
            InvariantCategory.BREAKDOWN_LEGEND_MISMATCH
            if fname == "legend"
            else InvariantCategory.ACCESSOR_BROKEN
        )
        detail = (
            "breakdown is bound to the synthetic 'legend' column but the query "
            "does not produce it (missing 'EVAL legend = CONCAT(...)')"
            if category is InvariantCategory.BREAKDOWN_LEGEND_MISMATCH
            else f"{role} references column '{fname}' that the query does not produce"
        )
        findings.append(
            Finding(
                category,
                Severity.ERROR,
                title,
                dashboard_title,
                detail,
                evidence={
                    "role": role,
                    "field": fname,
                    "query_columns": sorted(cols),
                },
            )
        )
    return findings


def _job_scope_extra_group_fields(
    query: str,
    *,
    group_fields: list[str],
    breakdown_field: str,
) -> list[str]:
    """Return extra BY fields treated as dashboard scope rather than drift.

    Mirrors ``panels._job_scope_extra_group_fields``: Prometheus
    ``by (instance, job)`` panels filtered by ``?job`` keep ``instance`` as the
    XY breakdown and intentionally leave ``job`` off the chart. That is a
    Kibana-fit choice, not an undisclosed series merge.
    """
    groups = [str(col) for col in (group_fields or []) if col]
    if len(groups) != 2 or not breakdown_field or "?job" not in str(query or ""):
        return []
    instance_fields = {"instance", "labels.instance", "service.instance.id", "host.name"}
    job_fields = {"job", "labels.job", "service.name"}
    if breakdown_field not in instance_fields:
        return []
    extras = [col for col in groups if col != breakdown_field]
    if len(extras) != 1:
        return []
    return extras if extras[0] in job_fields else []


def _check_merged_series(
    title: str,
    dashboard_title: str,
    query_ir: dict[str, Any],
    config: dict[str, Any],
    query: str,
    reasons: list[str],
) -> list[Finding]:
    chart_type = str(config.get("type") or "").lower()
    if chart_type not in _XY_CHART_TYPES:
        return []
    group_fields = [
        f for f in (query_ir.get("output_group_fields") or []) if not is_time_like(f)
    ]
    if len(group_fields) <= 1:
        return []

    breakdown = config.get("breakdown") if isinstance(config.get("breakdown"), dict) else {}
    breakdown_field = str((breakdown or {}).get("field") or "")
    composite_applied = (
        breakdown_field == "legend" and bool(_COMPOSITE_LEGEND_RE.search(query or ""))
    ) or (
        breakdown_field == "series_group"
        and bool(_COMPOSITE_SERIES_GROUP_RE.search(query or ""))
    )
    if composite_applied:
        return []
    if _job_scope_extra_group_fields(
        query,
        group_fields=group_fields,
        breakdown_field=breakdown_field,
    ):
        return []

    disclosures = (
        reasons
        + [str(w) for w in (query_ir.get("warnings") or [])]
        + [str(w) for w in (query_ir.get("semantic_losses") or [])]
    )
    disclosed = any(_MERGE_DISCLOSURE_RE.search(text) for text in disclosures)
    severity = Severity.WARNING if disclosed else Severity.ERROR
    message = (
        f"{len(group_fields)} grouping dimensions {group_fields} collapse to a "
        f"single XY breakdown ('{breakdown_field or 'none'}')"
        + (
            " - disclosed via warning"
            if disclosed
            else " with NO disclosing warning: series are silently merged"
        )
    )
    return [
        Finding(
            InvariantCategory.VISUAL_SEMANTIC_DRIFT,
            severity,
            title,
            dashboard_title,
            message,
            evidence={
                "group_fields": list(group_fields),
                "breakdown_field": breakdown_field,
                "disclosed": disclosed,
            },
        )
    ]


# --------------------------------------------------------------------- #
# Drivers
# --------------------------------------------------------------------- #


def _presentation_from_yaml(yaml_panel: dict[str, Any] | None) -> dict[str, Any]:
    yp = yaml_panel or {}
    if isinstance(yp.get("esql"), dict):
        return {"kind": "esql", "config": dict(yp["esql"])}
    if isinstance(yp.get("markdown"), dict):
        return {"kind": "markdown", "config": dict(yp["markdown"])}
    return {"kind": "", "config": {}}


def report_panel_from_translation(
    yaml_panel: dict[str, Any] | None, panel_result: Any
) -> dict[str, Any]:
    """Adapt a live translation result into the ``migration_report`` panel shape.

    Duck-typed against ``observability_migration``'s ``PanelResult`` so the
    verifier package keeps its zero-import contract: only attribute access is
    used (``query_ir``, ``status``, ``reasons``, ``esql_query``,
    ``post_validation_action``, ``title``). This lets the same Layer-9 checks run
    on freshly-translated panels (e.g. from a combinatorial matrix) without
    serializing to disk first.
    """
    query_ir = getattr(panel_result, "query_ir", None) or {}
    if hasattr(query_ir, "to_dict"):
        query_ir = query_ir.to_dict()
    if not isinstance(query_ir, dict):
        query_ir = {}
    title = getattr(panel_result, "title", "") or (yaml_panel or {}).get("title", "")

    # Prefer the pipeline-associated visual_ir: the emitter reorders/expands
    # panels, so an externally-zipped yaml panel can belong to a different panel.
    # Fall back to the yaml panel only when visual_ir is absent/empty.
    presentation = _presentation_from_visual_ir(getattr(panel_result, "visual_ir", None))
    if not presentation.get("kind"):
        presentation = _presentation_from_yaml(yaml_panel)

    return {
        "title": title,
        "status": str(getattr(panel_result, "status", "") or ""),
        "grafana_type": str(getattr(panel_result, "grafana_type", "") or ""),
        "reasons": list(getattr(panel_result, "reasons", []) or []),
        "post_validation_action": str(getattr(panel_result, "post_validation_action", "") or ""),
        "esql": str(getattr(panel_result, "esql_query", "") or ""),
        "query_ir": query_ir,
        "visual_ir": {"presentation": presentation},
    }


def _presentation_from_visual_ir(visual_ir: Any) -> dict[str, Any]:
    if visual_ir is None:
        return {"kind": "", "config": {}}
    vird = visual_ir.to_dict() if hasattr(visual_ir, "to_dict") else visual_ir
    if not isinstance(vird, dict):
        return {"kind": "", "config": {}}
    pres = vird.get("presentation")
    if isinstance(pres, dict) and isinstance(pres.get("config"), dict):
        return {"kind": str(pres.get("kind") or ""), "config": dict(pres["config"])}
    return {"kind": "", "config": {}}


def lint_translation(
    yaml_panel: dict[str, Any] | None,
    panel_result: Any,
    dashboard_title: str = "",
    *,
    columns_oracle: ColumnsOracle | None = None,
) -> list[Finding]:
    """Run the Layer-9 checks on a live ``(yaml_panel, panel_result)`` pair."""
    panel = report_panel_from_translation(yaml_panel, panel_result)
    return lint_report_panel(panel, dashboard_title, columns_oracle=columns_oracle)


def lint_report(
    report: dict[str, Any],
    *,
    columns_oracle: ColumnsOracle | None = None,
) -> list[Finding]:
    """Lint an in-memory ``migration_report.json`` payload."""
    findings: list[Finding] = []
    for dashboard in report.get("dashboards", []):
        dashboard_title = str(dashboard.get("title") or "")
        for panel in dashboard.get("panels", []):
            if not isinstance(panel, dict):
                continue
            findings.extend(
                lint_report_panel(panel, dashboard_title, columns_oracle=columns_oracle)
            )
    return findings


def lint_migration_output(
    migration_dir: Path,
    *,
    columns_oracle: ColumnsOracle | None = None,
) -> list[Finding]:
    """Lint the ``migration_report.json`` under a migration output directory."""
    report_path = Path(migration_dir) / "migration_report.json"
    report = json.loads(report_path.read_text())
    return lint_report(report, columns_oracle=columns_oracle)


def summarize(findings: list[Finding]) -> dict[str, Any]:
    by_category: dict[str, int] = {c.value: 0 for c in InvariantCategory}
    by_severity: dict[str, int] = {s.value: 0 for s in Severity}
    for f in findings:
        by_category[f.category.value] = by_category.get(f.category.value, 0) + 1
        by_severity[f.severity.value] = by_severity.get(f.severity.value, 0) + 1
    return {
        "total": len(findings),
        "by_category": by_category,
        "by_severity": by_severity,
        "error_count": by_severity.get(Severity.ERROR.value, 0),
    }


__all__ = [
    "ColumnsOracle",
    "Finding",
    "InvariantCategory",
    "Severity",
    "is_time_like",
    "lint_migration_output",
    "lint_report",
    "lint_report_panel",
    "lint_translation",
    "make_es_columns_oracle",
    "referenced_fields",
    "report_panel_from_translation",
    "static_query_columns",
    "summarize",
]
