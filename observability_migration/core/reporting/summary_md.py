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
    compiled_ok: int
    compiled_total: int
    uploaded_ok: int
    upload_attempted: int


@dataclass
class DashboardRow:
    title: str
    elements: int
    migrated: int
    warnings: int
    manual: int
    not_feasible: int
    compiled: bool | None
    compile_error: str
    risk_score: float | None
    rollout_state: str


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
    if totals.compiled_ok < totals.compiled_total:
        return "❌"
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
        f"{_plural('dashboard', t.dashboards)} · {t.compiled_ok}/{t.compiled_total} compiled"
    )
    lines.append("")
    verdict = _verdict(t)
    if verdict == "❌":
        lines.append(f"> {verdict} **Blocking errors** — {t.compiled_total - t.compiled_ok} failed to compile.")
    elif verdict == "⚠️":
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
    header = "| Dashboard | " + view.element_noun.title() + "s | ✓ | ⚠ | ? | ✗ | Compiled |"
    sep = "|---|--:|--:|--:|--:|--:|:--:|"
    if show_risk:
        header += " Risk |"
        sep += "--:|"
    out.append(header)
    out.append(sep)
    for d in rows:
        compiled = "✅" if d.compiled else ("❌" if d.compile_error else "—")
        row = (
            f"| {_cell(d.title)} | {d.elements} | {d.migrated} | {d.warnings} | "
            f"{d.manual} | {d.not_feasible} | {compiled} |"
        )
        if show_risk:
            row += f" {int(d.risk_score or 0)} |"
        out.append(row)
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
    "SummaryTotals",
    "SummaryView",
    "render_markdown",
    "save_markdown_summary",
]
