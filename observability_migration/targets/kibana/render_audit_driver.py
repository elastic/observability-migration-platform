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
from collections.abc import Callable

from observability_migration.adapters.source.grafana.smoke import (
    build_dashboard_url,
    discover_chrome_binary,
)
from observability_migration.targets.kibana.render_audit import (
    RenderVerdict,
    classify_render,
    interaction_regression,
)

# A DOM fetcher takes a URL and returns the rendered DOM HTML.
DomFetcher = Callable[[str], str]


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


def run_audit_cli(args: argparse.Namespace, *, dom_fetcher: DomFetcher | None = None) -> int:
    """Core of the render-audit CLI (separated from argparse for testing).

    Loads the dashboard, classifies the whole-dashboard render, prints a JSON
    verdict, and returns an exit code (1 on fail when ``--fail-on-error``).
    """
    url = build_dashboard_url(
        args.kibana_url, args.space, args.dashboard_id,
        time_from=args.time_from, time_to=args.time_to,
    )
    fetch = dom_fetcher or (lambda u: dump_dom(u, args.user_data_dir))
    verdict = classify_render(fetch(url))
    print(json.dumps(verdict.to_dict(), indent=2))
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
    return parser


def main(argv: list[str] | None = None) -> int:
    return run_audit_cli(_build_argparser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
