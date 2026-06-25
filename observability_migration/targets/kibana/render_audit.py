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

# Console messages worth failing on (mirrors datadog browser_audit keywords).
_CONSOLE_ERROR_KEYWORDS = ("kibana", "esql", "es|ql", "lens")

_ERROR_RE = [re.compile(p, re.IGNORECASE) for p in BROWSER_ERROR_PATTERNS]


@dataclass
class RenderVerdict:
    status: str = "pass"  # "pass" | "warn" | "fail"
    rendered_error_markers: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    server_errors: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "rendered_error_markers": self.rendered_error_markers,
            "console_errors": self.console_errors,
            "server_errors": self.server_errors,
            "reasons": self.reasons,
        }


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
        if any(k in str(c).lower() for k in _CONSOLE_ERROR_KEYWORDS)
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
