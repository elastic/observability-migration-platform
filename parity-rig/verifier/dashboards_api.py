"""Typed Kibana Dashboards API conformance oracle.

The saved-object import path accepts a stringified ``panelsJSON`` blob. That is
useful for migration, but it is not a strong contract for "can Kibana's typed UI
model accept this dashboard?". Kibana 9.4+ exposes a typed Dashboards API
(``POST /api/dashboards``) that validates dashboard and visualization payloads
server-side.

This module converts the *emitted* migration presentation (``visual_ir`` in
``migration_report.json``) into the typed Dashboards API shape for the common
ES|QL chart families, submits it to a scratch dashboard, and classifies any 400
as a UI-contract gap. Unsupported chart families are reported explicitly rather
than guessed.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

ApiCall = Callable[[str, str, dict[str, Any] | None], tuple[int, dict[str, Any] | str]]

_XY_TYPES = {"line", "area", "bar"}
# ES|QL-backed config shapes confirmed accepted by the native Dashboards API on
# Elastic 9.5.0 (discovered via server-side validation against a live cluster):
#   gauge -> config{type:gauge, data_source:esql, metric:{column}}
#   pie   -> config{type:pie,   data_source:esql, metrics:[{column}], group_by?:[{column}]}
# data_table has no ES|QL variant in 9.5.0 (its branches require a data_view
# source), so it stays unmapped until that lands / the data_view path is added.
_SUPPORTED_ESQL_TYPES = _XY_TYPES | {"metric", "gauge", "pie"}


def _is_time_like(field: str) -> bool:
    name = str(field or "").strip("`")
    return name in {"time_bucket", "timestamp_bucket", "step", "@timestamp"} or "bucket" in name.lower()


def _fallback_esql_config(panel: dict[str, Any]) -> dict[str, Any]:
    yaml_panel = panel.get("yaml_panel") if isinstance(panel.get("yaml_panel"), dict) else {}
    yaml_esql = yaml_panel.get("esql") if isinstance(yaml_panel.get("esql"), dict) else {}
    if yaml_esql:
        return dict(yaml_esql)
    query = str(panel.get("esql_query") or panel.get("esql") or "").strip()
    if not query:
        return {}
    query_ir = panel.get("query_ir") if isinstance(panel.get("query_ir"), dict) else {}
    metric = str(query_ir.get("output_metric_field") or "value")
    groups = [str(item) for item in (query_ir.get("output_group_fields") or []) if str(item)]
    time_dim = next((field for field in groups if _is_time_like(field)), "time_bucket")
    breakdown = next((field for field in groups if not _is_time_like(field)), "")
    kibana_type = str(panel.get("kibana_type") or "").lower()
    chart_type = {
        "xy": "line",
        "metric": "metric",
        "table": "datatable",
        "partition": "pie",
        "treemap": "treemap",
        "heatmap": "heatmap",
    }.get(kibana_type, kibana_type)
    config: dict[str, Any] = {"type": chart_type, "query": query}
    if chart_type in _XY_TYPES:
        config["dimension"] = {"field": time_dim}
        config["metrics"] = [{"field": metric}]
        if breakdown:
            config["breakdown"] = {"field": breakdown}
    elif chart_type == "metric":
        config["primary"] = {"field": metric}
    return config


@dataclass
class Finding:
    category: str
    severity: str
    dashboard: str
    panel: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "severity": self.severity,
            "dashboard": self.dashboard,
            "panel": self.panel,
            "message": self.message,
            "evidence": dict(self.evidence),
        }


def _visual_presentation(panel: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    vir = panel.get("visual_ir") if isinstance(panel.get("visual_ir"), dict) else {}
    pres = vir.get("presentation") if isinstance(vir, dict) else {}
    if not isinstance(pres, dict):
        fallback = _fallback_esql_config(panel)
        return ("esql", fallback) if fallback else ("", {})
    cfg = pres.get("config") if isinstance(pres.get("config"), dict) else {}
    kind = str(pres.get("kind") or "")
    if not kind and (fallback := _fallback_esql_config(panel)):
        return "esql", fallback
    return kind, dict(cfg)


def _layout(panel: dict[str, Any]) -> dict[str, int]:
    vir = panel.get("visual_ir") if isinstance(panel.get("visual_ir"), dict) else {}
    layout = vir.get("layout") if isinstance(vir, dict) else {}
    if not isinstance(layout, dict):
        layout = {}
    return {
        "x": int(layout.get("x") or 0),
        "y": int(layout.get("y") or 0),
        "w": int(layout.get("w") or 24),
        "h": int(layout.get("h") or 8),
    }


def _field(obj: Any) -> str:
    if isinstance(obj, dict):
        return str(obj.get("field") or obj.get("column") or "").strip("`")
    return ""


def _metric_fields(config: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    for key in ("metric", "primary"):
        value = _field(config.get(key))
        if value:
            fields.append(value)
    metrics = config.get("metrics")
    if isinstance(metrics, list):
        fields.extend(_field(item) for item in metrics if _field(item))
    # Preserve order while removing duplicates.
    return list(dict.fromkeys(fields))


def _api_panel_from_esql(
    dashboard: str,
    panel: dict[str, Any],
    config: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[Finding]]:
    title = str(panel.get("title") or "")
    chart_type = str(config.get("type") or "").lower()
    query = str(config.get("query") or "").strip()
    if not query:
        return None, [
            Finding("missing_query", "error", dashboard, title, "ES|QL panel has no query")
        ]
    if chart_type not in _SUPPORTED_ESQL_TYPES:
        return None, [
            Finding(
                "unsupported_by_api_oracle",
                "info",
                dashboard,
                title,
                f"ES|QL type '{chart_type}' is not yet mapped by the Dashboards API oracle",
            )
        ]

    grid = _layout(panel)
    if chart_type in _XY_TYPES:
        x_col = _field(config.get("dimension")) or "time_bucket"
        y_cols = _metric_fields(config) or ["value"]
        layer: dict[str, Any] = {
            "type": chart_type,
            "data_source": {"type": "esql", "query": query},
            "x": {"column": x_col},
            "y": [{"column": col} for col in y_cols],
        }
        breakdown = _field(config.get("breakdown"))
        if breakdown:
            layer["breakdown_by"] = {"column": breakdown}
        return {
            "grid": grid,
            "type": "vis",
            "config": {"type": "xy", "title": title, "layers": [layer]},
        }, []

    metric_col = (_metric_fields(config) or ["value"])[0]

    if chart_type == "gauge":
        return {
            "grid": grid,
            "type": "vis",
            "config": {
                "type": "gauge",
                "title": title,
                "data_source": {"type": "esql", "query": query},
                "metric": {"column": metric_col},
            },
        }, []

    if chart_type == "pie":
        pie_config: dict[str, Any] = {
            "type": "pie",
            "title": title,
            "data_source": {"type": "esql", "query": query},
            "metrics": [{"column": metric_col}],
        }
        breakdown = _field(config.get("breakdown"))
        if breakdown:
            pie_config["group_by"] = [{"column": breakdown}]
        return {"grid": grid, "type": "vis", "config": pie_config}, []

    return {
        "grid": grid,
        "type": "vis",
        "config": {
            "type": "metric",
            "title": title,
            "data_source": {"type": "esql", "query": query},
            "metrics": [{"type": "primary", "column": metric_col}],
        },
    }, []


def api_panel_from_report_panel(
    dashboard: str, panel: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[Finding]]:
    title = str(panel.get("title") or "")
    kind, config = _visual_presentation(panel)
    if kind == "markdown":
        return {
            "grid": _layout(panel),
            "type": "markdown",
            "config": {
                "title": title,
                "content": str(config.get("content") or ""),
            },
        }, []
    if kind == "esql":
        return _api_panel_from_esql(dashboard, panel, config)
    return None, [
        Finding(
            "unsupported_by_api_oracle",
            "info",
            dashboard,
            title,
            f"visual presentation kind '{kind or '(none)'}' is not mapped",
        )
    ]


def build_dashboard_payload(report: dict[str, Any]) -> tuple[dict[str, Any], list[Finding]]:
    panels: list[dict[str, Any]] = []
    findings: list[Finding] = []
    title = ""
    for dash in report.get("dashboards", []):
        title = title or str(dash.get("title") or "migration conformance")
        dashboard_title = str(dash.get("title") or "")
        for panel in dash.get("panels", []):
            if not isinstance(panel, dict):
                continue
            api_panel, panel_findings = api_panel_from_report_panel(dashboard_title, panel)
            findings.extend(panel_findings)
            if api_panel is not None:
                panels.append(api_panel)
    return {"title": f"vf-conformance-{title}", "panels": panels}, findings


def mapped_panel_count(payload: dict[str, Any]) -> int:
    panels = payload.get("panels")
    return len(panels) if isinstance(panels, list) else 0


def make_kibana_api_call(kibana_url: str, api_key: str) -> ApiCall:
    base = kibana_url.rstrip("/")
    headers = {
        "kbn-xsrf": "true",
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"

    def call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any] | str]:
        response = requests.request(
            method, f"{base}{path}", headers=headers, json=body, timeout=30
        )
        try:
            payload: dict[str, Any] | str = response.json()
        except ValueError:
            payload = response.text[:2000]
        return response.status_code, payload

    return call


def validate_payload(
    payload: dict[str, Any],
    *,
    api_call: ApiCall,
    delete_on_success: bool = True,
) -> list[Finding]:
    if not payload.get("panels"):
        return [
            Finding(
                "empty_payload",
                "warning",
                str(payload.get("title") or ""),
                "",
                "no panels were mapped into the typed Dashboards API payload",
            )
        ]
    status, body = api_call("POST", "/api/dashboards", payload)
    if 200 <= status < 300 and isinstance(body, dict):
        dash_id = body.get("id")
        if delete_on_success and dash_id:
            api_call("DELETE", f"/api/dashboards/{dash_id}", None)
        return []
    message = body if isinstance(body, str) else body.get("message", json.dumps(body))
    return [
        Finding(
            "dashboards_api_rejected",
            "error",
            str(payload.get("title") or ""),
            "",
            str(message),
            evidence={"status": status},
        )
    ]


def _panel_title(api_panel: dict[str, Any]) -> str:
    config = api_panel.get("config") if isinstance(api_panel.get("config"), dict) else {}
    return str(config.get("title") or "")


def validate_payload_per_panel(
    payload: dict[str, Any],
    *,
    api_call: ApiCall,
    delete_on_success: bool = True,
) -> list[Finding]:
    """Validate each mapped panel in its own scratch dashboard.

    The typed Dashboards API reports schema paths such as ``panels.0``; on a
    large dashboard that is not enough context for triage. Per-panel mode trades
    speed for precise attribution.
    """
    panels = payload.get("panels") if isinstance(payload.get("panels"), list) else []
    if not panels:
        return validate_payload(payload, api_call=api_call, delete_on_success=delete_on_success)
    findings: list[Finding] = []
    for idx, panel in enumerate(panels):
        title = _panel_title(panel)
        panel_payload = {
            "title": f"{payload.get('title') or 'vf-conformance'}-panel-{idx}",
            "panels": [panel],
        }
        status, body = api_call("POST", "/api/dashboards", panel_payload)
        if 200 <= status < 300 and isinstance(body, dict):
            dash_id = body.get("id")
            if delete_on_success and dash_id:
                api_call("DELETE", f"/api/dashboards/{dash_id}", None)
            continue
        message = body if isinstance(body, str) else body.get("message", json.dumps(body))
        findings.append(
            Finding(
                "dashboards_api_rejected",
                "error",
                str(payload.get("title") or ""),
                title,
                str(message),
                evidence={"status": status, "panel_index": idx},
            )
        )
    return findings


def validate_report(
    report: dict[str, Any],
    *,
    api_call: ApiCall,
    delete_on_success: bool = True,
    per_panel: bool = False,
) -> list[Finding]:
    payload, findings = build_dashboard_payload(report)
    validator = validate_payload_per_panel if per_panel else validate_payload
    findings.extend(validator(payload, api_call=api_call, delete_on_success=delete_on_success))
    return findings


def apply_coverage_budget(
    findings: list[Finding],
    *,
    mapped_panels: int,
    max_unsupported: int | None = None,
    min_mapped_panels: int | None = None,
) -> list[Finding]:
    budget_findings: list[Finding] = []
    unsupported = sum(1 for finding in findings if finding.category == "unsupported_by_api_oracle")
    if max_unsupported is not None and unsupported > max_unsupported:
        budget_findings.append(
            Finding(
                "unsupported_budget_exceeded",
                "error",
                "",
                "",
                f"unsupported panel count {unsupported} exceeds budget {max_unsupported}",
                evidence={"unsupported": unsupported, "max_unsupported": max_unsupported},
            )
        )
    if min_mapped_panels is not None and mapped_panels < min_mapped_panels:
        budget_findings.append(
            Finding(
                "mapped_panel_budget_not_met",
                "error",
                "",
                "",
                f"mapped panel count {mapped_panels} is below required minimum {min_mapped_panels}",
                evidence={"mapped_panels": mapped_panels, "min_mapped_panels": min_mapped_panels},
            )
        )
    return [*findings, *budget_findings]


def summarize(findings: list[Finding], *, mapped_panels: int = 0) -> dict[str, Any]:
    counts: dict[str, int] = {}
    errors = 0
    for finding in findings:
        counts[finding.category] = counts.get(finding.category, 0) + 1
        if finding.severity == "error":
            errors += 1
    return {
        "total": len(findings),
        "errors": errors,
        "mapped_panels": mapped_panels,
        "unsupported": counts.get("unsupported_by_api_oracle", 0),
        "by_category": counts,
    }


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verifier.dashboards_api",
        description="Validate migrated dashboards against Kibana's typed Dashboards API.",
    )
    parser.add_argument("--migration-out", type=Path, required=True)
    parser.add_argument("--kibana-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fail-on-error", action="store_true")
    parser.add_argument("--max-unsupported", type=int)
    parser.add_argument("--min-mapped-panels", type=int)
    parser.add_argument(
        "--per-panel",
        action="store_true",
        help="Validate each mapped panel in an isolated scratch dashboard for precise failures.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    report = json.loads((args.migration_out / "migration_report.json").read_text())
    payload, findings = build_dashboard_payload(report)
    mapped = mapped_panel_count(payload)
    validator = validate_payload_per_panel if args.per_panel else validate_payload
    findings.extend(validator(payload, api_call=make_kibana_api_call(args.kibana_url, args.api_key)))
    findings = apply_coverage_budget(
        findings,
        mapped_panels=mapped,
        max_unsupported=args.max_unsupported,
        min_mapped_panels=args.min_mapped_panels,
    )
    payload = {"summary": summarize(findings, mapped_panels=mapped), "findings": [f.to_jsonable() for f in findings]}
    if args.output:
        args.output.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload["summary"], indent=2))
    return 1 if args.fail_on_error and payload["summary"]["errors"] else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

