# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Extract synthetic-data seed requirements from Datadog monitor artifacts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from observability_migration.core.assets.alerting import build_alerting_ir_from_datadog

from .field_map import FieldMapProfile

_IDENT_RE = r"(?:`[^`]+`|[A-Za-z_][\w.-]*)"

_COUNTER_HINTS = {
    "_total", "_count", "_sum", "bytes_sent", "bytes_rcvd",
    "requests", "errors", "dropped", "accepted", "refused",
    "sent", "received", "connections", "restarts", "retrans",
    "completed", "failed", "rejected", "timeout", "evictions",
}

_GAUGE_HINTS = {
    "percent", "ratio", "usage", "utilization", "size",
    "free", "available", "used", "capacity", "current",
    "temperature", "load", "latency", "duration", "uptime",
    "active", "idle", "state", "status", "count",
    "in_use", "limit", "threshold",
}

_SKIP_DIMS = {
    "@timestamp", "time_bucket", "BUCKET", "value", "count", "*",
    "message", "log.level", "http.url", "http.status_code",
}


@dataclass
class MonitorSeedRequirements:
    metric_fields: dict[str, str] = field(default_factory=dict)
    log_measure_fields: dict[str, str] = field(default_factory=dict)
    dimensions: set[str] = field(default_factory=set)

    def merge_metric(self, field_name: str, metric_type: str) -> None:
        if field_name not in self.metric_fields:
            self.metric_fields[field_name] = metric_type
        elif self.metric_fields[field_name] == "gauge" and metric_type == "counter":
            self.metric_fields[field_name] = "counter"

    def merge_log_measure(self, field_name: str, metric_type: str) -> None:
        if field_name not in self.log_measure_fields:
            self.log_measure_fields[field_name] = metric_type
        elif self.log_measure_fields[field_name] == "gauge" and metric_type == "counter":
            self.log_measure_fields[field_name] = "counter"


def discover_monitor_artifact(yaml_dir: str) -> Path | None:
    """Return the sibling raw monitor artifact path when present."""
    root = Path(yaml_dir).resolve().parent
    candidate = root / "raw_monitors" / "datadog_monitors.json"
    return candidate if candidate.exists() else None


def load_monitor_seed_requirements(
    artifact_path: str | Path,
    field_map: FieldMapProfile,
) -> MonitorSeedRequirements:
    """Load a raw monitor export and return metric/dimension seed requirements."""
    path = Path(artifact_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        monitors = [item for item in raw if isinstance(item, dict)]
    elif isinstance(raw, dict) and isinstance(raw.get("monitors"), list):
        monitors = [item for item in raw["monitors"] if isinstance(item, dict)]
    else:
        monitors = []
    return extract_monitor_seed_requirements(monitors, field_map)


def extract_monitor_seed_requirements(
    monitors: list[dict[str, Any]],
    field_map: FieldMapProfile,
) -> MonitorSeedRequirements:
    """Extract target metric fields and dimensions required by supported monitors."""
    requirements = MonitorSeedRequirements()

    for monitor in monitors:
        ir = build_alerting_ir_from_datadog(monitor, field_map=field_map)
        query = str(ir.translated_query or "").strip()
        if not query:
            continue
        target_kind = _seed_target_kind(query)
        _extract_seed_fields_from_query(query, target_kind, requirements)
        _extract_dims_from_query(query, requirements.dimensions)

    return requirements


def _seed_target_kind(query: str) -> str:
    first_line = next((line.strip() for line in query.splitlines() if line.strip()), "")
    if first_line.upper().startswith("FROM LOGS"):
        return "log"
    return "metric"


def _extract_seed_fields_from_query(
    query: str,
    target_kind: str,
    requirements: MonitorSeedRequirements,
) -> None:
    agg_pattern = re.compile(
        rf'(AVG|SUM|MAX|MIN|COUNT|RATE|IRATE|LAST)\(\s*({_IDENT_RE})\s*(?:,|\))'
        rf'|PERCENTILE\(\s*({_IDENT_RE})\s*,',
        re.IGNORECASE,
    )
    for match in agg_pattern.finditer(query):
        agg_fn = (match.group(1) or "PERCENTILE").upper()
        metric_name = match.group(2) or match.group(3) or ""
        metric_name = _strip_identifier_quotes(metric_name)
        if metric_name in ("", "*", "time_bucket", "BUCKET", "@timestamp"):
            continue
        metric_type = _classify_metric(metric_name, agg_fn)
        if target_kind == "log":
            requirements.merge_log_measure(metric_name, metric_type)
        else:
            requirements.merge_metric(metric_name, metric_type)


def _extract_dims_from_query(query: str, dims: set[str]) -> None:
    by_pattern = re.compile(r'\bBY\b\s+(.+?)(?=\n\s*\||\|$|$)', re.IGNORECASE | re.DOTALL)
    where_pattern = re.compile(
        rf'({_IDENT_RE})\s*(?:==|!=|>=|<=|>|<|LIKE|NOT LIKE)\s*(?:\"|\(|-?\d|TRUE\b|FALSE\b)',
        re.IGNORECASE | re.DOTALL,
    )
    agg_pattern = re.compile(
        rf'(?:AVG|SUM|MAX|MIN|COUNT|RATE|IRATE|LAST)\(\s*({_IDENT_RE})\s*(?:,|\))'
        rf'|PERCENTILE\(\s*({_IDENT_RE})\s*,',
        re.IGNORECASE,
    )

    metric_fields = {
        _strip_identifier_quotes(match.group(1) or match.group(2) or "")
        for match in agg_pattern.finditer(query)
        if (match.group(1) or match.group(2))
    }

    for match in by_pattern.finditer(query):
        for part in _split_by_clause(match.group(1)):
            part = part.strip()
            if "=" in part:
                rhs = part.split("=", 1)[1].strip()
                if "BUCKET(" not in rhs.upper() and "(" not in rhs:
                    dims.add(_strip_identifier_quotes(rhs))
            elif "(" not in part and part not in _SKIP_DIMS:
                dims.add(_strip_identifier_quotes(part))

    for match in where_pattern.finditer(query):
        field_name = _strip_identifier_quotes(match.group(1))
        if field_name not in _SKIP_DIMS and field_name not in metric_fields:
            dims.add(field_name)

    dims.difference_update(_SKIP_DIMS)
    dims.difference_update(metric_fields)


def _split_by_clause(by_clause: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in by_clause:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


def _strip_identifier_quotes(field_name: str) -> str:
    field_name = field_name.strip().rstrip("|").strip()
    if not field_name:
        return field_name
    parts = []
    for part in field_name.split("."):
        part = part.strip()
        if part.startswith("`") and part.endswith("`") and len(part) >= 2:
            part = part[1:-1].replace("``", "`")
        parts.append(part)
    return ".".join(parts)


def _classify_metric(name: str, agg_fn: str) -> str:
    name_lower = name.lower()
    if agg_fn in {"RATE", "IRATE"}:
        return "counter"
    for hint in _COUNTER_HINTS:
        if hint in name_lower:
            return "counter"
    for hint in _GAUGE_HINTS:
        if hint in name_lower:
            return "gauge"
    return "gauge"


__all__ = [
    "MonitorSeedRequirements",
    "discover_monitor_artifact",
    "extract_monitor_seed_requirements",
    "load_monitor_seed_requirements",
]
