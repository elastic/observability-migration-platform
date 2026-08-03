# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Human-readable Markdown migration summary.

A single, source-agnostic renderer (`render_markdown`) turns a normalized
``SummaryView`` into a GitHub-friendly Markdown document. Source adapters build
the view from their own result models, so Grafana and Datadog get an identical
layout. The renderer is a pure string function with no I/O.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

# Maximum distinct warning groups shown per dashboard before a "+N more" pointer.
_WARNING_GROUP_CAP = 25
# Source query text is truncated to this many characters in the worklist.
_QUERY_TRUNCATE = 160

# A native-PROMQL panel reuses the source PromQL via ES's ``PROMQL index=…``
# command, so its emitted query begins with this marker.
_NATIVE_PROMQL_PREFIX = "PROMQL index="


class PanelProvenance:
    """Provenance buckets for a migrated panel's emitted query.

    - ``NATIVE``: emitted query is ``PROMQL index=… value=(…)`` — the source
      PromQL run through Elasticsearch's native PROMQL command. These panels are
      numerically oracle-verifiable (Grafana only).
    - ``ESQL``: emitted query is a translated ``FROM … | STATS …`` ES|QL
      pipeline. Structural-only unless separately validated (all Datadog panels
      and some Grafana panels).
    - ``PLACEHOLDER``: the panel was ``not_feasible`` and replaced with a
      "Migration Required" markdown placeholder (no query).
    """

    NATIVE = "native"
    ESQL = "esql"
    PLACEHOLDER = "placeholder"


# Kibana panel types that carry no executable query — pure presentational
# visuals (Grafana text panels, Datadog note/free_text/image/iframe widgets all
# emit a "markdown" panel). These are placeholders, not ES|QL-translated data
# panels, regardless of status (hunt #4).
_NON_DATA_KIBANA_TYPES = frozenset(
    {"markdown", "text", "note", "free_text", "image", "iframe"}
)


def classify_panel_provenance(*, status: str, query: str, query_ir, kibana_type: str = "") -> str:
    """Classify a single panel into a :class:`PanelProvenance` bucket.

    Provenance is derived from data the result models already carry. We prefer
    the structured ``query_ir["family"] == "native_promql"`` marker emitted by
    the planner over string-sniffing, falling back to the ``PROMQL index=``
    query prefix so panels that lack the marker (e.g. older traces) are still
    classified correctly.

    A ``not_feasible`` / ``requires_manual`` / ``skipped`` / ``blocked`` panel is
    always a placeholder, even if a stale query string is present, because each
    ships a markdown placeholder rather than an executable ES|QL query — counting
    them as "ES|QL translated" overstates the migrated surface (PR #234 review).

    A non-data visual (``kibana_type`` markdown/text/image/iframe) is likewise a
    placeholder even when migrated "ok": it carries no executable query.

    A blank ``query`` string alone is NOT treated as a placeholder: some
    successfully-migrated data panels (notably Datadog Lens) carry the executable
    query off this input, so blanking on it mis-classified real ES|QL panels as
    placeholders. Provenance therefore turns on ``status``, ``kibana_type``, and
    the native-PROMQL marker — never on a blank query string.
    """
    if status in ("not_feasible", "requires_manual", "skipped", "blocked"):
        return PanelProvenance.PLACEHOLDER
    # A non-data visual (markdown/text/image/iframe) ships no executable query
    # even when migrated "ok"; it is a placeholder, not an ES|QL data panel.
    if str(kibana_type or "").lower() in _NON_DATA_KIBANA_TYPES:
        return PanelProvenance.PLACEHOLDER
    family = ""
    if isinstance(query_ir, dict):
        family = str(query_ir.get("family", "") or "")
    else:
        family = str(getattr(query_ir, "family", "") or "")
    if family == "native_promql":
        return PanelProvenance.NATIVE
    if str(query or "").lstrip().startswith(_NATIVE_PROMQL_PREFIX):
        return PanelProvenance.NATIVE
    return PanelProvenance.ESQL


@dataclass
class SummaryTotals:
    dashboards: int
    elements_total: int
    migrated: int
    warnings: int
    manual: int
    not_feasible: int
    skipped: int
    green: int
    yellow: int
    red: int
    uploaded_ok: int
    upload_attempted: int
    # Translation-provenance breakdown across all dashboards. Native-PROMQL
    # panels are numerically oracle-verifiable; ES|QL-translated panels are
    # structural-only; placeholders are not_feasible panels.
    native_promql: int = 0
    esql_translated: int = 0
    placeholder: int = 0


@dataclass
class DashboardRow:
    title: str
    elements: int
    migrated: int
    warnings: int
    manual: int
    not_feasible: int
    risk_score: float | None
    rollout_state: str
    # Per-dashboard translation-provenance breakdown (see PanelProvenance).
    native_promql: int = 0
    esql_translated: int = 0
    placeholder: int = 0


@dataclass
class AttentionItem:
    dashboard: str
    panel: str
    status: str  # not_feasible | requires_manual | red | warning | blocked
    reasons: list[str] = field(default_factory=list)
    source_query: str = ""


@dataclass
class GapTask:
    category: str  # link | annotation | transformation | alert
    dashboard: str
    item: str
    detail: str
    kibana_alternative: str
    complexity: str = ""


@dataclass
class GapSummary:
    links: dict = field(default_factory=dict)
    annotations: dict = field(default_factory=dict)
    transformations: dict = field(default_factory=dict)
    alerts: dict = field(default_factory=dict)
    tasks: list[GapTask] = field(default_factory=list)


@dataclass
class SummaryView:
    source: str
    element_noun: str
    run_id: str
    timestamp: float
    totals: SummaryTotals
    dashboards: list[DashboardRow] = field(default_factory=list)
    attention: list[AttentionItem] = field(default_factory=list)
    warnings: list[AttentionItem] = field(default_factory=list)
    gaps: GapSummary = field(default_factory=GapSummary)


def _pct(n: int, total: int) -> str:
    return f"{n / total * 100:.1f}%" if total > 0 else "0%"


def _source_label(source: str) -> str:
    return {"grafana": "Grafana", "datadog": "Datadog"}.get(source, source.title() or "Source")


def _cell(text: str) -> str:
    """Escape a value so it is safe inside a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def _inline(text: str) -> str:
    """Sanitize a value for inline list/prose context (newlines only).

    Unlike table cells, list items do not treat ``|`` as a delimiter, so we
    leave pipes intact (e.g. "ES|QL" should read naturally).
    """
    return str(text).replace("\n", " ")


def _code(text: str) -> str:
    """Render text as an inline code span, neutralizing backticks."""
    return "`" + str(text).replace("`", "ʼ") + "`"


def _truncate(text: str, limit: int = _QUERY_TRUNCATE) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _verdict(totals: SummaryTotals) -> str:
    if totals.not_feasible or totals.manual or totals.red:
        return "⚠️"
    return "✅"


def _plural(noun: str, n: int) -> str:
    return noun if n == 1 else noun + "s"


def render_markdown(view: SummaryView) -> str:
    t = view.totals
    noun = view.element_noun or "panel"
    lines: list[str] = []

    # 1. Title + verdict
    lines.append(f"# Migration Summary — {_source_label(view.source)} → Kibana")
    when = datetime.fromtimestamp(view.timestamp, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    run_bit = f"`{view.run_id}` · " if view.run_id else ""
    lines.append(
        f"**Run** {run_bit}{when} · {t.dashboards} "
        f"{_plural('dashboard', t.dashboards)}"
    )
    lines.append("")
    verdict = _verdict(t)
    if verdict == "⚠️":
        lines.append(
            f"> {verdict} **Review recommended** — {t.not_feasible} not-feasible, "
            f"{t.red} Red, {t.warnings} with warnings."
        )
    else:
        lines.append(f"> {verdict} **Clean** — all {t.elements_total} {_plural(noun, t.elements_total)} migrated.")
    lines.append("")

    # 2. Scorecard
    lines.append("## Scorecard")
    lines.append("")
    lines.append(f"| Outcome | {noun.title()}s | % |")
    lines.append("|---|--:|--:|")
    lines.append(f"| ✓ Migrated | {t.migrated} | {_pct(t.migrated, t.elements_total)} |")
    lines.append(f"| ⚠ With warnings | {t.warnings} | {_pct(t.warnings, t.elements_total)} |")
    lines.append(f"| ? Requires manual | {t.manual} | {_pct(t.manual, t.elements_total)} |")
    lines.append(f"| ✗ Not feasible | {t.not_feasible} | {_pct(t.not_feasible, t.elements_total)} |")
    if t.skipped:
        lines.append(f"| Skipped | {t.skipped} | {_pct(t.skipped, t.elements_total)} |")
    if t.green or t.yellow or t.red:
        lines.append(f"| Verification | {t.green} 🟢 / {t.yellow} 🟡 / {t.red} 🔴 | |")
    lines.append("")

    # 2b. Translation provenance
    lines.extend(_render_provenance(view))

    # 3. Per-dashboard table
    lines.extend(_render_dashboard_table(view))

    # 4. Must-fix worklist
    lines.extend(_render_attention(view))

    # 5. Warnings
    lines.extend(_render_warnings(view))

    # 6. Non-panel gaps
    lines.extend(_render_gaps(view))

    # Footer
    lines.append("---")
    lines.append("_Full per-" + noun + " detail: `migration_report.json`._")
    lines.append("")

    return "\n".join(lines)


def _has_risk(view: SummaryView) -> bool:
    return any(d.risk_score is not None for d in view.dashboards)


def _render_dashboard_table(view: SummaryView) -> list[str]:
    if not view.dashboards:
        return []
    show_risk = _has_risk(view)
    rows = list(view.dashboards)
    if show_risk:
        rows.sort(key=lambda d: -(d.risk_score or 0))
    else:
        rows.sort(key=lambda d: -(d.not_feasible + d.manual))
    out = ["## Dashboards", ""]
    header = "| Dashboard | " + view.element_noun.title() + "s | ✓ | ⚠ | ? | ✗ |"
    sep = "|---|--:|--:|--:|--:|--:|"
    if show_risk:
        header += " Risk |"
        sep += "--:|"
    out.append(header)
    out.append(sep)
    for d in rows:
        row = (
            f"| {_cell(d.title)} | {d.elements} | {d.migrated} | {d.warnings} | "
            f"{d.manual} | {d.not_feasible} |"
        )
        if show_risk:
            row += f" {int(d.risk_score or 0)} |"
        out.append(row)
    out.append("")
    return out


def _render_provenance(view: SummaryView) -> list[str]:
    """Render the translation-provenance breakdown.

    Shows, per dashboard and in total, how the emitted queries split across
    native-PROMQL / ES|QL-translated / placeholder, plus a note clarifying which
    are numerically verifiable. Rendered identically for Grafana and Datadog.
    """
    t = view.totals
    noun = view.element_noun or "panel"
    native = t.native_promql
    esql = t.esql_translated
    placeholder = t.placeholder
    classified = native + esql + placeholder
    # Nothing to show if no panels were classified (e.g. an empty run).
    if classified <= 0:
        return []

    out = ["## Translation provenance", ""]
    out.append(f"| Provenance | {noun.title()}s | % | Numeric verifiability |")
    out.append("|---|--:|--:|:--|")
    out.append(
        f"| Native PROMQL | {native} | {_pct(native, classified)} | "
        "Oracle-verifiable (native-PROMQL oracle) |"
    )
    out.append(
        f"| ES\\|QL translated | {esql} | {_pct(esql, classified)} | "
        "Structural-only unless separately validated |"
    )
    out.append(
        f"| Placeholder (not feasible / manual) | {placeholder} | {_pct(placeholder, classified)} | "
        "Not migrated (manual rebuild) or a non-query visual (text/markdown) |"
    )
    out.append("")

    # Top-level one-line total so the split is grep-able at a glance.
    out.append(
        f"**Total:** {native} native PROMQL · {esql} ES\\|QL translated · "
        f"{placeholder} placeholder (of {classified} {_plural(noun, classified)})."
    )
    out.append("")

    # Verifiability note — calls out the all-ES|QL (e.g. Datadog) case.
    if native > 0:
        out.append(
            "> Native-PROMQL panels are **numerically verifiable** via the "
            "native-PROMQL oracle (source PromQL replayed in Elasticsearch). "
            "ES\\|QL-translated panels are **structural-only** unless separately "
            "validated against source data."
        )
    else:
        out.append(
            "> This run has **0 native** PROMQL panels — every migrated query is "
            "ES\\|QL-translated, which is **structural-only** unless separately "
            "validated against source data. (Native-PROMQL verification applies "
            "to Grafana/Prometheus sources only.)"
        )
    out.append("")

    # Per-dashboard breakdown — only when there is more than one dashboard, to
    # avoid duplicating the single-dashboard total table above.
    if len(view.dashboards) > 1:
        out.append("| Dashboard | Native | ES\\|QL | Placeholder |")
        out.append("|---|--:|--:|--:|")
        for d in view.dashboards:
            out.append(
                f"| {_cell(d.title)} | {d.native_promql} | "
                f"{d.esql_translated} | {d.placeholder} |"
            )
        out.append("")
    return out


def _render_attention(view: SummaryView) -> list[str]:
    if not view.attention:
        return []
    out = ["## 🔴 Must-fix worklist", ""]
    by_dash: dict[str, list[AttentionItem]] = {}
    for item in view.attention:
        by_dash.setdefault(item.dashboard, []).append(item)
    badge = {
        "not_feasible": "✗",
        "requires_manual": "?",
        "red": "🔴",
        "blocked": "⛔",
    }
    for dash, items in by_dash.items():
        out.append(f"### {_inline(dash)}")
        for item in items:
            reason = "; ".join(item.reasons) if item.reasons else "needs manual review"
            out.append(f"- **{badge.get(item.status, '•')} {_inline(item.panel)}** — {_inline(reason)}")
            if item.source_query:
                out.append(f"  {_code(_truncate(item.source_query))}")
        out.append("")
    return out


def _render_warnings(view: SummaryView) -> list[str]:
    if not view.warnings:
        return []
    out = ["## ⚠ Warnings", ""]
    by_dash: dict[str, list[AttentionItem]] = {}
    for item in view.warnings:
        by_dash.setdefault(item.dashboard, []).append(item)
    for dash, items in by_dash.items():
        groups: Counter = Counter()
        for item in items:
            reason = item.reasons[0] if item.reasons else "warning"
            groups[reason] += 1
        out.append(f"<details><summary>{_inline(dash)} — {len(items)} warnings</summary>")
        out.append("")
        for reason, count in groups.most_common(_WARNING_GROUP_CAP):
            suffix = f" ×{count}" if count > 1 else ""
            out.append(f"- {_inline(reason)}{suffix}")
        extra = len(groups) - _WARNING_GROUP_CAP
        if extra > 0:
            out.append(f"- _+{extra} more — see `migration_report.json`_")
        out.append("")
        out.append("</details>")
        out.append("")
    return out


def _render_gaps(view: SummaryView) -> list[str]:
    if not view.gaps.tasks:
        return []
    out = ["## 🔌 Non-panel gaps", ""]
    by_cat: dict[str, list[GapTask]] = {}
    for task in view.gaps.tasks:
        by_cat.setdefault(task.category, []).append(task)
    titles = {
        "transformation": "Transformations",
        "link": "Links",
        "annotation": "Annotations",
        "alert": "Alerts",
    }
    for cat, tasks in by_cat.items():
        out.append(f"### {titles.get(cat, cat.title())} ({len(tasks)})")
        for task in tasks:
            cx = f" _({task.complexity})_" if task.complexity else ""
            alt = f" → {_inline(task.kibana_alternative)}" if task.kibana_alternative else ""
            where = f"**{_inline(task.dashboard)}**" if task.dashboard else ""
            item = f" → *{_inline(task.item)}*" if task.item else ""
            out.append(f"- {where}{item}: {_inline(task.detail)}{alt}{cx}")
        out.append("")
    return out


def save_markdown_summary(view: SummaryView, output_path) -> None:
    """Render ``view`` and write the Markdown document to ``output_path``."""
    from pathlib import Path

    Path(output_path).write_text(render_markdown(view), encoding="utf-8")


__all__ = [
    "AttentionItem",
    "DashboardRow",
    "GapSummary",
    "GapTask",
    "PanelProvenance",
    "SummaryTotals",
    "SummaryView",
    "classify_panel_provenance",
    "render_markdown",
    "save_markdown_summary",
]
