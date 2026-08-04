# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Render-audit classifier — the deterministic core of the live render gate.

The smoke validator already executes each panel's ES|QL (a strong *data* proxy),
but nothing classifies whether the panel actually *rendered* in the browser
instead of showing a Lens "An error occurred" embeddable. This module is the pure
verdict logic for that gate: given a browser DOM/accessibility snapshot (text)
plus the console errors and failed requests collected while the dashboard was
open, it returns a structured pass/warn/fail verdict.

It is intentionally free of any browser driver so it can be unit-tested offline
with synthetic snapshots; the live driver (which opens the canary in an
authenticated Chrome via Chrome DevTools MCP — see the persistent-profile
workflow) feeds it real snapshot text. Source-agnostic: works for migrated
Grafana and Datadog dashboards alike.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

# Reuse the DOM error markers the smoke validator already trusts, so the live
# render gate and the smoke audit never drift apart.
from observability_migration.adapters.source.grafana.smoke import BROWSER_ERROR_PATTERNS

# In-panel render-error markers: the smoke validator's set plus the
# Elasticsearch/ES|QL error surfaces a live Kibana panel shows inline (observed
# on real community dashboards: schema-output drift, unimplemented PromQL
# functions, generic ES verification errors).
_RENDER_ERROR_PATTERNS = (
    *BROWSER_ERROR_PATTERNS,
    r"Unexpected error from Elasticsearch",
    r"verification_exception",
    r"is not yet implemented",
    r"Output has changed from",
    # Kibana's own client-side failures, which never reach Elasticsearch and so
    # carry none of the markers above. A panel whose query cannot even be built
    # shows this card, and the audit scored such a dashboard "pass" -- observed
    # with a native PROMQL panel whose control param Kibana does not forward:
    # "Couldn't parse Elasticsearch ES|QL query ... Parameter [?instance] value
    # not found". An unbound parameter is a construction bug, not data
    # readiness, so these stay hard errors and are never downgraded to a gap.
    r"Couldn't parse Elasticsearch ES\|QL query",
    r"Parameter \[\?[^\]]+\] value not found",
)
_ERROR_RE = [re.compile(p, re.IGNORECASE) for p in _RENDER_ERROR_PATTERNS]

# A render error is only a *field gap* (data-readiness warn) when the panel text
# actually names an absent column/field. A generic render marker
# (embPanel__error) or a translator/ES|QL bug marker (is not yet implemented,
# Output has changed from, a bare verification_exception) must NOT be downgraded
# to field_gap just because a breakdown field happens to be absent — that masked
# real translator bugs as warns (hunt #4).
_FIELD_ABSENCE_RE = re.compile(
    r"Unknown column|unknown field|invalid column|column name or index is invalid",
    re.IGNORECASE,
)
# Explicit ``Unknown column [field.name]`` / ``unknown field [field.name]`` —
# filter fields (not just breakdowns) that ES rejected because they are absent.
_UNKNOWN_COLUMN_NAME_RE = re.compile(
    r"(?:Unknown column|unknown field)\s*\[([^\]]+)\]",
    re.IGNORECASE,
)

# Markers a field gap can NEVER excuse, no matter what else the panel says: the
# panel's query was mis-constructed (unimplemented translation, schema-output
# drift, a query Kibana could not even build, an unbound control parameter). One
# real defect is not excused by accompanying field gaps, so these are checked
# before any field-absence evidence is considered.
_CONSTRUCTION_BUG_PATTERNS = (
    r"is not yet implemented",
    r"Output has changed from",
    r"Couldn't parse Elasticsearch ES\|QL query",
    r"Parameter \[\?[^\]]+\] value not found",
)
_CONSTRUCTION_BUG_RE = [
    re.compile(p, re.IGNORECASE) for p in _CONSTRUCTION_BUG_PATTERNS
]

# Markers that merely *frame* an in-panel error: the Lens error container and the
# generic Elasticsearch wrapper always accompany a field-absence error, so their
# presence alone cannot disqualify field-absence evidence. Any OTHER marker
# alongside a verification_exception is a second, distinct failure mode (a Lens
# accessor bug, a missing data view) and keeps the panel a hard render_error.
_ERROR_FRAME_PATTERNS = frozenset({
    r"dashboardPanelError",
    r"embPanel__error",
    r"An error occurred while loading this panel",
    r"Error loading data",
    r"Unexpected error from Elasticsearch",
    r"verification_exception",
})

# Elasticsearch reports an ES|QL ``verification_exception`` as an enumerated
# problem list, e.g.::
#
#     Unexpected error from Elasticsearch: verification_exception - Found 2 problems
#     line 3:22: Unknown column [mysql_net_connections]
#     line 3:61: Unknown column [mysql_net_max_connections]
#
# That same exception also wraps genuine translator defects (syntax errors, type
# mismatches, unsupported functions), so the marker alone says nothing about
# whether the panel is a data-readiness gap or a bug. The parse below reads the
# problem list so the verdict rests on evidence instead of the marker.
_VERIFICATION_RE = re.compile(r"verification_exception", re.IGNORECASE)
_PROBLEM_COUNT_RE = re.compile(r"Found\s+(\d+)\s+problems?", re.IGNORECASE)
# One problem: ``line <l>:<c>: <message>``. The message ends at the next problem,
# at an HTML tag (Kibana renders one ``<span>`` per line in a EUI code block), or
# at the end of the line — so trailing DOM chrome never leaks into it.
_PROBLEM_MSG_RE = re.compile(
    r"line\s+\d+:\d+:[ \t]*(.*?)(?=[ \t]*line\s+\d+:\d+:|<|$)",
    re.MULTILINE,
)
# A problem that is *purely* a field-absence complaint, optionally carrying
# Elasticsearch's "did you mean" suggestion. Anchored: any extra content (a
# syntax error, a type mismatch) fails the match and keeps the panel a hard
# render_error.
_ONLY_FIELD_ABSENCE_RE = re.compile(
    r"^(?:Unknown column|unknown field)\s*\[([^\]]+)\]"
    r"(?:\s*,?\s*did you mean.*)?$",
    re.IGNORECASE,
)


@dataclass
class _AbsenceEvidence:
    """What a ``verification_exception``'s problem list proves about field absence.

    ``columns`` are every column the exception named. ``exclusive`` is True only
    when every reported problem was read AND every one of them was a pure
    unknown-column/unknown-field complaint; ``reason`` explains why not.
    """
    columns: list[str] = field(default_factory=list)
    exclusive: bool = False
    reason: str = ""


def _construction_bug_marker(text: str) -> str:
    """The first construction-bug marker in ``text`` (empty when there is none)."""
    for pattern, compiled in zip(_CONSTRUCTION_BUG_PATTERNS, _CONSTRUCTION_BUG_RE, strict=True):
        if compiled.search(text):
            return pattern
    return ""


def _verification_absence_evidence(text: str) -> _AbsenceEvidence | None:
    """Read a ``verification_exception``'s problem list as field-absence evidence.

    Returns ``None`` when ``text`` carries no ``verification_exception`` (the
    caller then falls back to the marker-level heuristics). Otherwise every
    exception block in the text is parsed independently — Kibana renders the same
    error block more than once per panel — and the evidence is ``exclusive`` only
    if *all* blocks are fully accounted for and name nothing but absent columns.
    """
    starts = [m.start() for m in _VERIFICATION_RE.finditer(text)]
    if not starts:
        return None
    columns: list[str] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        block = text[start:end]
        count = _PROBLEM_COUNT_RE.search(block)
        if not count:
            return _AbsenceEvidence(
                reason="verification_exception without an enumerable problem list"
            )
        declared = int(count.group(1))
        problems = [
            m.group(1).strip()
            for m in _PROBLEM_MSG_RE.finditer(block, count.end())
        ][:declared]
        if len(problems) < declared:
            return _AbsenceEvidence(
                reason=f"only {len(problems)} of {declared} reported problem(s) could be read"
            )
        for problem in problems:
            named = _ONLY_FIELD_ABSENCE_RE.match(problem)
            if not named:
                return _AbsenceEvidence(
                    reason=f"problem is not a field absence: {problem[:120]}"
                )
            columns.append(named.group(1))
    columns = list(dict.fromkeys(columns))
    if not columns:
        return _AbsenceEvidence(reason="verification_exception named no column")
    return _AbsenceEvidence(columns=columns, exclusive=True)

# Console signatures that indicate a panel/query/render failure — specific enough
# to exclude benign platform noise. A bare "kibana" keyword is intentionally NOT
# used: it matches CSP violations referencing ``kibana.estccdn.com`` and other
# non-render noise (false positives), while missing render errors like the bare
# "Provided column name or index is invalid" (false negative). We instead reuse
# the DOM render-error markers plus explicit ES|QL / Lens error signatures.
_CONSOLE_ERROR_SIGNATURES = (
    *BROWSER_ERROR_PATTERNS,
    r"\[ES\|QL\]",
    r"\bES\|QL\b[^\n]*error",
    r"verification_exception",
    r"parsing_exception",
    r"Lens[^\n]*(?:error|failed)",
)
_CONSOLE_ERROR_RE = [re.compile(p, re.IGNORECASE) for p in _CONSOLE_ERROR_SIGNATURES]


@dataclass
class PanelRenderResult:
    """Per-panel render verdict.

    ``status``: ``rendered`` | ``error`` | ``empty``.
    ``error_class`` (when status==error): ``field_gap`` (the panel references a
    breakdown/group field absent from the target data — a data/field-mapping
    readiness gap, NOT a translation bug, mirroring live_validate's data_gap) vs
    ``render_error`` (an unexplained Lens/ES|QL failure — a real bug).

    ``field_gap`` is *evidence-based*, never marker-based. An Elasticsearch
    ``verification_exception`` wraps both pure field absence and genuine
    translator defects, so it is a ``field_gap`` only when every problem it
    reports is an unknown-column/unknown-field complaint AND every column it
    names is confirmed absent from the target's field caps; ``missing_fields``
    then lists those columns. Mixed content (one syntax/type problem among field
    gaps), an unreadable problem list, a column that does exist, or absent field
    caps all keep the panel a ``render_error`` — with ``detail`` recording why,
    including when absence could not be confirmed.

    ``data_gap`` (an *empty* panel whose metric column is confirmed absent) is
    held to the same standard: no attributable metric, or no field caps to check
    it against, leaves the panel in the stricter ``unexpected_empty`` with
    ``detail`` saying which evidence was missing. "We don't know why this is
    empty" is a weaker claim than "your target has no such metric", so it is
    what we report when we do not know.
    """
    title: str
    status: str
    error_class: str = ""
    missing_fields: list[str] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "status": self.status,
            "error_class": self.error_class,
            "missing_fields": self.missing_fields,
            "detail": self.detail,
        }


@dataclass
class RenderVerdict:
    status: str = "pass"  # "pass" | "warn" | "fail"
    rendered_error_markers: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    server_errors: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    panels: list[PanelRenderResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "rendered_error_markers": self.rendered_error_markers,
            "console_errors": self.console_errors,
            "server_errors": self.server_errors,
            "reasons": self.reasons,
            "panels": [p.to_dict() for p in self.panels],
        }


_EMPTY_STATE_RE = re.compile(r"No results found|No results|^\s*N/?A\s*$", re.IGNORECASE)

# Element extraction patterns (from the Kibana dashboard a11y snapshot). Kibana
# renders the type as a standalone "<word> chart" token ("line chart",
# "sunburst chart", "Bullet chart"); "Chart type" sits on a separate a11y line,
# so match the value token directly via an allowlist.
_CHART_TYPE_RE = re.compile(
    r"\b(line|bar|area|sunburst|pie|donut|treemap|mosaic|waffle|heatmap|heat map|bullet)\s+chart\b",
    re.IGNORECASE,
)
# Legend items render as "<name>; Click: to show, ... + Click: to hide".
_LEGEND_ITEM_RE = re.compile(r'"?([^";]+?); Click: to show')
_LOADING_RE = re.compile(r'progressbar "Loading"', re.IGNORECASE)
_GRID_RE = re.compile(
    r'grid "|columnheader |gridcell |role="grid"|euiDataGrid|lnsDataTable',
    re.IGNORECASE,
)
_MARKDOWN_RE = re.compile(r"markdownVis|markdownBody|kbnMarkdown__body", re.IGNORECASE)
# A metric value renders as a quoted number StaticText, e.g. ``"6.983"``.
_QUOTED_NUMBER_RE = re.compile(r'"\s*[-+]?[\d,]+(?:\.\d+)?\s*%?\s*"')

# Map the rendered chart-type word to a normalized kind.
_CHART_KIND = {
    "line": "xy", "bar": "xy", "area": "xy",
    "bullet": "gauge", "gauge": "gauge",
    "sunburst": "partition", "pie": "partition", "donut": "partition",
    "treemap": "treemap", "mosaic": "mosaic", "waffle": "waffle",
    "heat": "heatmap", "heatmap": "heatmap",
}


@dataclass
class PanelElements:
    """Observable rendered elements of one panel, extracted from the a11y/DOM
    snapshot. Lets the audit assert *how* a panel drew, not just that it didn't
    error: chart kind, legend series, whether data is present, still loading."""
    title: str
    status: str  # rendered | error | empty | loading
    chart_type: str = ""        # rendered word: line/bar/sunburst/bullet/...
    chart_kind: str = ""        # normalized: xy/gauge/partition/heatmap/metric/datatable/...
    legend_entries: list[str] = field(default_factory=list)
    has_data: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title, "status": self.status, "chart_type": self.chart_type,
            "chart_kind": self.chart_kind, "legend_entries": self.legend_entries,
            "has_data": self.has_data, "detail": self.detail,
        }


def extract_panel_elements(title: str, panel_text: str) -> PanelElements:
    """Parse one panel's rendered region into its observable elements."""
    text = str(panel_text or "")
    markers = find_render_error_markers(text)
    if markers:
        return PanelElements(title=title, status="error", detail=markers[0])
    if _LOADING_RE.search(text) and not _CHART_TYPE_RE.search(text):
        return PanelElements(title=title, status="loading", detail="still loading")

    legend = list(dict.fromkeys(m.strip() for m in _LEGEND_ITEM_RE.findall(text) if m.strip()))
    chart_word = ""
    m = _CHART_TYPE_RE.search(text)
    if m:
        chart_word = m.group(1).strip().lower().replace(" ", "")  # "heat map" -> "heatmap"
    kind = _CHART_KIND.get(chart_word, "")

    is_grid = bool(_GRID_RE.search(text))
    is_markdown = bool(_MARKDOWN_RE.search(text))
    has_metric_value = bool(_QUOTED_NUMBER_RE.search(text))

    if _EMPTY_STATE_RE.search(text.strip()) and not (legend or is_grid or chart_word):
        return PanelElements(title=title, status="empty", detail="no data")

    if not kind:
        if is_markdown:
            kind, chart_word = "markdown", "markdown"
        elif is_grid:
            kind, chart_word = "datatable", "table"
        elif has_metric_value:
            kind, chart_word = "metric", "metric"
    has_data = bool(legend) or is_grid or is_markdown or has_metric_value or kind in ("xy", "partition", "heatmap", "gauge")
    return PanelElements(
        title=title, status="rendered", chart_type=chart_word, chart_kind=kind,
        legend_entries=legend, has_data=has_data,
    )


def check_panel_elements(
    elements: PanelElements,
    *,
    expected_kind: str = "",
    expects_breakdown: bool = False,
) -> list[str]:
    """Element-level findings for one panel (empty == all elements correct).

    Flags: not rendered; rendered chart kind != expected; a breakdown panel with
    no legend series; a chart that rendered no data.
    """
    findings: list[str] = []
    if elements.status != "rendered":
        findings.append(f"{elements.title}: {elements.status}" + (f" ({elements.detail})" if elements.detail else ""))
        return findings
    if expected_kind and elements.chart_kind and elements.chart_kind != expected_kind:
        findings.append(f"{elements.title}: rendered as {elements.chart_kind}, expected {expected_kind}")
    # Legend is the series indicator only for xy/heatmap; partition charts show
    # series as slices/rows and gauges/metrics are single-value, so the legend
    # check would false-positive on them.
    if expects_breakdown and elements.chart_kind in ("xy", "heatmap") and not elements.legend_entries:
        findings.append(f"{elements.title}: breakdown panel has no legend series")
    if not elements.has_data:
        findings.append(f"{elements.title}: rendered but shows no data")
    return findings


_CONTROL_WARNING_RE = re.compile(
    r'combobox "([^"]+)"[^\n]*?Incompatible selections \((\d+)\)', re.IGNORECASE
)


def detect_control_warnings(snapshot_text: str) -> list[str]:
    """Detect dashboard controls that render an "Incompatible selections" warning.

    A migrated template-variable control whose options query returns nothing
    matching the current selection shows ``Incompatible selections (N)`` in
    Kibana — the panels it filters then silently return no data. The render audit
    flags these as data-readiness warnings (field-mapping / unseeded label
    values), distinct from a panel render error.
    """
    out: list[str] = []
    for name, count in _CONTROL_WARNING_RE.findall(str(snapshot_text or "")):
        out.append(f"control '{name}': incompatible selections ({count})")
    return out


def segment_panels(snapshot_text: str, titles: Iterable[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """Split a full dashboard snapshot into per-panel ``(title, chunk)`` pairs.

    A chunk runs from a panel's title to the next found title. Titles not present
    in the snapshot are returned separately (a panel whose title did not render —
    worth surfacing, since it often means the panel itself did not load, though
    some panels legitimately hide their title). Title-based and therefore
    best-effort; the element checks degrade gracefully on a missing chunk.

    ``titles`` may REPEAT: one dashboard can carry two same-titled panels
    (Kubernetes ships ``Pods``/``Containers``/``Deployments``/``DaemonSets``
    pairs). Each repeat is matched against the NEXT occurrence in the snapshot,
    so the pair yields two chunks in DOM order rather than two chunks both
    starting at the first occurrence — which would hand the first panel the whole
    region and the second an empty string, reporting a phantom clean render. A
    title that repeats in the report but rendered once is matched once and the
    surplus is returned as unmatched, which is the honest answer: one of the two
    panels did not draw.

    A title that is a strict PREFIX of a sibling title also matches inside the
    sibling's rendered title text, so plain report-order search lets whichever
    panel comes first in the report win. ``_ensure_unique_leaf_panel_titles``
    makes that the normal case rather than an edge one: it disambiguates a
    repeated Datadog widget title by appending ``(widget <id>)``, so every
    duplicated title is by construction a strict prefix of its disambiguated
    sibling (34 such pairs across the 13-dashboard corpus). The short title then
    took a ZERO-LENGTH chunk at the long title's offset — which
    :func:`classify_panel` reads as a clean ``rendered`` panel, so the
    misattribution surfaced as a phantom green record — while its own region was
    absorbed by whichever panel preceded it. Live in Docker - Overview:
    ``Running containers by image`` matched inside ``Running containers by image
    (widget 27)``.

    Segmentation therefore prefers the MOST SPECIFIC match, in two rules:

    * a hit CONTAINED in any occurrence of a longer title is rejected — it is
      part of another panel's title text, not this panel's. Every occurrence
      counts, not just the one that title claimed: the rendered HTML repeats a
      title in the header ``<span>``, the wrapper's ``data-title`` attribute and
      the context-menu button's ``aria-label``, and the Docker bare title's
      second-choice hit landed in the twin's ``data-title``.
    * a hit OVERLAPPING a span another panel already claimed is rejected, so no
      two panels can share a DOM offset — which is what makes a zero-length chunk
      impossible.

    Titles are matched longest-first so the specific one claims its text before
    its prefix is offered a hit at all. A "boundary after the match" rule cannot
    replace this: the disambiguator and the sibling titles are separated by a
    space (``Pods``/``Pods available``), the same character a genuine title
    boundary ends on, so no delimiter set distinguishes them. The competing
    titles are the only reliable boundary evidence, which is what the occurrence
    spans encode. A short title with no occurrence outside its siblings' title
    text is returned as unmatched — the honest answer, and a visible one.

    ``titles`` must be scoped to the dashboard being audited. Passing every title
    of a multi-dashboard migration report lets a stray DOM text match attribute a
    chunk of this dashboard to another dashboard's panel metadata — see
    :func:`scope_report_to_dashboard`.
    """
    text = str(snapshot_text or "")
    # (start offset, arrival order, title) — arrival order only breaks ties so
    # the sort is total and stable for titles found at the same offset.
    positions: list[tuple[int, int, str]] = []
    # (report order, title), so the returned list stays in report order even
    # though the search runs longest-first.
    missing: list[tuple[int, str]] = []
    searched_from: dict[str, int] = {}
    # [start, end) spans already taken by a panel: no two panels share offsets.
    claimed: list[tuple[int, int]] = []
    # [start, end) spans of EVERY occurrence of a strictly longer title. A hit
    # inside one of these is that title's rendered text, not this panel's.
    longer_title_spans: list[tuple[int, int]] = []

    by_length: dict[int, list[tuple[int, str]]] = {}
    occurrences: dict[str, list[int]] = {}
    for order, title in enumerate(titles):
        if not title:
            continue
        by_length.setdefault(len(title), []).append((order, title))
        if title not in occurrences:
            hits: list[int] = []
            at = text.find(title)
            while at >= 0:
                hits.append(at)
                at = text.find(title, at + 1)
            occurrences[title] = hits

    def _blocking_end(start: int, end: int) -> int:
        """End of a span that forbids ``[start, end)``, else -1.

        Returned so the search resumes past the blocker instead of one character
        on. Containment for a longer title's text (the hit belongs to that
        title); overlap for a claimed span (two panels cannot share offsets).
        """
        for span_start, span_end in longer_title_spans:
            if span_start <= start and end <= span_end:
                return span_end
        for span_start, span_end in claimed:
            if span_start < end and span_end > start:
                return span_end
        return -1

    # Longest first, report order within a length. Equal-length titles cannot
    # contain one another, so a group never blocks its own members — which keeps
    # two same-titled panels resolving against successive DOM occurrences.
    for length in sorted(by_length, reverse=True):
        group = sorted(by_length[length])
        for order, title in group:
            pos = searched_from.get(title, 0)
            while True:
                idx = text.find(title, pos)
                if idx < 0:
                    missing.append((order, title))
                    break
                blocked_until = _blocking_end(idx, idx + length)
                if blocked_until < 0:
                    # Resume past this hit so a repeated title advances instead
                    # of matching the same offset again.
                    searched_from[title] = idx + length
                    claimed.append((idx, idx + length))
                    positions.append((idx, order, title))
                    break
                pos = max(idx + 1, blocked_until)
        for _order, title in group:
            longer_title_spans += [(at, at + length) for at in occurrences[title]]
    positions.sort()
    segments: list[tuple[str, str]] = []
    for i, (start, _order, title) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        segments.append((title, text[start:end]))
    return segments, [title for _order, title in sorted(missing)]


def _panel_target_fields(
    title: str,
    target_fields_by_title: dict[str, set[str] | None] | None,
    fallback: Iterable[str] | None,
) -> Iterable[str] | None:
    """The field set to judge ``title`` against: its own index's, else ``fallback``.

    A dashboard mixes index patterns (``metrics-*`` panels beside ``FROM logs-*``
    ones), and a column's presence is only meaningful in the index the panel
    actually reads. A panel with a per-panel entry uses it — including a ``None``
    entry, which means that index's caps could not be read and so keeps the panel
    in the stricter class. Panels with no entry (a markdown panel, or a query
    whose source command names no index) fall back to the dashboard-wide set.
    """
    if target_fields_by_title is not None and title in target_fields_by_title:
        return target_fields_by_title[title]
    return fallback


def audit_dashboard_elements(
    snapshot_text: str,
    *,
    expected_kind_by_title: dict[str, str] | None = None,
    breakdown_titles: Iterable[str] | None = None,
    breakdown_by_title: dict[str, list[str]] | None = None,
    available_fields: Iterable[str] | None = None,
    expects_data_titles: Iterable[str] | None = None,
    metrics_by_title: dict[str, list[str]] | None = None,
    target_fields_by_title: dict[str, set[str] | None] | None = None,
    panel_titles: Iterable[str] | None = None,
) -> RenderVerdict:
    """Per-panel element audit of a whole-dashboard snapshot.

    Segments by the expected titles, extracts elements, and flags per-panel
    issues (wrong chart kind, missing legend series on xy/heatmap, no data).
    Element findings are ``warn`` (a render that drew the wrong thing or no data
    is a review signal, not a hard ES|QL/Lens failure — those are caught by
    classify_render). Titles whose chunk never rendered are also a warn.

    An errored/empty panel's ``error_class`` comes from :func:`classify_panel`, so
    this audit and the per-panel render audit share ONE classification contract:
    it too needs ``available_fields`` (target field caps) to confirm a field
    absence, and without them an error stays a hard ``render_error``. This section
    used to stamp every errored panel ``render_error`` with an empty
    ``missing_fields``, which reported a fully-explained data-readiness gap as a
    translator bug even when field caps were supplied. ``metrics_by_title`` and
    ``target_fields_by_title`` carry the rest of that contract — the metric a
    panel reads, and the field caps of the index it reads them from.

    ``panel_titles`` is the segmentation matcher, defaulting to
    ``expected_kind_by_title``'s keys. Callers holding a migration report pass
    :func:`panel_titles_in_order` instead, which keeps a dashboard's duplicate
    titles — a dict cannot, so one of each same-titled pair would be audited in the
    per-panel render section but not here, and the two sections would disagree on
    how many panels the dashboard has.
    """
    expected_kind_by_title = expected_kind_by_title or {}
    breakdown_by_title = breakdown_by_title or {}
    metrics_by_title = metrics_by_title or {}
    breakdown = set(breakdown_titles) if breakdown_titles is not None else set(breakdown_by_title)
    segments, unmatched = segment_panels(
        snapshot_text,
        expected_kind_by_title if panel_titles is None else panel_titles,
    )
    verdict = RenderVerdict()
    reasons: list[str] = []
    for title, chunk in segments:
        el = extract_panel_elements(title, chunk)
        if el.status in ("error", "empty"):
            # Unknown query-bearingness (no ``expects_data_titles``) must not
            # weaken the signal, so assume the panel expects data.
            panel_fields = _panel_target_fields(
                title, target_fields_by_title, available_fields
            )
            classified = classify_panel(
                title, chunk,
                breakdown_fields=breakdown_by_title.get(title, []),
                available_fields=panel_fields,
                expects_data=(
                    True if expects_data_titles is None else title in set(expects_data_titles)
                ),
                referenced_metrics=metrics_by_title.get(title, []),
                available_metrics=panel_fields,
            )
            el.detail = classified.detail or el.detail
            verdict.panels.append(
                PanelRenderResult(
                    title=title, status=el.status,
                    error_class=classified.error_class,
                    missing_fields=classified.missing_fields,
                    detail=classified.detail,
                )
            )
        else:
            verdict.panels.append(
                PanelRenderResult(
                    title=title,
                    status=el.status,
                    # "loading" keeps its historical qualifier; only rendered is clean.
                    error_class="" if el.status == "rendered" else "unexpected_empty",
                    detail=f"{el.chart_kind or '?'}; legend={len(el.legend_entries)}; data={el.has_data}",
                )
            )
        reasons += check_panel_elements(
            el,
            expected_kind=expected_kind_by_title.get(title, ""),
            expects_breakdown=title in breakdown,
        )
    for title in unmatched:
        reasons.append(f"{title}: panel title did not render (panel may not have loaded)")
    reasons += detect_control_warnings(snapshot_text)
    if reasons:
        verdict.status = "warn"
        verdict.reasons = reasons
    return verdict


def find_render_error_markers(snapshot_text: str) -> list[str]:
    """Return the distinct DOM error markers present in a rendered snapshot."""
    text = str(snapshot_text or "")
    hits: list[str] = []
    for pattern, compiled in zip(_RENDER_ERROR_PATTERNS, _ERROR_RE, strict=True):
        if compiled.search(text):
            hits.append(pattern)
    return hits


def count_render_error_markers(snapshot_text: str) -> int:
    """Count render-error marker occurrences in a snapshot.

    ``find_render_error_markers`` intentionally returns distinct pattern names
    for compact reporting. The CLI also needs occurrence counts so a hidden
    broken panel with the same generic marker as a visible field-gap panel cannot
    be swallowed by per-panel attribution.
    """
    text = str(snapshot_text or "")
    return sum(len(list(compiled.finditer(text))) for compiled in _ERROR_RE)


def _filter_console(console_errors: Iterable[str]) -> list[str]:
    return [
        c for c in console_errors
        if any(rx.search(str(c)) for rx in _CONSOLE_ERROR_RE)
    ]


def _server_5xx(failed_requests: Iterable[str]) -> list[str]:
    # Failed-request strings carry the status code as a whitespace-delimited
    # token, e.g. "POST /api/dashboards 503 Service Unavailable". Match a 5xx
    # only in that status position (surrounded by whitespace / line edges) so a
    # 500-599 substring embedded in a URL path, dashboard id, or port is not
    # misread as a server error (PR #234 review).
    return [r for r in failed_requests if re.search(r"(?:^|\s)5\d\d(?:\s|$)", str(r))]


def classify_render(
    snapshot_text: str,
    *,
    console_errors: Iterable[str] = (),
    failed_requests: Iterable[str] = (),
    screenshot_ok: bool = True,
) -> RenderVerdict:
    """Classify a single dashboard's rendered state.

    * ``fail`` — a Lens/embeddable error marker is in the DOM, a Kibana/ES|QL
      console error fired, or a 5xx hit the ES/Kibana API. The panel did not
      render correctly.
    * ``warn`` — no hard error, but the screenshot is missing/empty or some
      non-5xx request failed (degraded, needs a human glance).
    * ``pass`` — clean render.
    """
    verdict = RenderVerdict()

    markers = find_render_error_markers(snapshot_text)
    console = _filter_console(console_errors)
    fivexx = _server_5xx(failed_requests)

    verdict.rendered_error_markers = markers
    verdict.console_errors = console
    verdict.server_errors = fivexx

    if markers:
        verdict.status = "fail"
        verdict.reasons.append(f"rendered error markers: {markers}")
    if console:
        verdict.status = "fail"
        verdict.reasons.append(f"console errors: {len(console)}")
    if fivexx:
        verdict.status = "fail"
        verdict.reasons.append(f"server 5xx: {fivexx}")

    if verdict.status != "fail":
        if not screenshot_ok:
            verdict.status = "warn"
            verdict.reasons.append("screenshot missing or empty")

    return verdict


def _no_metric_gap_reason(
    referenced_metrics: list[str], available_metrics: Iterable[str] | None
) -> str:
    """Why an empty panel could NOT be called a ``data_gap``.

    An operator reads ``data_gap`` as "your target has no such metric" and
    ``unexpected_empty`` as "we don't know why this is empty". Silently reporting
    the second when the first was merely unprovable hides which evidence was
    missing, so the missing evidence is named instead — the same discipline that
    keeps an unconfirmable field absence a ``render_error``.
    """
    if available_metrics is None:
        return (
            "target field caps were unavailable, so a metric gap could not be "
            "confirmed or ruled out"
        )
    if not referenced_metrics:
        return (
            "no source metric could be attributed to this panel (e.g. COUNT(*) "
            "reads no column), so a metric gap could not be confirmed"
        )
    return (
        f"referenced metric(s) {referenced_metrics} DO exist in the index this "
        "panel reads, so the emptiness is not a metric gap (check the filter "
        "values and the time window)"
    )


def classify_panel(
    title: str,
    panel_text: str,
    *,
    breakdown_fields: Iterable[str] = (),
    available_fields: Iterable[str] | None = None,
    expects_data: bool = False,
    referenced_metrics: Iterable[str] = (),
    available_metrics: Iterable[str] | None = None,
) -> PanelRenderResult:
    """Classify a single panel's rendered region.

    Render error markers are attributed (``field_gap`` when a breakdown field is
    absent from ``available_fields``, else ``render_error``).

    Empty states ("No results"/"N/A") are split three ways so a query panel that
    silently shows nothing is not mistaken for a benign blank:

    * ``data_gap`` — the panel references a metric absent from ``available_metrics``
      (expected empty; remediate data/mapping).
    * ``unexpected_empty`` — ``expects_data`` (the panel has a query) but it
      rendered nothing despite no known metric gap (a finding: verify
      data/time-window; could be a broken query).
    * benign ``empty`` — no query (e.g. a markdown panel) / no data expected.
    """
    text = str(panel_text or "")
    markers = find_render_error_markers(text)
    if markers:
        # Only a column/field-absence error can be a field_gap. A translator/ES|QL
        # bug marker (or a bare embPanel__error) is a real render_error even when
        # a breakdown field is absent — never downgrade it (hunt #4).
        construction_bug = _construction_bug_marker(text)
        if construction_bug:
            return PanelRenderResult(
                title=title, status="error", error_class="render_error",
                detail=f"{markers[0]}; {construction_bug} is a construction bug, not a data gap",
            )
        evidence = _verification_absence_evidence(text)
        if evidence is not None:
            # A verification_exception wraps both field absence and real defects,
            # so the verdict rests on its parsed problem list — never the marker.
            other = [m for m in markers if m not in _ERROR_FRAME_PATTERNS]
            if other:
                return PanelRenderResult(
                    title=title, status="error", error_class="render_error",
                    detail=f"{markers[0]}; second failure mode alongside the field absence: {other}",
                )
            if not evidence.exclusive:
                return PanelRenderResult(
                    title=title, status="error", error_class="render_error",
                    detail=f"{markers[0]}; {evidence.reason}",
                )
            if available_fields is None:
                # No field caps: absence is unproven. Downgrading here is how a
                # gate stops catching things, so stay hard and say why.
                return PanelRenderResult(
                    title=title, status="error", error_class="render_error",
                    detail=(
                        f"{markers[0]}; names column(s) {evidence.columns} but target "
                        "field caps were unavailable, so absence is unconfirmed"
                    ),
                )
            present = [c for c in evidence.columns if c in set(available_fields)]
            if present:
                return PanelRenderResult(
                    title=title, status="error", error_class="render_error",
                    detail=(
                        f"{markers[0]}; column(s) {present} DO exist in the target, "
                        "so this is a real failure, not a data gap"
                    ),
                )
            return PanelRenderResult(
                title=title, status="error", error_class="field_gap",
                missing_fields=evidence.columns,
                detail=(
                    f"{markers[0]}; field(s) confirmed absent from target: {evidence.columns}"
                ),
            )
        if available_fields is not None and _FIELD_ABSENCE_RE.search(text):
            avail = set(available_fields)
            named_missing = [
                name
                for name in _UNKNOWN_COLUMN_NAME_RE.findall(text)
                if name and name not in avail
            ]
            breakdown_missing = [f for f in breakdown_fields if f and f not in avail]
            missing = list(dict.fromkeys([*named_missing, *breakdown_missing]))
            if missing:
                return PanelRenderResult(
                    title=title, status="error", error_class="field_gap",
                    missing_fields=missing,
                    detail=(
                        f"{markers[0]}; field(s) absent from target: {missing}"
                    ),
                )
        return PanelRenderResult(
            title=title, status="error", error_class="render_error", detail=markers[0]
        )
    if _EMPTY_STATE_RE.search(text.strip()):
        metrics = list(dict.fromkeys(m for m in referenced_metrics if m))
        if metrics and available_metrics is not None:
            missing_metrics = [m for m in metrics if m not in set(available_metrics)]
            if missing_metrics:
                return PanelRenderResult(
                    title=title, status="empty", error_class="data_gap",
                    missing_fields=missing_metrics,
                    detail=f"empty: referenced metric(s) absent from target: {missing_metrics}",
                )
        if expects_data:
            return PanelRenderResult(
                title=title, status="empty", error_class="unexpected_empty",
                detail=(
                    "query panel rendered no data (verify data/time window or query); "
                    + _no_metric_gap_reason(metrics, available_metrics)
                ),
            )
        return PanelRenderResult(
            title=title, status="empty", detail="empty (no query / no data expected)"
        )
    return PanelRenderResult(title=title, status="rendered")


# Per-panel finding classes that warrant a "warn" (not a hard "fail"): they are
# data-readiness / verify-me signals, not translator render bugs.
_WARN_CLASSES = ("field_gap", "data_gap", "unexpected_empty")


def classify_render_per_panel(
    panels: Iterable[tuple[str, str]],
    *,
    breakdown_by_title: dict[str, list[str]] | None = None,
    available_fields: Iterable[str] | None = None,
    expects_data_titles: Iterable[str] | None = None,
    metrics_by_title: dict[str, list[str]] | None = None,
    available_metrics: Iterable[str] | None = None,
    target_fields_by_title: dict[str, set[str] | None] | None = None,
    console_errors: Iterable[str] = (),
    failed_requests: Iterable[str] = (),
) -> RenderVerdict:
    """Per-panel render audit.

    ``panels`` is a list of ``(title, rendered_text)`` segments. Aggregation
    follows live_validate's philosophy: an unexplained ``render_error`` (or a
    render-error console message / 5xx) is a hard ``fail``; data-readiness
    findings — ``field_gap``, ``data_gap``, ``unexpected_empty`` — are ``warn``.

    ``target_fields_by_title`` supplies the field caps of the index *each* panel
    reads (see :func:`source_indices_by_panel`) and, where present, replaces both
    ``available_fields`` and ``available_metrics`` for that panel: both ask the
    one question "does this column exist in the index this panel queries?", and
    answering it from another index is unfounded either way — it can excuse a
    real bug as a gap, or invent a gap for a column that is right there.
    """
    breakdown_by_title = breakdown_by_title or {}
    metrics_by_title = metrics_by_title or {}
    expects = set(expects_data_titles or ())
    verdict = RenderVerdict()
    for title, text in panels:
        panel_fields = _panel_target_fields(title, target_fields_by_title, available_fields)
        result = classify_panel(
            title, text,
            breakdown_fields=breakdown_by_title.get(title, []),
            available_fields=panel_fields,
            expects_data=title in expects,
            referenced_metrics=metrics_by_title.get(title, []),
            available_metrics=_panel_target_fields(
                title, target_fields_by_title, available_metrics
            ),
        )
        verdict.panels.append(result)

    hard = [p for p in verdict.panels if p.status == "error" and p.error_class == "render_error"]
    warns = [p for p in verdict.panels if p.error_class in _WARN_CLASSES]

    verdict.console_errors = _filter_console(console_errors)
    verdict.server_errors = _server_5xx(failed_requests)

    if hard or verdict.console_errors or verdict.server_errors:
        verdict.status = "fail"
        if hard:
            verdict.reasons.append(
                f"{len(hard)} panel(s) with render errors: {[p.title for p in hard]}"
            )
        if verdict.console_errors:
            verdict.reasons.append(f"console errors: {len(verdict.console_errors)}")
        if verdict.server_errors:
            verdict.reasons.append(f"server 5xx: {verdict.server_errors}")
    elif warns:
        verdict.status = "warn"
        verdict.reasons.append(
            "data-readiness findings (not translator render bugs): "
            + str([(p.title, p.error_class, p.missing_fields) for p in warns])
        )

    return verdict


def breakdown_fields_by_panel(report: dict) -> dict[str, list[str]]:
    """Extract each panel's breakdown/group source fields from a migration_report.

    These are the data labels (e.g. ``method``, ``instance``) that must exist in
    the target; synthetic output columns (``value``, ``step``, ``time_bucket``)
    are not data fields and are excluded.
    """
    out: dict[str, list[str]] = {}
    for dashboard in report.get("dashboards", []):
        for panel in dashboard.get("panels", []):
            if not isinstance(panel, dict):
                continue
            title = str(panel.get("title") or "")
            esql = (panel.get("yaml_panel") or {}).get("esql") if isinstance(panel.get("yaml_panel"), dict) else {}
            esql = esql if isinstance(esql, dict) else {}
            fields: list[str] = []
            for key in ("breakdown", "y_axis"):
                value = esql.get(key)
                if isinstance(value, dict) and value.get("field"):
                    fields.append(str(value["field"]))
            for key in ("breakdown", "breakdowns"):
                value = esql.get(key)
                if isinstance(value, list):
                    fields.extend(str(b.get("field")) for b in value if isinstance(b, dict) and b.get("field"))
            if fields:
                out[title] = list(dict.fromkeys(fields))
    return out


# --------------------------------------------------------------------------- #
# What a panel reads: its source index, and the columns it reads from it
# --------------------------------------------------------------------------- #
#
# Both answers come from the panel's own ES|QL, and both feed the classifier:
# the index decides WHICH field caps a column's presence must be judged against,
# and the columns are the metric whose absence makes an empty panel a
# ``data_gap`` instead of an unexplained ``unexpected_empty``.

# An ES|QL identifier: a column name, possibly dotted (``log.level``), starting
# with ``@`` (``@timestamp``), or quoted in backticks.
_ESQL_IDENT = r"(?:`[^`]+`|[A-Za-z_@][\w@.]*)"
_ESQL_IDENT_RE = re.compile(_ESQL_IDENT)
# String literals are stripped before any column parsing, so a service name or a
# KQL fragment inside quotes is never mistaken for a column.
_TRIPLE_QUOTED_RE = re.compile(r'"""(?:.|\n)*?"""')
_QUOTED_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'")

# A query string is only ES|QL if it starts with an ES|QL source command. The
# guard keeps a Datadog/PromQL *source* expression (which also lives in the
# report) from being parsed as if it were the emitted target query.
_ESQL_SOURCE_COMMAND_RE = re.compile(
    r"^(?:FROM|TS|ROW|SHOW|PROMQL|LENS|CONTROL|FILTER|RANGE)\b", re.IGNORECASE
)
# ``FROM <indices>`` / ``TS <indices>``, stopping at the first pipe so only the
# source command is read. Non-``FROM`` commands carry ``index=<pattern>``.
_FROM_CLAUSE_RE = re.compile(r"^(?:FROM|TS)\s+([^|\n]+)", re.IGNORECASE)
_INDEX_OPTION_RE = re.compile(r"\bindex=(\S+)", re.IGNORECASE)
_METADATA_RE = re.compile(r"\bMETADATA\b", re.IGNORECASE)

# Aggregations whose argument is a column read from the index. The alias the
# result is assigned to (``value``, ``count``, ``query1``) is an OUTPUT name that
# exists in no index, so aliases are subtracted below — reporting one as a
# missing metric would send an operator hunting for a column we invented.
_METRIC_AGG_FUNCTIONS = (
    "AVERAGE", "AVG", "AVG_OVER_TIME", "COUNT", "COUNT_DISTINCT",
    "FIRST_OVER_TIME", "INCREASE", "IRATE", "LAST_OVER_TIME", "MAX",
    "MAX_OVER_TIME", "MEDIAN", "MEDIAN_ABSOLUTE_DEVIATION", "MIN",
    "MIN_OVER_TIME", "PERCENTILE", "RATE", "STD_DEV", "SUM", "SUM_OVER_TIME",
    "TOP", "VALUES", "WEIGHTED_AVG",
)
# Alternated longest-first, so ``AVG_OVER_TIME(`` is never read as ``AVG``.
_METRIC_AGG_RE = re.compile(
    rf"\b(?:{'|'.join(sorted(_METRIC_AGG_FUNCTIONS, key=len, reverse=True))})\s*\(",
    re.IGNORECASE,
)
# Tokens that sit where a column would but name none.
_ESQL_RESERVED = frozenset({
    "AND", "AS", "ASC", "BY", "DESC", "FALSE", "IN", "IS", "LIKE", "METADATA",
    "NOT", "NULL", "ON", "OR", "RLIKE", "TRUE", "WHERE", "WITH",
})
# ``<name> =`` (an EVAL/STATS/BY alias) but never a comparison (``==``, ``>=``).
_ALIAS_ASSIGNMENT_RE = re.compile(rf"({_ESQL_IDENT})\s*=(?!=)")
_RENAME_ALIAS_RE = re.compile(rf"\bAS\s+({_ESQL_IDENT})", re.IGNORECASE)
_STATS_COMMAND_RE = re.compile(r"\|\s*(?:INLINE)?STATS\b", re.IGNORECASE)
_KEEP_COMMAND_RE = re.compile(r"\|\s*KEEP\s+([^|\n]+)", re.IGNORECASE)


def _collect_esql_queries(node: object, out: list[str]) -> None:
    """Collect every ES|QL query string under ``node``, in document order.

    Where a panel keeps its query depends on the chart family, exactly as it does
    for a *stored* Kibana panel (see ``_stored_panel_query`` in the verifier's
    collectors): single-series panels hold one query at ``esql.query`` /
    ``data_source.query``, while an ``xy`` panel has no ``data_source`` at the
    config root at all and holds one per layer under ``layers[*]``. Reading only
    the root found nothing for those, and a panel with no query then silently
    fell back to the CLI's default index — so walk the whole shape.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "query" and isinstance(value, str):
                text = value.strip()
                if text and _ESQL_SOURCE_COMMAND_RE.match(text) and text not in out:
                    out.append(text)
                continue
            _collect_esql_queries(value, out)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _collect_esql_queries(item, out)


def panel_esql_queries(panel: dict) -> list[str]:
    """Every ES|QL query one migration-report panel emitted (usually one)."""
    out: list[str] = []
    _collect_esql_queries(panel.get("yaml_panel"), out)
    direct = str(panel.get("esql_query") or "").strip()
    if direct and _ESQL_SOURCE_COMMAND_RE.match(direct) and direct not in out:
        out.append(direct)
    return out


def esql_source_indices(query: str) -> list[str]:
    """The index pattern(s) an ES|QL query reads, named by its source command."""
    first_line = next(
        (line.strip() for line in str(query or "").splitlines() if line.strip()), ""
    )
    match = _FROM_CLAUSE_RE.match(first_line)
    if match:
        clause = _METADATA_RE.split(match.group(1), maxsplit=1)[0]
    else:
        option = _INDEX_OPTION_RE.search(first_line)
        if not option:
            return []
        clause = option.group(1)
    out: list[str] = []
    for part in clause.split(","):
        name = part.strip().strip('"').strip("`")
        if name and name not in out:
            out.append(name)
    return out


def _balanced_argument(text: str, open_paren: int) -> str:
    """The text inside the parentheses opening at ``open_paren``.

    Balanced, so a field after a nested call — ``AVG(CASE((..), node_x, 0))`` —
    is still inside the argument instead of being cut at the first ``)``.
    """
    depth = 0
    for index in range(open_paren, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1:index]
    return text[open_paren + 1:]


def _column_candidates(text: str) -> list[str]:
    """Identifiers in ``text`` that name a column (not a function, param or keyword)."""
    out: list[str] = []
    for match in _ESQL_IDENT_RE.finditer(text):
        if text[max(match.start() - 1, 0):match.start()] == "?":
            continue  # a bound ES|QL parameter (?_tstart, ?instance)
        if text[match.end():].lstrip().startswith("("):
            continue  # a function call, not a column
        name = match.group(0).strip("`")
        if not name or name.upper() in _ESQL_RESERVED or name in out:
            continue
        out.append(name)
    return out


def metric_fields_from_query(query: str) -> list[str]:
    """The index columns an ES|QL query reads as its metric.

    Aggregated columns first (``AVG(redis_keys)`` -> ``redis_keys``), minus every
    EVAL/STATS/RENAME alias. A projection-only panel — a log table with no
    ``STATS`` at all — reads its columns straight from the index, so its ``KEEP``
    list names them.

    Returns ``[]`` when nothing is attributable (``COUNT(*)`` reads no column).
    That is a real answer, not a shrug: an empty list means the audit will not
    claim a metric gap it cannot point at.
    """
    text = _QUOTED_RE.sub('""', _TRIPLE_QUOTED_RE.sub('""', str(query or "")))
    aliases = {m.group(1).strip("`") for m in _ALIAS_ASSIGNMENT_RE.finditer(text)}
    aliases |= {m.group(1).strip("`") for m in _RENAME_ALIAS_RE.finditer(text)}
    fields: list[str] = []
    for match in _METRIC_AGG_RE.finditer(text):
        argument = _balanced_argument(text, match.end() - 1)
        for name in _column_candidates(argument):
            if name not in aliases and name not in fields:
                fields.append(name)
    if fields or _STATS_COMMAND_RE.search(text):
        return fields
    for match in _KEEP_COMMAND_RE.finditer(text):
        for name in _column_candidates(match.group(1)):
            if name not in aliases and name not in fields:
                fields.append(name)
    return fields


def _by_panel_title(
    report: dict, extract: Callable[[dict], list[str]]
) -> dict[str, list[str]]:
    """Apply ``extract`` to every non-skipped report panel, keyed by title.

    Titles are the only join key the render audit has (it segments the DOM by
    them), and two panels can share one. Their results are UNIONED rather than
    overwritten: a union can only make a column look *present*, which keeps a
    render error hard and an empty panel unexplained — the strict direction. An
    arbitrary last-write-wins would instead guess.
    """
    out: dict[str, list[str]] = {}
    for dashboard in report.get("dashboards", []):
        for panel in dashboard.get("panels", []):
            if not isinstance(panel, dict) or panel.get("status") == "skipped":
                continue
            values = extract(panel)
            if not values:
                continue
            title = str(panel.get("title") or "")
            merged = out.setdefault(title, [])
            merged.extend(value for value in values if value not in merged)
    return out


def source_indices_by_panel(report: dict) -> dict[str, list[str]]:
    """Each panel's target index pattern(s), from its own ES|QL source command.

    The render audit used to judge every panel's columns against ONE
    ``--es-index`` pattern. Nine panels in the Datadog corpus are ``FROM logs-*``
    (``Log Events``, ``Consul Logs``, ``Error Logs``, ...), so their columns were
    looked up in ``metrics-*`` — an index that cannot hold them. Every verdict
    that follows from such a lookup is unfounded in both directions: absent
    "proves" a gap that is not there, and the reverse excuses a real bug.
    """
    return _by_panel_title(
        report,
        lambda panel: [
            index
            for query in panel_esql_queries(panel)
            for index in esql_source_indices(query)
        ],
    )


def metric_fields_by_panel(report: dict) -> dict[str, list[str]]:
    """Each panel's metric columns — what an empty panel would be missing.

    Panels with nothing attributable are absent from the result, so the caller
    passes no metric for them and the classifier keeps them ``unexpected_empty``.
    """
    return _by_panel_title(
        report,
        lambda panel: [
            name
            for query in panel_esql_queries(panel)
            for name in metric_fields_from_query(query)
        ],
    )


_RENDER_RANK = {"rendered": 3, "empty": 2, "error": 1}


def render_snapshot(panels: Iterable[PanelRenderResult]) -> dict[str, str]:
    """Serialize per-panel verdicts to a committable baseline: title -> state.

    The state is ``status`` plus the ``error_class`` qualifier (e.g.
    ``error:render_error``, ``empty:unexpected_empty``, ``rendered``) so a
    baseline captures not just pass/fail but the kind of finding.
    """
    out: dict[str, str] = {}
    for panel in panels:
        state = panel.status
        if panel.error_class:
            state = f"{panel.status}:{panel.error_class}"
        out[panel.title] = state
    return out


def diff_render_snapshots(
    baseline: dict[str, str], current: dict[str, str]
) -> list[str]:
    """Render-regression ratchet: report panels that got worse vs ``baseline``.

    A regression is a panel that rendered before and now errors/empties, a panel
    whose state degraded (rendered > empty > error), or a panel that vanished.
    Improvements (error -> rendered) and brand-new panels are allowed. Returns a
    list of human-readable regression strings (empty == no regression).
    """
    regressions: list[str] = []
    for title, base_state in sorted(baseline.items()):
        if title not in current:
            regressions.append(f"{title}: panel disappeared (was {base_state})")
            continue
        cur_state = current[title]
        base_rank = _RENDER_RANK.get(base_state.split(":", 1)[0], 0)
        cur_rank = _RENDER_RANK.get(cur_state.split(":", 1)[0], 0)
        if cur_rank < base_rank:
            regressions.append(f"{title}: {base_state} -> {cur_state}")
    return regressions


def interaction_regression(
    baseline: dict[str, str], after: dict[str, str], *, control_label: str
) -> list[str]:
    """Render regressions caused by changing a control: a panel that rendered
    before the control change and broke after it. Reuses diff_render_snapshots,
    attributing each regression to the control."""
    return [
        f"control '{control_label}': {r}" for r in diff_render_snapshots(baseline, after)
    ]


_ESQL_TYPE_TO_KIND = {
    "line": "xy", "bar": "xy", "area": "xy",
    "heatmap": "heatmap", "pie": "partition", "treemap": "treemap",
    "gauge": "gauge", "metric": "metric", "datatable": "datatable", "markdown": "markdown",
}


def _iter_kinded_panels(report: dict) -> Iterator[tuple[str, str]]:
    """Yield ``(title, normalized_kind)`` per non-skipped report panel that has a
    kind, in report order — the panels the render audit expects to find drawn.

    Shared by :func:`expected_kind_by_panel` (which keys them by title) and
    :func:`panel_titles_in_order` (which keeps duplicates), so the kind lookup and
    the segmentation matcher can never disagree about which panels count.
    """
    for dashboard in report.get("dashboards", []):
        if not isinstance(dashboard, dict):
            continue
        for panel in dashboard.get("panels", []):
            if not isinstance(panel, dict) or panel.get("status") == "skipped":
                continue
            etype = str(panel.get("kibana_type") or "").lower()
            if not etype:
                yaml_panel = panel.get("yaml_panel") if isinstance(panel.get("yaml_panel"), dict) else {}
                esql = yaml_panel.get("esql") if isinstance(yaml_panel.get("esql"), dict) else {}
                etype = str((esql or {}).get("type") or "").lower()
            if etype:
                yield str(panel.get("title") or ""), _ESQL_TYPE_TO_KIND.get(etype, etype)


def expected_kind_by_panel(report: dict) -> dict[str, str]:
    """Map each panel title to the normalized chart kind to expect in the render.

    Prefers the report's ``kibana_type`` (always present), falling back to the
    emitted YAML ``esql.type`` when a report doesn't carry it. Skipped panels are
    excluded."""
    return dict(_iter_kinded_panels(report))


def panel_titles_in_order(report: dict) -> list[str]:
    """The titles to segment a rendered dashboard by, in report order.

    :func:`expected_kind_by_panel` is a title-keyed *dict*, so two same-titled
    panels of one dashboard collapse into one key and the audit silently drops the
    second — Kubernetes carries ``Pods``/``Containers``/``Deployments``/
    ``DaemonSets`` twice. This keeps the duplicates, which
    :func:`segment_panels` resolves against successive DOM occurrences.

    Pass a report already scoped to the dashboard being audited
    (:func:`scope_report_to_dashboard`); this function deliberately does not
    filter by dashboard, so handing it a whole run's report reintroduces the
    cross-dashboard misattribution.
    """
    return [title for title, _kind in _iter_kinded_panels(report) if title]


# --------------------------------------------------------------------------- #
# Which dashboard is being audited
# --------------------------------------------------------------------------- #
#
# ``--migration-out`` points at a whole migration run, so its
# ``migration_report.json`` describes every dashboard of that run, while
# ``--dashboard-id`` names ONE uploaded dashboard. Segmenting the rendered DOM by
# every title in the report lets a stray text match hand a chunk of the audited
# dashboard to a *different* dashboard's panel metadata -- the wrong breakdown
# field, the wrong metric, the wrong index -- and the verdict that follows is
# confidently wrong rather than merely missing. Observed live: a Docker panel's
# error chunk was labelled ``Untitled``, which is a *Celery* table, and was
# therefore judged against ``logs-*`` when the erroring query reads ``metrics-*``.
#
# This is the same join bug the verifier fixed in 07e5829 (``Kafka``'s
# ``Error Logs`` compared against Redis's query, five of six drift findings fake),
# and it is fixed the same way: index the report by dashboard identity, drop a key
# two different dashboards claim, and report an unmatchable dashboard instead of
# guessing.

_ID_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _dashboard_id_slug(text: str) -> str:
    """The Kibana-id slug of a dashboard title.

    Mirrors ``targets/kibana/dashboards_api.py::_dashboard_id_slug``, which
    produces the ``obs-migrate-<title-slug>`` id an uploaded dashboard carries.
    Only a FALLBACK: the producer also appends a disambiguator when two
    dashboards of one run share a title, which no slug rule can reproduce from
    the title alone, so the driver prefers the ids recorded in ``native/`` (see
    ``render_audit_driver.native_dashboard_id_aliases``).
    """
    return _ID_SLUG_RE.sub("-", str(text or "").lower()).strip("-")


def dashboard_identity_keys(dashboard: dict) -> list[str]:
    """Every identity one migration-report dashboard entry can be joined on.

    A migration report is written before (or without) an upload, so which of
    these is populated varies: Grafana carries a source ``id``/``uid``, Datadog
    carries only a ``title``, an uploaded run also records
    ``upload.saved_object_id``, and ``--dashboard-id`` is whichever id Kibana
    actually stored it under. All of them are offered as keys and the caller
    matches on any one, exactly as the verifier tries uid-then-title.
    """
    upload = dashboard.get("upload") if isinstance(dashboard.get("upload"), dict) else {}
    title = str(dashboard.get("title") or "")
    slug = _dashboard_id_slug(title)
    candidates = (
        str((upload or {}).get("saved_object_id") or ""),
        str(dashboard.get("id") or ""),
        str(dashboard.get("uid") or ""),
        title,
        str(dashboard.get("artifact_stem") or ""),
        f"obs-migrate-{slug}" if slug else "",
    )
    return list(dict.fromkeys(key for key in candidates if key))


def _report_dashboard_index(
    report: dict, id_aliases: dict[str, str] | None
) -> tuple[list[dict], dict[str, int]]:
    """``([dashboards], {identity_key: position})`` with ambiguous keys dropped.

    A key claimed by two *different* dashboards is removed rather than resolved
    arbitrarily, so an artifact set with no usable dashboard identity yields an
    unmatchable dashboard -- visible as a note -- instead of a confidently wrong
    attribution. ``id_aliases`` maps a Kibana dashboard id to the dashboard title
    it was uploaded under; an alias whose title two dashboards share is dropped
    for the same reason.
    """
    dashboards = [d for d in (report or {}).get("dashboards") or [] if isinstance(d, dict)]
    positions_by_title: dict[str, list[int]] = {}
    for index, dashboard in enumerate(dashboards):
        positions_by_title.setdefault(str(dashboard.get("title") or ""), []).append(index)

    keyed: dict[str, int] = {}
    ambiguous: set[str] = set()

    def claim(key: str, index: int) -> None:
        if not key:
            return
        if keyed.get(key, index) != index:
            ambiguous.add(key)
            return
        keyed[key] = index

    for index, dashboard in enumerate(dashboards):
        for key in dashboard_identity_keys(dashboard):
            claim(key, index)
    for dashboard_id, title in (id_aliases or {}).items():
        owners = positions_by_title.get(str(title or ""), [])
        if len(owners) == 1:
            claim(str(dashboard_id), owners[0])
        elif owners:
            ambiguous.add(str(dashboard_id))
    for key in ambiguous:
        keyed.pop(key, None)
    return dashboards, keyed


def report_dashboards_by_key(
    report: dict, *, id_aliases: dict[str, str] | None = None
) -> dict[str, dict]:
    """``{identity_key: dashboard}`` for a migration report.

    The dashboard-scoped twin of the report's flat panel readers, in the spirit of
    the verifier's ``load_ir_panels_by_dashboard``. Keys two different dashboards
    claim are absent rather than guessed.
    """
    dashboards, keyed = _report_dashboard_index(report, id_aliases)
    return {key: dashboards[index] for key, index in keyed.items()}


def unmatchable_dashboard_note(report: dict, dashboard_id: str) -> str:
    """Why per-panel attribution is unavailable for ``dashboard_id``."""
    titles = [
        str(d.get("title") or "?")
        for d in (report or {}).get("dashboards") or []
        if isinstance(d, dict)
    ]
    shown = ", ".join(repr(t) for t in titles[:6]) + (", ..." if len(titles) > 6 else "")
    return (
        f"per-panel attribution unavailable: --dashboard-id {dashboard_id!r} matches "
        f"none of the {len(titles)} dashboard(s) in this migration report ({shown}). "
        "Panel titles are not unique across dashboards, so the whole report's titles "
        "were NOT used as a fallback -- that would attribute this dashboard's DOM to "
        "another dashboard's panel metadata. Point --migration-out at the run that "
        "produced this dashboard."
    )


def scope_report_to_dashboard(
    report: dict, dashboard_id: str, *, id_aliases: dict[str, str] | None = None
) -> tuple[dict | None, str]:
    """Narrow a migration report to the single dashboard being audited.

    Returns ``(report_shaped_dict, "")`` on a match -- the same shape every
    ``*_by_panel`` reader already consumes, so they all become dashboard-scoped
    without changing -- or ``(None, note)`` when the dashboard cannot be
    identified. ``None`` means "per-panel attribution is unavailable", never "use
    every title": a fallback to the global title set is the bug this exists to
    prevent, and it would fail silently.

    Resolution order:

    1. An exact identity key (:func:`dashboard_identity_keys`, plus any
       ``id_aliases`` recorded at upload time).
    2. The longest key ``dashboard_id`` extends with ``-<suffix>``, which matches
       a throwaway re-upload such as the ``-renderaudit-tmp`` copy
       ``scripts/render_audit_all_panels.py`` makes to expand collapsed rows.
       Longest wins, so ``obs-migrate-shared-title-dash-beta`` is preferred over
       ``obs-migrate-shared-title``; a tie between two different dashboards is
       not guessed.
    3. A report with exactly ONE dashboard, whatever the id: there is nothing to
       confuse it with, and a single-dashboard ``--migration-out`` is the shape
       the docs describe. Same allowance the verifier's
       ``_scoped_dashboard_index`` makes.
    """
    dashboards, keyed = _report_dashboard_index(report, id_aliases)
    if not dashboards:
        return None, unmatchable_dashboard_note(report, dashboard_id)

    def scoped(dashboard: dict) -> dict:
        return {**report, "dashboards": [dashboard]}

    wanted = str(dashboard_id or "")
    if wanted and wanted in keyed:
        return scoped(dashboards[keyed[wanted]]), ""
    if wanted:
        extended = sorted(
            (key for key in keyed if wanted.startswith(f"{key}-")), key=len, reverse=True
        )
        if extended:
            longest = [key for key in extended if len(key) == len(extended[0])]
            if len({keyed[key] for key in longest}) == 1:
                return scoped(dashboards[keyed[longest[0]]]), ""
    if len(dashboards) == 1:
        return scoped(dashboards[0]), ""
    return None, unmatchable_dashboard_note(report, dashboard_id)


def expects_data_by_panel(report: dict) -> set[str]:
    """Titles of panels that carry a query and therefore should render data.

    A query-bearing panel that renders empty is a finding (``unexpected_empty``);
    a markdown/control panel rendering "empty" is benign. Detection is robust: a
    non-empty ``esql.query`` whose Kibana type is not markdown.
    """
    titles: set[str] = set()
    for dashboard in report.get("dashboards", []):
        for panel in dashboard.get("panels", []):
            if not isinstance(panel, dict):
                continue
            yaml_panel = panel.get("yaml_panel") if isinstance(panel.get("yaml_panel"), dict) else {}
            esql = yaml_panel.get("esql") if isinstance(yaml_panel.get("esql"), dict) else {}
            query = str((esql or {}).get("query") or panel.get("esql_query") or "").strip()
            kibana_type = str(panel.get("kibana_type") or (esql or {}).get("type") or "").lower()
            if query and kibana_type != "markdown":
                titles.add(str(panel.get("title") or ""))
    return titles
