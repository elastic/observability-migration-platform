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
import sys
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
    metric_fields_by_panel,
    panel_titles_in_order,
    scope_report_to_dashboard,
    segment_panels,
    source_indices_by_panel,
)

# A DOM fetcher takes a URL and returns the rendered DOM HTML.
DomFetcher = Callable[[str], str]
# A field fetcher maps ONE index pattern to the set of field names present in it
# (or None when unavailable), used to attribute render markers to field gaps and
# empty panels to metric gaps. It takes the index pattern because a dashboard
# mixes them: a ``FROM logs-*`` panel's columns are not in ``metrics-*``, so a
# single dashboard-wide lookup answers a question about the wrong index.
FieldFetcher = Callable[[str], "set[str] | None"]


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

    Returns ``None`` when the target schema is *unknown or empty* — no ES URL is
    configured, discovery fails, or ``_field_caps`` returns no fields (a 200 on
    an absent/empty index is indistinguishable from a real schema with zero
    fields). In every such case the per-panel classifier treats a render marker
    as a hard ``render_error`` rather than downgrading it to a ``field_gap`` it
    cannot prove (hunt #4: an empty/unreachable-but-200 result masked real render
    errors). Only a *non-empty* field set enables field-gap attribution.

    Delegates to ``fetch_field_capabilities`` so TLS, retry, and header logic
    stay in one place.
    """
    if not es_url:
        return None
    try:
        caps = fetch_field_capabilities(
            es_url, index_pattern, es_api_key=es_api_key, timeout=timeout, verify=verify
        )
        # Empty caps ({} or None) -> unknown schema (None); only a populated
        # field set enables field-gap attribution.
        return set(caps.keys()) if caps else None
    except Exception:
        return None


def fetch_panel_field_caps(
    report: dict,
    *,
    fetch: FieldFetcher,
    cache: dict[str, set[str] | None] | None = None,
) -> dict[str, set[str] | None]:
    """Target field caps per panel, resolved against the index that panel reads.

    ``--es-index`` names ONE pattern, but a dashboard mixes them. Nine panels in
    the Datadog corpus are ``FROM logs-*``; looking their columns up in
    ``metrics-*`` answers a question about an index that cannot contain them, and
    every verdict drawn from that answer is unfounded — "absent" invents a gap
    that is not there, "present" would excuse a real bug.

    ``cache`` is keyed by index pattern, so a dashboard of forty ``metrics-*``
    panels and two ``logs-*`` panels costs two ``_field_caps`` calls, not
    forty-two. Pass one in (pre-seeded with the default index) to reuse it.

    A panel maps to ``None`` — schema unknown, which keeps it in the stricter
    class — as soon as ANY index it reads could not be read. A partial union
    would let a column absent from the unreadable half look confirmed.
    """
    caps = cache if cache is not None else {}
    out: dict[str, set[str] | None] = {}
    for title, indices in source_indices_by_panel(report).items():
        resolved: set[str] | None = set()
        for index in indices:
            if index not in caps:
                caps[index] = fetch(index)
            available = caps[index]
            if available is None:
                resolved = None
                break
            resolved = (resolved or set()) | available
        out[title] = resolved
    return out


def native_dashboard_id_aliases(migration_out: Path) -> dict[str, str]:
    """``{kibana_dashboard_id: dashboard_title}`` for a run's ``native/`` artifacts.

    ``native/index.json`` is written once per migration run and already records the
    deterministic id (``obs-migrate-<title-slug>``, plus a disambiguator when two
    dashboards of the run share a title) each dashboard was or would be uploaded
    under. Reading it lets ``--dashboard-id`` be joined to a report dashboard
    exactly, instead of re-deriving a slug that cannot reproduce the
    disambiguator. Mirrors the verifier's ``load_native_dashboard_index``.

    Returns ``{}`` when the artifacts are absent or unreadable — the caller then
    falls back to the identity keys the report itself carries.
    """
    native_dir = Path(migration_out) / "native"
    if not native_dir.exists():
        return {}
    out: dict[str, str] = {}

    def record(blob: object) -> None:
        if not isinstance(blob, dict):
            return
        dashboard_id = str(blob.get("dashboard_id") or "")
        if dashboard_id:
            out.setdefault(dashboard_id, str(blob.get("title") or ""))

    index_path = native_dir / "index.json"
    if index_path.exists():
        try:
            blob = json.loads(index_path.read_text())
        except (OSError, ValueError):
            blob = {}
        for item in (blob or {}).get("dashboards") or []:
            record(item)
        if out:
            return out
    for artifact in sorted(native_dir.glob("*.native.json")):
        try:
            record(json.loads(artifact.read_text()))
        except (OSError, ValueError):
            continue
    return out


def build_render_audit_command(
    chrome_binary: str,
    url: str,
    user_data_dir: str,
    *,
    window_width: int = 1600,
    window_height: int = 1200,
    virtual_time_budget_ms: int = 30000,
    no_sandbox: bool = False,
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
    if no_sandbox:
        cmd.append("--no-sandbox")
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
    no_sandbox: bool = False,
) -> str:
    """Render ``url`` in headless Chrome (reusing ``user_data_dir``) and return the DOM."""
    binary = discover_chrome_binary(chrome_binary)
    if not binary:
        raise RuntimeError("Chrome/Chromium binary not found for render audit")
    command = build_render_audit_command(
        binary,
        url,
        user_data_dir,
        virtual_time_budget_ms=virtual_time_budget_ms,
        no_sandbox=no_sandbox,
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
    metric columns, query-bearing panels) via ``classify_render_per_panel`` — so a
    data-readiness finding (``field_gap``/``data_gap``/``unexpected_empty``) is a
    ``warn`` and only a genuine ``render_error`` (or console/5xx error) is a hard
    ``fail``. Field- and metric-gap attribution additionally needs the target
    field set, fetched from ``--es-url`` (``_field_caps``) when available;
    without it a render marker stays a ``render_error`` and an empty panel stays
    ``unexpected_empty``. Field caps are fetched **per index**: each panel is
    judged against the index its own ES|QL ``FROM`` names, because a
    ``FROM logs-*`` panel's columns cannot be in ``metrics-*`` and any conclusion
    from looking there is unfounded. Without ``--migration-out`` it falls back to
    the whole-dashboard ``classify_render``.

    The report is first narrowed to the dashboard ``--dashboard-id`` names
    (:func:`scope_report_to_dashboard`), because ``--migration-out`` points at a
    whole run: segmenting one dashboard's DOM by every title in the run lets a
    stray text match attribute a chunk to another dashboard's breakdown field,
    metric and index. A dashboard the report cannot identify reports that fact —
    on stderr and in ``render.reasons`` — and gets no per-panel attribution;
    borrowing the run's full title set is exactly the misattribution being
    prevented, and it would fail silently.

    With ``--elements`` it also runs the per-panel element audit (chart kind /
    legend / data vs the emitted YAML). Prints a JSON verdict; exits non-zero on
    a render ``fail`` when ``--fail-on-error``.

    ``--agent-browser`` is a tab-selection helper: it focuses the Kibana
    ``/app/*`` tab of a live agent-browser session (so a stray gemini-glic /
    found.no / SSO-interstitial tab being "active" is not left in front). DOM
    capture always goes through the headless ``dump_dom`` path, which navigates
    to the exact target URL and reads HTML — so CSS-class render markers
    (``embPanel__error``) are visible and we never snapshot the wrong tab
    mid-load. Headless capture needs a logged-in ``--user-data-dir`` profile.
    """
    url = build_dashboard_url(
        args.kibana_url, args.space, args.dashboard_id,
        time_from=args.time_from, time_to=args.time_to,
    )
    drive = tab_driver or _default_tab_driver
    if getattr(args, "agent_browser", False):
        # Focus a Kibana tab first so the session isn't left on a stray
        # gemini-glic / SSO tab. Best-effort UX only — capture still uses the
        # headless path below regardless of which tab is active.
        activate_kibana_tab(args.kibana_url, args.dashboard_id, tab_driver=drive)
    fetch = dom_fetcher or (
        lambda u: dump_dom(
            u,
            args.user_data_dir,
            no_sandbox=bool(getattr(args, "chrome_no_sandbox", False)),
        )
    )
    snapshot = fetch(url)

    report: dict | None = None
    # Non-empty when --migration-out was supplied but the audited dashboard could
    # not be found in it. Per-panel attribution is then unavailable and says so;
    # it never falls back to the whole report's titles, which would attribute this
    # dashboard's DOM to another dashboard's panel metadata.
    scope_note = ""
    # Target field caps, needed by BOTH the per-panel render classification and
    # the --elements audit to confirm a field absence. None == unknown schema, in
    # which case every render marker stays a hard render_error. ``available_fields``
    # covers panels whose query names no index; ``fields_by_title`` carries the
    # per-panel caps for every panel that does name one.
    available_fields: set[str] | None = None
    fields_by_title: dict[str, set[str] | None] = {}
    metrics_by_title: dict[str, list[str]] = {}
    migration_out = getattr(args, "migration_out", "")
    if migration_out:
        report_path = Path(migration_out) / "migration_report.json"
        if report_path.exists():
            try:
                report = json.loads(report_path.read_text())
            except (ValueError, OSError) as exc:
                # A malformed/unreadable report must not crash the audit; degrade
                # to the whole-dashboard render classification (hunt #4). stderr,
                # for the reason given below.
                print(f"warning: could not read migration_report.json ({exc}); "
                      "falling back to whole-dashboard render classification",
                      file=sys.stderr)
                report = None
        else:
            # stderr, not stdout: stdout carries the JSON report and a stray
            # line there makes it unparseable for every downstream consumer.
            print(f"warning: no migration_report.json under {migration_out!r}; "
                  "falling back to whole-dashboard render classification "
                  "(no per-panel attribution)", file=sys.stderr)
    else:
        # Say so. Without the report the audit still detects that the dashboard
        # renders errors, but reports "panels": [] -- it cannot say WHICH panel,
        # which is the whole reason to run it. Degrading silently sent a real
        # investigation down the path of re-executing every panel query by hand.
        print("note: --migration-out not supplied; reporting whole-dashboard "
              "render status only. Pass --migration-out <dir>/dashboards for "
              "per-panel attribution.", file=sys.stderr)

    if report is not None:
        # ONE dashboard is being audited; the report describes the whole run. Scope
        # it before anything reads a panel list, so the segmentation matcher, the
        # breakdown fields, the metrics and the per-panel index all come from the
        # dashboard actually on screen.
        report, scope_note = scope_report_to_dashboard(
            report,
            args.dashboard_id,
            id_aliases=native_dashboard_id_aliases(Path(migration_out)),
        )
        if scope_note:
            print(f"warning: {scope_note}", file=sys.stderr)

    if report is not None:
        titles = panel_titles_in_order(report)
        segments, unmatched = segment_panels(snapshot, titles)
        fetch_fields = field_fetcher or (
            lambda index: fetch_available_fields(
                getattr(args, "es_url", "") or "",
                getattr(args, "es_api_key", "") or "",
                index,
                verify=not getattr(args, "insecure", False),
            )
        )
        # --es-index is the fallback for panels whose query names no index (a
        # markdown panel, or an unrecognized source command); every panel that
        # names one is resolved against THAT index instead. One shared cache, so
        # each distinct index pattern is fetched once per dashboard.
        default_index = getattr(args, "es_index", "") or "metrics-*"
        caps_by_index: dict[str, set[str] | None] = {default_index: fetch_fields(default_index)}
        available_fields = caps_by_index[default_index]
        fields_by_title = fetch_panel_field_caps(
            report, fetch=fetch_fields, cache=caps_by_index
        )
        metrics_by_title = metric_fields_by_panel(report)
        verdict = classify_render_per_panel(
            segments,
            breakdown_by_title=breakdown_fields_by_panel(report),
            expects_data_titles=expects_data_by_panel(report),
            metrics_by_title=metrics_by_title,
            available_fields=available_fields,
            available_metrics=available_fields,
            target_fields_by_title=fields_by_title,
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
        # A title with no DOM region of its own is reported WHATEVER the verdict
        # is. It used to be reported only while the verdict was still "pass", so
        # any data-readiness warn elsewhere on the dashboard swallowed "this panel
        # never drew" — and a panel with no chunk is exactly how the prefix
        # misattribution hid (a zero-length chunk reads as a clean render).
        if unmatched:
            verdict.reasons.append(f"panel title(s) did not render: {unmatched}")
            if verdict.status == "pass":
                verdict.status = "warn"
    else:
        verdict = classify_render(snapshot)
        if scope_note:
            # Machine-visible, not just a stderr line: a consumer reading only the
            # JSON must be able to see that "panels": [] means "we could not
            # identify this dashboard", not "every panel rendered".
            verdict.reasons.append(scope_note)
            if verdict.status == "pass":
                verdict.status = "warn"

    output: dict[str, object] = {"render": verdict.to_dict()}

    if getattr(args, "elements", False) and report is not None:
        breakdowns = breakdown_fields_by_panel(report)
        elements = audit_dashboard_elements(
            snapshot,
            expected_kind_by_title=expected_kind_by_panel(report),
            breakdown_titles=set(breakdowns),
            breakdown_by_title=breakdowns,
            available_fields=available_fields,
            expects_data_titles=expects_data_by_panel(report),
            metrics_by_title=metrics_by_title,
            target_fields_by_title=fields_by_title,
            panel_titles=panel_titles_in_order(report),
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
             "field gaps so missing-breakdown panels warn instead of failing, and "
             "empty panels to metric gaps).",
    )
    parser.add_argument("--es-api-key", default="", help="API key for --es-url.")
    parser.add_argument(
        "--es-index", default="metrics-*",
        help="FALLBACK index pattern for --es-url field-caps discovery, used only for "
             "panels whose query names no index (default: metrics-*). Every panel that "
             "does is resolved against the index its own ES|QL FROM names, so a "
             "FROM logs-* panel is never judged against metrics-*.",
    )
    parser.add_argument(
        "--insecure", action="store_true",
        help="Skip TLS verification for --es-url.",
    )
    parser.add_argument(
        "--agent-browser", action="store_true",
        help="Tab-selection helper: focus the Kibana tab matching the "
             "host/dashboard-id in a live agent-browser session (ignores stray "
             "gemini-glic / SSO-interstitial tabs). DOM capture still uses the "
             "headless --user-data-dir path.",
    )
    parser.add_argument(
        "--chrome-no-sandbox",
        action="store_true",
        help="Pass --no-sandbox to headless Chrome. Intended for trusted local CI runners "
             "where Chrome cannot use the Linux sandbox.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    return run_audit_cli(_build_argparser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
