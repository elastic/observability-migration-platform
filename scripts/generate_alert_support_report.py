#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Generate alert-support standings from real Grafana and Datadog tool outputs."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = ROOT / "examples" / "alerting"
GENERATED_DIR = EXAMPLES_DIR / "generated"
GRAFANA_INPUT_DIR = EXAMPLES_DIR / "grafana"
DATADOG_MONITORS_FILE = EXAMPLES_DIR / "monitors" / "datadog_monitors.json"
DATADOG_FIELD_PROFILE = ROOT / "examples" / "datadog-field-profile.example.yaml"
TIER_ORDER = ["automated", "draft_requires_review", "manual_required"]
TIER_LABELS = {
    "automated": "Automated",
    "draft_requires_review": "Draft review",
    "manual_required": "Manual required",
}
GRAFANA_EXPECTED_DETAILED_FAMILIES = {
    "Legacy dashboard alerts",
    "Prometheus native PromQL",
    "Mimir native PromQL",
    "Loki / LogQL",
    "Graphite datasource",
    "PromQL topk()",
    "PromQL bottomk()",
    "PromQL subquery",
    "PromQL @ modifier",
    "PromQL changes()",
    "PromQL label_replace()",
    "PromQL label_join()",
    "PromQL scalar()",
    "PromQL nested comparison",
    "PromQL metric-to-metric comparison",
    "PromQL or operator",
    "PromQL unless operator",
    "PromQL known server bug pattern",
}
DATADOG_EXPECTED_DETAILED_FAMILIES = {
    "Warning-free metric/query alerts",
    "Change query alerts",
    "Shifted formula metric/query alerts",
    "Warning-free log count alerts",
    "Warning-free log measure alerts",
    "Metric/query alerts with as_rate()",
    "Metric/query alerts with rollup()",
    "Metric/query alerts with default_zero()",
    "Log alerts with explicit index()",
    "Formula-style metric/query alerts",
    "Anomaly query alerts",
    "Forecast query alerts",
    "Outlier query alerts",
    "Metric/query alerts with exclude_null()",
    "Composite monitors",
    "Event alerts",
    "Service check monitors",
    "APM monitors",
    "RUM monitors",
    "Synthetics monitors",
    "CI monitors",
    "SLO monitors",
    "Audit monitors",
    "Cost monitors",
    "Network monitors",
    "Watchdog monitors",
}


def _offline_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in [
        "ELASTICSEARCH_ENDPOINT",
        "ES_URL",
        "ES_API_KEY",
        "KIBANA_ENDPOINT",
        "KIBANA_URL",
        "KIBANA_API_KEY",
        "KEY",
    ]:
        env.pop(key, None)
    return env


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, cwd=ROOT, check=True, env=_offline_env())


def _datadog_support_dashboard_stub() -> dict[str, Any]:
    return {
        "title": "Datadog Alert Support Fixtures",
        "description": (
            "Dashboard placeholder so the Datadog CLI can run file-mode support reporting "
            "with monitor fixtures."
        ),
        "widgets": [],
    }


def _prepare_datadog_input_dir() -> Path:
    staging_dir = GENERATED_DIR / "_staging" / "datadog"
    monitors_dir = staging_dir / "monitors"
    shutil.rmtree(staging_dir, ignore_errors=True)
    monitors_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "support_dashboard.json").write_text(
        json.dumps(_datadog_support_dashboard_stub(), indent=2),
        encoding="utf-8",
    )
    shutil.copy2(DATADOG_MONITORS_FILE, monitors_dir / "datadog_monitors.json")
    return staging_dir


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _tier_count(summary: dict[str, Any], tier: str) -> int:
    return int((summary.get("by_automation_tier", {}) or {}).get(tier, 0) or 0)


def _table_escape(value: str) -> str:
    return str(value or "").replace("|", r"\|").replace("\n", " ")


def _source_snippet(row: dict[str, Any]) -> str:
    source = row.get("source", {}) or {}
    query = str(source.get("query", "") or "").strip()
    if query:
        return query
    condition = str(source.get("condition", "") or "").strip()
    if condition:
        return condition
    panel = str(source.get("panel", "") or "").strip()
    if panel:
        return panel
    return "(no source query recorded)"


def _reason_items(row: dict[str, Any]) -> list[str]:
    items: list[str] = []
    review_gates = ((row.get("target") or {}).get("review_gates", {}) or {})
    if review_gates.get("no_data_only_blocks_strict_automation"):
        items.append("Only exact no-data parity blocks automated promotion")
    for bucket in [
        row.get("blocked_reasons", []) or [],
        ((row.get("translation") or {}).get("warnings", []) or []),
        row.get("semantic_losses", []) or [],
    ]:
        for item in bucket:
            text = str(item or "").strip()
            if text and text not in items:
                items.append(text)
    return items


def _default_reason(row: dict[str, Any]) -> str:
    tier = str((row.get("target") or {}).get("automation_tier", "") or "").strip()
    if tier == "draft_requires_review":
        return "review-only by migration policy; translated query should be human-validated before enablement"
    return "none"


def _reason_summary(row: dict[str, Any]) -> str:
    items = _reason_items(row)
    return "; ".join(items) if items else _default_reason(row)


def _first_source_query_type(row: dict[str, Any]) -> str:
    source = row.get("source", {}) or {}
    queries = list(source.get("source_queries", []) or [])
    if not queries:
        return ""
    first = queries[0] if isinstance(queries[0], dict) else {}
    return str(first.get("datasource_type", "") or "").strip().lower()


def _first_source_query_expr(row: dict[str, Any]) -> str:
    source = row.get("source", {}) or {}
    queries = list(source.get("source_queries", []) or [])
    if queries:
        first = queries[0] if isinstance(queries[0], dict) else {}
        expr = str(first.get("expr", "") or "").strip()
        if expr:
            return expr
    return str(source.get("query", "") or "").strip()


def _grafana_has_metric_to_metric_comparison(expr: str) -> bool:
    stripped = re.sub(r"\{[^{}]*\}", "{}", str(expr or ""))
    stripped = re.sub(r'"(?:\\.|[^"])*"', '""', stripped)
    stripped = re.sub(r"'(?:\\.|[^'])*'", "''", stripped)
    match = re.search(r"(==\s*bool\b|==|!=|>=|<=|(?<![=!~<>])>(?![=])|(?<![=!~<>])<(?![=]))", stripped)
    if not match:
        return False
    rhs = stripped[match.end():].lstrip()
    return bool(rhs) and not re.match(r"^[\d.+-]", rhs)


def _grafana_has_nested_comparison(expr: str) -> bool:
    stripped = re.sub(r"\{[^{}]*\}", "{}", str(expr or ""))
    stripped = re.sub(r'"(?:\\.|[^"])*"', '""', stripped)
    stripped = re.sub(r"'(?:\\.|[^'])*'", "''", stripped)
    comp_re = re.compile(r"(==\s*bool\b|==|!=|>=|<=|(?<![=!~<>])>(?![=])|(?<![=!~<>])<(?![=]))")
    depth = 0
    for idx, char in enumerate(stripped):
        if char in "([":
            depth += 1
            continue
        if char in ")]":
            depth = max(depth - 1, 0)
            continue
        if comp_re.match(stripped, idx) and depth > 0:
            return True
    return False


def _grafana_known_server_bug(expr: str) -> bool:
    text = str(expr or "")
    if (
        "node_filesystem_avail_bytes" in text
        and "node_filesystem_free_bytes" in text
        and "node_filesystem_size_bytes" in text
        and "+(" in text
    ):
        return True
    stripped = re.sub(r'"(?:\\.|[^"])*"', '""', text)
    stripped = re.sub(r"'(?:\\.|[^'])*'", "''", stripped)
    stripped = re.sub(r"\{[^{}]*\}", "{}", stripped)
    return bool(re.search(r"\bor\b|\bunless\b", stripped, re.IGNORECASE))


def _grafana_detailed_family_label(row: dict[str, Any]) -> str:
    kind = str(row.get("kind", "") or "").strip().lower()
    translation = row.get("translation", {}) or {}
    datasource_type = _first_source_query_type(row)
    expr = _first_source_query_expr(row)
    query = expr.lower()
    provenance = str(translation.get("provenance", "") or "").strip().lower()

    if kind == "grafana_legacy":
        return "Legacy dashboard alerts"
    if datasource_type == "loki":
        return "Loki / LogQL"
    if datasource_type == "graphite":
        return "Graphite datasource"
    if datasource_type == "mimir" and provenance == "native_promql":
        return "Mimir native PromQL"
    if datasource_type == "prometheus" and provenance == "native_promql":
        return "Prometheus native PromQL"
    if re.search(r"\btopk\s*\(", query, re.IGNORECASE):
        return "PromQL topk()"
    if re.search(r"\bbottomk\s*\(", query, re.IGNORECASE):
        return "PromQL bottomk()"
    if re.search(r"\[\d+[smhd]:\d+[smhd]\]", query, re.IGNORECASE):
        return "PromQL subquery"
    if re.search(r"@\s*\d", query):
        return "PromQL @ modifier"
    if re.search(r"\bchanges\s*\(", query, re.IGNORECASE):
        return "PromQL changes()"
    if re.search(r"\blabel_replace\s*\(", query, re.IGNORECASE):
        return "PromQL label_replace()"
    if re.search(r"\blabel_join\s*\(", query, re.IGNORECASE):
        return "PromQL label_join()"
    if re.search(r"\bscalar\s*\(", query, re.IGNORECASE):
        return "PromQL scalar()"
    if _grafana_has_nested_comparison(expr):
        return "PromQL nested comparison"
    if _grafana_has_metric_to_metric_comparison(expr):
        return "PromQL metric-to-metric comparison"
    if re.search(r"\bor\b", query, re.IGNORECASE):
        return "PromQL or operator"
    if re.search(r"\bunless\b", query, re.IGNORECASE):
        return "PromQL unless operator"
    if _grafana_known_server_bug(expr):
        return "PromQL known server bug pattern"
    if datasource_type:
        return f"{datasource_type.title()} datasource"
    return "Other Grafana alerts"


def _datadog_detailed_family_label(row: dict[str, Any]) -> str:
    kind = str(row.get("kind", "") or "").strip().lower()
    source = row.get("source", {}) or {}
    translation = row.get("translation", {}) or {}
    target = row.get("target", {}) or {}
    tier = str(target.get("automation_tier", "") or "").strip()
    warnings = " ".join(str(item or "").lower() for item in (translation.get("warnings", []) or []))
    query = str(source.get("query", "") or "").strip().lower()
    translation_query = str(translation.get("query", "") or "").strip().lower()
    source_type = str(source.get("type", "") or "").strip().lower()

    if source_type == "log alert" and '.index("' in query and '.index("*")' not in query:
        return "Log alerts with explicit index()"
    if "exclude_null(" in query:
        return "Metric/query alerts with exclude_null()"
    if source_type in {"metric alert", "query alert"} and "default_zero(" in query:
        return "Metric/query alerts with default_zero()"
    if source_type in {"metric alert", "query alert"} and ".as_rate()" in query:
        return "Metric/query alerts with as_rate()"
    if source_type in {"metric alert", "query alert"} and ".rollup(" in query:
        return "Metric/query alerts with rollup()"
    if "rate semantics approximated" in warnings:
        return "Metric/query alerts with as_rate()"
    if "rollup interval is approximated" in warnings:
        return "Metric/query alerts with rollup()"
    if "default_zero semantics are approximated" in warnings:
        return "Metric/query alerts with default_zero()"
    if source_type in {"metric alert", "query alert"} and (
        query.startswith("change(") or query.startswith("pct_change(")
    ):
        return "Change query alerts"
    if source_type in {"metric alert", "query alert"} and any(
        token in query
        for token in (
            "week_before(",
            "day_before(",
            "hour_before(",
            "month_before(",
            "calendar_shift(",
            "timeshift(",
        )
    ):
        return "Shifted formula metric/query alerts"
    if source_type in {"metric alert", "query alert"} and (" / " in query or "100 *" in query):
        return "Formula-style metric/query alerts"
    if source_type in {"metric alert", "query alert"} and tier == "automated":
        return "Warning-free metric/query alerts"
    if source_type == "log alert" and tier == "draft_requires_review":
        if "percentile(" in translation_query or "avg(" in translation_query:
            return "Warning-free log measure alerts"
        return "Warning-free log count alerts"
    if source_type == "composite":
        return "Composite monitors"
    if source_type == "service check":
        return "Service check monitors"
    if source_type == "event alert":
        return "Event alerts"
    if source_type in {"apm", "apm alert"}:
        return "APM monitors"
    if source_type in {"rum", "rum alert"}:
        return "RUM monitors"
    if source_type in {"synthetics", "synthetics alert"}:
        return "Synthetics monitors"
    if source_type in {"ci", "ci alert"}:
        return "CI monitors"
    if source_type in {"slo", "slo alert"}:
        return "SLO monitors"
    if source_type in {"audit", "audit alert"}:
        return "Audit monitors"
    if source_type in {"cost", "cost alert"}:
        return "Cost monitors"
    if source_type in {"network", "network alert"}:
        return "Network monitors"
    if source_type in {"watchdog", "watchdog alert"}:
        return "Watchdog monitors"
    if "anomalies(" in query:
        return "Anomaly query alerts"
    if "forecast(" in query:
        return "Forecast query alerts"
    if "outliers(" in query or "outlier(" in query:
        return "Outlier query alerts"
    return source_type.title() or kind or "Other Datadog alerts"


def _grouped_family_label(source_name: str, row: dict[str, Any]) -> str:
    if source_name == "grafana":
        detailed = _grafana_detailed_family_label(row)
        if detailed == "Legacy dashboard alerts":
            return detailed
        if detailed in {"Prometheus native PromQL", "Mimir native PromQL"}:
            return "Native PromQL"
        if detailed == "Loki / LogQL":
            return detailed
        if detailed == "Graphite datasource" or detailed.endswith(" datasource"):
            return "Other datasource families"
        return "Native subset exclusions"

    detailed = _datadog_detailed_family_label(row)
    if detailed in {
        "Warning-free metric/query alerts",
        "Change query alerts",
        "Shifted formula metric/query alerts",
    }:
        return detailed
    if detailed in {"Warning-free log count alerts", "Warning-free log measure alerts"}:
        return "Warning-free log alerts"
    if detailed in {
        "Metric/query alerts with as_rate()",
        "Metric/query alerts with rollup()",
        "Metric/query alerts with default_zero()",
    }:
        return "Approximation-blocked metric/query alerts"
    if detailed == "Log alerts with explicit index()":
        return "Explicit-index log alerts"
    if detailed == "Formula-style metric/query alerts":
        return detailed
    if detailed in {
        "Anomaly query alerts",
        "Forecast query alerts",
        "Outlier query alerts",
        "Metric/query alerts with exclude_null()",
    }:
        return "Analytic/manual query families"
    return "Manual-only monitor families"


def _detailed_family_label(source_name: str, row: dict[str, Any]) -> str:
    if source_name == "grafana":
        return _grafana_detailed_family_label(row)
    return _datadog_detailed_family_label(row)


def _family_label(source_name: str, row: dict[str, Any], granularity: str = "grouped") -> str:
    if granularity == "detailed":
        return _detailed_family_label(source_name, row)
    return _grouped_family_label(source_name, row)


def _expected_detailed_families(source_name: str) -> set[str]:
    if source_name == "grafana":
        return set(GRAFANA_EXPECTED_DETAILED_FAMILIES)
    return set(DATADOG_EXPECTED_DETAILED_FAMILIES)


def _missing_expected_detailed_families(rows: list[dict[str, Any]], source_name: str) -> list[str]:
    present = {_detailed_family_label(source_name, row) for row in rows}
    return sorted(_expected_detailed_families(source_name) - present)


def _group_examples(
    rows: list[dict[str, Any]],
    source_name: str,
    *,
    granularity: str = "grouped",
) -> list[tuple[str, str, list[dict[str, Any]]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        family = _family_label(source_name, row, granularity=granularity)
        tier = str((row.get("target") or {}).get("automation_tier", "") or "")
        grouped[(family, tier)].append(row)
    return sorted(
        ((family, tier, entries) for (family, tier), entries in grouped.items()),
        key=lambda item: (TIER_ORDER.index(item[1]) if item[1] in TIER_ORDER else len(TIER_ORDER), item[0]),
    )


def _group_reason_summary(rows: list[dict[str, Any]], *, limit: int = 2) -> str:
    counts: Counter[str] = Counter()
    for row in rows:
        counts.update(_reason_items(row))
    if not counts:
        return _default_reason(rows[0]) if rows else "none"
    top = counts.most_common(limit)
    return "; ".join(
        f"{reason} ({count})" if count > 1 else reason
        for reason, count in top
    )


def _group_example_names(rows: list[dict[str, Any]], *, limit: int = 3) -> str:
    names = [f"`{str(row.get('name', '')).strip()}`" for row in rows if str(row.get("name", "")).strip()]
    if not names:
        return "_none_"
    if len(names) <= limit:
        return ", ".join(names)
    return f"{', '.join(names[:limit])}, +{len(names) - limit} more"


def _render_family_matrix(
    rows: list[dict[str, Any]],
    heading: str,
    source_name: str,
    *,
    include_heading: bool = True,
    granularity: str = "grouped",
) -> list[str]:
    lines: list[str] = []
    if include_heading:
        lines.extend([f"### {heading}", ""])
    lines.extend([
        "| Family | Tier | Cases | Example alerts | Evidence |",
        "| --- | --- | ---: | --- | --- |",
    ])
    for family, tier, entries in _group_examples(rows, source_name, granularity=granularity):
        lines.append(
            "| "
            f"{_table_escape(family)} | "
            f"{_table_escape(TIER_LABELS.get(tier, tier or 'Unknown'))} | "
            f"{len(entries)} | "
            f"{_table_escape(_group_example_names(entries))} | "
            f"{_table_escape(_group_reason_summary(entries))} |"
        )
    lines.append("")
    return lines


def _render_blocker_table(rows: list[dict[str, Any]], heading: str) -> list[str]:
    counts: Counter[str] = Counter()
    for row in rows:
        if (row.get("target") or {}).get("automation_tier") != "manual_required":
            continue
        counts.update(_reason_items(row))

    lines = [
        f"### {heading}",
        "",
    ]
    if not counts:
        lines.extend(["_None_", ""])
        return lines

    lines.extend([
        "| Blocker | Cases |",
        "| --- | ---: |",
    ])
    for blocker, count in counts.most_common():
        lines.append(f"| {_table_escape(blocker)} | {count} |")
    lines.append("")
    return lines


def _family_count(rows: list[dict[str, Any]], source_name: str, *, granularity: str = "grouped") -> int:
    return len({_family_label(source_name, row, granularity=granularity) for row in rows})


def _family_breakdown(
    rows: list[dict[str, Any]],
    source_name: str,
    *,
    granularity: str = "grouped",
) -> list[dict[str, Any]]:
    family_counts: dict[tuple[str, str], int] = Counter()
    for row in rows:
        family_counts[(
            _family_label(source_name, row, granularity=granularity),
            str((row.get("target") or {}).get("automation_tier", "") or ""),
        )] += 1
    return [
        {
            "family": family,
            "automation_tier": tier,
            "count": count,
        }
        for (family, tier), count in sorted(
            family_counts.items(),
            key=lambda item: (TIER_ORDER.index(item[0][1]) if item[0][1] in TIER_ORDER else len(TIER_ORDER), item[0][0]),
        )
    ]


def _top_blockers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for row in rows:
        if (row.get("target") or {}).get("automation_tier") != "manual_required":
            continue
        counts.update(_reason_items(row))
    return [{"reason": reason, "count": count} for reason, count in counts.most_common()]


def _parser_diagnostic_codes(row: dict[str, Any]) -> list[str]:
    diagnostics = ((row.get("translation") or {}).get("parser_diagnostics", []) or [])
    seen: list[str] = []
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            continue
        code = str(diagnostic.get("code", "") or "").strip()
        if code and code not in seen:
            seen.append(code)
    return seen


def _top_parser_diagnostics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for row in rows:
        counts.update(_parser_diagnostic_codes(row))
    return [{"code": code, "count": count} for code, count in counts.most_common()]


def _render_parser_diagnostic_table(rows: list[dict[str, Any]], title: str) -> list[str]:
    lines = [f"### {title}", ""]
    entries = _top_parser_diagnostics(rows)
    if not entries:
        lines.append("_None_")
        lines.append("")
        return lines
    lines.extend(
        [
            "| Diagnostic code | Count |",
            "| --- | ---: |",
        ]
    )
    for entry in entries:
        lines.append(f"| `{_table_escape(entry['code'])}` | {entry['count']} |")
    lines.append("")
    return lines


def _render_cases(rows: list[dict[str, Any]], source_name: str) -> list[str]:
    sections: list[str] = []
    for tier in TIER_ORDER:
        tier_rows = [row for row in rows if ((row.get("target") or {}).get("automation_tier") == tier)]
        sections.append(f"### `{tier}`")
        if not tier_rows:
            sections.append("")
            sections.append("_None_")
            sections.append("")
            continue
        for row in tier_rows:
            translation = row.get("translation", {}) or {}
            target = row.get("target", {}) or {}
            selected_rule_type = str(target.get("selected_target_rule_type", "") or "").strip()
            emitted_rule_type = str(target.get("target_rule_type", "") or "").strip()
            payload_emitted = bool(target.get("payload_emitted"))
            sections.append("")
            sections.append(f"#### `{row.get('name', '')}`")
            sections.append("")
            sections.append(f"- Family: `{_family_label(source_name, row, granularity='detailed')}`")
            sections.append(f"- Kind: `{row.get('kind', '')}`")
            sections.append(f"- Selected rule type: `{selected_rule_type or 'none'}`")
            sections.append(f"- Emitted rule type: `{emitted_rule_type or 'none'}`")
            sections.append(f"- Payload emitted: `{'yes' if payload_emitted else 'no'}`")
            sections.append(f"- Provenance: `{translation.get('provenance', '') or 'none'}`")
            sections.append(f"- Evidence: `{_reason_summary(row)}`")
            sections.append("")
            sections.append("Source example:")
            sections.append("")
            sections.append("```text")
            sections.append(_source_snippet(row))
            sections.append("```")
        sections.append("")
    return sections


def _source_rows(source_name: str, comparison: dict[str, Any]) -> list[dict[str, Any]]:
    if source_name == "grafana":
        return list(comparison.get("alerts", []) or [])
    return list(comparison.get("monitors", []) or [])


def _artifact_paths(source_name: str) -> dict[str, str]:
    if source_name == "grafana":
        return {
            "results": "examples/alerting/generated/grafana/alerts/alert_migration_results.json",
            "comparison": "examples/alerting/generated/grafana/alerts/alert_comparison_results.json",
        }
    return {
        "results": "examples/alerting/generated/datadog/alerts/monitor_migration_results.json",
        "comparison": "examples/alerting/generated/datadog/alerts/monitor_comparison_results.json",
    }


def _source_support_summary(source_name: str, comparison: dict[str, Any]) -> dict[str, Any]:
    rows = _source_rows(source_name, comparison)
    return {
        "total": comparison.get("total", 0),
        "summary": comparison.get("summary", {}),
        "families_represented": _family_count(rows, source_name, granularity="grouped"),
        "family_breakdown": _family_breakdown(rows, source_name, granularity="grouped"),
        "grouped_families_represented": _family_count(rows, source_name, granularity="grouped"),
        "grouped_family_breakdown": _family_breakdown(rows, source_name, granularity="grouped"),
        "detailed_families_represented": _family_count(rows, source_name, granularity="detailed"),
        "detailed_family_breakdown": _family_breakdown(rows, source_name, granularity="detailed"),
        "expected_detailed_families": sorted(_expected_detailed_families(source_name)),
        "missing_expected_detailed_families": _missing_expected_detailed_families(rows, source_name),
        "top_blockers": _top_blockers(rows),
        "parser_diagnostic_breakdown": _top_parser_diagnostics(rows),
        "artifacts": _artifact_paths(source_name),
    }


def build_support_summary(grafana_comparison: dict[str, Any], datadog_comparison: dict[str, Any]) -> dict[str, Any]:
    return {
        "grafana": _source_support_summary("grafana", grafana_comparison),
        "datadog": _source_support_summary("datadog", datadog_comparison),
    }


def render_markdown_report(grafana_comparison: dict[str, Any], datadog_comparison: dict[str, Any]) -> str:
    grafana_rows = list(grafana_comparison.get("alerts", []) or [])
    datadog_rows = list(datadog_comparison.get("monitors", []) or [])
    summary = build_support_summary(grafana_comparison, datadog_comparison)

    lines = [
        "# Alert Support Standings",
        "",
        "Generated by `scripts/generate_alert_support_report.py` from the real Grafana and Datadog migration CLIs.",
        "",
        "This report is generated from curated example suites. The coverage matrix shows the families currently exercised by those suites, and the appendix lists every example that produced the standings.",
        "",
        "## How To Refresh",
        "",
        "```bash",
        ".venv/bin/python scripts/generate_alert_support_report.py",
        "```",
        "",
        "## Source Inputs",
        "",
        f"- Grafana examples: `{GRAFANA_INPUT_DIR.relative_to(ROOT)}`",
        f"- Datadog monitor examples: `{DATADOG_MONITORS_FILE.relative_to(ROOT)}`",
        "",
        "## Generated Artifacts",
        "",
        "- Grafana:",
        f"  - `{(GENERATED_DIR / 'grafana' / 'alerts' / 'alert_migration_results.json').relative_to(ROOT)}`",
        f"  - `{(GENERATED_DIR / 'grafana' / 'alerts' / 'alert_comparison_results.json').relative_to(ROOT)}`",
        "- Datadog:",
        f"  - `{(GENERATED_DIR / 'datadog' / 'alerts' / 'monitor_migration_results.json').relative_to(ROOT)}`",
        f"  - `{(GENERATED_DIR / 'datadog' / 'alerts' / 'monitor_comparison_results.json').relative_to(ROOT)}`",
        "",
        "## Summary",
        "",
        "| Source | Total cases | Grouped families | Detailed families | Automated | Draft Review | Manual Required |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        f"| Grafana | {grafana_comparison.get('total', 0)} | {summary['grafana']['grouped_families_represented']} | {summary['grafana']['detailed_families_represented']} | {_tier_count(grafana_comparison.get('summary', {}), 'automated')} | {_tier_count(grafana_comparison.get('summary', {}), 'draft_requires_review')} | {_tier_count(grafana_comparison.get('summary', {}), 'manual_required')} |",
        f"| Datadog | {datadog_comparison.get('total', 0)} | {summary['datadog']['grouped_families_represented']} | {summary['datadog']['detailed_families_represented']} | {_tier_count(datadog_comparison.get('summary', {}), 'automated')} | {_tier_count(datadog_comparison.get('summary', {}), 'draft_requires_review')} | {_tier_count(datadog_comparison.get('summary', {}), 'manual_required')} |",
        "",
        "## Coverage Matrix",
        "",
    ]
    lines.extend(_render_family_matrix(grafana_rows, "Grafana", "grafana", granularity="grouped"))
    lines.extend(_render_family_matrix(datadog_rows, "Datadog", "datadog", granularity="grouped"))
    lines.extend([
        "## Detailed Family Coverage",
        "",
        "### Grafana",
        "",
        f"Covered detailed families: {summary['grafana']['detailed_families_represented']}/{len(summary['grafana']['expected_detailed_families'])}",
        "",
        "Missing detailed families: "
        + (
            ", ".join(f"`{item}`" for item in summary["grafana"]["missing_expected_detailed_families"])
            if summary["grafana"]["missing_expected_detailed_families"]
            else "`none`"
        ),
        "",
    ])
    lines.extend(_render_family_matrix(grafana_rows, "Grafana", "grafana", include_heading=False, granularity="detailed"))
    lines.extend([
        "### Datadog",
        "",
        f"Covered detailed families: {summary['datadog']['detailed_families_represented']}/{len(summary['datadog']['expected_detailed_families'])}",
        "",
        "Missing detailed families: "
        + (
            ", ".join(f"`{item}`" for item in summary["datadog"]["missing_expected_detailed_families"])
            if summary["datadog"]["missing_expected_detailed_families"]
            else "`none`"
        ),
        "",
    ])
    lines.extend(_render_family_matrix(datadog_rows, "Datadog", "datadog", include_heading=False, granularity="detailed"))
    lines.extend([
        "## Top Blockers",
        "",
    ])
    lines.extend(_render_blocker_table(grafana_rows, "Grafana"))
    lines.extend(_render_blocker_table(datadog_rows, "Datadog"))
    lines.extend([
        "## Parser Diagnostics",
        "",
    ])
    lines.extend(_render_parser_diagnostic_table(grafana_rows, "Grafana"))
    lines.extend(_render_parser_diagnostic_table(datadog_rows, "Datadog"))
    lines.extend([
        "## Example Appendix",
        "",
        "### Grafana",
        "",
    ])
    lines.extend(_render_cases(grafana_rows, "grafana"))
    lines.extend([
        "### Datadog",
        "",
    ])
    lines.extend(_render_cases(datadog_rows, "datadog"))
    return "\n".join(lines).rstrip() + "\n"


def _write_markdown(grafana_comparison: dict[str, Any], datadog_comparison: dict[str, Any]) -> None:
    report = render_markdown_report(grafana_comparison, datadog_comparison)
    (GENERATED_DIR / "alert_support_standings.md").write_text(report, encoding="utf-8")


def _write_summary_json(grafana_comparison: dict[str, Any], datadog_comparison: dict[str, Any]) -> None:
    summary = build_support_summary(grafana_comparison, datadog_comparison)
    (GENERATED_DIR / "alert_support_standings.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    _parse_args(argv)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    grafana_output = GENERATED_DIR / "grafana"
    datadog_output = GENERATED_DIR / "datadog"
    shutil.rmtree(grafana_output, ignore_errors=True)
    shutil.rmtree(datadog_output, ignore_errors=True)

    datadog_input_dir = _prepare_datadog_input_dir()
    python = sys.executable

    _run([
        python,
        "-m",
        "observability_migration.adapters.source.grafana.cli",
        "--source",
        "files",
        "--assets",
        "alerts",
        "--input-dir",
        str(GRAFANA_INPUT_DIR),
        "--output-dir",
        str(grafana_output),
        "--data-view",
        "metrics-*",
        "--es-url",
        "",
        "--kibana-url",
        "",
    ])
    _run([
        python,
        "-m",
        "observability_migration.adapters.source.datadog.cli",
        "--source",
        "files",
        "--assets",
        "alerts",
        "--input-dir",
        str(datadog_input_dir),
        "--output-dir",
        str(datadog_output),
        "--field-profile",
        str(DATADOG_FIELD_PROFILE),
        "--data-view",
        "metrics-*",
        "--es-url",
        "",
        "--kibana-url",
        "",
        "--es-api-key",
        "",
        "--kibana-api-key",
        "",
    ])

    grafana_comparison = _load_json(grafana_output / "alerts" / "alert_comparison_results.json")
    datadog_comparison = _load_json(datadog_output / "alerts" / "monitor_comparison_results.json")
    _write_markdown(grafana_comparison, datadog_comparison)
    _write_summary_json(grafana_comparison, datadog_comparison)

    print("Generated alert support report:")
    print(f"  - {(GENERATED_DIR / 'alert_support_standings.md').relative_to(ROOT)}")
    print(f"  - {(GENERATED_DIR / 'alert_support_standings.json').relative_to(ROOT)}")


if __name__ == "__main__":
    main()
