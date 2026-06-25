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
from pathlib import Path

from observability_migration.adapters.source.grafana.smoke import (
    build_dashboard_url,
    discover_chrome_binary,
)
from observability_migration.targets.kibana.render_audit import (
    RenderVerdict,
    audit_dashboard_elements,
    breakdown_fields_by_panel,
    classify_render,
    classify_render_per_panel,
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


def fetch_available_fields(
    es_url: str, es_api_key: str, index_pattern: str, *, timeout: int = 10, verify: bool = True
) -> set[str] | None:
    """Field names present in the target index (for field_gap/data_gap attribution).

    Returns ``None`` when no ES URL is configured or discovery fails — the
    per-panel classifier then treats a render marker as a hard ``render_error``
    (a field gap cannot be proven without knowing the target schema). Mirrors the
    ``_field_caps`` discovery in the Grafana schema resolver.
    """
    if not es_url:
        return None
    import requests

    headers = {"Authorization": f"ApiKey {es_api_key}"} if es_api_key else {}
    try:
        resp = requests.get(
            f"{es_url.rstrip('/')}/{index_pattern}/_field_caps",
            params={"fields": "*"},
            headers=headers,
            timeout=timeout,
            verify=verify,
        )
        if resp.status_code == 200:
            return set(resp.json().get("fields", {}).keys())
    except Exception:
        return None
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
    """
    url = build_dashboard_url(
        args.kibana_url, args.space, args.dashboard_id,
        time_from=args.time_from, time_to=args.time_to,
    )
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
        segments, _unmatched = segment_panels(snapshot, kinds.keys())
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
    return parser


def main(argv: list[str] | None = None) -> int:
    return run_audit_cli(_build_argparser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
