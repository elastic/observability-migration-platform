"""Translate Grafana annotations into Kibana migration guidance.

Grafana annotations are typically PromQL queries pinned to the dashboard
timeline. Kibana Lens supports event annotations backed by a data view
and a KQL/Lucene filter, but the migration pipeline generally cannot prove
the exact target field/index mapping for annotation queries. This module
therefore emits either safe manual tasks or candidate event-annotation hints.
"""

from __future__ import annotations

import re
from typing import Any


def _extract_metric_from_promql(expr: str) -> str:
    """Best-effort metric name extraction from a PromQL expression."""
    cleaned = re.sub(r"\{[^}]*\}", "", expr)
    match = re.match(r"[\w:]+", cleaned.strip())
    return match.group(0) if match else ""


def translate_annotations(
    dashboard: dict[str, Any],
    *,
    data_view: str = "metrics-*",
) -> list[dict[str, Any]]:
    """Translate Grafana annotations to Kibana-compatible annotation descriptors.

    Returns a list of annotation entries suitable for inclusion in the YAML
    dashboard output and/or the migration manifest.
    """
    raw = dashboard.get("annotations") or {}
    annotation_list = raw.get("list", []) if isinstance(raw, dict) else []
    translated: list[dict[str, Any]] = []

    for ann in annotation_list:
        name = str(ann.get("name", "") or "")
        datasource = ann.get("datasource") or {}
        ds_type = str(datasource.get("type", "") or "").lower() if isinstance(datasource, dict) else ""
        ds_uid = str(datasource.get("uid", "") or "") if isinstance(datasource, dict) else ""
        enable = ann.get("enable", True)
        hide = ann.get("hide", False)
        icon_color = str(ann.get("iconColor", "") or "")
        expr = str(ann.get("expr", "") or ann.get("query", "") or "")

        entry: dict[str, Any] = {
            "name": name or "Annotation",
            "enabled": bool(enable) and not bool(hide),
            "source_datasource": ds_type or ds_uid,
            "source_query": expr,
            "icon_color": icon_color,
        }

        if ds_uid == "-- Grafana --" or ds_type == "grafana":
            entry["type"] = "grafana_native"
            entry["kibana_action"] = "unsupported"
            entry["description"] = (
                "Grafana native annotations (manual markers) have no Kibana equivalent; "
                "use Kibana's annotation layer or saved annotations instead."
            )
            translated.append(entry)
            continue

        if "prom" in ds_type or (expr and not expr.strip().startswith("{")):
            metric = _extract_metric_from_promql(expr)
            entry["type"] = "promql_query"
            if metric:
                entry["suggested_metric"] = metric
                entry["suggested_data_view"] = data_view
                entry["suggested_filter_hint"] = f"{metric}: *"
                entry["kibana_action"] = "candidate_event_annotation"
                entry["description"] = (
                    f"PromQL annotation '{name}' looks like a Kibana event-annotation candidate "
                    f"for data view '{data_view}', but the exact target field mapping for metric "
                    f"'{metric}' must be confirmed manually before creating it."
                )
            else:
                entry["kibana_action"] = "manual_annotation"
                entry["description"] = (
                    f"PromQL annotation '{name}' could not be auto-translated; "
                    "create a manual Kibana annotation layer"
                )
        elif "loki" in ds_type:
            entry["type"] = "logql_query"
            entry["kibana_action"] = "manual_annotation"
            entry["description"] = (
                f"LogQL annotation '{name}' requires manual Kibana annotation setup"
            )
        else:
            entry["type"] = "unknown"
            entry["kibana_action"] = "manual_annotation"
            entry["description"] = (
                f"Annotation '{name}' from datasource type '{ds_type}' "
                "needs manual configuration in Kibana"
            )

        translated.append(entry)

    return translated


def build_annotations_summary(annotations: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize annotation translation results."""
    total = len(annotations)
    auto = sum(1 for a in annotations if a.get("kibana_action") == "event_annotation")
    candidate = sum(1 for a in annotations if a.get("kibana_action") == "candidate_event_annotation")
    manual = sum(1 for a in annotations if a.get("kibana_action") == "manual_annotation")
    unsupported = sum(1 for a in annotations if a.get("kibana_action") == "unsupported")
    return {
        "total": total,
        "auto_translated": auto,
        "candidate_event_annotations": candidate,
        "manual_needed": manual,
        "unsupported": unsupported,
    }


__all__ = [
    "build_annotations_summary",
    "translate_annotations",
]
