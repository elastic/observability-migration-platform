# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Preflight validation: detect target incompatibilities before generation.

Checks run before the planner/translator to catch:
    - Kibana version compatibility
    - Data view existence and permissions
    - Field type compatibility (aggregatable, searchable, type family)
    - ES|QL row limits and unsupported field types
    - Runtime field budget

Each check returns a PreflightResult that can block, warn, or pass.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from observability_migration.core.verification.field_capabilities import (
    FieldCapability,
    assess_field_usage,
)

from .field_map import FieldMapProfile
from .models import LogAttributeFilter, LogBoolOp, LogNot, LogRange, LogWildcard, ScopeBoolOp, TagFilter

ESQL_DEFAULT_ROW_LIMIT = 1000
ESQL_MAX_ROW_LIMIT = 10000
DEFAULT_RUNTIME_FIELD_BUDGET = 5
MIN_KIBANA_MAJOR = 8
MIN_KIBANA_MINOR = 12
_TEMPLATE_TOKEN_RE = re.compile(r"\$\w+(?:\.\w+)*")


@dataclass
class PreflightIssue:
    """A single preflight finding."""
    level: str  # "block", "warn", "info"
    category: str  # "version", "data_view", "field", "esql_limit", "runtime_field"
    message: str
    widget_id: str = ""
    field_name: str = ""


@dataclass
class PreflightResult:
    """Aggregate preflight result for a dashboard."""
    passed: bool = True
    issues: list[PreflightIssue] = field(default_factory=list)
    field_capabilities: dict[str, FieldCapability] = field(default_factory=dict)

    def add(self, issue: PreflightIssue) -> None:
        self.issues.append(issue)
        if issue.level == "block":
            self.passed = False

    @property
    def blocking_issues(self) -> list[PreflightIssue]:
        return [i for i in self.issues if i.level == "block"]

    @property
    def warnings(self) -> list[PreflightIssue]:
        return [i for i in self.issues if i.level == "warn"]


def check_kibana_version(
    target_version: str,
    source_version: str = "",
) -> list[PreflightIssue]:
    """Validate that the target Kibana version is import-compatible.

    Saved objects import into: same version, newer minor on same major,
    or next major. Older versions are blocked.
    """
    issues: list[PreflightIssue] = []
    target_parts = _parse_version(target_version)
    if not target_parts:
        issues.append(PreflightIssue(
            level="block", category="version",
            message=f"cannot parse target Kibana version: {target_version}",
        ))
        return issues

    t_major, t_minor = target_parts
    if t_major < MIN_KIBANA_MAJOR:
        issues.append(PreflightIssue(
            level="block", category="version",
            message=f"target Kibana {target_version} is below minimum supported {MIN_KIBANA_MAJOR}.x",
        ))
    elif t_major == MIN_KIBANA_MAJOR and t_minor < MIN_KIBANA_MINOR:
        issues.append(PreflightIssue(
            level="warn", category="version",
            message=f"target Kibana {target_version} is below recommended {MIN_KIBANA_MAJOR}.{MIN_KIBANA_MINOR}; ES|QL features may be limited",
        ))

    if source_version:
        s_parts = _parse_version(source_version)
        if s_parts:
            s_major, s_minor = s_parts
            if t_major < s_major - 1:
                issues.append(PreflightIssue(
                    level="block", category="version",
                    message=f"target {target_version} is more than one major version behind source {source_version}; import will fail",
                ))
            elif t_major < s_major:
                issues.append(PreflightIssue(
                    level="warn", category="version",
                    message=f"target {target_version} is one major version behind source {source_version}; some objects may not import cleanly",
                ))
            elif t_major == s_major and t_minor < s_minor:
                issues.append(PreflightIssue(
                    level="block", category="version",
                    message=f"cannot import from {source_version} into older minor {target_version}",
                ))

    return issues


def check_field_compatibility(
    required_fields: list[dict[str, str]],
    field_caps: dict[str, FieldCapability],
) -> list[PreflightIssue]:
    """Check that required fields exist and are usable.

    Each ``required_fields`` entry has keys: ``name``, ``usage`` (filter,
    group_by, aggregate), optionally ``widget_id``, and optionally
    ``type_family`` when the translator needs a specific family.
    """
    issues: list[PreflightIssue] = []

    for req in required_fields:
        name = req["name"]
        usage = req.get("usage", "filter")
        widget_id = req.get("widget_id", "")
        required_type_family = req.get("type_family", "")

        cap = field_caps.get(name)
        assessment = assess_field_usage(
            cap,
            field_name=name,
            display_name=name,
            usage=usage,
            required_type_family=required_type_family,
        )
        for message in assessment.warnings:
            issues.append(PreflightIssue(
                level="warn", category="field",
                message=message,
                widget_id=widget_id, field_name=name,
            ))
        for message in assessment.blocking_reasons:
            issues.append(PreflightIssue(
                level="block", category="field",
                message=message,
                widget_id=widget_id, field_name=name,
            ))

    return issues


def check_field_compatibility_with_profile(
    required_fields: list[dict[str, str]],
    field_map: FieldMapProfile,
) -> list[PreflightIssue]:
    """Check required fields using the Datadog field-map context."""
    issues: list[PreflightIssue] = []

    for req in required_fields:
        name = req["name"]
        usage = req.get("usage", "filter")
        widget_id = req.get("widget_id", "")
        required_type_family = req.get("type_family", "")
        context = req.get("context", "")

        cap = field_map.field_capability(name, context=context)
        issues.extend(
            _issues_for_capability(
                req=req,
                capability=cap,
                usage=usage,
                widget_id=widget_id,
                required_type_family=required_type_family,
            )
        )

    return issues


def check_esql_limits(
    estimated_rows: int | None = None,
    estimated_columns: int | None = None,
    has_unsupported_field_types: bool = False,
) -> list[PreflightIssue]:
    """Check ES|QL execution limits."""
    issues: list[PreflightIssue] = []

    if estimated_rows is not None:
        if estimated_rows > ESQL_MAX_ROW_LIMIT:
            issues.append(PreflightIssue(
                level="block", category="esql_limit",
                message=f"estimated {estimated_rows} rows exceeds ES|QL maximum of {ESQL_MAX_ROW_LIMIT}",
            ))
        elif estimated_rows > ESQL_DEFAULT_ROW_LIMIT:
            issues.append(PreflightIssue(
                level="warn", category="esql_limit",
                message=f"estimated {estimated_rows} rows exceeds ES|QL default limit of {ESQL_DEFAULT_ROW_LIMIT}; explicit LIMIT needed",
            ))

    if estimated_columns is not None and estimated_columns > 50:
        issues.append(PreflightIssue(
            level="warn", category="esql_limit",
            message=f"estimated {estimated_columns} columns may cause display issues in Discover",
        ))

    if has_unsupported_field_types:
        issues.append(PreflightIssue(
            level="warn", category="esql_limit",
            message="query depends on field types not fully supported by ES|QL",
        ))

    return issues


def check_runtime_field_budget(
    runtime_fields_needed: int,
    budget: int = DEFAULT_RUNTIME_FIELD_BUDGET,
) -> list[PreflightIssue]:
    """Check runtime field budget per dashboard."""
    issues: list[PreflightIssue] = []

    if runtime_fields_needed > budget:
        issues.append(PreflightIssue(
            level="block", category="runtime_field",
            message=f"dashboard needs {runtime_fields_needed} runtime fields, exceeding budget of {budget}",
        ))
    elif runtime_fields_needed > 0:
        issues.append(PreflightIssue(
            level="info", category="runtime_field",
            message=f"dashboard uses {runtime_fields_needed} of {budget} runtime field budget",
        ))

    return issues


def check_data_view(
    index_pattern: str,
    data_views_available: list[str] | None = None,
    can_create_data_view: bool = True,
) -> list[PreflightIssue]:
    """Check data view availability for Lens panels."""
    issues: list[PreflightIssue] = []

    if data_views_available is not None:
        if index_pattern not in data_views_available:
            if can_create_data_view:
                issues.append(PreflightIssue(
                    level="info", category="data_view",
                    message=f"data view '{index_pattern}' will be created",
                ))
            else:
                issues.append(PreflightIssue(
                    level="block", category="data_view",
                    message=f"data view '{index_pattern}' does not exist and cannot be created (insufficient privileges)",
                ))

    return issues


def run_preflight(
    dashboard_ir: Any,
    target_kibana_version: str = "",
    field_caps: dict[str, FieldCapability] | None = None,
    field_map: FieldMapProfile | None = None,
    data_views_available: list[str] | None = None,
    runtime_field_budget: int = DEFAULT_RUNTIME_FIELD_BUDGET,
) -> PreflightResult:
    """Run all preflight checks for a normalized dashboard.

    This is the main entry point. Individual checks can also be called
    separately for more granular control.
    """
    result = PreflightResult()

    if target_kibana_version:
        for issue in check_kibana_version(target_kibana_version):
            result.add(issue)

    if field_map is not None:
        result.field_capabilities = dict(field_map.field_caps)
        required = _extract_required_fields(dashboard_ir, field_map=field_map)
        for issue in check_field_compatibility_with_profile(required, field_map):
            result.add(issue)
    elif field_caps is not None:
        result.field_capabilities = field_caps
        required = _extract_required_fields(dashboard_ir)
        for issue in check_field_compatibility(required, field_caps):
            result.add(issue)

    for issue in check_runtime_field_budget(0, budget=runtime_field_budget):
        result.add(issue)

    return result


def build_target_readiness_contract(
    dashboards: list[Any],
    field_map: FieldMapProfile,
) -> dict[str, Any]:
    """Build a machine-readable source-to-target field readiness contract."""
    has_shared_caps = bool(getattr(field_map, "field_caps", {}) or {})
    has_metric_caps = bool(getattr(field_map, "metric_field_caps", {}) or {})
    has_log_caps = bool(getattr(field_map, "log_field_caps", {}) or {})
    required_fields: dict[str, dict[str, Any]] = {}

    for dashboard in dashboards:
        for req in _extract_required_fields(dashboard, field_map=field_map):
            target_name = str(req.get("name", "") or "").strip()
            if not target_name:
                continue
            source_name = str(req.get("source_name", "") or "").strip() or target_name
            context = str(req.get("context", "") or "")
            role = str(req.get("usage", "") or "field")
            widget_id = str(req.get("widget_id", "") or "")
            capability = field_map.field_capability(target_name, context=context)
            context_has_caps = has_shared_caps
            if context == "metric":
                context_has_caps = has_shared_caps or has_metric_caps
            elif context == "log":
                context_has_caps = has_shared_caps or has_log_caps

            status = "unknown"
            field_type = None
            if capability is not None:
                status = "confirmed"
                field_type = capability.type
            elif context_has_caps:
                status = "missing"

            entry = required_fields.setdefault(
                target_name,
                {
                    "target_field": target_name,
                    "source_fields": set(),
                    "roles": set(),
                    "contexts": set(),
                    "widgets": set(),
                    "status": status,
                    "type": field_type,
                },
            )
            entry["source_fields"].add(source_name)
            entry["roles"].add(role)
            if context:
                entry["contexts"].add(context)
            if widget_id:
                entry["widgets"].add(widget_id)
            if entry["status"] != "confirmed":
                entry["status"] = status
                entry["type"] = field_type

    serialized_fields = {}
    for field_name, info in sorted(required_fields.items()):
        serialized_fields[field_name] = {
            "target_field": info["target_field"],
            "source_fields": sorted(info["source_fields"]),
            "roles": sorted(info["roles"]),
            "contexts": sorted(info["contexts"]),
            "widgets": sorted(info["widgets"]),
            "status": info["status"],
            "type": info["type"],
        }

    confirmed = sum(1 for v in serialized_fields.values() if v["status"] == "confirmed")
    missing = sum(1 for v in serialized_fields.values() if v["status"] == "missing")
    unknown = sum(1 for v in serialized_fields.values() if v["status"] == "unknown")
    return {
        "source": "datadog",
        "field_profile": field_map.name,
        "metric_index": field_map.metric_index,
        "logs_index": field_map.logs_index,
        "capabilities": {
            "metric_fields": len(getattr(field_map, "metric_field_caps", {}) or {}),
            "log_fields": len(getattr(field_map, "log_field_caps", {}) or {}),
            "total_fields": len(getattr(field_map, "field_caps", {}) or {}),
        },
        "required_fields": serialized_fields,
        "totals": {
            "fields": len(serialized_fields),
            "fields_confirmed": confirmed,
            "fields_missing": missing,
            "fields_unknown": unknown,
        },
    }


def save_target_readiness_contract(contract: dict[str, Any], output_path: str | Path) -> None:
    output_path = Path(output_path)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(contract, fh, indent=2)


def _extract_required_fields(
    dashboard_ir: Any,
    field_map: FieldMapProfile | None = None,
) -> list[dict[str, str]]:
    """Walk a NormalizedDashboard and extract fields used in queries."""
    required: list[dict[str, str]] = []

    for widget in _iter_widgets(getattr(dashboard_ir, "widgets", [])):
        for q in widget.queries:
            if q.metric_query:
                mq = q.metric_query
                required.append({
                    "name": field_map.map_metric(mq.metric) if field_map else mq.metric,
                    "source_name": mq.metric,
                    "usage": "aggregate",
                    "widget_id": widget.id,
                    "context": "metric",
                    "type_family": "numeric",
                })
                for filt in _collect_metric_scope_filters(mq.scope):
                    if _is_template_token(filt.key):
                        continue
                    required.append({
                        "name": field_map.map_tag(filt.key, context="metric") if field_map else filt.key,
                        "source_name": filt.key,
                        "usage": "filter",
                        "widget_id": widget.id,
                        "context": "metric",
                    })
                for tag in mq.group_by:
                    if _is_template_token(tag):
                        continue
                    required.append({
                        "name": field_map.map_tag(tag, context="metric") if field_map else tag,
                        "source_name": tag,
                        "usage": "group_by",
                        "widget_id": widget.id,
                        "context": "metric",
                    })
            if q.log_query and q.log_query.ast is not None:
                for log_req in _collect_log_required_fields(q.log_query.ast):
                    raw_name = log_req.get("name", "")
                    if _is_template_token(raw_name):
                        continue
                    is_tag = log_req.get("is_tag", False)
                    required.append({
                        "name": _map_log_field_name(raw_name, field_map, is_tag=is_tag) if field_map else raw_name,
                        "source_name": raw_name,
                        "usage": "filter",
                        "widget_id": widget.id,
                        "context": "log",
                    })
        for log_group_field in _extract_log_group_fields(widget, field_map):
            required.append({
                "name": log_group_field["name"],
                "source_name": log_group_field.get("source_name", ""),
                "usage": "group_by",
                "widget_id": widget.id,
                "context": "log",
            })

    return required


def _issues_for_capability(
    req: dict[str, str],
    capability: FieldCapability | None,
    usage: str,
    widget_id: str,
    required_type_family: str,
) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    name = req["name"]
    source_name = req.get("source_name", "")
    display_name = _field_display_name(name, source_name)
    assessment = assess_field_usage(
        capability,
        field_name=name,
        display_name=display_name,
        usage=usage,
        required_type_family=required_type_family,
    )
    for message in assessment.warnings:
        issues.append(PreflightIssue(
            level="warn",
            category="field",
            message=message,
            widget_id=widget_id,
            field_name=name,
        ))
    for message in assessment.blocking_reasons:
        issues.append(PreflightIssue(
            level="block",
            category="field",
            message=message,
            widget_id=widget_id,
            field_name=name,
        ))

    return issues


def _field_display_name(target_name: str, source_name: str) -> str:
    if source_name and source_name != target_name:
        return f"{target_name} (mapped from {source_name})"
    return target_name


def _iter_widgets(widgets: list[Any]) -> list[Any]:
    ordered: list[Any] = []
    for widget in widgets or []:
        ordered.append(widget)
        ordered.extend(_iter_widgets(getattr(widget, "children", [])))
    return ordered


def _collect_metric_scope_filters(nodes: list[Any]) -> list[TagFilter]:
    collected: list[TagFilter] = []
    for node in nodes or []:
        if isinstance(node, TagFilter):
            collected.append(node)
        elif isinstance(node, ScopeBoolOp):
            collected.extend(_collect_metric_scope_filters(node.children))
    return collected


def _collect_log_required_fields(node: Any) -> list[dict[str, Any]]:
    if node is None:
        return []
    if isinstance(node, LogAttributeFilter):
        return [{"name": node.attribute, "is_tag": node.is_tag}]
    if isinstance(node, LogRange):
        return [{"name": node.attribute, "is_tag": False}]
    if isinstance(node, LogWildcard) and node.attribute:
        return [{"name": node.attribute, "is_tag": False}]
    if isinstance(node, LogNot):
        return _collect_log_required_fields(node.child)
    if isinstance(node, LogBoolOp):
        collected: list[dict[str, Any]] = []
        for child in node.children:
            collected.extend(_collect_log_required_fields(child))
        return collected
    return []


def _extract_log_group_fields(
    widget: Any,
    field_map: FieldMapProfile | None = None,
) -> list[dict[str, str]]:
    required: list[dict[str, str]] = []
    for req in (getattr(widget, "raw_definition", {}) or {}).get("requests", []):
        if not isinstance(req, dict):
            continue
        for group_by in req.get("group_by", []):
            if not isinstance(group_by, dict):
                continue
            facet = str(group_by.get("facet", "") or "").strip()
            if not facet or _is_template_token(facet.lstrip("@")):
                continue
            required.append({
                "name": _map_log_field_name(facet, field_map, is_tag=not facet.startswith("@")) if field_map else facet.lstrip("@"),
                "source_name": facet,
            })
    return required


def _map_log_field_name(
    field_name: str,
    field_map: FieldMapProfile | None,
    *,
    is_tag: bool,
) -> str:
    normalized = str(field_name or "").strip().lstrip("@")
    if not field_map:
        return normalized
    if is_tag or normalized in field_map.tag_map:
        return field_map.map_tag(normalized, context="log")
    return field_map.map_log_field(normalized)


def _is_template_token(value: str) -> bool:
    return bool(_TEMPLATE_TOKEN_RE.fullmatch(str(value or "").strip()))


def _parse_version(version_str: str) -> tuple[int, int] | None:
    """Parse 'major.minor' or 'major.minor.patch' → (major, minor)."""
    parts = version_str.strip().split(".")
    try:
        return (int(parts[0]), int(parts[1]))
    except (IndexError, ValueError):
        return None
