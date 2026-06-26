# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Live render-audit driver.

Opens an uploaded Kibana dashboard in a real (headless) Chrome, dumps the
rendered DOM, and classifies it with :mod:`render_audit` — the gate that proves
panels actually *render* (no Lens "An error occurred" embeddable), which the
ES|QL/data checks cannot see.

Auth: Elastic Serverless is behind cloud SAML SSO, so a fresh automated browser
has no session. The driver therefore reuses a **persistent Chrome profile**
(``--user-data-dir``) that you log into once:

    1. Launch Chrome headful against that profile and sign in to Kibana::

         "<chrome>" --user-data-dir=/path/to/profile "<KIBANA_URL>/login"

       Complete SSO, confirm a dashboard loads, then quit Chrome.
    2. Point the driver at the same ``--user-data-dir``. Headless Chrome reuses
       the profile's cookies.

Caveat: if the SSO cookie is session-scoped it may not survive a full browser
restart; re-run the one-time login when the driver starts hitting the login
wall. The verdict logic lives in :mod:`render_audit` and is unit-tested offline;
this module only adds the browser plumbing (injectable for tests).
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, TypeVar
from urllib.parse import urlsplit

from observability_migration.adapters.source.grafana.smoke import (
    build_dashboard_url,
    discover_chrome_binary,
)
from observability_migration.core.verification.field_capabilities import fetch_field_capabilities
from observability_migration.targets.kibana.render_audit import (
    RenderVerdict,
    audit_dashboard_elements,
    breakdown_fields_by_panel,
    classify_render,
    classify_render_per_panel,
    count_render_error_markers,
    expected_kind_by_panel,
    expects_data_by_panel,
    interaction_regression,
    segment_panels,
)

# A DOM fetcher takes a URL and returns the rendered DOM HTML.
DomFetcher = Callable[[str], str]
# A field fetcher returns the set of field names present in the target index
# (or None when unavailable), used to attribute render markers to field gaps.
FieldFetcher = Callable[[], "set[str] | None"]


# --------------------------------------------------------------------------- #
# Kibana-tab / page selection (pure, unit-tested without a browser)
# --------------------------------------------------------------------------- #
#
# The render audit drives a logged-in ``agent-browser`` session on Serverless.
# That session can carry MULTIPLE tabs/targets — Kibana dashboard tabs PLUS
# unrelated ones such as Chrome's Gemini "glic" side-panel
# (``https://gemini.google.com/glic``). The active target is frequently the
# wrong tab, so a naive ``agent-browser get url`` (or a single-page driver)
# reads the wrong page: URL checks and login-detection then look at gemini,
# never the Kibana ``/app/*`` page, even after SAML completed in the Kibana tab.
#
# The fix is to enumerate every open tab and pick the Kibana ``/app/*`` page on
# the target host (preferring the one whose URL carries the dashboard id),
# ignoring non-Kibana tabs and Elastic SSO interstitials. The rule below is the
# pure, testable core of that selection; the browser plumbing (``agent-browser
# tab list`` / ``tab t<N>``) is a thin wrapper around it.

# Substrings that mark a URL as a transient Elastic SSO/security interstitial
# rather than a usable, logged-in Kibana page.
_INTERSTITIAL_MARKERS = (
    "/internal/security/capture-url",
    "auth_provider_hint",
)


def _bare_host(value: str) -> str:
    """Reduce ``value`` (a bare host or a full URL) to just its lowercased host."""
    if not value:
        return ""
    raw = value.strip()
    # ``urlsplit`` only populates ``netloc`` when a scheme (or leading //) is
    # present; otherwise the whole thing lands in ``path``.
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    host = parsed.netloc or parsed.path
    host = host.split("@")[-1]  # drop any userinfo
    host = host.split("/")[0]   # drop any path that slipped through
    host = host.split(":")[0]   # drop any :port
    return host.lower()


def _is_kibana_app_url(url: str, kibana_host: str) -> bool:
    """True when ``url`` is a logged-in Kibana ``/app/*`` page on ``kibana_host``.

    Rejects non-Kibana origins (gemini glic, staging.found.no, upstream IdP
    ``/app/`` URLs like okta) by comparing the *exact* parsed host, and rejects
    Elastic SSO interstitials (``capture-url`` / ``auth_provider_hint``) that
    live on the Kibana host but are not yet a usable page.
    """
    if not url:
        return False
    if _bare_host(url) != kibana_host:
        return False
    # The space prefix (``/s/<space>``) is optional and precedes ``/app/``.
    path = urlsplit(url).path
    if "/app/" not in path and not path.endswith("/app"):
        return False
    return not any(marker in url for marker in _INTERSTITIAL_MARKERS)


def select_kibana_page_url(
    urls: Sequence[str], kibana_host: str, dashboard_id: str | None
) -> str | None:
    """Pick the Kibana tab URL to drive from every open tab's URL.

    Selection rule, applied to ``urls`` in order:

    1. Keep only logged-in Kibana ``/app/*`` URLs on ``kibana_host`` — ignoring
       non-Kibana tabs (e.g. ``gemini.google.com/glic``, ``staging.found.no``,
       upstream IdP ``/app/`` URLs) and Elastic SSO interstitials
       (``capture-url`` / ``auth_provider_hint``).
    2. If ``dashboard_id`` is given and any candidate's URL contains it, return
       the first such candidate.
    3. Otherwise return the first Kibana ``/app/*`` candidate (so login is still
       detected and a sibling dashboard tab is usable).
    4. Return ``None`` when no Kibana ``/app/*`` tab is open.

    ``kibana_host`` may be a bare host or a full ``KIBANA_URL`` (scheme/path are
    stripped). The function takes no I/O and is unit-tested without a browser.
    """
    host = _bare_host(kibana_host)
    if not host:
        return None
    candidates = [u for u in urls if u and _is_kibana_app_url(u, host)]
    if not candidates:
        return None
    if dashboard_id:
        for url in candidates:
            if dashboard_id in url:
                return url
    return candidates[0]


class _HasUrl(Protocol):
    url: str


_PageT = TypeVar("_PageT", bound=_HasUrl)


def select_kibana_page(
    pages: Sequence[_PageT], kibana_host: str, dashboard_id: str | None
) -> _PageT | None:
    """Object-oriented twin of :func:`select_kibana_page_url`.

    Picks the page (any object exposing a ``.url`` attribute) whose URL the pure
    rule selects, so callers holding agent-browser/Playwright-style page handles
    can activate the right tab directly.
    """
    by_url: dict[str, _PageT] = {}
    for page in pages:
        url = getattr(page, "url", "") or ""
        # Keep the first page per URL so ordering ties resolve like the URL rule.
        by_url.setdefault(url, page)
    chosen = select_kibana_page_url(list(by_url.keys()), kibana_host, dashboard_id)
    if chosen is None:
        return None
    return by_url[chosen]


def parse_agent_browser_tabs(payload: str) -> list[tuple[str, str]]:
    """Parse ``agent-browser tab list --json`` into ``[(tab_id, url), ...]``.

    The JSON shape is ``{"data": {"tabs": [{"tabId": "t4", "url": ...}, ...]}}``.
    Tabs without an id or url are dropped. Returns ``[]`` on any parse error so
    the caller degrades to the active tab rather than raising.
    """
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return []
    tabs = (data.get("data") or {}).get("tabs") if isinstance(data, dict) else None
    if not isinstance(tabs, list):
        return []
    out: list[tuple[str, str]] = []
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        tab_id = str(tab.get("tabId") or "").strip()
        url = str(tab.get("url") or "").strip()
        if tab_id and url:
            out.append((tab_id, url))
    return out


def select_kibana_tab_id(
    tabs: Sequence[tuple[str, str]], kibana_host: str, dashboard_id: str | None
) -> str | None:
    """Pick the agent-browser tab id of the Kibana page to drive.

    ``tabs`` are ``(tab_id, url)`` pairs (e.g. from
    :func:`parse_agent_browser_tabs`). Applies the same selection rule as
    :func:`select_kibana_page_url` and returns the matching tab id (``t<N>``), or
    ``None`` when no Kibana ``/app/*`` tab is open.
    """
    by_url: dict[str, str] = {}
    for tab_id, url in tabs:
        by_url.setdefault(url, tab_id)
    chosen = select_kibana_page_url(list(by_url.keys()), kibana_host, dashboard_id)
    return by_url.get(chosen) if chosen is not None else None


# A tab driver runs an ``agent-browser tab ...`` subcommand and returns stdout.
TabDriver = Callable[[list[str]], str]


def _default_tab_driver(args: list[str]) -> str:
    """Run ``agent-browser <args>`` and return stdout (empty on failure)."""
    try:
        proc = subprocess.run(
            ["agent-browser", *args], capture_output=True, text=True, timeout=30
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    return proc.stdout or ""


def activate_kibana_tab(
    kibana_url: str,
    dashboard_id: str | None,
    *,
    tab_driver: TabDriver | None = None,
) -> str | None:
    """Select and activate the Kibana ``/app/*`` tab in a live agent-browser session.

    Enumerates every open tab (``agent-browser tab list --json``), picks the one
    on the Kibana host that matches the dashboard id (ignoring gemini-glic /
    found.no / SSO interstitial tabs), and switches to it (``agent-browser tab
    t<N>``) so subsequent URL/DOM reads target the right page instead of the
    wrong active tab.

    Returns the activated tab id, or ``None`` when no Kibana tab is open (the
    caller should then fall back to opening the dashboard URL). ``tab_driver`` is
    injectable so this is unit-tested without a browser.
    """
    drive = tab_driver or _default_tab_driver
    listing = drive(["tab", "list", "--json"])
    tabs = parse_agent_browser_tabs(listing)
    if not tabs:
        return None
    tab_id = select_kibana_tab_id(tabs, kibana_url, dashboard_id)
    if tab_id is None:
        return None
    drive(["tab", tab_id])
    return tab_id


def fetch_available_fields(
    es_url: str, es_api_key: str, index_pattern: str, *, timeout: int = 10, verify: bool = True
) -> set[str] | None:
    """Field names present in the target index (for field_gap/data_gap attribution).

    Returns ``None`` only when the target schema is *unknown* — no ES URL is
    configured or discovery fails — so the per-panel classifier treats a render
    marker as a hard ``render_error`` (a field gap cannot be proven without
    knowing the target schema). A successful field-caps call that simply matched
    no fields returns an empty ``set()`` (schema known, field absent), which lets
    the classifier still attribute a field gap.

    Delegates to ``fetch_field_capabilities`` so TLS, retry, and header logic
    stay in one place.
    """
    if not es_url:
        return None
    try:
        caps = fetch_field_capabilities(
            es_url, index_pattern, es_api_key=es_api_key, timeout=timeout, verify=verify
        )
        # ``{}`` is a *successful* empty result (schema known, no matching field)
        # and must stay distinct from ``None`` (fetch failure) — see PR #234.
        return set(caps.keys()) if caps is not None else set()
    except Exception:
        return None


def build_render_audit_command(
    chrome_binary: str,
    url: str,
    user_data_dir: str,
    *,
    window_width: int = 1600,
    window_height: int = 1200,
    virtual_time_budget_ms: int = 30000,
) -> list[str]:
    """Headless-Chrome argv that loads ``url`` with a persistent profile and dumps the DOM."""
    cmd = [
        chrome_binary,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        f"--window-size={window_width},{window_height}",
        f"--virtual-time-budget={virtual_time_budget_ms}",
    ]
    if user_data_dir:
        cmd.append(f"--user-data-dir={user_data_dir}")
    cmd += ["--dump-dom", url]
    return cmd


def dump_dom(
    url: str,
    user_data_dir: str,
    *,
    chrome_binary: str = "",
    virtual_time_budget_ms: int = 30000,
    timeout: int = 90,
) -> str:
    """Render ``url`` in headless Chrome (reusing ``user_data_dir``) and return the DOM."""
    binary = discover_chrome_binary(chrome_binary)
    if not binary:
        raise RuntimeError("Chrome/Chromium binary not found for render audit")
    command = build_render_audit_command(
        binary, url, user_data_dir, virtual_time_budget_ms=virtual_time_budget_ms
    )
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"headless Chrome exited {proc.returncode}: {proc.stderr.strip()[:500]}"
        )
    return proc.stdout


def audit_dashboard_render(
    kibana_url: str,
    dashboard_id: str,
    *,
    space_id: str = "",
    user_data_dir: str = "",
    time_from: str = "",
    time_to: str = "",
    console_errors: tuple[str, ...] = (),
    failed_requests: tuple[str, ...] = (),
    dom_fetcher: DomFetcher | None = None,
) -> RenderVerdict:
    """Open a dashboard, capture its DOM, and return a render verdict.

    ``dom_fetcher`` is injectable so the orchestration is unit-tested offline; it
    defaults to :func:`dump_dom` bound to ``user_data_dir``.
    """
    url = build_dashboard_url(kibana_url, space_id, dashboard_id, time_from=time_from, time_to=time_to)
    fetch = dom_fetcher or (lambda u: dump_dom(u, user_data_dir))
    snapshot = fetch(url)
    return classify_render(
        snapshot,
        console_errors=console_errors,
        failed_requests=failed_requests,
        screenshot_ok=True,
    )


def audit_control_interactions(
    plan: list[dict[str, str]],
    *,
    capture_render_snapshot: Callable[[], dict[str, str]],
    select_control_nondefault: Callable[[str], None],
) -> list[str]:
    """Exercise each planned control and report render regressions it causes.

    Captures a baseline per-panel render snapshot, then for each control step
    selects a non-default value and re-captures; a panel that rendered before the
    change and broke after it is an interaction regression (attributed to the
    control). The two callables are injected so this orchestration is unit-tested
    offline; the live wiring backs them with the browser (set the Kibana control,
    re-snapshot the panels).
    """
    baseline = capture_render_snapshot()
    findings: list[str] = []
    for step in plan:
        select_control_nondefault(step["variable_name"])
        after = capture_render_snapshot()
        findings.extend(
            interaction_regression(baseline, after, control_label=step.get("label", step["variable_name"]))
        )
    return findings


def run_audit_cli(
    args: argparse.Namespace,
    *,
    dom_fetcher: DomFetcher | None = None,
    field_fetcher: FieldFetcher | None = None,
    tab_driver: TabDriver | None = None,
) -> int:
    """Core of the render-audit CLI (separated from argparse for testing).

    When ``--migration-out`` is supplied, the render is classified **per panel**
    against the emitted migration metadata (panel segments, breakdown fields,
    query-bearing panels) via ``classify_render_per_panel`` — so a data-readiness
    finding (``field_gap``/``data_gap``/``unexpected_empty``) is a ``warn`` and
    only a genuine ``render_error`` (or console/5xx error) is a hard ``fail``.
    Field-gap attribution additionally needs the target field set, fetched from
    ``--es-url`` (``_field_caps``) when available; without it a render marker
    stays a ``render_error``. Without ``--migration-out`` it falls back to the
    whole-dashboard ``classify_render``.

    With ``--elements`` it also runs the per-panel element audit (chart kind /
    legend / data vs the emitted YAML). Prints a JSON verdict; exits non-zero on
    a render ``fail`` when ``--fail-on-error``.

    With ``--agent-browser`` the driver first selects and activates the Kibana
    ``/app/*`` tab of a live agent-browser session (so a stray gemini-glic /
    found.no / SSO-interstitial tab being "active" does not make us read the
    wrong page) before capturing the DOM.
    """
    url = build_dashboard_url(
        args.kibana_url, args.space, args.dashboard_id,
        time_from=args.time_from, time_to=args.time_to,
    )
    if getattr(args, "agent_browser", False):
        activate_kibana_tab(args.kibana_url, args.dashboard_id, tab_driver=tab_driver)
    fetch = dom_fetcher or (lambda u: dump_dom(u, args.user_data_dir))
    snapshot = fetch(url)

    report: dict | None = None
    migration_out = getattr(args, "migration_out", "")
    if migration_out:
        report_path = Path(migration_out) / "migration_report.json"
        if report_path.exists():
            report = json.loads(report_path.read_text())

    if report is not None:
        kinds = expected_kind_by_panel(report)
        segments, unmatched = segment_panels(snapshot, kinds.keys())
        fetch_fields = field_fetcher or (
            lambda: fetch_available_fields(
                getattr(args, "es_url", "") or "",
                getattr(args, "es_api_key", "") or "",
                getattr(args, "es_index", "") or "metrics-*",
                verify=not getattr(args, "insecure", False),
            )
        )
        available_fields = fetch_fields()
        verdict = classify_render_per_panel(
            segments,
            breakdown_by_title=breakdown_fields_by_panel(report),
            expects_data_titles=expects_data_by_panel(report),
            available_fields=available_fields,
            available_metrics=available_fields,
        )
        whole_verdict = classify_render(snapshot)
        segmented_text = "\n".join(chunk for _title, chunk in segments)
        segmented_verdict = classify_render(segmented_text)
        # Markers OUTSIDE every recognized panel segment are genuinely
        # unattributed: they belong to no titled panel (dashboard chrome, or a
        # panel that rendered before the first recognized title). Markers INSIDE
        # a recognized panel are already attributed by classify_render_per_panel
        # (field_gap/data_gap -> warn; a real render error -> the per-panel
        # "error" status already drives verdict.status). The original guard
        # compared a whole-snapshot occurrence count to the number of error
        # panels, which false-failed an in-panel DUPLICATE marker, and a later
        # string-truncation heuristic was fragile (false-failed a panel whose
        # DOM merely contained "untitled panel"). So escalate only on a NEW
        # marker TYPE outside the segments or an occurrence in the unsegmented
        # region (PR #234 review).
        #
        # Known limitation: a trailing *untitled* panel absorbed into the last
        # (EOF-extended) recognized segment cannot be reliably attributed from
        # a11y text, so it is not hard-failed here; its data-readiness still
        # surfaces as a per-panel warn.
        unattributed_markers = count_render_error_markers(
            snapshot
        ) - count_render_error_markers(segmented_text)
        unsegmented_hard_error = (
            whole_verdict.status == "fail"
            and whole_verdict.rendered_error_markers
            and (
                set(whole_verdict.rendered_error_markers)
                - set(segmented_verdict.rendered_error_markers)
                or unattributed_markers > 0
            )
        )
        if unsegmented_hard_error:
            verdict.status = "fail"
            verdict.rendered_error_markers = whole_verdict.rendered_error_markers
            verdict.reasons.extend(
                reason for reason in whole_verdict.reasons if reason not in verdict.reasons
            )
        elif unmatched and verdict.status == "pass":
            verdict.status = "warn"
            verdict.reasons.append(f"panel title(s) did not render: {unmatched}")
    else:
        verdict = classify_render(snapshot)

    output: dict[str, object] = {"render": verdict.to_dict()}

    if getattr(args, "elements", False) and report is not None:
        elements = audit_dashboard_elements(
            snapshot,
            expected_kind_by_title=expected_kind_by_panel(report),
            breakdown_titles=set(breakdown_fields_by_panel(report)),
        )
        output["elements"] = elements.to_dict()

    print(json.dumps(output, indent=2))
    if verdict.status == "fail" and args.fail_on_error:
        return 1
    return 0


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="render_audit_driver",
        description="Render-audit a Kibana dashboard in headless Chrome.",
    )
    parser.add_argument("--kibana-url", required=True)
    parser.add_argument("--dashboard-id", required=True)
    parser.add_argument("--space", default="")
    parser.add_argument(
        "--user-data-dir", default="",
        help="Persistent Chrome profile (for SSO targets). Omit for local no-SSO Kibana.",
    )
    parser.add_argument("--time-from", default="now-24h")
    parser.add_argument("--time-to", default="now")
    parser.add_argument("--fail-on-error", action="store_true")
    parser.add_argument(
        "--elements", action="store_true",
        help="Also run the per-panel element audit (chart kind / legend / data).",
    )
    parser.add_argument(
        "--migration-out", default="",
        help="Migration output dir (with migration_report.json). Enables per-panel "
             "render classification (field_gap/data_gap warn vs render_error fail).",
    )
    parser.add_argument(
        "--es-url", default="",
        help="Elasticsearch URL for target field caps (attributes render markers to "
             "field gaps so missing-breakdown panels warn instead of failing).",
    )
    parser.add_argument("--es-api-key", default="", help="API key for --es-url.")
    parser.add_argument(
        "--es-index", default="metrics-*",
        help="Index pattern for --es-url field-caps discovery (default: metrics-*).",
    )
    parser.add_argument(
        "--insecure", action="store_true",
        help="Skip TLS verification for --es-url.",
    )
    parser.add_argument(
        "--agent-browser", action="store_true",
        help="Drive a live agent-browser session: select+activate the Kibana "
             "tab matching the host/dashboard-id before capturing the DOM "
             "(ignores stray gemini-glic / SSO-interstitial tabs).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    return run_audit_cli(_build_argparser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
