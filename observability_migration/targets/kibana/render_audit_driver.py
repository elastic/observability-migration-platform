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

import subprocess
from collections.abc import Callable

from observability_migration.adapters.source.grafana.smoke import (
    build_dashboard_url,
    discover_chrome_binary,
)
from observability_migration.targets.kibana.render_audit import (
    RenderVerdict,
    classify_render,
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
