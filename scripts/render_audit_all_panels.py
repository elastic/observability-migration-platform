#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Render-audit EVERY panel, including the ones inside collapsed rows.

A dashboard's collapsed rows never render, so the browser audit only ever sees
the panels in expanded rows. On Node Exporter Full that is 19 of 143 — the other
124 were being taken on trust from query execution alone, which cannot see Lens
accessor errors, empty states, or anything else that only appears once a panel
is actually drawn.

Clicking each row header would work but is slow and flaky. Collapsed state lives
in the saved object, so this uploads a THROWAWAY copy of the dashboard with every
row expanded, audits that, and deletes it. The real dashboard is never modified.

Usage::

    python scripts/render_audit_all_panels.py \\
        --migration-out <out>/dashboards \\
        --kibana-url http://localhost:5602 \\
        --es-url http://localhost:9201 --es-index 'metrics-*'

Add ``--keep`` to leave the expanded copy in Kibana for manual inspection.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

_AUDIT_SUFFIX = "-renderaudit-tmp"


def expand_rows(payload: dict) -> int:
    """Expand every collapsed row in place. Returns how many were expanded."""
    expanded = 0

    def visit(panels: list) -> None:
        nonlocal expanded
        for panel in panels or []:
            if not isinstance(panel, dict):
                continue
            if panel.get("collapsed"):
                panel["collapsed"] = False
                expanded += 1
            visit(panel.get("panels") or [])

    visit(payload.get("panels") or [])
    return expanded


def _request(method: str, url: str, body: dict | None, api_key: str) -> tuple[int, str]:
    headers = {"Content-Type": "application/json", "kbn-xsrf": "true",
               "elastic-api-version": "2023-10-31"}
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data, headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode()[:400]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:400]
    except OSError as exc:
        return 0, str(exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration-out", required=True)
    parser.add_argument("--kibana-url", required=True)
    parser.add_argument("--kibana-api-key", default="")
    parser.add_argument("--es-url", default="")
    parser.add_argument("--es-index", default="metrics-*")
    parser.add_argument("--es-api-key", default="")
    parser.add_argument("--time-from", default="now-30m")
    parser.add_argument("--time-to", default="now")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    out = Path(args.migration_out)
    native = sorted((out / "native").glob("*.native.json"))
    if not native:
        print(f"no native artifacts under {out / 'native'}", file=sys.stderr)
        return 2

    from observability_migration.targets.kibana.render_audit_driver import run_audit_cli

    failures = 0
    for path in native:
        doc = json.loads(path.read_text(encoding="utf-8"))
        payload = doc.get("payload") or {}
        count = expand_rows(payload)
        audit_id = f"{doc.get('dashboard_id') or path.stem}{_AUDIT_SUFFIX}"
        payload = dict(payload)
        payload["title"] = f"{payload.get('title', path.stem)} (render audit)"

        url = f"{args.kibana_url.rstrip('/')}/api/dashboards/{audit_id}"
        status, detail = _request("PUT", url, payload, args.kibana_api_key)
        if status >= 300:
            print(f"{path.name}: upload failed ({status}) {detail}", file=sys.stderr)
            failures += 1
            continue
        print(f"{path.name}: expanded {count} row(s) -> {audit_id}")

        audit_args = argparse.Namespace(
            kibana_url=args.kibana_url, dashboard_id=audit_id, space="",
            user_data_dir="", time_from=args.time_from, time_to=args.time_to,
            elements=True, migration_out=str(out), es_url=args.es_url,
            es_api_key=args.es_api_key, es_index=args.es_index, insecure=False,
            agent_browser=False, chrome_no_sandbox=True, fail_on_error=False,
        )
        try:
            run_audit_cli(audit_args)
        finally:
            if not args.keep:
                _request("DELETE", f"{args.kibana_url.rstrip('/')}"
                                   f"/api/saved_objects/dashboard/{audit_id}",
                         None, args.kibana_api_key)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
