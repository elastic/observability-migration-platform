#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Replace provably-absent-metric panels with an informative note.

A panel whose metric does not exist in the target renders a red "Unknown column"
error card in Kibana, where Grafana would draw an empty chart. The migration
deliberately keeps the real field reference so the panel self-heals the moment the
metric is ingested — that is the right default, and this script does not change it.

But an operator publishing a board to a team wants no error cards on it. This
rewrites those panels, in place and in their existing grid slot, into a markdown
note naming the missing metric and what to ingest to get the panel back. The
layout is unchanged, nothing renders red, and the note says exactly what is
missing rather than pretending the panel is fine.

Reversible by construction: re-run the migration once the metric lands and the
real panel comes back.

Usage::

    python scripts/curate_absent_metric_panels.py \\
        --migration-out <out>/dashboards --dry-run

Drop ``--dry-run`` to rewrite the ``native/*.native.json`` payloads in place, then
upload them.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_MISSING_RE = re.compile(
    r"Target field (?P<field>\S+) is missing from live schema discovery"
)
_METRIC_REF_RE = re.compile(r"\bmetrics\.[A-Za-z_][A-Za-z0-9_]*")


def live_metric_fields(es_url: str, index: str, api_key: str = "") -> set[str]:
    """Every ``metrics.*`` field the target actually has."""
    import urllib.request

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
    url = f"{es_url.rstrip('/')}/{index}/_field_caps?fields=metrics.*"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
        return set(json.load(resp).get("fields", {}))


def absent_from_queries(payload: dict, present: set[str]) -> dict[str, set[str]]:
    """Panel title -> metric fields its query references but the target lacks.

    The migration report only lists what its own validator flagged. Checking the
    emitted query against live field caps is authoritative and catches panels the
    validator missed -- two on MySQL Overview, which still rendered red.
    """
    out: dict[str, set[str]] = {}

    def visit(panels: list) -> None:
        for panel in panels or []:
            if not isinstance(panel, dict):
                continue
            visit(panel.get("panels") or [])
            config = panel.get("config") or {}
            title = config.get("title")
            # A panel carries its query either directly or, for multi-series
            # visualisations, once per layer. Reading only the direct spelling
            # missed every xy panel -- two on MySQL Overview stayed red.
            queries = [((config.get("data_source") or {}).get("query")) or ""]
            for layer in config.get("layers") or []:
                if isinstance(layer, dict):
                    queries.append(((layer.get("data_source") or {}).get("query")) or "")
            query = "\n".join(q for q in queries if q)
            if not title or not query:
                continue
            missing = {ref for ref in _METRIC_REF_RE.findall(query) if ref not in present}
            if missing:
                out.setdefault(str(title), set()).update(missing)

    visit(payload.get("panels") or [])
    return out


def absent_by_panel(report: dict) -> dict[str, set[str]]:
    """Panel title -> the fields live discovery proved absent."""
    out: dict[str, set[str]] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            title = node.get("title")
            reasons = node.get("reasons") or []
            if title and isinstance(reasons, list):
                for reason in reasons:
                    match = _MISSING_RE.search(str(reason))
                    if match:
                        out.setdefault(str(title), set()).add(match.group("field"))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(report)
    return out


def note_panel(panel: dict, fields: set[str]) -> dict:
    """A markdown panel occupying the same grid slot as ``panel``."""
    config = panel.get("config") or {}
    title = config.get("title") or "Panel"
    listed = "\n".join(f"- `{name}`" for name in sorted(fields))
    return {
        "grid": dict(panel.get("grid") or {}),
        "type": "markdown",
        "config": {
            "title": title,
            "content": (
                f"**{title}** is not available yet.\n\n"
                "This panel's metrics are absent from the target index:\n\n"
                f"{listed}\n\n"
                "The translation is correct — the data has not been ingested. "
                "Re-run the migration once these metrics land and the panel "
                "returns automatically."
            ),
        },
    }


def rewrite(payload: dict, absent: dict[str, set[str]]) -> int:
    """Swap absent-metric panels for notes, in place. Returns the count."""
    changed = 0

    def visit(panels: list) -> None:
        nonlocal changed
        for index, panel in enumerate(panels):
            if not isinstance(panel, dict):
                continue
            nested = panel.get("panels")
            if isinstance(nested, list):
                visit(nested)
            config = panel.get("config") or {}
            title = config.get("title")
            if title in absent and panel.get("type") != "markdown":
                panels[index] = note_panel(panel, absent[title])
                changed += 1

    visit(payload.get("panels") or [])
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration-out", required=True)
    parser.add_argument("--es-url", default="", help="Verify absence against live field caps too.")
    parser.add_argument("--es-index", default="metrics-*")
    parser.add_argument("--es-api-key", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    out = Path(args.migration_out)
    try:
        report = json.loads((out / "migration_report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"could not read migration_report.json: {exc}", file=sys.stderr)
        return 2

    absent = absent_by_panel(report)
    present: set[str] = set()
    if args.es_url:
        try:
            present = live_metric_fields(args.es_url, args.es_index, args.es_api_key)
        except Exception as exc:
            print(f"warning: field-caps probe failed ({exc}); using the report only",
                  file=sys.stderr)
    if not absent and not present:
        print("No panel has a provably absent metric; nothing to curate.")
        return 0
    print(f"{len(absent)} panel(s) reference metrics absent from the target:")
    for title, fields in sorted(absent.items()):
        print(f"  {title}: {', '.join(sorted(fields))}")

    native_dir = out / "native"
    total = 0
    for path in sorted(native_dir.glob("*.native.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        panel_absent = dict(absent)
        if present:
            for title, fields in absent_from_queries(doc.get("payload") or {}, present).items():
                panel_absent.setdefault(title, set()).update(fields)
        count = rewrite(doc.get("payload") or {}, panel_absent)
        if not count:
            continue
        total += count
        if args.dry_run:
            print(f"  would rewrite {count} panel(s) in {path.name}")
            continue
        path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        print(f"  rewrote {count} panel(s) in {path.name}")
    if args.dry_run:
        print(f"\n(dry run) {total} panel(s) would become notes.")
    else:
        print(f"\n{total} panel(s) are now notes. Upload the rewritten payloads.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
