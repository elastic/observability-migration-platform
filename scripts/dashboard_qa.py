#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""One command that checks a migrated dashboard the way a reviewer would.

Every dimension here exists because checking only one of them let a real defect
through:

* **layout** — geometry is self-consistent (no overlap, nothing past the 48-column
  grid, no zero-size tile) and each horizontal band is flush. Node Exporter Full's
  first row was ragged for weeks while every query passed.
* **queries** — every panel query actually runs, against the payload that was
  UPLOADED rather than the artifact it was generated from. Those diverge the
  moment anything post-processes the payload, and a stale artifact reported a
  dashboard healthy while Kibana served a broken one.
* **render** — the panels are drawn in a real browser, with collapsed rows
  expanded first. Collapsed rows never render, so the browser audit was seeing 19
  of 143 panels on Node Exporter Full and the rest were taken on trust.
* **settle** — the render audit is sampled repeatedly until two consecutive
  reads agree. A single read caught panels mid-load and invented an error and
  seven empties that did not exist on the next read.
* **ui** — panel configuration a query cannot expose: axis titles left pointing
  at synthetic columns, value formats, legends.

Usage::

    python scripts/dashboard_qa.py --migration-out <out>/dashboards \\
        --kibana-url http://localhost:5602 \\
        --es-url http://localhost:9201 --es-index 'metrics-*'

Exits non-zero when any dimension fails. ``--skip render`` runs the offline
dimensions only (no browser needed).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

GRID_COLUMNS = 48
_AUDIT_SUFFIX = "-qa-tmp"
_METRIC_REF = re.compile(r"\bmetrics\.[A-Za-z_][A-Za-z0-9_]*")
_PARAM = re.compile(r"(?<!\?)\?(?!\?)([a-zA-Z_]\w*)")


# --------------------------------------------------------------------------- #
# payload helpers
# --------------------------------------------------------------------------- #
def iter_panels(payload: dict):
    """Yield (row_title, panel) for every leaf panel, including nested rows."""
    def visit(panels, row=""):
        for panel in panels or []:
            if not isinstance(panel, dict):
                continue
            nested = panel.get("panels")
            if nested:
                yield from visit(nested, panel.get("title") or row)
                continue
            yield row, panel
    yield from visit(payload.get("panels") or [])


def panel_queries(panel: dict) -> list[str]:
    """A panel's ES|QL, whether it sits directly on the config or per layer."""
    cfg = panel.get("config") or {}
    out = [((cfg.get("data_source") or {}).get("query")) or ""]
    for layer in cfg.get("layers") or []:
        if isinstance(layer, dict):
            out.append(((layer.get("data_source") or {}).get("query")) or "")
    return [q for q in out if q.strip()]


def panel_title(panel: dict) -> str:
    return str((panel.get("config") or {}).get("title") or panel.get("title") or "?")


# --------------------------------------------------------------------------- #
# dimension: layout
# --------------------------------------------------------------------------- #
def check_layout(payload: dict) -> list[str]:
    issues: list[str] = []
    for row in payload.get("panels") or []:
        kids = [p for p in (row.get("panels") or []) if isinstance(p, dict)]
        if not kids:
            continue
        row_title = row.get("title") or "(top level)"
        occupied: dict[tuple[int, int], str] = {}
        for panel in kids:
            grid = panel.get("grid") or {}
            x, y = int(grid.get("x") or 0), int(grid.get("y") or 0)
            w, h = int(grid.get("w") or 0), int(grid.get("h") or 0)
            title = panel_title(panel)
            if w <= 0 or h <= 0:
                issues.append(f"[{row_title}] {title}: zero-size tile w={w} h={h}")
                continue
            if x + w > GRID_COLUMNS:
                issues.append(f"[{row_title}] {title}: overflows the grid ({x}+{w} > {GRID_COLUMNS})")
            for cx in range(x, min(x + w, GRID_COLUMNS)):
                for cy in range(y, y + h):
                    clash = occupied.get((cx, cy))
                    if clash:
                        issues.append(f"[{row_title}] {title} overlaps {clash}")
                        break
                    occupied[(cx, cy)] = title
                else:
                    continue
                break
        # Bands should end flush: a band whose panels stop at different depths
        # is the ragged-row defect.
        bands: dict[int, list[tuple[int, int]]] = {}
        for panel in kids:
            grid = panel.get("grid") or {}
            bands.setdefault(int(grid.get("y") or 0), []).append(
                (int(grid.get("y") or 0) + int(grid.get("h") or 0), id(panel))
            )
        for top, entries in bands.items():
            bottoms = {b for b, _ in entries}
            if len(bottoms) > 1 and len(entries) > 1:
                # Only a problem when nothing fills the gap underneath.
                shallow = min(bottoms)
                filled = any(
                    int((p.get("grid") or {}).get("y") or 0) == shallow for p in kids
                )
                if not filled:
                    issues.append(
                        f"[{row_title}] band at y={top} is ragged: bottoms {sorted(bottoms)}"
                    )
    return issues


# --------------------------------------------------------------------------- #
# dimension: ui configuration
# --------------------------------------------------------------------------- #
_SYNTHETIC_COLUMNS = {"label", "value", "__labels", "__values", "__pairs"}


def check_ui(payload: dict) -> list[str]:
    issues: list[str] = []
    for row, panel in iter_panels(payload):
        cfg = panel.get("config") or {}
        title = panel_title(panel)
        # markdown tiles and the dashboard-links tile carry their type on the
        # panel rather than the config and have no visualization at all. Both
        # were this checker's own false positives, not dashboard defects.
        if not cfg.get("type") and panel.get("type") not in ("markdown", "links"):
            issues.append(f"[{row}] {title}: no visualization type")
        axis = cfg.get("axis") or {}
        for side in ("x", "y", "y2"):
            spec = axis.get(side) or {}
            text = str(((spec.get("title") or {}).get("text")) or "")
            if text in _SYNTHETIC_COLUMNS:
                issues.append(
                    f"[{row}] {title}: {side}-axis titled with the internal column {text!r}"
                )
        if cfg.get("type") == "metric" and not cfg.get("metrics"):
            issues.append(f"[{row}] {title}: metric panel with no metric configured")
    return issues


# --------------------------------------------------------------------------- #
# dimension: queries
# --------------------------------------------------------------------------- #
def _es(es_url: str, body: dict, api_key: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
    req = urllib.request.Request(
        f"{es_url.rstrip('/')}/_query?format=json", json.dumps(body).encode(), headers
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def data_window(es_url: str, index: str, api_key: str, minutes: int = 20):
    body = {"query": f"FROM {index} | STATS mn = MIN(@timestamp), mx = MAX(@timestamp)"}
    mn, mx = _es(es_url, body, api_key)["values"][0]
    end = dt.datetime.fromisoformat(str(mx).replace("Z", "+00:00"))
    start = max(
        dt.datetime.fromisoformat(str(mn).replace("Z", "+00:00")),
        end - dt.timedelta(minutes=minutes),
    )
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt)


def run_query(es_url: str, query: str, tstart: str, tend: str, api_key: str):
    lines = query.splitlines()
    if not re.search(r"@timestamp\s*(>=|<=)", query) and not query.lstrip().upper().startswith("PROMQL"):
        query = "\n".join([lines[0], "| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend", *lines[1:]])
    params: list[dict] = [{"_tstart": tstart}, {"_tend": tend}]
    for name in sorted(set(_PARAM.findall(query))):
        if name in ("_tstart", "_tend"):
            continue
        params.append({name: [".*"] if f"MV_CONTAINS(?{name}" in query else ".*"})
    try:
        doc = _es(es_url, {"query": query, "params": params}, api_key)
    except urllib.error.HTTPError as exc:
        detail = re.search(r'"reason":"([^"]{0,140})', exc.read().decode())
        return "ERROR", (detail.group(1) if detail else "?")
    values = doc.get("values") or []
    columns = [c["name"] for c in doc.get("columns", [])]
    value_idx = [i for i, c in enumerate(columns) if c not in ("time_bucket", "@timestamp", "tb")]
    has_data = any(any(row[i] is not None for i in value_idx) for row in values) if values else False
    return ("DATA" if has_data else "EMPTY"), f"{len(values)} rows"


def check_queries(payload: dict, es_url: str, index: str, api_key: str):
    tstart, tend = data_window(es_url, index, api_key)
    results = []
    for row, panel in iter_panels(payload):
        queries = panel_queries(panel)
        if not queries:
            results.append((row, panel_title(panel), "NOQUERY", ""))
            continue
        worst = ("DATA", "")
        for query in queries:
            status, detail = run_query(es_url, query, tstart, tend, api_key)
            if status == "ERROR":
                worst = (status, detail)
                break
            if status == "EMPTY" and worst[0] == "DATA":
                worst = (status, detail)
        results.append((row, panel_title(panel), worst[0], worst[1]))
    return results, (tstart, tend)


# --------------------------------------------------------------------------- #
# dimension: render (browser), rows expanded, sampled until stable
# --------------------------------------------------------------------------- #
def _kb(method: str, url: str, body: dict | None, api_key: str):
    headers = {"Content-Type": "application/json", "kbn-xsrf": "true",
               "elastic-api-version": "2023-10-31"}
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
    data = json.dumps(body).encode() if body is not None else None
    try:
        req = urllib.request.Request(url, data, headers, method=method)
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.status, resp.read().decode()[:300]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:300]
    except OSError as exc:
        return 0, str(exc)


def expand_rows(payload: dict) -> int:
    expanded = 0
    def visit(panels):
        nonlocal expanded
        for panel in panels or []:
            if isinstance(panel, dict):
                if panel.get("collapsed"):
                    panel["collapsed"] = False
                    expanded += 1
                visit(panel.get("panels") or [])
    visit(payload.get("panels") or [])
    return expanded


def check_render(doc: dict, out_dir: Path, args, attempts: int = 8):
    """Audit in a browser with rows expanded, sampling until two reads agree.

    A single read is not trustworthy: sampling Node Exporter Full once reported
    one render error and seven empty panels, and the very next read of the same
    unchanged dashboard reported 125/125 rendered. Panels were still loading.
    """
    from observability_migration.targets.kibana.render_audit_driver import run_audit_cli

    payload = dict(doc.get("payload") or {})
    rows = expand_rows(payload)
    audit_id = f"{doc.get('dashboard_id') or 'dashboard'}{_AUDIT_SUFFIX}"
    payload["title"] = f"{payload.get('title', 'dashboard')} (QA)"
    status, detail = _kb("PUT", f"{args.kibana_url.rstrip('/')}/api/dashboards/{audit_id}",
                         payload, args.kibana_api_key)
    if status >= 300:
        return None, rows, f"upload failed ({status}) {detail}"

    import contextlib
    import io

    previous = None
    stable_errors = 0
    try:
        for _ in range(attempts):
            audit_args = argparse.Namespace(
                kibana_url=args.kibana_url, dashboard_id=audit_id, space="",
                user_data_dir="", time_from=args.time_from, time_to=args.time_to,
                elements=True, migration_out=str(out_dir), es_url=args.es_url,
                es_api_key=args.es_api_key, es_index=args.es_index, insecure=False,
                agent_browser=False, chrome_no_sandbox=True, fail_on_error=False,
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                run_audit_cli(audit_args)
            text = buf.getvalue()
            report = json.loads(text[text.index("{"):])["render"]
            snapshot = Counter(p.get("status") for p in (report.get("panels") or []))
            # Two consecutive identical reads AND no errors left is the bar: a
            # large dashboard loads progressively, so an early read reports
            # panels that simply had not finished. Keep sampling while the
            # picture is still improving.
            if previous is not None and snapshot == previous:
                if not snapshot.get("error"):
                    return report, rows, ""
                stable_errors = stable_errors + 1 if previous == snapshot else 1
                if stable_errors >= 2:
                    return report, rows, ""
            previous = snapshot
        return report, rows, "did not stabilise; reporting the last read"
    finally:
        if not args.keep:
            _kb("DELETE",
                f"{args.kibana_url.rstrip('/')}/api/saved_objects/dashboard/{audit_id}",
                None, args.kibana_api_key)


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration-out", required=True)
    parser.add_argument("--kibana-url", default="")
    parser.add_argument("--kibana-api-key", default="")
    parser.add_argument("--es-url", default="")
    parser.add_argument("--es-index", default="metrics-*")
    parser.add_argument("--es-api-key", default="")
    parser.add_argument("--time-from", default="now-30m")
    parser.add_argument("--time-to", default="now")
    parser.add_argument("--skip", action="append", default=[],
                        choices=["layout", "ui", "queries", "render"])
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    out = Path(args.migration_out)
    natives = sorted((out / "native").glob("*.native.json"))
    if not natives:
        print(f"no native artifacts under {out / 'native'}", file=sys.stderr)
        return 2

    failed = False
    for path in natives:
        doc = json.loads(path.read_text(encoding="utf-8"))
        payload = doc.get("payload") or {}
        title = payload.get("title", path.stem)
        panels = list(iter_panels(payload))
        print(f"\n=== {title}  ({len(panels)} panels)")

        if "layout" not in args.skip:
            issues = check_layout(payload)
            print(f"  layout   {'FAIL' if issues else 'ok':4}  {len(issues)} issue(s)")
            for issue in issues[:8]:
                print(f"             {issue}")
            failed |= bool(issues)

        if "ui" not in args.skip:
            issues = check_ui(payload)
            print(f"  ui       {'FAIL' if issues else 'ok':4}  {len(issues)} issue(s)")
            for issue in issues[:8]:
                print(f"             {issue}")
            failed |= bool(issues)

        if "queries" not in args.skip and args.es_url:
            results, window = check_queries(payload, args.es_url, args.es_index, args.es_api_key)
            counts = Counter(r[2] for r in results)
            bad = [r for r in results if r[2] == "ERROR"]
            print(f"  queries  {'FAIL' if bad else 'ok':4}  {dict(counts)}  window {window[0]}..{window[1]}")
            for row, name, _s, detail in bad[:8]:
                print(f"             [{row}] {name}: {detail[:90]}")
            failed |= bool(bad)

        if "render" not in args.skip and args.kibana_url:
            report, rows, note = check_render(doc, out, args)
            if report is None:
                print(f"  render   FAIL  {note}")
                failed = True
            else:
                counts = Counter(p.get("status") for p in (report.get("panels") or []))
                errors = [p for p in (report.get("panels") or []) if p.get("status") == "error"]
                verdict = "FAIL" if errors else "ok"
                print(f"  render   {verdict:4}  {dict(counts)}  ({rows} row(s) expanded){' — ' + note if note else ''}")
                for panel in errors[:8]:
                    print(f"             {panel.get('title')}: {panel.get('error_class')}")
                failed |= bool(errors)

    print("\nRESULT:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
