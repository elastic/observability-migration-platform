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
from collections.abc import Iterable
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
    """
    text = str(snapshot_text or "")
    positions: list[tuple[int, str]] = []
    unmatched: list[str] = []
    for title in titles:
        if not title:
            continue
        idx = text.find(title)
        if idx < 0:
            unmatched.append(title)
        else:
            positions.append((idx, title))
    positions.sort()
    segments: list[tuple[str, str]] = []
    for i, (start, title) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        segments.append((title, text[start:end]))
    return segments, unmatched


def audit_dashboard_elements(
    snapshot_text: str,
    *,
    expected_kind_by_title: dict[str, str] | None = None,
    breakdown_titles: Iterable[str] | None = None,
) -> RenderVerdict:
    """Per-panel element audit of a whole-dashboard snapshot.

    Segments by the expected titles, extracts elements, and flags per-panel
    issues (wrong chart kind, missing legend series on xy/heatmap, no data).
    Element findings are ``warn`` (a render that drew the wrong thing or no data
    is a review signal, not a hard ES|QL/Lens failure — those are caught by
    classify_render). Titles whose chunk never rendered are also a warn.
    """
    expected_kind_by_title = expected_kind_by_title or {}
    breakdown = set(breakdown_titles or ())
    segments, unmatched = segment_panels(snapshot_text, expected_kind_by_title)
    verdict = RenderVerdict()
    reasons: list[str] = []
    for title, chunk in segments:
        el = extract_panel_elements(title, chunk)
        verdict.panels.append(
            PanelRenderResult(
                title=title,
                status="rendered" if el.status == "rendered" else el.status,
                error_class="" if el.status == "rendered" else "render_error" if el.status == "error" else "unexpected_empty",
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
        if available_fields is not None and _FIELD_ABSENCE_RE.search(text):
            avail = set(available_fields)
            missing = [f for f in breakdown_fields if f and f not in avail]
            if missing:
                return PanelRenderResult(
                    title=title, status="error", error_class="field_gap",
                    missing_fields=list(dict.fromkeys(missing)),
                    detail=f"{markers[0]}; breakdown field(s) absent from target: {missing}",
                )
        return PanelRenderResult(
            title=title, status="error", error_class="render_error", detail=markers[0]
        )
    if _EMPTY_STATE_RE.search(text.strip()):
        if referenced_metrics and available_metrics is not None:
            missing_metrics = [
                m for m in referenced_metrics if m and m not in set(available_metrics)
            ]
            if missing_metrics:
                return PanelRenderResult(
                    title=title, status="empty", error_class="data_gap",
                    missing_fields=list(dict.fromkeys(missing_metrics)),
                    detail=f"empty: referenced metric(s) absent from target: {missing_metrics}",
                )
        if expects_data:
            return PanelRenderResult(
                title=title, status="empty", error_class="unexpected_empty",
                detail="query panel rendered no data (verify data/time window or query)",
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
    console_errors: Iterable[str] = (),
    failed_requests: Iterable[str] = (),
) -> RenderVerdict:
    """Per-panel render audit.

    ``panels`` is a list of ``(title, rendered_text)`` segments. Aggregation
    follows live_validate's philosophy: an unexplained ``render_error`` (or a
    render-error console message / 5xx) is a hard ``fail``; data-readiness
    findings — ``field_gap``, ``data_gap``, ``unexpected_empty`` — are ``warn``.
    """
    breakdown_by_title = breakdown_by_title or {}
    metrics_by_title = metrics_by_title or {}
    expects = set(expects_data_titles or ())
    verdict = RenderVerdict()
    for title, text in panels:
        result = classify_panel(
            title, text,
            breakdown_fields=breakdown_by_title.get(title, []),
            available_fields=available_fields,
            expects_data=title in expects,
            referenced_metrics=metrics_by_title.get(title, []),
            available_metrics=available_metrics,
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


@dataclass
class ControlInteraction:
    """A dashboard control worth exercising in an interaction audit."""
    variable_name: str
    label: str
    control_type: str
    default: str = ""
    multiple: bool = False


def extract_controls(report: dict) -> list[ControlInteraction]:
    """Extract the dashboard's controls (migrated template variables) from a
    migration report / compiled dashboard dict."""
    controls: list[ControlInteraction] = []
    for dashboard in report.get("dashboards", []):
        for control in dashboard.get("controls") or []:
            if not isinstance(control, dict):
                continue
            name = str(control.get("variable_name") or "")
            if not name:
                continue
            controls.append(
                ControlInteraction(
                    variable_name=name,
                    label=str(control.get("label") or name),
                    control_type=str(control.get("type") or ""),
                    default=str(control.get("default") or ""),
                    multiple=bool(control.get("multiple", False)),
                )
            )
    return controls


def build_interaction_plan(controls: Iterable[ControlInteraction]) -> list[dict[str, str]]:
    """Plan one interaction step per control: select a non-default value and
    re-audit. The live driver resolves concrete values from the control's
    options; this is the deterministic 'what to exercise' list."""
    return [
        {"variable_name": c.variable_name, "label": c.label, "action": "select_nondefault"}
        for c in controls
    ]


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


def expected_kind_by_panel(report: dict) -> dict[str, str]:
    """Map each panel title to the normalized chart kind to expect in the render.

    Prefers the report's ``kibana_type`` (always present), falling back to the
    emitted YAML ``esql.type`` when a report doesn't carry it. Skipped panels are
    excluded."""
    out: dict[str, str] = {}
    for dashboard in report.get("dashboards", []):
        for panel in dashboard.get("panels", []):
            if not isinstance(panel, dict) or panel.get("status") == "skipped":
                continue
            etype = str(panel.get("kibana_type") or "").lower()
            if not etype:
                yaml_panel = panel.get("yaml_panel") if isinstance(panel.get("yaml_panel"), dict) else {}
                esql = yaml_panel.get("esql") if isinstance(yaml_panel.get("esql"), dict) else {}
                etype = str((esql or {}).get("type") or "").lower()
            if etype:
                out[str(panel.get("title") or "")] = _ESQL_TYPE_TO_KIND.get(etype, etype)
    return out


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
