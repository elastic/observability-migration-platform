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

_ERROR_RE = [re.compile(p, re.IGNORECASE) for p in BROWSER_ERROR_PATTERNS]

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


def find_render_error_markers(snapshot_text: str) -> list[str]:
    """Return the distinct DOM error markers present in a rendered snapshot."""
    text = str(snapshot_text or "")
    hits: list[str] = []
    for pattern, compiled in zip(BROWSER_ERROR_PATTERNS, _ERROR_RE, strict=True):
        if compiled.search(text):
            hits.append(pattern)
    return hits


def _filter_console(console_errors: Iterable[str]) -> list[str]:
    return [
        c for c in console_errors
        if any(rx.search(str(c)) for rx in _CONSOLE_ERROR_RE)
    ]


def _server_5xx(failed_requests: Iterable[str]) -> list[str]:
    # Failed-request strings carry the status code, e.g. "GET /api/... 503".
    return [r for r in failed_requests if re.search(r"\b5\d\d\b", str(r))]


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
        if available_fields is not None:
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
            breakdown = esql.get("breakdown")
            if isinstance(breakdown, dict) and breakdown.get("field"):
                fields.append(str(breakdown["field"]))
            elif isinstance(breakdown, list):
                fields.extend(str(b.get("field")) for b in breakdown if isinstance(b, dict) and b.get("field"))
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
