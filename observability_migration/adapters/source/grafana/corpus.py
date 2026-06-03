#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0


from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import requests
import yaml

from .promql import PromQLFragment, _parse_fragment, _parse_logql_search, preprocess_grafana_macros
from .rules import RulePackConfig, load_rule_pack_files
from .schema import SchemaResolver

ROOT = Path(__file__).resolve().parents[1]
migrate = SimpleNamespace(
    PromQLFragment=PromQLFragment,
    RulePackConfig=RulePackConfig,
    SchemaResolver=SchemaResolver,
    _parse_fragment=_parse_fragment,
    _parse_logql_search=_parse_logql_search,
    load_rule_pack_files=load_rule_pack_files,
    preprocess_grafana_macros=preprocess_grafana_macros,
)


DEFAULT_METRICS_STREAM = "metrics-prometheus-synthetic"
DEFAULT_LOGS_STREAM = "logs-observability-synthetic"
DEFAULT_SCOPE = "failed"
DEFAULT_SERIES_CAP = 12
DEFAULT_POINTS = 24
DEFAULT_STEP_SECONDS = 300

METRIC_FUNCTIONS = {
    "avg",
    "sum",
    "max",
    "min",
    "count",
    "stddev",
    "rate",
    "irate",
    "increase",
    "delta",
    "deriv",
    "avg_over_time",
    "sum_over_time",
    "max_over_time",
    "min_over_time",
    "count_over_time",
    "histogram_quantile",
    "scalar",
    "time",
    "label_replace",
    "abs",
    "ceil",
    "floor",
    "round",
    "topk",
    "bottomk",
    "sort",
    "sort_desc",
    "clamp_max",
    "clamp_min",
}

PROMQL_KEYWORDS = {
    "by",
    "without",
    "on",
    "group_left",
    "group_right",
    "bool",
    "ignoring",
    "offset",
    "and",
    "or",
    "unless",
}

DEFAULT_LABEL_VALUES = {
    "alertname": ["SyntheticAlertA", "SyntheticAlertB"],
    "chip": ["coretemp", "acpitz"],
    "cluster": ["synthetic-cluster"],
    "container": ["app", "sidecar"],
    "cpu": ["0", "1", "2", "3"],
    "device": ["eth0", "sda1", "nvme0n1p1"],
    "fstype": ["ext4", "xfs"],
    "hostname": ["synthetic-host-1", "synthetic-host-2"],
    "host.ip": ["10.10.0.11", "10.10.0.12"],
    "host.name": ["synthetic-host-1", "synthetic-host-2"],
    "instance": ["synthetic-1:9100", "synthetic-2:9100"],
    "integration": ["slack", "pagerduty", "email"],
    "job": ["alertmanager", "node-exporter", "nginx-exporter"],
    "k8s.cluster.name": ["synthetic-cluster"],
    "k8s.container.name": ["app", "sidecar"],
    "k8s.namespace.name": ["default", "observability"],
    "k8s.node.name": ["synthetic-node-1", "synthetic-node-2"],
    "k8s.pod.name": ["synthetic-pod-1", "synthetic-pod-2"],
    "le": ["0.1", "0.5", "1", "5", "+Inf"],
    "mode": ["idle", "user", "system", "iowait"],
    "mountpoint": ["/", "/data", "/var"],
    "msg_type": ["info", "warning", "error"],
    "namespace": ["default", "observability"],
    "node": ["synthetic-node-1", "synthetic-node-2"],
    "nodename": ["synthetic-host-1", "synthetic-host-2"],
    "orchestrator.cluster.name": ["synthetic-cluster"],
    "pod": ["synthetic-pod-1", "synthetic-pod-2"],
    "quantile": ["0.5", "0.9", "0.99"],
    "service.instance.id": ["synthetic-1:9100", "synthetic-2:9100"],
    "service.name": ["synthetic-service", "alertmanager", "node-exporter"],
    "state": ["active", "inactive"],
    "status": ["firing", "suppressed", "resolved"],
}

DISK_COMBOS = [
    {"mountpoint": "/", "fstype": "ext4", "device": "sda1"},
    {"mountpoint": "/data", "fstype": "xfs", "device": "nvme0n1p1"},
]

CPU_MODE_COMBOS = [
    {"cpu": "0", "mode": "idle"},
    {"cpu": "0", "mode": "user"},
    {"cpu": "0", "mode": "system"},
    {"cpu": "0", "mode": "iowait"},
    {"cpu": "1", "mode": "idle"},
    {"cpu": "1", "mode": "user"},
]

IDENTITY_FIELDS = {
    "service.instance.id",
    "instance",
    "service.name",
    "job",
    "host.name",
    "host.ip",
    "nodename",
    "hostname",
    "cluster",
    "k8s.cluster.name",
    "orchestrator.cluster.name",
    "namespace",
    "k8s.namespace.name",
    "node",
    "k8s.node.name",
    "pod",
    "k8s.pod.name",
    "container",
    "k8s.container.name",
}


@dataclass
class MetricDemand:
    name: str
    kind: str
    labels: set[str] = field(default_factory=set)
    panels: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)


@dataclass
class LogDemand:
    fields: set[str] = field(default_factory=set)
    search_terms: set[str] = field(default_factory=set)
    panels: set[str] = field(default_factory=set)


@dataclass
class CorpusManifest:
    metrics: dict[str, MetricDemand] = field(default_factory=dict)
    labels: set[str] = field(default_factory=set)
    logs: LogDemand = field(default_factory=LogDemand)


def _load_structured_file(path: Path) -> dict[str, Any]:
    text = path.read_text()
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text) or {}


def load_profile(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    return _load_structured_file(Path(path))


def _stable_int(text: str) -> int:
    return int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:12], 16)


def _slug(text: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_").lower()
    return clean or "value"


def infer_service_name(metric_name: str, profile: dict[str, Any]) -> str:
    service_overrides = profile.get("service_name_overrides", {})
    if metric_name in service_overrides:
        return str(service_overrides[metric_name])
    prefixes = [
        ("alertmanager_", "alertmanager"),
        ("node_", "node-exporter"),
        ("nginx_", "nginx-exporter"),
        ("container_", "cadvisor"),
        ("otelcol_", "otel-collector"),
        ("awsotelcol_", "aws-otel-collector"),
        ("prometheus_", "prometheus"),
        ("promhttp_", "prometheus"),
        ("process_", "system"),
        ("go_", "go-runtime"),
    ]
    for prefix, service in prefixes:
        if metric_name.startswith(prefix):
            return service
    return "synthetic-service"


def infer_metric_kind(metric_name: str, profile: dict[str, Any]) -> str:
    overrides = profile.get("metric_kinds", {})
    if metric_name in overrides:
        return str(overrides[metric_name]).lower()
    if metric_name.endswith("_bucket") or metric_name.endswith("_sum") or metric_name.endswith("_count"):
        return "counter"
    if any(metric_name.endswith(suffix) for suffix in ("_total", "_seconds_total", "_bytes_total", "_created")):
        return "counter"
    if metric_name.endswith("_info") or "_build_info" in metric_name or "_version_info" in metric_name:
        return "info"
    if "timestamp" in metric_name:
        return "timestamp"
    return "gauge"


def _merge_metric_kind(existing: str, candidate: str) -> str:
    existing = (existing or "").lower()
    candidate = (candidate or "").lower()
    if not existing:
        return candidate or "gauge"
    if not candidate or candidate == existing:
        return existing
    if "timestamp" in {existing, candidate}:
        return "timestamp"
    if "info" in {existing, candidate}:
        return "info"
    if "counter" in {existing, candidate}:
        return "counter"
    return existing


def _label_values(field_name: str, metric_name: str, profile: dict[str, Any]) -> list[str]:
    overrides = profile.get("label_values", {})
    if field_name in overrides:
        return [str(item) for item in overrides[field_name]]
    if field_name in DEFAULT_LABEL_VALUES:
        return list(DEFAULT_LABEL_VALUES[field_name])
    leaf = field_name.split(".")[-1]
    if leaf in overrides:
        return [str(item) for item in overrides[leaf]]
    if leaf in DEFAULT_LABEL_VALUES:
        return list(DEFAULT_LABEL_VALUES[leaf])
    if leaf == "service":
        return [infer_service_name(metric_name, profile)]
    return [f"{_slug(leaf)}-a", f"{_slug(leaf)}-b"]


def _identity_bundle(metric_name: str, idx: int, profile: dict[str, Any]) -> dict[str, str]:
    service_name = infer_service_name(metric_name, profile)
    host_name = f"{_slug(service_name)}-host-{idx}"
    instance_id = f"{_slug(service_name)}-{idx}:9100"
    cluster_name = str(profile.get("fixed_dimensions", {}).get("cluster", "synthetic-cluster"))
    namespace_name = str(profile.get("fixed_dimensions", {}).get("namespace", "default"))
    node_name = f"{_slug(service_name)}-node-{idx}"
    pod_name = f"{_slug(service_name)}-pod-{idx}"
    container_name = _slug(service_name)
    return {
        "service.instance.id": instance_id,
        "instance": instance_id,
        "service.name": service_name,
        "job": service_name,
        "host.name": host_name,
        "hostname": host_name,
        "nodename": host_name,
        "host.ip": f"10.10.0.{10 + idx}",
        "cluster": cluster_name,
        "k8s.cluster.name": cluster_name,
        "orchestrator.cluster.name": cluster_name,
        "namespace": namespace_name,
        "k8s.namespace.name": namespace_name,
        "node": node_name,
        "k8s.node.name": node_name,
        "pod": pod_name,
        "k8s.pod.name": pod_name,
        "container": container_name,
        "k8s.container.name": container_name,
    }


def _resolve_label_variants(label: str, resolver: Any) -> set[str]:
    results = set()
    mapped = resolver.resolve_label(label) if resolver else label
    if mapped:
        results.add(mapped)
    if resolver and label not in resolver._rule_pack.ignored_labels:
        results.add(label)
        for candidate in resolver._candidate_fields(label):
            results.add(candidate)
    elif label:
        results.add(label)
    return {item for item in results if item and item != "time_bucket"}


def _update_metric(
    manifest: CorpusManifest,
    metric_name: str,
    labels: set[str],
    panel_ref: str,
    source: str,
    profile: dict[str, Any],
    kind_hint: str = "",
):
    metric = manifest.metrics.get(metric_name)
    inferred_kind = _merge_metric_kind(infer_metric_kind(metric_name, profile), kind_hint)
    if metric is None:
        metric = MetricDemand(name=metric_name, kind=inferred_kind)
        manifest.metrics[metric_name] = metric
    else:
        metric.kind = _merge_metric_kind(metric.kind, inferred_kind)
    metric.labels.update(labels)
    metric.panels.add(panel_ref)
    metric.sources.add(source)
    manifest.labels.update(labels)


def _collect_metric_kinds(frag: Any, kind_hints: dict[str, str]):
    if not frag:
        return
    metric_name = getattr(frag, "metric", "")
    range_func = (getattr(frag, "range_func", "") or "").lower()
    if metric_name and range_func in {"rate", "irate", "increase"}:
        kind_hints[metric_name] = _merge_metric_kind(kind_hints.get(metric_name, ""), "counter")

    children = [
        getattr(frag, "binary_rhs", None),
        (frag.extra or {}).get("left_frag"),
        (frag.extra or {}).get("right_frag"),
    ]
    for child in children:
        if isinstance(child, migrate.PromQLFragment):
            _collect_metric_kinds(child, kind_hints)


def _collect_fragment(frag: Any, resolver: Any, metrics: set[str], labels: set[str], log_demand: LogDemand):
    if not frag:
        return
    if getattr(frag, "metric", ""):
        metrics.add(frag.metric)
    for matcher in getattr(frag, "matchers", []) or []:
        labels.update(_resolve_label_variants(matcher.get("label", ""), resolver))
    for label in getattr(frag, "group_labels", []) or []:
        labels.update(_resolve_label_variants(label, resolver))
    for label in (frag.extra or {}).get("inner_group", []) or []:
        labels.update(_resolve_label_variants(label, resolver))
    for label in (frag.extra or {}).get("join_labels", []) or []:
        labels.update(_resolve_label_variants(label, resolver))
    for matcher in (frag.extra or {}).get("start_matchers", []) or []:
        labels.update(_resolve_label_variants(matcher.get("label", ""), resolver))
    if (frag.extra or {}).get("start_metric"):
        metrics.add(frag.extra["start_metric"])

    if getattr(frag, "family", "") in {"logql_stream", "logql_count"}:
        log_demand.fields.update(labels)
        search_expr = migrate._parse_logql_search(getattr(frag, "raw_expr", "") or "")
        if search_expr:
            log_demand.search_terms.add(search_expr)

    children = [
        getattr(frag, "binary_rhs", None),
        (frag.extra or {}).get("left_frag"),
        (frag.extra or {}).get("right_frag"),
    ]
    for child in children:
        if isinstance(child, migrate.PromQLFragment):
            _collect_fragment(child, resolver, metrics, labels, log_demand)


def _regex_metrics(expr: str) -> set[str]:
    metrics = set()
    for match in re.finditer(r"\b([A-Za-z_:][A-Za-z0-9_:]*)\b(?=\s*(?:\{|\[))", expr):
        token = match.group(1)
        if token.lower() not in METRIC_FUNCTIONS and token.lower() not in PROMQL_KEYWORDS:
            metrics.add(token)
    return metrics


def _regex_labels(expr: str, resolver: Any) -> set[str]:
    labels = set()
    for match in re.finditer(r"\bby\s*\(([^)]*)\)|\bwithout\s*\(([^)]*)\)", expr, re.IGNORECASE):
        raw = match.group(1) or match.group(2) or ""
        for label in [item.strip() for item in raw.split(",") if item.strip()]:
            labels.update(_resolve_label_variants(label, resolver))
    for match in re.finditer(r"\{([^}]*)\}", expr):
        selector = match.group(1)
        for label_match in re.finditer(r'([A-Za-z_][A-Za-z0-9_]*)\s*(?:=|!=|=~|!~)', selector):
            labels.update(_resolve_label_variants(label_match.group(1), resolver))
    return labels


def _extract_metrics_from_esql(esql: str) -> set[str]:
    metrics = set()
    pattern = (
        r"\b(?:AVG|SUM|COUNT|MAX|MIN|RATE|IRATE|INCREASE|DELTA|DERIV|AVG_OVER_TIME|SUM_OVER_TIME|"
        r"MAX_OVER_TIME|MIN_OVER_TIME|COUNT_OVER_TIME|COUNT_DISTINCT|PERCENTILE_OVER_TIME)\(\s*([A-Za-z0-9_.:]+)"
    )
    for match in re.finditer(pattern, esql or ""):
        candidate = match.group(1)
        if candidate not in {"*", "NULL"} and not candidate.startswith("@"):
            metrics.add(candidate)
    return metrics


def collect_demand_from_promql(
    expr: str,
    panel_ref: str,
    manifest: CorpusManifest,
    resolver: Any,
    rule_pack: Any,
    profile: dict[str, Any],
    source: str = "promql",
) -> set[str]:
    metrics = set()
    labels = set()
    log_demand = LogDemand()
    kind_hints: dict[str, str] = {}
    clean = migrate.preprocess_grafana_macros(expr, rule_pack)
    frag = migrate._parse_fragment(clean)
    _collect_fragment(frag, resolver, metrics, labels, log_demand)
    _collect_metric_kinds(frag, kind_hints)
    if not metrics:
        metrics.update(_regex_metrics(clean))
    if not labels:
        labels.update(_regex_labels(clean, resolver))
    for metric_name in metrics:
        extra_labels = set(profile.get("metric_labels", {}).get(metric_name, []))
        _update_metric(
            manifest,
            metric_name,
            labels | extra_labels,
            panel_ref,
            source,
            profile,
            kind_hint=kind_hints.get(metric_name, ""),
        )
    if log_demand.fields or log_demand.search_terms or getattr(frag, "family", "") in {"logql_stream", "logql_count"}:
        manifest.logs.fields.update(log_demand.fields | labels)
        manifest.logs.search_terms.update(log_demand.search_terms)
        manifest.logs.panels.add(panel_ref)
    return metrics


def _split_promql_bundle(text: str) -> list[str]:
    return [part.strip() for part in (text or "").split(" ||| ") if part.strip()]


def _panel_lookup(report: dict[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    lookup = defaultdict(list)
    for dashboard in report.get("dashboards", []):
        for panel in dashboard.get("panels", []):
            lookup[(dashboard.get("title", ""), panel.get("title", ""))].append(panel)
    return lookup


def collect_demand_from_report(path: Path, manifest: CorpusManifest, resolver: Any, rule_pack: Any, profile: dict[str, Any], scope: str):
    report = _load_structured_file(path)
    panel_lookup = _panel_lookup(report)

    if scope == "all" or "validation" not in report:
        for dashboard in report.get("dashboards", []):
            for panel in dashboard.get("panels", []):
                panel_ref = f"{dashboard.get('title', '')}::{panel.get('title', '')}"
                for expr in _split_promql_bundle(panel.get("promql", "")):
                    collect_demand_from_promql(expr, panel_ref, manifest, resolver, rule_pack, profile, source="report")
        return

    for record in report.get("validation", {}).get("records", []):
        if record.get("status") == "pass":
            continue
        panel_ref = f"{record.get('dashboard', '')}::{record.get('panel', '')}"
        panels = panel_lookup.get((record.get("dashboard", ""), record.get("panel", "")), [])
        panel_metrics = set()
        for panel in panels[:1]:
            for expr in _split_promql_bundle(panel.get("promql", "")):
                panel_metrics.update(
                    collect_demand_from_promql(expr, panel_ref, manifest, resolver, rule_pack, profile, source="validation_panel")
                )
            if not panel_metrics:
                panel_metrics.update(_extract_metrics_from_esql(panel.get("esql", "")))
        analysis = record.get("analysis") or {}
        missing_labels = set()
        for entry in analysis.get("unknown_columns", []):
            field_name = entry.get("name", "")
            if entry.get("role") == "metric" and field_name:
                _update_metric(
                    manifest,
                    field_name,
                    set(profile.get("metric_labels", {}).get(field_name, [])),
                    panel_ref,
                    "validation_missing_metric",
                    profile,
                )
                panel_metrics.add(field_name)
            elif entry.get("role") == "label" and field_name:
                missing_labels.add(field_name)
                manifest.labels.add(field_name)
                manifest.logs.fields.add(field_name)
        if missing_labels and panel_metrics:
            for metric_name in panel_metrics:
                _update_metric(manifest, metric_name, missing_labels, panel_ref, "validation_missing_label", profile)
        for index_name in analysis.get("unknown_indexes", []):
            if str(index_name).startswith("logs-"):
                manifest.logs.panels.add(panel_ref)


def collect_demand_from_dashboards(input_dir: Path, manifest: CorpusManifest, resolver: Any, rule_pack: Any, profile: dict[str, Any]):
    for path in sorted(input_dir.glob("*.json")):
        dashboard = _load_structured_file(path)
        dashboard_title = dashboard.get("title", path.stem)
        panels = list(dashboard.get("panels", []))
        for row in dashboard.get("rows", []):
            panels.extend(row.get("panels", []))
        for panel in panels:
            panel_ref = f"{dashboard_title}::{panel.get('title', '')}"
            for target in panel.get("targets", []):
                expr = target.get("expr")
                if expr:
                    collect_demand_from_promql(expr, panel_ref, manifest, resolver, rule_pack, profile, source="dashboard")


def _limited_expand(series: list[dict[str, str]], additions: list[dict[str, str]], cap: int) -> list[dict[str, str]]:
    if not additions:
        return series
    expanded = []
    for base in series:
        for extra in additions:
            item = dict(base)
            item.update(extra)
            expanded.append(item)
            if len(expanded) >= cap:
                return expanded
    return expanded


def _special_label_combos(label_fields: set[str]) -> tuple[list[dict[str, str]], set[str]]:
    used = set()
    combos = []
    if label_fields & {"mountpoint", "fstype", "device"}:
        used.update({"mountpoint", "fstype", "device"} & label_fields)
        combos = [{key: value for key, value in combo.items() if key in label_fields} for combo in DISK_COMBOS]
        return combos, used
    if label_fields & {"cpu", "mode"}:
        used.update({"cpu", "mode"} & label_fields)
        combos = [{key: value for key, value in combo.items() if key in label_fields} for combo in CPU_MODE_COMBOS]
        return combos, used
    return [], used


def build_metric_series(metric: MetricDemand, profile: dict[str, Any], cap: int) -> list[dict[str, str]]:
    fixed_dimensions = {str(key): str(value) for key, value in profile.get("fixed_dimensions", {}).items()}
    label_fields = set(metric.labels) | set(fixed_dimensions)
    service_bundles = [_identity_bundle(metric.name, idx, profile) for idx in (1, 2)]
    wants_identity = bool(label_fields & IDENTITY_FIELDS)
    series = service_bundles if wants_identity else [service_bundles[0]]

    special_combos, used_labels = _special_label_combos(label_fields)
    if special_combos:
        series = _limited_expand(series[:1], special_combos, cap)

    remaining = sorted(field for field in label_fields if field not in used_labels and field not in IDENTITY_FIELDS)
    for field_name in remaining:
        values = _label_values(field_name, metric.name, profile)[:3]
        if len(series) * len(values) <= cap:
            series = _limited_expand(series, [{field_name: value} for value in values], cap)
            continue
        for idx, item in enumerate(series):
            item[field_name] = values[idx % len(values)]

    for item in series:
        item.update(fixed_dimensions)
    return series[:cap]


def _assign_path(target: dict[str, Any], field_path: str, value: Any):
    parts = field_path.split(".")
    node = target
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _mapping_for_path(properties: dict[str, Any], field_path: str, leaf_mapping: dict[str, Any]):
    parts = field_path.split(".")
    node = properties
    for part in parts[:-1]:
        entry = node.setdefault(part, {"properties": {}})
        entry.setdefault("properties", {})
        node = entry["properties"]
    node[parts[-1]] = leaf_mapping


def _metric_value(metric: MetricDemand, dims: dict[str, str], point_idx: int, timestamp: dt.datetime) -> float:
    seed = _stable_int(metric.name + "|" + "|".join(f"{k}={v}" for k, v in sorted(dims.items())))
    if metric.kind == "info":
        return 1.0
    if metric.kind == "timestamp":
        return float(int((timestamp - dt.timedelta(minutes=90)).timestamp()))
    if metric.kind == "counter":
        slope = 2.0 + float(seed % 17)
        bucket_factor = 1.0
        if "le" in dims:
            order = _label_values("le", metric.name, {})
            try:
                bucket_factor = 1.0 + order.index(dims["le"])
            except ValueError:
                bucket_factor = 1.0
        return round((100.0 + (seed % 91)) + (point_idx * slope * bucket_factor), 4)

    baseline = 10.0 + float(seed % 250)
    amplitude = 2.0 + float(seed % 19)
    phase = (seed % 13) / 3.0
    if any(token in metric.name for token in ("bytes", "memory", "filesystem", "disk")):
        baseline *= 10_000_000.0
    elif any(token in metric.name for token in ("seconds", "duration", "latency")):
        baseline = 5.0 + float(seed % 45)
    elif any(token in metric.name for token in ("percent", "usage", "health_score", "score")):
        baseline = 55.0
        amplitude = 20.0
    if "quantile" in dims:
        try:
            baseline *= float(dims["quantile"])
        except ValueError:
            pass
    value = baseline + (amplitude * math.sin(point_idx / 3.0 + phase))
    if any(token in metric.name for token in ("percent", "usage", "score")):
        value = max(0.0, min(100.0, value))
    return round(value, 4)


def generate_metric_documents(manifest: CorpusManifest, profile: dict[str, Any], points: int, step_seconds: int, cap: int) -> tuple[list[dict[str, Any]], set[str]]:
    docs_by_key: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
    labels = set(manifest.labels)
    end = dt.datetime.now(dt.UTC).replace(microsecond=0)
    start = end - dt.timedelta(seconds=step_seconds * max(points - 1, 1))
    for metric in sorted(manifest.metrics.values(), key=lambda item: item.name):
        series = build_metric_series(metric, profile, cap)
        labels.update(metric.labels)
        for dims in series:
            dims_key = tuple(sorted((key, str(value)) for key, value in dims.items()))
            for point_idx in range(points):
                timestamp = start + dt.timedelta(seconds=point_idx * step_seconds)
                timestamp_text = timestamp.isoformat().replace("+00:00", "Z")
                key = (timestamp_text, dims_key)
                doc = docs_by_key.get(key)
                if doc is None:
                    doc = {
                        "@timestamp": timestamp_text,
                        "start_timestamp": start.isoformat().replace("+00:00", "Z"),
                    }
                    for field_name, value in dims.items():
                        _assign_path(doc, field_name, value)
                    docs_by_key[key] = doc
                _assign_path(doc, metric.name, _metric_value(metric, dims, point_idx, timestamp))
    return list(docs_by_key.values()), labels


def generate_log_documents(manifest: CorpusManifest, profile: dict[str, Any], points: int, step_seconds: int) -> tuple[list[dict[str, Any]], set[str]]:
    if not manifest.logs.panels:
        return [], set()
    docs = []
    fields = set(manifest.logs.fields) | {"service.name", "service.instance.id", "host.name"}
    terms = sorted(manifest.logs.search_terms) or ["synthetic", "obs-migrate"]
    end = dt.datetime.now(dt.UTC).replace(microsecond=0)
    start = end - dt.timedelta(seconds=step_seconds * max(points - 1, 1))
    bundles = [_identity_bundle("synthetic_logs", 1, profile), _identity_bundle("synthetic_logs", 2, profile)]
    extra_fields = sorted(field for field in fields if field not in IDENTITY_FIELDS)
    for idx in range(points):
        timestamp = start + dt.timedelta(seconds=idx * step_seconds)
        for bundle_idx, bundle in enumerate(bundles):
            doc = {
                "@timestamp": timestamp.isoformat().replace("+00:00", "Z"),
                "message": f"synthetic log event {idx} {' '.join(terms)} panel={bundle_idx + 1}",
                "log": {"level": ["INFO", "WARN", "ERROR"][idx % 3]},
            }
            for field_name, value in bundle.items():
                _assign_path(doc, field_name, value)
            for field_name in extra_fields:
                values = _label_values(field_name, "synthetic_logs", profile)
                _assign_path(doc, field_name, values[(idx + bundle_idx) % len(values)])
            docs.append(doc)
    return docs, fields


def _write_bulk_file(path: Path, stream_name: str, docs: list[dict[str, Any]]):
    with path.open("w") as fh:
        for doc in docs:
            fh.write(json.dumps({"create": {"_index": stream_name}}) + "\n")
            fh.write(json.dumps(doc) + "\n")


def _build_metrics_template(stream_name: str, manifest: CorpusManifest, metric_fields: set[str], label_fields: set[str]) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "@timestamp": {"type": "date"},
        "start_timestamp": {"type": "date"},
        "data_stream": {
            "properties": {
                "type": {"type": "constant_keyword", "value": "metrics"},
                "dataset": {"type": "constant_keyword", "value": "prometheus"},
                "namespace": {"type": "constant_keyword", "value": "synthetic"},
            }
        },
    }
    for field_name in sorted(label_fields):
        if field_name == "@timestamp":
            continue
        _mapping_for_path(properties, field_name, {"type": "keyword", "time_series_dimension": True, "ignore_above": 1024})
    for metric_name in sorted(metric_fields):
        metric = manifest.metrics[metric_name]
        _mapping_for_path(
            properties,
            metric_name,
            {"type": "double", "time_series_metric": "counter" if metric.kind == "counter" else "gauge"},
        )
    return {
        "index_patterns": [stream_name],
        "priority": 500,
        "data_stream": {},
        "template": {
            "settings": {
                "index": {
                    "mode": "time_series",
                    "mapping": {
                        "total_fields": {"limit": 20000},
                    },
                }
            },
            "mappings": {
                "dynamic": False,
                "properties": properties,
            },
        },
    }


def _build_logs_template(stream_name: str, label_fields: set[str]) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "@timestamp": {"type": "date"},
        "message": {"type": "wildcard"},
        "data_stream": {
            "properties": {
                "type": {"type": "constant_keyword", "value": "logs"},
                "dataset": {"type": "constant_keyword", "value": "observability_migration.synthetic"},
                "namespace": {"type": "constant_keyword", "value": "default"},
            }
        },
        "log": {
            "properties": {
                "level": {"type": "keyword"},
            }
        },
    }
    for field_name in sorted(label_fields):
        if field_name in {"@timestamp", "message"}:
            continue
        _mapping_for_path(properties, field_name, {"type": "keyword", "ignore_above": 1024})
    return {
        "index_patterns": [stream_name],
        "priority": 500,
        "data_stream": {},
        "template": {
            "settings": {
                "index": {
                    "mapping": {
                        "total_fields": {"limit": 20000},
                    }
                }
            },
            "mappings": {
                "dynamic": False,
                "properties": properties,
            },
        },
    }


def _request(method: str, url: str, *, verify: bool | str = True, **kwargs) -> requests.Response:
    kwargs.setdefault("verify", verify)
    response = requests.request(method, url, timeout=30, **kwargs)
    response.raise_for_status()
    return response


def _delete_if_exists(es_url: str, resource: str, verify: bool | str = True):
    response = requests.delete(f"{es_url}/{resource}", timeout=30, verify=verify)
    if response.status_code not in {200, 404}:
        response.raise_for_status()


def apply_to_elasticsearch(
    es_url: str,
    metrics_stream: str,
    logs_stream: str,
    metrics_template: dict[str, Any],
    logs_template: dict[str, Any],
    metrics_bulk: Path,
    logs_bulk: Path | None,
    reset: bool,
    verify: bool | str = True,
):
    metrics_template_name = "observability-migration-synthetic-metrics"
    logs_template_name = "observability-migration-synthetic-logs"
    if reset:
        _delete_if_exists(es_url, f"_data_stream/{metrics_stream}", verify=verify)
        _delete_if_exists(es_url, f"_data_stream/{logs_stream}", verify=verify)
        _delete_if_exists(es_url, f"_index_template/{metrics_template_name}", verify=verify)
        _delete_if_exists(es_url, f"_index_template/{logs_template_name}", verify=verify)

    _request("PUT", f"{es_url}/_index_template/{metrics_template_name}", json=metrics_template, verify=verify)
    _request("PUT", f"{es_url}/_data_stream/{metrics_stream}", verify=verify)
    with metrics_bulk.open("rb") as fh:
        bulk_response = _request(
            "POST",
            f"{es_url}/_bulk?refresh=true",
            data=fh.read(),
            headers={"Content-Type": "application/x-ndjson"},
            verify=verify,
        )
    bulk_body = bulk_response.json()
    if bulk_body.get("errors"):
        raise RuntimeError(f"Metric bulk upload reported errors: {json.dumps(bulk_body)[:2000]}")

    if logs_bulk and logs_bulk.exists() and logs_bulk.stat().st_size:
        _request("PUT", f"{es_url}/_index_template/{logs_template_name}", json=logs_template, verify=verify)
        _request("PUT", f"{es_url}/_data_stream/{logs_stream}", verify=verify)
        with logs_bulk.open("rb") as fh:
            logs_response = _request(
                "POST",
                f"{es_url}/_bulk?refresh=true",
                data=fh.read(),
                headers={"Content-Type": "application/x-ndjson"},
                verify=verify,
            )
        logs_body = logs_response.json()
        if logs_body.get("errors"):
            raise RuntimeError(f"Log bulk upload reported errors: {json.dumps(logs_body)[:2000]}")


def _manifest_json(manifest: CorpusManifest, metrics_docs: list[dict[str, Any]], log_docs: list[dict[str, Any]], metrics_stream: str, logs_stream: str) -> dict[str, Any]:
    return {
        "summary": {
            "metrics": len(manifest.metrics),
            "labels": len(manifest.labels),
            "log_fields": len(manifest.logs.fields),
            "metric_docs": len(metrics_docs),
            "log_docs": len(log_docs),
        },
        "streams": {
            "metrics": metrics_stream,
            "logs": logs_stream,
        },
        "metrics": [
            {
                "name": item.name,
                "kind": item.kind,
                "labels": sorted(item.labels),
                "panels": sorted(item.panels),
                "sources": sorted(item.sources),
            }
            for item in sorted(manifest.metrics.values(), key=lambda entry: entry.name)
        ],
        "logs": {
            "fields": sorted(manifest.logs.fields),
            "search_terms": sorted(manifest.logs.search_terms),
            "panels": sorted(manifest.logs.panels),
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Generate a synthetic corpus for Grafana migration validation.")
    parser.add_argument(
        "--migration-report",
        action="append",
        default=[],
        help="Path to migration_report.json from the migration pipeline",
    )
    parser.add_argument(
        "--input-dir",
        action="append",
        default=[],
        help="Directory with Grafana dashboard JSON files",
    )
    parser.add_argument(
        "--rules-file",
        action="append",
        default=[],
        help="Optional migration rule pack for label resolution",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Optional YAML/JSON profile with label values and metric kind overrides",
    )
    parser.add_argument(
        "--output-dir",
        default="validation/synthetic_corpus",
        help="Directory for generated manifest and NDJSON bulk files",
    )
    parser.add_argument(
        "--scope",
        choices=["failed", "all"],
        default=DEFAULT_SCOPE,
        help="When a migration report is supplied, synthesize only failed-panel demand or all panel demand",
    )
    parser.add_argument(
        "--metrics-stream",
        default=DEFAULT_METRICS_STREAM,
        help="Synthetic metrics data stream name",
    )
    parser.add_argument(
        "--logs-stream",
        default=DEFAULT_LOGS_STREAM,
        help="Synthetic logs data stream name",
    )
    parser.add_argument(
        "--points",
        type=int,
        default=DEFAULT_POINTS,
        help="Points per synthetic series",
    )
    parser.add_argument(
        "--step-seconds",
        type=int,
        default=DEFAULT_STEP_SECONDS,
        help="Spacing between generated timestamps",
    )
    parser.add_argument(
        "--series-cap",
        type=int,
        default=DEFAULT_SERIES_CAP,
        help="Maximum synthetic series per metric",
    )
    parser.add_argument(
        "--es-url",
        default="http://localhost:9200",
        help="Elasticsearch URL for optional apply step",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create synthetic data streams/templates and bulk-index the generated corpus",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete and recreate the synthetic data streams/templates before applying",
    )
    args = parser.parse_args()

    if not args.migration_report and not args.input_dir:
        parser.error("Provide at least one --migration-report or --input-dir")

    output_dir = ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    profile = load_profile(args.profile)
    rule_pack = migrate.load_rule_pack_files(args.rules_file)
    resolver = migrate.SchemaResolver(rule_pack)

    manifest = CorpusManifest()

    for raw_path in args.migration_report:
        collect_demand_from_report(Path(raw_path), manifest, resolver, rule_pack, profile, args.scope)
    for raw_dir in args.input_dir:
        collect_demand_from_dashboards(Path(raw_dir), manifest, resolver, rule_pack, profile)

    metrics_docs, metric_label_fields = generate_metric_documents(
        manifest,
        profile,
        points=max(args.points, 2),
        step_seconds=max(args.step_seconds, 1),
        cap=max(args.series_cap, 1),
    )
    log_docs, log_label_fields = generate_log_documents(
        manifest,
        profile,
        points=max(min(args.points, 50), 2),
        step_seconds=max(args.step_seconds, 1),
    )

    metrics_bulk = output_dir / "metrics.bulk.ndjson"
    logs_bulk = output_dir / "logs.bulk.ndjson"
    _write_bulk_file(metrics_bulk, args.metrics_stream, metrics_docs)
    _write_bulk_file(logs_bulk, args.logs_stream, log_docs)

    metrics_template = _build_metrics_template(
        args.metrics_stream,
        manifest,
        set(manifest.metrics.keys()),
        metric_label_fields,
    )
    logs_template = _build_logs_template(args.logs_stream, log_label_fields)

    (output_dir / "metrics.template.json").write_text(json.dumps(metrics_template, indent=2))
    (output_dir / "logs.template.json").write_text(json.dumps(logs_template, indent=2))
    (output_dir / "corpus_manifest.json").write_text(
        json.dumps(_manifest_json(manifest, metrics_docs, log_docs, args.metrics_stream, args.logs_stream), indent=2)
    )

    if args.apply:
        apply_to_elasticsearch(
            args.es_url,
            args.metrics_stream,
            args.logs_stream,
            metrics_template,
            logs_template,
            metrics_bulk,
            logs_bulk if log_docs else None,
            args.reset,
        )

    print(f"Generated synthetic corpus in {output_dir}")
    print(f"  Metrics demanded: {len(manifest.metrics)}")
    print(f"  Metric docs:      {len(metrics_docs)}")
    print(f"  Log docs:         {len(log_docs)}")
    print(f"  Metrics stream:   {args.metrics_stream}")
    print(f"  Logs stream:      {args.logs_stream}")
    if args.apply:
        print(f"Applied synthetic corpus to {args.es_url}")


if __name__ == "__main__":
    main()
