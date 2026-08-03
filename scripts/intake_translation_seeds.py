#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Propose committed translation regression seeds from live/smoke JSON reports.

Parses offline validation or smoke reports and emits seed JSON files under
``tests/fixtures/translation_seeds/``. No live Elasticsearch calls — only
report JSON is read.

Minimal accepted report schema::

    {
      "source": "grafana",
      "panels": [
        {
          "title": "CPU Frequency Scaling",
          "status": "fail",
          "disposition": "real_bug",
          "error": "Unknown column [node_cpu_scaling_frequency_hertz]",
          "esql_query": "...",
          "targets": [{"expr": "..."}]
        }
      ]
    }

Nested ``dashboards[].panels[]`` entries are also accepted. ``disposition`` may
be omitted when ``classification`` is present (live_validate shape).

Entries with ``disposition`` in ``{data_gap, field_gap}`` are skipped unless
``unknown_column_looks_like_alias_bug`` reclassifies them as translator bugs.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from observability_migration.adapters.source.grafana.esql_structural_oracle import (
    check_esql_structure,
    structural_errors,
)
from observability_migration.core.verification.disposition import (
    unknown_column_looks_like_alias_bug,
)

_SKIP_DISPOSITIONS = frozenset({"data_gap", "field_gap"})
_UNKNOWN_COLUMN = re.compile(r"Unknown column\s*\[([^\]]+)\]", re.IGNORECASE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug_from_title(title: str) -> str:
    slug = _SLUG_RE.sub("_", (title or "panel").lower()).strip("_")
    return slug or "panel"


def extract_unknown_column(error: str) -> str | None:
    match = _UNKNOWN_COLUMN.search(error or "")
    return match.group(1).strip() if match else None


def _panel_records(report: dict) -> list[dict]:
    panels = report.get("panels")
    if isinstance(panels, list):
        return [p for p in panels if isinstance(p, dict)]
    out: list[dict] = []
    for dash in report.get("dashboards") or []:
        if not isinstance(dash, dict):
            continue
        for panel in dash.get("panels") or []:
            if isinstance(panel, dict):
                out.append(panel)
    return out


def _promql_or_targets(panel: dict) -> list | str:
    if "promql_or_targets" in panel:
        return panel["promql_or_targets"]
    targets = panel.get("targets")
    if isinstance(targets, list):
        return targets
    expr = panel.get("expr") or panel.get("promql")
    if expr:
        return [{"expr": expr}]
    return []


def _rule_hint(esql_query: str, *, source: str = "grafana") -> str:
    if source == "datadog":
        from observability_migration.adapters.source.datadog.esql_structural_oracle import (
            check_datadog_esql_structure,
        )
        from observability_migration.adapters.source.datadog.esql_structural_oracle import (
            structural_errors as dd_structural_errors,
        )

        errs = dd_structural_errors(
            check_datadog_esql_structure(esql_query or "", status="ok", backend="esql")
        )
    else:
        errs = structural_errors(check_esql_structure(esql_query or ""))
    return errs[0].rule_id.value if errs else ""


def effective_disposition(panel: dict, esql_query: str) -> str | None:
    disposition = str(
        panel.get("disposition") or panel.get("classification") or ""
    ).strip()
    error = str(panel.get("error") or "")

    if disposition in _SKIP_DISPOSITIONS:
        column = extract_unknown_column(error)
        if column and unknown_column_looks_like_alias_bug(column, esql_query):
            return "real_bug"
        return None

    if disposition == "real_bug":
        return "real_bug"

    if panel.get("status") == "fail" and error and disposition not in _SKIP_DISPOSITIONS:
        return disposition or "real_bug"

    return None


def propose_seed(panel: dict, *, source: str, seed_id: str) -> dict | None:
    esql_query = str(panel.get("esql_query") or panel.get("query") or "")
    disposition = effective_disposition(panel, esql_query)
    if not disposition:
        return None

    return {
        "id": seed_id,
        "source": source,
        "panel_title": str(panel.get("title") or panel.get("panel_title") or ""),
        "promql_or_targets": _promql_or_targets(panel),
        "esql_query": esql_query,
        "error": str(panel.get("error") or ""),
        "disposition": disposition,
        "rule_hint": _rule_hint(esql_query, source=source),
    }


def propose_seeds(report: dict) -> list[dict]:
    source = str(report.get("source") or "grafana")
    seen: set[str] = set()
    seeds: list[dict] = []
    for panel in _panel_records(report):
        title = str(panel.get("title") or panel.get("panel_title") or "panel")
        seed_id = slug_from_title(title)
        if seed_id in seen:
            suffix = 2
            while f"{seed_id}_{suffix}" in seen:
                suffix += 1
            seed_id = f"{seed_id}_{suffix}"
        seen.add(seed_id)
        seed = propose_seed(panel, source=source, seed_id=seed_id)
        if seed:
            seeds.append(seed)
    return seeds


def write_seeds(seeds: list[dict], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for seed in seeds:
        path = out_dir / f"{seed['id']}.json"
        path.write_text(json.dumps(seed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written


def missing_seed_paths(seeds: list[dict], out_dir: Path) -> list[Path]:
    return [out_dir / f"{seed['id']}.json" for seed in seeds if not (out_dir / f"{seed['id']}.json").is_file()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path, help="live_validate or smoke JSON report")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("tests/fixtures/translation_seeds"),
        help="Directory for committed seed JSON files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print proposed seeds as JSON to stdout; do not write files",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 when proposals would create new seed files (nightly gate)",
    )
    args = parser.parse_args(argv)

    report = json.loads(args.report.read_text(encoding="utf-8"))
    seeds = propose_seeds(report)

    if args.dry_run:
        json.dump(seeds, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    missing = missing_seed_paths(seeds, args.out_dir)
    if args.check:
        if missing:
            for path in missing:
                print(f"MISSING SEED: {path}", file=sys.stderr)
            return 1
        return 0

    write_seeds(seeds, args.out_dir)
    for seed in seeds:
        print(f"WROTE {args.out_dir / seed['id']}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
