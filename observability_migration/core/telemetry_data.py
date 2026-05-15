# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Source-agnostic telemetry schema and synthetic data generation."""

from __future__ import annotations

import dataclasses
import datetime
import itertools
import json
import math
import random
import re
from collections.abc import Callable, Iterable, Iterator
from typing import Any

RequestFn = Callable[[str, str, Any | None, str], dict[str, Any]]


def concrete_stream_name(index_pattern: str, stream: dict[str, Any] | None = None) -> str:
    """Return a concrete data stream name that is matched by an artifact index pattern."""
    value = index_pattern.strip()
    required_dataset = _single_required_value(stream, "data_stream.dataset")
    if required_dataset:
        stream_type = _stream_type_from_pattern(value)
        namespace = _single_required_value(stream, "data_stream.namespace") or "default"
        return f"{stream_type}-{_data_stream_part(required_dataset)}-{_data_stream_part(namespace)}"
    if value in {"metrics-*", "logs-*", "traces-*"}:
        return f"{value[:-2]}-generic-default"
    if value.endswith("-*"):
        return f"{value[:-2]}-default"
    if "*" in value or "?" in value:
        prefix = value.split("*", 1)[0].rstrip("-")
        if prefix in {"metrics", "logs", "traces"}:
            return f"{prefix}-generic-default"
        return f"{prefix or 'metrics'}-generic-default"
    return value


def plan_index_template(index_pattern: str, stream: dict[str, Any]) -> dict[str, Any]:
    """Build an index template for one stream contract."""
    concrete_name = concrete_stream_name(index_pattern, stream)
    stream_type = _stream_type_for_contract(index_pattern, concrete_name, stream)
    is_metrics = stream_type == "metrics"
    dataset = _dataset_from_stream(concrete_name)
    namespace = _namespace_from_stream(concrete_name)
    props: dict[str, Any] = {
        "@timestamp": {"type": "date"},
        "data_stream.type": {"type": "constant_keyword", "value": stream_type},
        "data_stream.dataset": {"type": "constant_keyword", "value": dataset},
        "data_stream.namespace": {"type": "constant_keyword", "value": namespace},
    }
    if not is_metrics:
        props["message"] = {"type": "text"}
    routing_path: list[str] = []

    fields = stream.get("fields") or {}
    _dotted_prefixes = _dotted_field_prefixes(fields)
    for field_name, info in sorted(fields.items()):
        if field_name.startswith("data_stream.") or field_name in props:
            continue
        if "." not in field_name and field_name in _dotted_prefixes:
            continue  # skip flat field whose name is also a dotted-child prefix
        if info.get("role") == "metric":
            props[field_name] = {"type": "double"}
            if is_metrics:
                props[field_name]["time_series_metric"] = (
                    "counter" if info.get("metric_kind") == "counter" else "gauge"
                )
        elif is_metrics:
            props[field_name] = {"type": "keyword", "time_series_dimension": True}
            routing_path.append(field_name)
        else:
            props[field_name] = {"type": "keyword"}

    for field_name in _generated_dimension_fields(stream):
        if field_name.startswith("data_stream.") or field_name in props:
            continue
        if "." not in field_name and field_name in _dotted_prefixes:
            continue  # skip flat field that conflicts with dotted children
        if is_metrics:
            props[field_name] = {"type": "keyword", "time_series_dimension": True}
            routing_path.append(field_name)
        else:
            props[field_name] = {"type": "keyword"}

    template: dict[str, Any] = {
        "index_patterns": [concrete_name],
        "data_stream": {},
        "priority": 1000,
        "template": {
            "settings": {"index": {"codec": "best_compression"}},
            "mappings": {"properties": props},
        },
    }
    if is_metrics:
        template["template"]["settings"]["index"]["mode"] = "time_series"
        template["template"]["settings"]["index"]["look_back_time"] = _look_back_time(stream)
        template["template"]["settings"]["index"]["routing_path"] = routing_path or ["data_stream.dataset"]
    return template


def generate_documents(
    contract: dict[str, Any],
    *,
    now: datetime.datetime | None = None,
    data_hours: float = 2,
    interval_sec: int = 60,
    max_combinations: int = 12,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(data_stream, document)`` pairs that satisfy a telemetry contract."""
    now = now or datetime.datetime.now(datetime.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.UTC)
    timestamps = _document_timestamps(now, data_hours=data_hours, interval_sec=interval_sec)
    rng = random.Random(42)
    counter_state: dict[tuple[str, str, int], float] = {}

    for index_pattern, stream in sorted((contract.get("streams") or {}).items()):
        concrete_name = concrete_stream_name(index_pattern, stream)
        stream_type = _stream_type_for_contract(index_pattern, concrete_name, stream)
        is_metrics = stream_type == "metrics"
        dataset = _dataset_from_stream(concrete_name)
        namespace = _namespace_from_stream(concrete_name)
        combinations = _dimension_combinations(stream, max_combinations=max_combinations)
        metric_fields = {
            field_name: info
            for field_name, info in (stream.get("fields") or {}).items()
            if info.get("role") == "metric"
        }

        previous_ts: datetime.datetime | None = None
        for ts in timestamps:
            hour = ts.hour + ts.minute / 60.0
            effective_interval = (
                int((ts - previous_ts).total_seconds())
                if previous_ts is not None
                else min(interval_sec, 60)
            )
            for combo_idx, dimensions in enumerate(combinations):
                doc: dict[str, Any] = {
                    "@timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "data_stream.type": stream_type,
                    "data_stream.dataset": dataset,
                    "data_stream.namespace": namespace,
                    **dimensions,
                }
                if is_metrics:
                    for field_name, info in metric_fields.items():
                        if info.get("metric_kind") == "counter":
                            key = (concrete_name, field_name, combo_idx)
                            counter_state[key] = counter_state.get(key, float(10 + combo_idx))
                            counter_state[key] += _counter_increment(field_name, effective_interval, hour, rng)
                            doc[field_name] = round(counter_state[key], 4)
                        else:
                            doc[field_name] = round(_gauge_value(field_name, hour, combo_idx, rng), 4)
                else:
                    doc.setdefault("message", _log_message(doc, combo_idx))
                yield concrete_name, doc
            previous_ts = ts


def bulk_lines(documents: Iterator[tuple[str, dict[str, Any]]]) -> Iterator[str]:
    for index_name, doc in documents:
        yield json.dumps({"create": {"_index": index_name}})
        yield json.dumps(doc)


def setup_templates_and_streams(
    contract: dict[str, Any],
    request: RequestFn,
    *,
    recreate: bool = True,
) -> None:
    for index_pattern, stream in sorted((contract.get("streams") or {}).items()):
        concrete_name = concrete_stream_name(index_pattern, stream)
        template_name = f"telemetry-data-{concrete_name}"
        if recreate:
            request("DELETE", f"/_data_stream/{concrete_name}", None, "application/json")
        request("DELETE", f"/_index_template/{template_name}", None, "application/json")
        template_result = request(
            "PUT",
            f"/_index_template/{template_name}",
            plan_index_template(index_pattern, stream),
            "application/json",
        )
        _raise_on_error(template_result, f"create index template {template_name}")
        stream_result = request("PUT", f"/_data_stream/{concrete_name}", None, "application/json")
        _raise_on_error(stream_result, f"create data stream {concrete_name}")


def ingest_documents(
    documents: Iterator[tuple[str, dict[str, Any]]],
    request: RequestFn,
    *,
    batch_docs: int = 5000,
) -> IngestSummary:
    summary = IngestSummary()
    batch: list[str] = []
    for index_name, doc in documents:
        summary.docs_per_stream[index_name] = summary.docs_per_stream.get(index_name, 0) + 1
        batch.append(json.dumps({"create": {"_index": index_name}}))
        batch.append(json.dumps(doc))
        if len(batch) >= batch_docs * 2:
            _flush_into_summary(batch, request, summary)
            batch = []
    if batch:
        _flush_into_summary(batch, request, summary)
    return summary


def _dotted_field_prefixes(field_names: Iterable[str]) -> set[str]:
    """Return the set of bare-name prefixes that have at least one dotted child."""
    return {
        name.split(".", 1)[0]
        for name in field_names
        if "." in name and not name.startswith("data_stream.")
    }


def _dimension_combinations(stream: dict[str, Any], *, max_combinations: int) -> list[dict[str, str]]:
    fields = {
        field_name: info
        for field_name, info in (stream.get("fields") or {}).items()
        if info.get("role") != "metric" and not field_name.startswith("data_stream.")
    }
    metric_fields = {
        field_name
        for field_name, info in (stream.get("fields") or {}).items()
        if info.get("role") == "metric"
    }
    required_values = stream.get("required_values") or {}
    required_patterns = stream.get("required_patterns") or {}
    control_fields = stream.get("control_fields") or []
    group_fields = stream.get("group_fields") or []
    value_options: dict[str, list[str]] = {}
    all_names = (
        set(fields)
        | set(required_values)
        | set(required_patterns)
        | set(control_fields)
        | set(group_fields)
    ) - metric_fields
    # Exclude flat names that have dotted children — they would conflict with
    # the object mapping created by the dotted field (e.g. "container" conflicts
    # with "container.name").
    dotted_prefixes = _dotted_field_prefixes(all_names)
    dimension_names = {
        n for n in all_names
        if not n.startswith("data_stream.")
        and not ("." not in n and n in dotted_prefixes)
    }
    for field_name in sorted(dimension_names):
        values = list(required_values.get(field_name) or [])
        values.extend(_expand_patterns(field_name, required_patterns.get(field_name) or []))
        if field_name in set(group_fields) | set(control_fields) and not values:
            values.extend(_default_dimension_values(field_name, count=3))
        if not values:
            values = _default_dimension_values(field_name, count=1)
        value_options[field_name] = _unique(values)

    if not value_options:
        return [{}]
    names = sorted(value_options)
    combos = []
    for values in itertools.product(*(value_options[name] for name in names)):
        combos.append(dict(zip(names, values, strict=True)))
        if len(combos) >= max_combinations:
            break
    _ensure_dimension_value_coverage(
        combos,
        value_options,
        sorted(set(control_fields) | set(required_values) | set(required_patterns)),
    )
    return combos or [{}]


def _document_timestamps(
    now: datetime.datetime,
    *,
    data_hours: float,
    interval_sec: int,
) -> list[datetime.datetime]:
    total_points = max(2, int(data_hours * 3600 // interval_sec) + 1)
    timestamps = {
        now - datetime.timedelta(seconds=(total_points - idx - 1) * interval_sec)
        for idx in range(total_points)
    }
    if interval_sec > 60 and data_hours > 0:
        dense_start = now - datetime.timedelta(hours=min(data_hours, 1))
        dense_points = int((now - dense_start).total_seconds() // 60) + 1
        timestamps.update(
            dense_start + datetime.timedelta(seconds=idx * 60)
            for idx in range(dense_points)
        )
    return sorted(timestamps)


def _ensure_dimension_value_coverage(
    combos: list[dict[str, str]],
    value_options: dict[str, list[str]],
    required_fields: list[str],
) -> None:
    if not combos:
        return
    base = dict(combos[0])
    seen_combos = {tuple(sorted(combo.items())) for combo in combos}
    for field_name in required_fields:
        existing = {combo.get(field_name, "") for combo in combos}
        for value in value_options.get(field_name, []):
            if value in existing:
                continue
            combo = dict(base)
            combo[field_name] = value
            key = tuple(sorted(combo.items()))
            if key in seen_combos:
                existing.add(value)
                continue
            combos.append(combo)
            seen_combos.add(key)
            existing.add(value)


def _expand_patterns(field_name: str, patterns: list[str]) -> list[str]:
    values: list[str] = []
    for pattern in patterns:
        cleaned = pattern.strip()
        if re.fullmatch(r"\d\.\.", cleaned) or re.fullmatch(r"\dxx", cleaned, re.IGNORECASE):
            values.append(f"{cleaned[0]}00")
        elif cleaned and set(cleaned) <= {".", "*"}:
            values.append(_default_dimension_values(field_name, count=1)[0])
        elif cleaned:
            values.append(cleaned.strip("*").replace(".*", "sample"))
    return values


def _default_dimension_values(field_name: str, *, count: int) -> list[str]:
    lowered = field_name.lower()
    if "level" in lowered or lowered.endswith("status"):
        pool = ["error", "warn", "info"]
    elif "environment" in lowered or lowered.endswith(".env") or lowered == "env":
        pool = ["production", "staging", "development"]
    elif "status_code" in lowered or "response.status" in lowered or lowered.endswith("status.code"):
        pool = ["200", "500", "404"]
    elif "method" in lowered:
        pool = ["GET", "POST", "PUT"]
    elif "route" in lowered or "url" in lowered or "path" in lowered:
        pool = ["/api/v1/orders", "/api/v1/users", "/api/health"]
    elif "service" in lowered:
        pool = ["checkout", "frontend", "backend"]
    elif "host" in lowered or "node" in lowered:
        pool = ["host-1", "host-2", "host-3"]
    elif "namespace" in lowered:
        pool = ["default", "monitoring", "production"]
    elif "reason" in lowered:
        pool = ["timeout", "validation", "dependency"]
    else:
        base = field_name.replace(".", "_").replace("@", "").strip("_") or "value"
        pool = [f"{base}_{idx}" for idx in range(1, 4)]
    return pool[:count]


def _counter_increment(field_name: str, interval_sec: int, hour: float, rng: random.Random) -> float:
    base_rate = 0.5 + (abs(hash(field_name)) % 40) / 10
    return max(0.1, base_rate * interval_sec * (0.5 + _diurnal(hour)) + rng.random())


def _gauge_value(field_name: str, hour: float, combo_idx: int, rng: random.Random) -> float:
    base = 10 + abs(hash(field_name)) % 500
    return base + combo_idx * 3 + 25 * _diurnal(hour) + rng.random()


def _diurnal(hour: float) -> float:
    return 0.5 + 0.5 * math.sin(math.pi * (hour - 4) / 12)


def _log_message(doc: dict[str, Any], combo_idx: int) -> str:
    level = doc.get("log.level", "info")
    service = doc.get("service.name", "service")
    return f"{level} synthetic telemetry event for {service} #{combo_idx}"


def _dataset_from_stream(stream_name: str) -> str:
    parts = stream_name.split("-")
    return parts[1] if len(parts) >= 3 else "generic"


def _generated_dimension_fields(stream: dict[str, Any]) -> list[str]:
    fields = stream.get("fields") or {}
    metric_fields = {
        field_name
        for field_name, info in fields.items()
        if info.get("role") == "metric"
    }
    names = set(stream.get("required_values") or {})
    names.update(stream.get("required_patterns") or {})
    names.update(stream.get("control_fields") or [])
    names.update(stream.get("group_fields") or [])
    names -= metric_fields
    for field_name, info in fields.items():
        if info.get("role") != "metric":
            names.add(field_name)
    return sorted(str(name) for name in names if str(name))


def _single_required_value(stream: dict[str, Any] | None, field_name: str) -> str:
    if not stream:
        return ""
    values = (stream.get("required_values") or {}).get(field_name) or []
    unique_values = _unique([str(value).strip() for value in values if str(value).strip()])
    return unique_values[0] if len(unique_values) == 1 else ""


def _stream_type_for_contract(index_pattern: str, concrete_name: str, stream: dict[str, Any] | None) -> str:
    prefix = concrete_name.split("-", 1)[0].strip()
    if prefix in {"metrics", "logs", "traces"}:
        return prefix
    if _has_metric_fields(stream):
        return "metrics"
    name_tokens = set(re.split(r"[^a-z0-9]+", f"{index_pattern} {concrete_name}".lower()))
    if "logs" in name_tokens or "log" in name_tokens:
        return "logs"
    if "traces" in name_tokens or "trace" in name_tokens:
        return "traces"
    if "metrics" in name_tokens or "metric" in name_tokens:
        return "metrics"
    return _stream_type_from_pattern(index_pattern)


def _has_metric_fields(stream: dict[str, Any] | None) -> bool:
    return any(info.get("role") == "metric" for info in (stream or {}).get("fields", {}).values())


def _stream_type_from_pattern(index_pattern: str) -> str:
    prefix = index_pattern.split("-", 1)[0].strip()
    return prefix if prefix in {"metrics", "logs", "traces"} else "metrics"


def _data_stream_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_") or "generic"


def _look_back_time(stream: dict[str, Any]) -> str:
    seconds = max(
        7 * 24 * 60 * 60,
        int(stream.get("_lookback_seconds") or 0),
        _lookback_seconds_from_text(str(stream.get("minimum_lookback") or "")),
    )
    # ES hard cap: index.look_back_time max is 7d (docs.elastic.co/reference/elasticsearch/index-settings/time-series).
    # Anything above 7d is rejected by Elasticsearch at index creation time.
    seconds = min(seconds, 7 * 24 * 60 * 60)
    days = max(1, math.ceil(seconds / (24 * 60 * 60)))
    return f"{days}d"


def _lookback_seconds_from_text(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*(s|m|h|d|day|days|w|week|weeks)\s*", value, re.IGNORECASE)
    if not match:
        return 0
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit == "s":
        return amount
    if unit == "m":
        return amount * 60
    if unit == "h":
        return amount * 3600
    if unit in {"d", "day", "days"}:
        return amount * 24 * 3600
    return amount * 7 * 24 * 3600


def _namespace_from_stream(stream_name: str) -> str:
    parts = stream_name.split("-")
    return parts[2] if len(parts) >= 3 else "default"


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


@dataclasses.dataclass
class IngestSummary:
    """Outcome of ``ingest_documents``: aggregate counts and per-stream stats."""

    ok: int = 0
    errors: int = 0
    docs_per_stream: dict[str, int] = dataclasses.field(default_factory=dict)
    error_samples: list[str] = dataclasses.field(default_factory=list)


def _flush_into_summary(lines: list[str], request: RequestFn, summary: IngestSummary) -> None:
    result = request(
        "POST",
        "/_bulk",
        ("\n".join(lines) + "\n").encode(),
        "application/x-ndjson",
    )
    items = result.get("items", []) if isinstance(result, dict) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        operation = item.get("create") or item.get("index") or {}
        error = operation.get("error") if isinstance(operation, dict) else None
        if error:
            summary.errors += 1
            if len(summary.error_samples) < 3:
                if isinstance(error, dict):
                    sample = str(error.get("reason") or error)
                else:
                    sample = str(error)
                summary.error_samples.append(sample[:240])
        else:
            summary.ok += 1


def _raise_on_error(result: dict[str, Any], action: str) -> None:
    if isinstance(result, dict) and result.get("error"):
        reason = result["error"].get("reason") if isinstance(result["error"], dict) else str(result["error"])
        raise RuntimeError(f"Failed to {action}: {reason}")


__all__ = [
    "IngestSummary",
    "bulk_lines",
    "concrete_stream_name",
    "generate_documents",
    "ingest_documents",
    "plan_index_template",
    "setup_templates_and_streams",
]
