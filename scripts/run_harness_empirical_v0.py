#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""V0 offline harness empirical campaign.

Re-runs structural oracles on:
1. Prior live smoke reports (Grafana + Datadog after-seed)
2. Fresh translations of grafana_selected + infra/datadog fixtures

Writes JSON + markdown summary under a campaign output dir (default:
``harness_empirical_20260720/``). Does not call live Elasticsearch.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from observability_migration.adapters.source.datadog.esql_structural_oracle import (  # noqa: E402
    ESQL_EMITTING_BACKENDS,
    check_datadog_esql_structure,
)
from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE  # noqa: E402
from observability_migration.adapters.source.datadog.normalize import (  # noqa: E402
    normalize_dashboard,
)
from observability_migration.adapters.source.datadog.planner import plan_widget  # noqa: E402
from observability_migration.adapters.source.datadog.translate import (  # noqa: E402
    translate_widget,
)
from observability_migration.adapters.source.grafana.esql_structural_oracle import (  # noqa: E402
    check_esql_structure,
    structural_errors,
)
from observability_migration.adapters.source.grafana.panels import translate_panel  # noqa: E402
from observability_migration.adapters.source.grafana.rules import RulePackConfig  # noqa: E402
from observability_migration.adapters.source.grafana.schema import SchemaResolver  # noqa: E402

KNOWN_GRAFANA_LIVE_FAILS = {
    "CPU spent seconds in guests (VMs)",
    "CPU Frequency Scaling",
}


@dataclass
class PanelFinding:
    source: str
    dashboard: str
    panel: str
    status: str
    classification: str
    rule_ids: list[str]
    error: str
    query_preview: str


def _preview(query: str, n: int = 120) -> str:
    text = (query or "").replace("\n", " | ")
    return text if len(text) <= n else text[: n - 3] + "..."


def classify_smoke_panel(
    *,
    source: str,
    dashboard: str,
    panel: dict,
) -> PanelFinding:
    title = str(panel.get("panel") or panel.get("title") or "")
    status = str(panel.get("status") or "")
    error = str(panel.get("error") or "")
    query = str(panel.get("materialized_query") or panel.get("query") or panel.get("esql_query") or "")

    if source == "datadog":
        findings = check_datadog_esql_structure(query, status="ok", backend="esql")
    else:
        findings = check_esql_structure(query, feasibility="feasible")
    errs = structural_errors(findings)
    rule_ids = [f.rule_id.value for f in errs]

    if errs:
        classification = "caught_by_oracle"
    elif status == "fail" and error:
        # Prior live failure oracle did not catch
        if title in KNOWN_GRAFANA_LIVE_FAILS or "Unknown column" in error or "cannot be cast" in error:
            classification = "harness_gap"
        else:
            classification = "would_need_live"
    elif status == "empty":
        classification = "data_gap"
    elif status == "pass":
        classification = "clean"
    else:
        classification = "would_need_live"

    return PanelFinding(
        source=source,
        dashboard=dashboard,
        panel=title,
        status=status,
        classification=classification,
        rule_ids=rule_ids,
        error=error[:300],
        query_preview=_preview(query),
    )


def scan_smoke_report(path: Path, *, source: str) -> list[PanelFinding]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: list[PanelFinding] = []
    for dash in raw.get("dashboards") or []:
        title = str(dash.get("title") or dash.get("id") or "")
        for panel in dash.get("panels") or []:
            if not isinstance(panel, dict):
                continue
            # Skip non-query panels with no query text
            query = panel.get("materialized_query") or panel.get("query") or ""
            if not query and panel.get("status") not in {"fail", "empty"}:
                continue
            out.append(classify_smoke_panel(source=source, dashboard=title, panel=panel))
    return out


def _walk_grafana_panels(panels, out: list) -> None:
    for p in panels or []:
        if p.get("type") == "row":
            _walk_grafana_panels(p.get("panels"), out)
            continue
        out.append(p)
        _walk_grafana_panels(p.get("panels"), out)


def translate_grafana_fixtures(paths: list[Path]) -> list[PanelFinding]:
    rule_pack = RulePackConfig()
    resolver = SchemaResolver(rule_pack)
    findings: list[PanelFinding] = []
    for path in paths:
        dash = json.loads(path.read_text(encoding="utf-8"))
        panels: list = []
        _walk_grafana_panels(dash.get("panels"), panels)
        for row in dash.get("rows") or []:
            _walk_grafana_panels(row.get("panels"), panels)
        for panel in panels:
            if panel.get("type") in {"row", "text", "news", "dashlist", "alertlist"}:
                continue
            title = str(panel.get("title") or "")
            try:
                yaml_panel, result = translate_panel(
                    panel,
                    datasource_index="metrics-*",
                    esql_index="metrics-*",
                    rule_pack=rule_pack,
                    resolver=resolver,
                )
            except Exception as exc:  # pragma: no cover
                findings.append(
                    PanelFinding(
                        source="grafana",
                        dashboard=path.name,
                        panel=title,
                        status="translate_error",
                        classification="would_need_live",
                        rule_ids=[],
                        error=str(exc)[:300],
                        query_preview="",
                    )
                )
                continue
            if result.status in {"requires_manual", "skipped"}:
                continue
            query = (yaml_panel or {}).get("esql", {}).get("query") or ""
            if not query:
                continue
            errs = structural_errors(
                check_esql_structure(query, feasibility="feasible")
            )
            findings.append(
                PanelFinding(
                    source="grafana",
                    dashboard=path.name,
                    panel=title,
                    status=str(result.status),
                    classification="caught_by_oracle" if errs else "clean",
                    rule_ids=[e.rule_id.value for e in errs],
                    error="",
                    query_preview=_preview(query),
                )
            )
    return findings


def translate_datadog_fixtures(paths: list[Path]) -> list[PanelFinding]:
    findings: list[PanelFinding] = []
    for path in paths:
        raw = json.loads(path.read_text(encoding="utf-8"))
        try:
            nd = normalize_dashboard(raw)
        except Exception as exc:  # pragma: no cover
            findings.append(
                PanelFinding(
                    source="datadog",
                    dashboard=str(path),
                    panel="(normalize)",
                    status="normalize_error",
                    classification="would_need_live",
                    rule_ids=[],
                    error=str(exc)[:300],
                    query_preview="",
                )
            )
            continue
        for widget in nd.widgets:
            try:
                plan = plan_widget(widget)
                result = translate_widget(widget, plan, OTEL_PROFILE)
            except Exception as exc:  # pragma: no cover
                findings.append(
                    PanelFinding(
                        source="datadog",
                        dashboard=path.name,
                        panel=widget.title,
                        status="translate_error",
                        classification="would_need_live",
                        rule_ids=[],
                        error=str(exc)[:300],
                        query_preview="",
                    )
                )
                continue
            if result.backend not in ESQL_EMITTING_BACKENDS:
                continue
            if result.status not in {"ok", "warning"}:
                continue
            errs = structural_errors(
                check_datadog_esql_structure(
                    result.esql_query or "",
                    status=result.status,
                    backend=result.backend,
                )
            )
            findings.append(
                PanelFinding(
                    source="datadog",
                    dashboard=path.name,
                    panel=widget.title,
                    status=result.status,
                    classification="caught_by_oracle" if errs else "clean",
                    rule_ids=[e.rule_id.value for e in errs],
                    error="",
                    query_preview=_preview(result.esql_query or ""),
                )
            )
    return findings


def summarize(findings: list[PanelFinding]) -> dict:
    by_class = Counter(f.classification for f in findings)
    by_source = Counter(f.source for f in findings)
    gaps = [f for f in findings if f.classification == "harness_gap"]
    caught = [f for f in findings if f.classification == "caught_by_oracle"]
    return {
        "total": len(findings),
        "by_classification": dict(by_class),
        "by_source": dict(by_source),
        "harness_gap_count": len(gaps),
        "caught_by_oracle_count": len(caught),
        "harness_gaps": [asdict(f) for f in gaps],
        "oracle_hits": [asdict(f) for f in caught[:50]],
    }


def write_markdown(summary: dict, out_md: Path) -> None:
    lines = [
        "# Harness empirical validation report (V0 offline)",
        "",
        "**Date:** 2026-07-20  ",
        "**Branch:** feat/301-translation-harness-datadog (#302)  ",
        f"**Total panel observations:** {summary['total']}",        "",
        "## Classification counts",
        "",
        "| Classification | Count |",
        "|---|---:|",
    ]
    for key, count in sorted(summary["by_classification"].items()):
        lines.append(f"| `{key}` | {count} |")
    lines.extend(
        [
            "",
            "## Source mix",
            "",
            "| Source | Count |",
            "|---|---:|",
        ]
    )
    for key, count in sorted(summary["by_source"].items()):
        lines.append(f"| {key} | {count} |")

    lines.extend(["", "## Harness gaps (live fail / known bug; oracle clean)", ""])
    gaps = summary.get("harness_gaps") or []
    if not gaps:
        lines.append("_None in this V0 slice._")
    else:
        lines.append("| Source | Dashboard | Panel | Live error (truncated) |")
        lines.append("|---|---|---|---|")
        for g in gaps:
            err = (g.get("error") or "").replace("|", "\\|")[:120]
            lines.append(
                f"| {g['source']} | {g['dashboard']} | {g['panel']} | `{err}` |"
            )

    lines.extend(
        [
            "",
            "## Implications for PR2 / PR3",
            "",
            "- **PR2 (alerts):** not covered by this V0 dashboard campaign — still required as a separate offline gate.",
            "- **PR3 (broader Grafana):** LogQL / variables / native PromQL passthrough not exercised here; "
            "expand if V1 live shows non-PromQL gaps.",
            "- **Harness gaps** above are the highest-value next oracle rules or translator fixes.",
            "",
            "## Next",
            "",
            "V1 scoped live smoke on Node Exporter Full + Diverse Panels + 2–3 Datadog dashboards.",
            "",
        ]
    )
    out_md.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus-root",
        type=Path,
        default=ROOT / "live_panel_check_20260717_151502",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "harness_empirical_20260720",
    )
    args = parser.parse_args(argv)
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    findings: list[PanelFinding] = []

    gf_smoke = (
        args.corpus_root
        / "grafana_out"
        / "dashboards"
        / "uploaded_dashboard_smoke_report_after_seed.json"
    )
    dd_smoke = (
        args.corpus_root
        / "datadog_out"
        / "dashboards"
        / "uploaded_dashboard_smoke_report_after_seed.json"
    )
    if gf_smoke.is_file():
        findings.extend(scan_smoke_report(gf_smoke, source="grafana"))
    if dd_smoke.is_file():
        findings.extend(scan_smoke_report(dd_smoke, source="datadog"))

    grafana_selected = sorted((args.corpus_root / "grafana_selected").glob("*.json"))
    if grafana_selected:
        findings.extend(translate_grafana_fixtures(grafana_selected))

    datadog_infra = sorted((ROOT / "infra" / "datadog" / "dashboards").rglob("*.json"))
    if datadog_infra:
        findings.extend(translate_datadog_fixtures(datadog_infra))

    summary = summarize(findings)
    summary["known_grafana_live_fails"] = sorted(KNOWN_GRAFANA_LIVE_FAILS)

    (out_dir / "v0_findings.json").write_text(
        json.dumps([asdict(f) for f in findings], indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "v0_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    write_markdown(summary, out_dir / "v0_report.md")

    # Also stage a force-addable docs copy for the PR
    docs_report = (
        ROOT
        / "docs"
        / "superpowers"
        / "specs"
        / "2026-07-20-harness-empirical-validation-report.md"
    )
    docs_report.parent.mkdir(parents=True, exist_ok=True)
    write_markdown(summary, docs_report)

    print(json.dumps(summary["by_classification"], indent=2))
    print(f"wrote {out_dir}")
    print(f"wrote {docs_report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
