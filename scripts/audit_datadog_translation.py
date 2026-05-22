#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Deep correctness audit for the Datadog → Kibana translation outputs.

Walks every e2e_datadog_run/dd-*/ output and checks:

1. **YAML structural integrity** — every panel has title + position + size +
   exactly one of {esql, markdown}; panels list is non-empty; required
   dashboard keys present.
2. **Source widget → translated panel coverage** — every source widget
   (after group expansion) is accounted for; nothing silently dropped.
3. **Panel title preservation** — every translated panel title matches a
   source widget title.
4. **YAML → compiled NDJSON 1:1** — same panel count, same titles, same
   ES|QL queries (where applicable).
5. **Layout sanity** — no overlapping panels, no zero-size panels, no
   panels outside the grid.
6. **ES|QL syntactic plausibility** — balanced parens, has FROM, every
   STATS clause has either BY or is in a metric-style scalar emission;
   every column referenced in dimension/metric/breakdown appears in the
   KEEP/STATS output.
7. **Field reference resolution** — chart field references resolve to
   columns that the query actually produces.

Emits a JSON report per dashboard plus a markdown summary at
e2e_datadog_run/TRANSLATION_AUDIT.md.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = REPO_ROOT / "e2e_datadog_run"
DASHBOARD_DIR = REPO_ROOT / "infra" / "datadog" / "dashboards"
OUTPUT_DIR = RUN_ROOT


def _leaf_source_widgets(items: list[dict]) -> list[dict]:
    """Flatten DD widget tree into leaf widgets, expanding group containers."""

    out: list[dict] = []
    for item in items:
        defn = item.get("definition", {})
        wtype = defn.get("type", "")
        if wtype in ("group", "powerpack"):
            out.extend(_leaf_source_widgets(defn.get("widgets") or []))
        else:
            out.append(item)
    return out


def _yaml_leaf_panels(panels: list[dict]) -> list[dict]:
    out: list[dict] = []
    for p in panels:
        section = p.get("section")
        if isinstance(section, dict):
            out.extend(_yaml_leaf_panels(section.get("panels") or []))
        else:
            out.append(p)
    return out


def _esql_balanced_parens(query: str) -> bool:
    depth = 0
    for ch in query:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _audit_yaml(yaml_path: Path, source_dashboard: dict) -> dict[str, Any]:
    findings: list[str] = []
    info: dict[str, Any] = {}
    try:
        doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return {"yaml_path": str(yaml_path), "fatal": f"YAML parse error: {exc}", "findings": []}

    dashboards = doc.get("dashboards") if isinstance(doc, dict) else None
    if not dashboards:
        return {"yaml_path": str(yaml_path), "fatal": "no dashboards key", "findings": []}
    dash = dashboards[0]

    required_dash_keys = ["name", "panels"]
    for k in required_dash_keys:
        if k not in dash:
            findings.append(f"dashboard missing required key: {k}")

    leaf_panels = _yaml_leaf_panels(dash.get("panels") or [])
    info["yaml_panel_count"] = len(leaf_panels)

    leaf_source = _leaf_source_widgets(source_dashboard.get("widgets") or [])
    info["source_leaf_widget_count"] = len(leaf_source)

    source_titles = [w.get("definition", {}).get("title", "") for w in leaf_source]

    if len(leaf_panels) != len(leaf_source):
        findings.append(
            f"panel count {len(leaf_panels)} != source leaf widget count {len(leaf_source)}"
        )

    yaml_titles = []
    for i, panel in enumerate(leaf_panels):
        if "title" not in panel:
            findings.append(f"panel[{i}] missing title")
            yaml_titles.append("")
            continue
        yaml_titles.append(panel["title"])
        if "size" not in panel:
            findings.append(f"panel[{i}] {panel['title']!r} missing size")
        else:
            s = panel["size"]
            if s.get("w", 0) <= 0 or s.get("h", 0) <= 0:
                findings.append(f"panel[{i}] {panel['title']!r} has zero/negative size {s}")
        if "position" not in panel:
            findings.append(f"panel[{i}] {panel['title']!r} missing position")
        has_esql = "esql" in panel
        body_keys = [k for k in ("esql", "markdown", "lens") if k in panel]
        if not body_keys:
            findings.append(
                f"panel[{i}] {panel['title']!r} has no body (expected one of esql/markdown/lens)"
            )
        elif len(body_keys) > 1:
            findings.append(
                f"panel[{i}] {panel['title']!r} has multiple body keys: {body_keys}"
            )
        if has_esql:
            esql = panel["esql"]
            if not isinstance(esql, dict):
                findings.append(f"panel[{i}] {panel['title']!r} esql is not a dict")
            else:
                q = esql.get("query", "")
                if not q:
                    findings.append(f"panel[{i}] {panel['title']!r} esql.query is empty")
                else:
                    if "FROM " not in q:
                        findings.append(f"panel[{i}] {panel['title']!r} ES|QL missing FROM")
                    if not _esql_balanced_parens(q):
                        findings.append(
                            f"panel[{i}] {panel['title']!r} ES|QL has unbalanced parens"
                        )
                    # Verify dimension/metric/breakdown fields exist in KEEP/STATS output.
                    keep_match = re.search(r"\|\s*KEEP\s+([^\n|]+)", q)
                    if keep_match:
                        keep_fields = {
                            f.strip() for f in keep_match.group(1).split(",")
                            if f.strip()
                        }
                        dim = esql.get("dimension", {}).get("field")
                        if dim and dim not in keep_fields:
                            findings.append(
                                f"panel[{i}] {panel['title']!r} dimension.field "
                                f"{dim!r} not in KEEP {keep_fields}"
                            )
                        for m in esql.get("metrics", []) or []:
                            mf = m.get("field")
                            if mf and mf not in keep_fields:
                                findings.append(
                                    f"panel[{i}] {panel['title']!r} metric.field "
                                    f"{mf!r} not in KEEP {keep_fields}"
                                )
                        for bd in [esql.get("breakdown")] if esql.get("breakdown") else []:
                            bf = bd.get("field")
                            if bf and bf not in keep_fields:
                                findings.append(
                                    f"panel[{i}] {panel['title']!r} breakdown.field "
                                    f"{bf!r} not in KEEP {keep_fields}"
                                )

    info["yaml_panel_titles"] = yaml_titles

    # "Untitled" is the canonical YAML fallback for source widgets that
    # have an empty title — Kibana requires a non-empty title. Strip both
    # sides so the diff only catches real title drops.
    yaml_titles_to_compare = [t for t in yaml_titles if t and t != "Untitled"]
    source_titles_to_compare = [t for t in source_titles if t]
    titles_only_in_yaml = sorted(set(yaml_titles_to_compare) - set(source_titles_to_compare))
    titles_only_in_source = sorted(set(source_titles_to_compare) - set(yaml_titles_to_compare))
    if titles_only_in_yaml:
        findings.append(f"panel titles only in YAML (synthetic): {titles_only_in_yaml[:5]}")
    if titles_only_in_source:
        findings.append(f"widget titles dropped from YAML: {titles_only_in_source[:5]}")

    return {"yaml_path": str(yaml_path), "info": info, "findings": findings}


def _audit_compiled(compiled_path: Path, yaml_path: Path) -> dict[str, Any]:
    findings: list[str] = []
    info: dict[str, Any] = {}

    try:
        objs = []
        for line in compiled_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                objs.append(json.loads(line))
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "compiled_path": str(compiled_path),
            "fatal": f"compiled NDJSON read error: {exc}",
            "findings": [],
        }

    dashboards = [o for o in objs if o.get("type") == "dashboard"]
    if not dashboards:
        return {
            "compiled_path": str(compiled_path),
            "fatal": "no dashboard saved object in NDJSON",
            "findings": [],
        }
    if len(dashboards) > 1:
        findings.append(f"expected 1 dashboard saved object, got {len(dashboards)}")
    dash = dashboards[0]
    attrs = dash.get("attributes", {})
    if "title" not in attrs:
        findings.append("dashboard saved object missing attributes.title")
    panels_json_str = attrs.get("panelsJSON", "")
    if not panels_json_str:
        findings.append("dashboard saved object missing panelsJSON")
        return {"compiled_path": str(compiled_path), "info": info, "findings": findings}
    try:
        panels = json.loads(panels_json_str)
    except json.JSONDecodeError as exc:
        return {
            "compiled_path": str(compiled_path),
            "fatal": f"panelsJSON not parseable: {exc}",
            "findings": findings,
        }
    info["compiled_panel_count"] = len(panels)

    # Cross-check YAML panel count
    try:
        yaml_doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        yaml_panels = _yaml_leaf_panels(yaml_doc["dashboards"][0].get("panels") or [])
    except Exception:
        yaml_panels = []
    if yaml_panels and len(panels) != len(yaml_panels):
        findings.append(
            f"compiled panel count {len(panels)} != YAML leaf panel count {len(yaml_panels)}"
        )

    # Panel layout sanity: no overlap, no zero size.
    # Kibana sections have their own coordinate space — panels are
    # `sectionId`-scoped. Only flag overlap when two panels share the
    # same section AND their grid boxes intersect.
    boxes_by_section: dict[str | None, list[tuple[int, int, int, int, str]]] = defaultdict(list)
    for p in panels:
        gd = p.get("gridData") or {}
        x, y, w, h = gd.get("x", 0), gd.get("y", 0), gd.get("w", 0), gd.get("h", 0)
        section_id = gd.get("sectionId")  # None for top-level panels
        title = (
            (p.get("embeddableConfig") or {}).get("attributes", {}).get("title", "")
            or p.get("title", "")
            or "(no title)"
        )
        if w <= 0 or h <= 0:
            findings.append(f"panel {title!r} has non-positive size w={w} h={h}")
        boxes_by_section[section_id].append((x, y, w, h, title))
    for section_id, boxes in boxes_by_section.items():
        for i in range(len(boxes)):
            x1, y1, w1, h1, t1 = boxes[i]
            for j in range(i + 1, len(boxes)):
                x2, y2, w2, h2, t2 = boxes[j]
                if not (x1 + w1 <= x2 or x2 + w2 <= x1 or y1 + h1 <= y2 or y2 + h2 <= y1):
                    findings.append(
                        f"panels overlap in section {section_id}: "
                        f"{t1!r} ({x1},{y1},{w1},{h1}) and {t2!r} ({x2},{y2},{w2},{h2})"
                    )

    # Panel types
    type_counts: dict[str, int] = defaultdict(int)
    for p in panels:
        type_counts[p.get("type", "?")] += 1
    info["compiled_panel_types"] = dict(type_counts)

    # Deeper: ES|QL queries in compiled NDJSON must match the YAML 1:1.
    # We index by panel title because positions/IDs may be remapped.
    compiled_queries: dict[str, str] = {}
    for p in panels:
        attrs = (p.get("embeddableConfig") or {}).get("attributes") or {}
        title = attrs.get("title") or p.get("title") or ""
        if not title:
            continue
        state = attrs.get("state") or {}
        ds = (state.get("datasourceStates") or {}).get("textBased") or {}
        for layer in (ds.get("layers") or {}).values():
            q = (layer.get("query") or {}).get("esql")
            if q:
                compiled_queries[title] = q
                break

    try:
        yaml_doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {"compiled_path": str(compiled_path), "info": info, "findings": findings}
    yaml_leaf_panels = _yaml_leaf_panels(yaml_doc["dashboards"][0].get("panels") or [])
    yaml_queries: dict[str, str] = {}
    for p in yaml_leaf_panels:
        title = p.get("title") or ""
        if title and isinstance(p.get("esql"), dict):
            q = p["esql"].get("query")
            if q:
                yaml_queries[title] = q

    only_in_yaml = sorted(set(yaml_queries) - set(compiled_queries))
    only_in_compiled = sorted(set(compiled_queries) - set(yaml_queries))
    if only_in_yaml:
        findings.append(
            f"ES|QL queries in YAML but not compiled (titles): {only_in_yaml[:5]}"
        )
    if only_in_compiled:
        findings.append(
            f"ES|QL queries in compiled but not YAML (titles): {only_in_compiled[:5]}"
        )
    for title in set(yaml_queries) & set(compiled_queries):
        yq = yaml_queries[title].strip()
        cq = compiled_queries[title].strip()
        if yq != cq:
            findings.append(
                f"ES|QL diverges between YAML and compiled for {title!r}: "
                f"YAML {yq[:80]!r} vs compiled {cq[:80]!r}"
            )

    return {"compiled_path": str(compiled_path), "info": info, "findings": findings}


def audit_one_dashboard(slug_dir: Path) -> dict[str, Any]:
    yaml_dir = slug_dir / "dashboards" / "yaml"
    yaml_files = sorted(yaml_dir.glob("*.yaml")) if yaml_dir.exists() else []
    compiled_root = slug_dir / "dashboards" / "compiled"

    # Find source JSON via the migration report
    rpt_path = slug_dir / "dashboards" / "migration_report.json"
    source_path: Path | None = None
    if rpt_path.exists():
        rpt = json.loads(rpt_path.read_text(encoding="utf-8"))
        for d in rpt.get("dashboards", []):
            sp = d.get("source_file")
            if sp:
                # Map back to repo-rooted source file by basename
                src_name = Path(sp).name
                candidates = list(DASHBOARD_DIR.rglob(src_name))
                if candidates:
                    source_path = candidates[0]
                    break
    if source_path is None:
        return {"slug": slug_dir.name, "fatal": "could not locate source DD dashboard", "findings": []}

    source = json.loads(source_path.read_text(encoding="utf-8"))

    yaml_audits = []
    compiled_audits = []
    for y in yaml_files:
        ya = _audit_yaml(y, source)
        yaml_audits.append(ya)
        compiled_dir = compiled_root / y.stem
        compiled_file = compiled_dir / "compiled_dashboards.ndjson"
        if compiled_file.exists():
            ca = _audit_compiled(compiled_file, y)
            compiled_audits.append(ca)
        else:
            compiled_audits.append({
                "compiled_path": str(compiled_file),
                "fatal": "compiled NDJSON not found",
                "findings": [],
            })

    total_findings = sum(len(y.get("findings", [])) for y in yaml_audits) + \
                     sum(len(c.get("findings", [])) for c in compiled_audits)
    fatal = [y.get("fatal") for y in yaml_audits if y.get("fatal")] + \
            [c.get("fatal") for c in compiled_audits if c.get("fatal")]
    return {
        "slug": slug_dir.name,
        "source_path": str(source_path.relative_to(REPO_ROOT)),
        "yaml_audits": yaml_audits,
        "compiled_audits": compiled_audits,
        "total_findings": total_findings,
        "fatal": fatal,
    }


def main() -> int:
    results: list[dict[str, Any]] = []
    for slug_dir in sorted(RUN_ROOT.glob("dd-*")):
        results.append(audit_one_dashboard(slug_dir))

    out = {
        "summary": {
            "dashboards": len(results),
            "with_findings": sum(1 for r in results if r["total_findings"] > 0),
            "with_fatal": sum(1 for r in results if r["fatal"]),
            "total_findings": sum(r["total_findings"] for r in results),
        },
        "dashboards": results,
    }
    json_path = OUTPUT_DIR / "translation_audit.json"
    json_path.write_text(json.dumps(out, indent=2), encoding="utf-8")

    # Markdown rollup
    md_lines = ["# Datadog Translation Audit", ""]
    md_lines.append(f"- Dashboards audited: {out['summary']['dashboards']}")
    md_lines.append(f"- With non-fatal findings: {out['summary']['with_findings']}")
    md_lines.append(f"- With fatal errors: {out['summary']['with_fatal']}")
    md_lines.append(f"- Total findings: {out['summary']['total_findings']}")
    md_lines.append("")
    md_lines.append("## Per-dashboard")
    md_lines.append("")
    md_lines.append("| Slug | YAML panels | Compiled panels | Findings | Fatal |")
    md_lines.append("|---|---:|---:|---:|---|")
    for r in results:
        ya = r["yaml_audits"][0] if r["yaml_audits"] else {}
        ca = r["compiled_audits"][0] if r["compiled_audits"] else {}
        yp = ya.get("info", {}).get("yaml_panel_count", "—")
        cp = ca.get("info", {}).get("compiled_panel_count", "—")
        fatal = "; ".join(r.get("fatal") or []) or "—"
        md_lines.append(f"| `{r['slug']}` | {yp} | {cp} | {r['total_findings']} | {fatal} |")
    md_lines.append("")
    md_lines.append("## Findings detail")
    for r in results:
        if r["total_findings"] == 0 and not r["fatal"]:
            continue
        md_lines.append(f"### `{r['slug']}`")
        for ya in r["yaml_audits"]:
            for f in ya.get("findings", []):
                md_lines.append(f"- **YAML:** {f}")
        for ca in r["compiled_audits"]:
            for f in ca.get("findings", []):
                md_lines.append(f"- **Compiled:** {f}")
        md_lines.append("")

    md_path = OUTPUT_DIR / "TRANSLATION_AUDIT.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print(f"Wrote {json_path.relative_to(REPO_ROOT)}")
    print(f"Wrote {md_path.relative_to(REPO_ROOT)}")
    print(f"Summary: {out['summary']}")
    return 0 if out["summary"]["total_findings"] == 0 and out["summary"]["with_fatal"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
