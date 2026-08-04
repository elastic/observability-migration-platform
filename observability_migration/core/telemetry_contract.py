# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Generate telemetry producer contracts from migrated assets."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from itertools import product
from pathlib import Path
from typing import Any

from .assets.dashboard import DashboardIR

LOG = logging.getLogger(__name__)

# The migration's semantic IR export, written per dashboard next to the
# native Dashboards API payload (``targets/kibana/native_artifacts.py``).
# This is the contract's dashboard input: it carries every panel query,
# control and dashboard filter, and unlike the YAML export it is not
# scheduled for removal.
_IR_ARTIFACT_DIRNAME = "ir"
_IR_ARTIFACT_GLOB = "*.ir.json"
# Artifact subdirectories a caller may hand us instead of the dashboards
# artifact root; ``ir/`` is then a sibling rather than a child.
_ARTIFACT_SUBDIRS = ("ir", "native", "yaml")

_IDENT_RE = r"(?:`[^`]+`|[A-Za-z_][\w.-]*)"

# Scalar casts the translator wraps around filter fields (e.g. Datadog log
# attributes compared as strings). The underlying column is still required in
# the target index / seed contract — strip these wrappers before extraction so
# ``TO_STRING(http.status_code) == "404"`` yields dimension ``http.status_code``.
_SCALAR_CAST_RE = re.compile(
    rf"\bTO_(?:STRING|LONG|INTEGER|INT|DOUBLE|BOOLEAN|DATETIME|VERSION)\(\s*({_IDENT_RE})\s*\)",
    re.IGNORECASE,
)

# Tokens that are not real telemetry fields. They appear in extracted
# query text either as ES|QL command keywords, translator scaffolding
# aliases, or aggregation column names that the schema-report should
# never surface to a producer.
_ESQL_COMMAND_KEYWORDS = {
    "EVAL",
    "KEEP",
    "STATS",
    "WHERE",
    "SORT",
    "LIMIT",
    "FROM",
    "TS",
    "BY",
    "ASC",
    "DESC",
    "DROP",
    "RENAME",
    "DISSECT",
    "GROK",
    "ENRICH",
    "ROW",
    "MV_EXPAND",
    "LOOKUP",
    "JOIN",
    "META",
    "LIKE",
    "RLIKE",
    "NOT",
    "AND",
    "OR",
    "NULL",
    "TRUE",
    "FALSE",
    "IS",
    "CASE",
    "MATCH",
    "IN",
    "WHEN",
    "THEN",
    "ELSE",
    "END",
}

# Aliases the translator emits as pipeline scaffolding. These are never
# produced by user data, so dropping them removes noise without losing
# real schema fields.
_TRANSLATOR_INTERNAL_FIELDS = {
    "step",
    "label",
    "unknown",
    "bucket_value",
    "_bucket_value",
    "_per_series_value",
    "_timeseries",
    "_raw_value",
    "_gauge_min",
    "_gauge_max",
    "_gauge_goal",
    "_ts",
}

# Prefix-based scaffolding aliases (e.g. `_raw_user`, `_gauge_value`,
# `_per_series_count`). The translator generates one alias per series so
# enumerating them is impractical; a prefix denylist is the right shape.
_TRANSLATOR_INTERNAL_PREFIX_RE = re.compile(
    r"^_(?:raw|per_series|timeseries|gauge|bucket|stats|ts)(?:_|\d|$)"
)

# Dimension *values* harvested from queries are sometimes not real label values
# but unsubstituted Grafana template variables (``$instance``, ``${job}``) or the
# migrator's own placeholder tokens (``__obs_migration_param_node``). Seeding
# documents with these literal strings produces series no migrated panel query
# can match — the query filters on a sampled real value, never on ``$instance``.
# Such tokens must be dropped from required value/pattern sets so the seeder
# falls back to realistic, coherent defaults instead.
# Anchored at the start (not the end) so a value that *begins* with a template
# variable is rejected even when the rest is literal — e.g. a composite
# ``$host:$port`` instance value. A fully literal value such as ``label_value``
# or ``production`` is kept; ``label_*`` is a relabeled metric *name* handled by
# the field skip-list, not a dimension value, so it must not be dropped here.
_NON_LITERAL_VALUE_RE = re.compile(
    r"""
    ^\s*(?:
        \$\{?[\w.]+\}?            # Grafana template var: $instance / ${job}
      | \[\[[\w.]+\]\]            # legacy Grafana template var: [[instance]]
      | __obs_migration_param_\w* # migrator placeholder token
    )
    """,
    re.VERBOSE,
)


def _is_literal_dimension_value(value: str) -> bool:
    """True when *value* is a concrete label value worth seeding.

    Filters unsubstituted template variables and migrator placeholder tokens so
    they never reach the synthetic seeder as dimension values.
    """
    if not value:
        return False
    if value in {".*", ".+"}:
        return False
    return _NON_LITERAL_VALUE_RE.match(value) is None

_SKIP_FIELDS = {
    "",
    "*",
    "@timestamp",
    "time_bucket",
    "BUCKET",
    "value",
    "current_value",
    "previous_value",
    "computed_value",
    "constant_value",
    "inner_val",
    "COUNT_OVER_TIME",
    "SUM_OVER_TIME",
    "AVG_OVER_TIME",
    "MIN_OVER_TIME",
    "MAX_OVER_TIME",
    "LAST_OVER_TIME",
    "PRESENT_OVER_TIME",
} | _ESQL_COMMAND_KEYWORDS | _TRANSLATOR_INTERNAL_FIELDS
_COUNTER_HINTS = (
    "_total",
    "_count",
    "_sum",
    "_bucket",
    "bytes_sent",
    "bytes_rcvd",
    # "requests" removed: too broad — matches kube_pod_container_resource_requests
    # (a gauge for K8s CPU/memory allocation) and breaks SUM(CASE(...)) in ES|QL.
    # Metrics like nginx_http_requests are caught by the rate() context in PromQL.
    "errors",
    "dropped",
    "accepted",
    "refused",
    "connections",
    "restarts",
    "failed",
    "rejected",
    "timeout",
)
# Field names that match a counter hint (e.g. ``_total``) but are really gauges
# reporting a current level — most notably node-exporter memory-pool ``*_Total``
# series, which sit alongside ``*_Free``/``*_Rsvd`` gauges of the same base.
# Misclassifying these as counters causes counter/gauge mapping ambiguities at
# index time (``Cannot use field [...] mapped as [2] incompatible types``).
_GAUGE_OVERRIDE_RE = re.compile(
    r"(?:"
    r"node_memory_hugepages_(?:total|free|rsvd|surp)"
    r"|hugepages_total"
    r"|node_memory_[a-z_]*_total"   # node_memory_*_Total pool sizes are gauges
    r")$"
)
_LOOKBACK_SECONDS = {
    "minute": 60,
    "minutes": 60,
    "hour": 3600,
    "hours": 3600,
    "day": 86400,
    "days": 86400,
}


def build_telemetry_contract(
    artifact_dir: str | Path,
    *,
    metric_kind_overrides: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build a producer-facing schema contract from dashboard artifacts.

    ``metric_kind_overrides`` maps a metric field name to an authoritative
    ``"counter"``/``"gauge"`` classification. Overrides win over every inferred
    signal (query context and name heuristics) because they come from ground
    truth — a rule-pack declaration, live source metadata, or ES field caps.
    """
    artifact_path = Path(artifact_dir)
    streams: dict[str, dict[str, Any]] = {}
    for query, source in _iter_artifact_queries(artifact_path):
        if query.startswith("RANGE "):
            index = _query_index(query)
            if not index:
                continue
            stream = streams.setdefault(
                index,
                {
                    "fields": {},
                    "control_fields": [],
                    "group_fields": [],
                    "required_values": {},
                    "required_patterns": {},
                    "requires_native_promql": False,
                    "minimum_lookback": "",
                    "query_sources": [],
                    "requirements": [],
                },
            )
            _append_unique(stream["query_sources"], source)
            field_match = re.search(r"\bfield=([^\s]+)", query)
            min_match = re.search(r"\bmin=([^\s]+)", query)
            max_match = re.search(r"\bmax=([^\s]+)", query)
            if not (field_match and min_match and max_match):
                continue
            field_name = _normalize_field(field_match.group(1))
            try:
                low = float(min_match.group(1))
                high = float(max_match.group(1))
            except ValueError:
                continue
            if low > high:
                low, high = high, low
            _merge_field(
                stream["fields"],
                field_name,
                role="metric",
                type_family="numeric",
                metric_kind="gauge",
                source=source,
            )
            info = stream["fields"][field_name]
            info["seed_range"] = [low, high]
            continue
        index = _query_index(query)
        if not index:
            continue
        stream = streams.setdefault(
            index,
            {
                "fields": {},
                "control_fields": [],
                "group_fields": [],
                "required_values": {},
                "required_patterns": {},
                "requires_native_promql": False,
                "minimum_lookback": "",
                "query_sources": [],
                "requirements": [],
            },
        )
        if query.startswith("PROMQL "):
            stream["requires_native_promql"] = True
        _append_unique(stream["query_sources"], source)
        stream["_lookback_seconds"] = max(
            int(stream.get("_lookback_seconds", 0)),
            _max_lookback_seconds(query),
        )
        metrics = _extract_metrics(query)
        dimensions = _extract_dimensions(query)
        control_fields = _extract_control_fields(query)
        group_fields = _extract_group_fields(query)
        keyword_multifields = _extract_keyword_multifields(query)
        required_values, required_patterns = _extract_required_filters(query)
        for metric_name, metric_kind in metrics.items():
            _merge_field(
                stream["fields"],
                metric_name,
                role="metric",
                type_family="numeric",
                metric_kind=metric_kind,
                source=source,
                requires_native_promql=query.startswith("PROMQL "),
            )
        for dimension in dimensions | set(control_fields) | set(group_fields) | set(required_values) | set(required_patterns):
            _merge_field(
                stream["fields"],
                dimension,
                role="dimension",
                type_family="keyword",
                metric_kind="",
                source=source,
                keyword_multifield=dimension in keyword_multifields,
            )
        for field_name, relations in _extract_field_relationships(query, set(metrics)).items():
            info = stream["fields"].get(field_name)
            if info and info.get("role") == "metric":
                bucket = info.setdefault("relationships", [])
                for relation in relations:
                    if relation not in bucket:
                        bucket.append(relation)
        for control_field in control_fields:
            _append_unique(stream["control_fields"], control_field)
        for group_field in group_fields:
            _append_unique(stream["group_fields"], group_field)
        _merge_required_map(stream["required_values"], required_values)
        _merge_required_map(stream["required_patterns"], required_patterns)
        stream["requirements"].append(
            {
                "source": source,
                "index": index,
                "metrics": sorted(metrics),
                "dimensions": sorted(dimensions),
                "control_fields": control_fields,
                "group_fields": group_fields,
                "required_values": required_values,
                "required_patterns": required_patterns,
                "minimum_lookback": _format_lookback(_max_lookback_seconds(query)),
            }
        )

    _propagate_control_fields(streams)
    _apply_dimension_evidence(streams)
    _apply_seed_range_metric_precedence(streams)
    _apply_metric_kind_overrides(streams, metric_kind_overrides)
    _drop_metric_object_prefix_conflicts(streams)

    for stream in streams.values():
        seconds = int(stream.pop("_lookback_seconds", 0))
        stream["minimum_lookback"] = _format_lookback(seconds)
        for field_info in stream["fields"].values():
            field_info.pop("_counter_locked", None)
        stream["fields"] = dict(sorted(stream["fields"].items()))

    metric_fields = {
        field_name
        for stream in streams.values()
        for field_name, field_info in stream["fields"].items()
        if field_info["role"] == "metric"
    }
    dimension_fields = {
        field_name
        for stream in streams.values()
        for field_name, field_info in stream["fields"].items()
        if field_info["role"] == "dimension"
    }
    return {
        "version": 1,
        "artifact_dir": str(artifact_path),
        "streams": dict(sorted(streams.items())),
        "summary": {
            "streams": len(streams),
            "metric_fields": len(metric_fields),
            "dimension_fields": len(dimension_fields),
        },
    }


def _drop_metric_object_prefix_conflicts(streams: dict[str, dict[str, Any]]) -> None:
    """Drop impossible flat metrics that collide with dotted metric objects.

    Elasticsearch cannot store both a scalar metric ``metrics`` and dotted
    metrics such as ``metrics.redis_up`` in the same stream: the dotted fields
    require ``metrics`` to be an object. Some query-shape extraction paths can
    still surface the object prefix as a bogus metric field even though no panel
    actually reads it. Remove those impossible flat metrics from both the stream
    field map and the per-requirement metric lists before the contract drives
    seeding or template creation.
    """
    for stream in streams.values():
        fields = stream.get("fields")
        if not isinstance(fields, dict):
            continue
        dotted_prefixes: set[str] = set()
        for field_name, info in fields.items():
            if not isinstance(info, dict) or info.get("role") != "metric" or "." not in field_name:
                continue
            parts = field_name.split(".")
            for index in range(1, len(parts)):
                dotted_prefixes.add(".".join(parts[:index]))
        dropped = {
            field_name
            for field_name in dotted_prefixes
            if isinstance(fields.get(field_name), dict)
            and fields[field_name].get("role") == "metric"
        }
        if not dropped:
            continue
        for field_name in dropped:
            fields.pop(field_name, None)
        for requirement in stream.get("requirements") or []:
            metrics = requirement.get("metrics")
            if isinstance(metrics, list):
                requirement["metrics"] = [name for name in metrics if name not in dropped]


def merge_metric_kind_overrides(*sources: Mapping[str, str] | None) -> dict[str, str]:
    """Compose metric-kind override maps in descending order of authority.

    Earlier sources win over later ones, so callers pass the most authoritative
    source first (e.g. rule-pack, then live Prometheus metadata, then ES field
    caps). Empty/None sources are ignored.
    """
    merged: dict[str, str] = {}
    for source in reversed(sources):
        if source:
            merged.update(source)
    return merged


def metric_kinds_from_prometheus_metadata(metadata: Mapping[str, Any]) -> dict[str, str]:
    """Derive authoritative counter/gauge classifications from Prometheus metadata.

    Accepts either a full ``/api/v1/metadata`` response (``{"data": {...}}``) or
    the bare ``{metric: [{"type": ...}]}`` mapping. Only unambiguous ``counter``
    and ``gauge`` types are returned; histogram/summary/untyped and conflicting
    declarations are skipped so they fall back to inference.
    """
    data = metadata.get("data", metadata) if isinstance(metadata, Mapping) else {}
    result: dict[str, str] = {}
    for name, entries in (data or {}).items():
        if not isinstance(entries, list):
            continue
        types = {
            str(entry.get("type", "")).strip().lower()
            for entry in entries
            if isinstance(entry, Mapping)
        }
        if types == {"counter"}:
            result[name] = "counter"
        elif types == {"gauge"}:
            result[name] = "gauge"
    return result


def metric_kinds_from_field_caps(field_caps: Mapping[str, Any]) -> dict[str, str]:
    """Derive counter/gauge classifications from an ES ``_field_caps`` response.

    Accepts either the full response (``{"fields": {...}}``) or the bare
    ``fields`` mapping. A field is ``counter`` when ES marks it as a counter
    metric, ``gauge`` when its time-series metric kind is gauge; plain numerics
    without TSDB metadata are skipped.
    """
    from observability_migration.core.verification.field_capabilities import (
        field_capability_from_es_field_caps,
        is_counter_metric_field,
    )

    fields = field_caps.get("fields", field_caps) if isinstance(field_caps, Mapping) else {}
    result: dict[str, str] = {}
    for name, entry in (fields or {}).items():
        if not isinstance(entry, Mapping):
            continue
        capability = field_capability_from_es_field_caps(name, dict(entry))
        if is_counter_metric_field(capability):
            result[name] = "counter"
        elif capability.time_series_metric_kind == "gauge":
            result[name] = "gauge"
    return result


def _apply_metric_kind_overrides(
    streams: dict[str, dict[str, Any]],
    overrides: Mapping[str, str] | None,
) -> None:
    """Force authoritative counter/gauge classification on named metric fields.

    Only fields already classified as metrics are affected — naming a dimension
    must not promote it to a metric. The provenance is recorded as ``override``
    so downstream consumers can distinguish ground truth from inference.
    """
    if not overrides:
        return
    normalized = {name: str(kind).strip().lower() for name, kind in overrides.items()}
    for stream in streams.values():
        for field_name, info in (stream.get("fields") or {}).items():
            if info.get("role") != "metric":
                continue
            kind = normalized.get(field_name)
            if kind in ("counter", "gauge"):
                info["metric_kind"] = kind
                info["kind_source"] = "override"


def build_combined_telemetry_contract(
    artifact_dirs: Sequence[str | Path],
    *,
    metric_kind_overrides: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build one telemetry contract from multiple migrated artifact directories."""
    contracts = [build_telemetry_contract(path) for path in artifact_dirs]
    combined = {
        "version": 1,
        "artifact_dirs": [str(Path(path)) for path in artifact_dirs],
        "streams": {},
        "summary": {"streams": 0, "metric_fields": 0, "dimension_fields": 0},
    }
    for contract in contracts:
        for stream_name, stream in (contract.get("streams") or {}).items():
            target = combined["streams"].setdefault(
                stream_name,
                {
                    "fields": {},
                    "control_fields": [],
                    "group_fields": [],
                    "required_values": {},
                    "required_patterns": {},
                    "requires_native_promql": False,
                    "minimum_lookback": "",
                    "query_sources": [],
                    "requirements": [],
                },
            )
            target["requires_native_promql"] = bool(
                target.get("requires_native_promql") or stream.get("requires_native_promql")
            )
            target["minimum_lookback"] = _format_lookback(
                max(
                    _lookback_to_seconds(target.get("minimum_lookback", "")),
                    _lookback_to_seconds(stream.get("minimum_lookback", "")),
                )
            )
            for field_name, field_info in (stream.get("fields") or {}).items():
                _merge_field(
                    target["fields"],
                    field_name,
                    role=field_info.get("role", "dimension"),
                    type_family=field_info.get("type_family", "keyword"),
                    metric_kind=field_info.get("metric_kind", ""),
                    source=", ".join(field_info.get("sources") or []),
                    requires_native_promql=bool(field_info.get("requires_native_promql")),
                    keyword_multifield=bool(field_info.get("keyword_multifield")),
                )
                relations = field_info.get("relationships")
                if relations and target["fields"][field_name].get("role") == "metric":
                    bucket = target["fields"][field_name].setdefault("relationships", [])
                    for relation in relations:
                        if relation not in bucket:
                            bucket.append(relation)
            for key in ("control_fields", "group_fields", "query_sources"):
                for value in stream.get(key) or []:
                    _append_unique(target[key], value)
            _merge_required_map(target["required_values"], stream.get("required_values") or {})
            _merge_required_map(target["required_patterns"], stream.get("required_patterns") or {})
            target["requirements"].extend(stream.get("requirements") or [])

    _propagate_control_fields(combined["streams"])
    _apply_dimension_evidence(combined["streams"])
    _apply_seed_range_metric_precedence(combined["streams"])
    _apply_metric_kind_overrides(combined["streams"], metric_kind_overrides)
    for stream in combined["streams"].values():
        stream["fields"] = dict(sorted(stream["fields"].items()))
    combined["streams"] = dict(sorted(combined["streams"].items()))
    metric_fields = {
        field_name
        for stream in combined["streams"].values()
        for field_name, field_info in stream["fields"].items()
        if field_info["role"] == "metric"
    }
    dimension_fields = {
        field_name
        for stream in combined["streams"].values()
        for field_name, field_info in stream["fields"].items()
        if field_info["role"] == "dimension"
    }
    combined["summary"] = {
        "streams": len(combined["streams"]),
        "metric_fields": len(metric_fields),
        "dimension_fields": len(dimension_fields),
    }
    return combined


def build_schema_change_report(
    artifact_dir: str | Path | Sequence[str | Path],
) -> str:
    """Build a human-readable source-to-target schema report from migrated artifacts."""
    if isinstance(artifact_dir, (str, Path)):
        artifact_paths: list[Path] = [Path(artifact_dir)]
    else:
        artifact_paths = [Path(path) for path in artifact_dir]

    sections: list[tuple[Path, list[dict[str, Any]]]] = []
    target_streams: list[str] = []
    total_panels = 0
    for path in artifact_paths:
        rows = list(_iter_schema_change_rows(path))
        sections.append((path, rows))
        total_panels += len(rows)
        for row in rows:
            stream = row["target_stream"]
            if stream and stream not in target_streams:
                target_streams.append(stream)

    lines = [
        "# Telemetry Schema Change Report",
        "",
        "## Summary",
        "",
        f"- Artifact directories: {len(artifact_paths)}",
        f"- Total panels: {total_panels}",
        f"- Target streams: {', '.join(target_streams) if target_streams else 'n/a'}",
        "",
        "## Artifact directories",
        "",
    ]
    for index, path in enumerate(artifact_paths, start=1):
        lines.append(f"{index}. `{path}`")
    lines.append("")

    for path, rows in sections:
        lines.extend(
            [
                f"## {path.name or path}",
                "",
                f"Artifact directory: `{path}`",
                "",
                "| Dashboard | Panel | Source fields | Target stream | Target fields |",
                "|---|---|---|---|---|",
            ]
        )
        if not rows:
            lines.append("| n/a | n/a | No source query metadata found | n/a | n/a |")
        for row in rows:
            lines.append(
                "| {dashboard} | {panel} | {source_fields} | {target_stream} | {target_fields} |".format(
                    dashboard=_markdown_cell(row["dashboard"]),
                    panel=_markdown_cell(row["panel"]),
                    source_fields=_markdown_cell(", ".join(row["source_fields"]) or "n/a"),
                    target_stream=_markdown_cell(row["target_stream"] or "n/a"),
                    target_fields=_markdown_cell(", ".join(row["target_fields"]) or "n/a"),
                )
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_telemetry_contract(contract: dict[str, Any], output_path: str | Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_schema_report_artifacts(
    artifact_dir: str | Path,
    *,
    report_filename: str = "schema_change_report.md",
    contract_filename: str = "telemetry_contract.json",
) -> dict[str, Path]:
    """Write the default human and machine-readable schema reports.

    Source adapters call this after writing their YAML and verification packets
    so a normal migration run produces the same artifacts that the advanced
    ``obs-migrate schema-report`` command can regenerate later.
    """
    artifact_path = Path(artifact_dir)
    report_path = artifact_path / report_filename
    contract_path = artifact_path / contract_filename
    try:
        report_path.write_text(build_schema_change_report(artifact_path), encoding="utf-8")
        write_telemetry_contract(build_telemetry_contract(artifact_path), contract_path)
    except Exception:
        for path in (report_path, contract_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    return {
        "schema_report": report_path,
        "telemetry_contract": contract_path,
    }


def _propagate_control_fields(streams: dict[str, dict[str, Any]]) -> None:
    control_fields: list[str] = []
    for stream in streams.values():
        for field_name in stream.get("control_fields") or []:
            _append_unique(control_fields, field_name)
    for stream in streams.values():
        for field_name in control_fields:
            if _should_skip_field(field_name):
                continue
            _append_unique(stream["control_fields"], field_name)
            _merge_field(
                stream["fields"],
                field_name,
                role="dimension",
                type_family="keyword",
                metric_kind="",
                source="dashboard_control",
            )


def _apply_seed_range_metric_precedence(streams: dict[str, dict[str, Any]]) -> None:
    """Keep range-slider seed targets as numeric metrics even when also controls.

    Range handles bind to a numeric field that may also appear in dashboard
    ``control_fields``. Dimension evidence must not demote those fields to
    keyword columns when the contract carries an explicit ``seed_range``.
    """
    for stream in streams.values():
        for info in (stream.get("fields") or {}).values():
            if not info.get("seed_range"):
                continue
            info["role"] = "metric"
            info["type_family"] = "numeric"
            info.setdefault("metric_kind", "gauge")


def _apply_dimension_evidence(streams: dict[str, dict[str, Any]]) -> None:
    """Resolve metric/dimension collisions in favour of explicit label evidence.

    A TSDB field cannot be mapped as both ``time_series_metric`` (double) and
    ``time_series_dimension`` (keyword); one role must win. Metric extraction is
    the *noisy* signal: a CPU panel like ``node_cpu_seconds_total{mode="idle"}``
    leaks the label values (``idle``/``system``/...) and the label key (``mode``)
    as metric names. Explicit ``BY``/control/filter usage is the *authoritative*
    signal that the field is a label. So when a field is used as a
    group/control/filter dimension anywhere in the stream, the dimension role
    wins — otherwise every panel filtering or grouping on that label fails to
    match the synthetic data (the field would be a numeric column, not a
    keyword).
    """
    for stream in streams.values():
        evidence = set(stream.get("control_fields") or [])
        evidence.update(stream.get("group_fields") or [])
        evidence.update((stream.get("required_values") or {}).keys())
        evidence.update((stream.get("required_patterns") or {}).keys())
        for field_name in evidence:
            info = (stream.get("fields") or {}).get(field_name)
            if not info or info.get("role") != "metric":
                continue
            info["role"] = "dimension"
            info["type_family"] = "keyword"
            info["metric_kind"] = ""
            info.pop("relationships", None)


def count_declared_controls(artifact_dir: str | Path) -> int:
    """Count the dashboard controls the source declared, from the native payload.

    Reads ``mapping.controls`` from ``native/*.native.json``, which the
    migration writes per dashboard independently of the IR export. That
    makes it a usable cross-check on the contract: a source that
    declares controls must yield ``control_fields``, otherwise the
    seeder emits data that matches no control selection while the
    contract still looks healthy (``streams`` stays non-empty).

    Returns 0 when there are no native artifacts to read — absence of
    evidence must not be turned into an assertion.
    """
    artifact_path = Path(artifact_dir)
    candidates = [artifact_path / "native"]
    if artifact_path.name in _ARTIFACT_SUBDIRS:
        candidates.append(artifact_path.parent / "native")
    native_dir = next((path for path in candidates if path.is_dir()), None)
    if native_dir is None:
        return 0
    total = 0
    for native_file in sorted(native_dir.glob("*.native.json")):
        try:
            payload = json.loads(native_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        mapping = payload.get("mapping") if isinstance(payload, dict) else None
        declared = mapping.get("controls") if isinstance(mapping, dict) else None
        if isinstance(declared, bool) or not isinstance(declared, int):
            continue
        total += max(0, declared)
    return total


def _resolve_ir_dir(artifact_path: Path, *, caller: str) -> Path:
    """Locate the dashboard-IR directory for an artifact path.

    Callers pass either the dashboards artifact root (which holds an
    ``ir/`` subdirectory) or one of its artifact subdirectories -- most
    often ``ir/`` or ``native/``. The legacy ``yaml/``/``compiled/`` names
    are still resolved so an archived artifact tree from an older release
    keeps working.
    The fallback to ``artifact_path`` is legitimate for the ``ir/`` shape.
    It is *not* legitimate for anything else: an unconditional fallback
    root-globs ``*.ir.json``, matches nothing, and warns nothing — so a
    wrong path looks exactly like a dashboard with no queries. Warn
    loudly instead; the caller may still find queries in
    ``verification_packets.json``, so this is not fatal here.
    """
    ir_dir = artifact_path / _IR_ARTIFACT_DIRNAME
    if ir_dir.is_dir():
        return ir_dir
    if artifact_path.name in _ARTIFACT_SUBDIRS:
        sibling = artifact_path.parent / _IR_ARTIFACT_DIRNAME
        if sibling.is_dir():
            return sibling
    if artifact_path.is_dir() and any(artifact_path.glob(_IR_ARTIFACT_GLOB)):
        return artifact_path
    LOG.warning(
        "%s: no dashboard IR found for %s — neither %s (expected a "
        "migration's ir/ directory) nor %s/%s matched. Falling back to "
        "root-globbing %s, which will yield zero queries from the IR.",
        caller,
        artifact_path,
        ir_dir,
        artifact_path,
        _IR_ARTIFACT_GLOB,
        artifact_path,
    )
    return artifact_path


def _iter_artifact_dashboards(artifact_path: Path, *, caller: str):
    """Yield ``(source_label, dashboard_document)`` per IR artifact.

    ``dashboard_document`` is the kb-dashboard-core ``{"dashboards": [...]}``
    shape that :meth:`DashboardIR.to_yaml_dict` produces -- the same shape
    the dashboard YAML carried, rebuilt straight from ``ir/*.ir.json`` so the
    extractors below keep one input contract while the YAML export goes away.
    """
    ir_dir = _resolve_ir_dir(artifact_path, caller=caller)
    for ir_file in sorted(ir_dir.glob(_IR_ARTIFACT_GLOB)):
        try:
            artifact = json.loads(ir_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(artifact, dict):
            continue
        dashboard_ir = artifact.get("dashboard_ir")
        if not isinstance(dashboard_ir, dict):
            continue
        entry = DashboardIR.from_dict(dashboard_ir).to_yaml_dict()
        yield f"ir:{ir_file.name}", {"dashboards": [entry]}


def _iter_artifact_queries(artifact_path: Path):
    for source, document in _iter_artifact_dashboards(
        artifact_path, caller="_iter_artifact_queries"
    ):
        yield from _iter_dashboard_queries(document, source)

    packet_candidates = [artifact_path / "verification_packets.json"]
    if artifact_path.name in _ARTIFACT_SUBDIRS:
        packet_candidates.append(artifact_path.parent / "verification_packets.json")
    packets_path = next((path for path in packet_candidates if path.exists()), None)
    if packets_path is not None:
        try:
            packets = json.loads(packets_path.read_text(encoding="utf-8")).get("packets", [])
        except Exception:
            packets = []
        for packet in packets or []:
            if not isinstance(packet, dict):
                continue
            source = "verification_packet"
            if packet.get("dashboard") or packet.get("panel"):
                source = f"verification_packet:{packet.get('dashboard', '')}:{packet.get('panel', '')}"
            for query in (
                packet.get("translated_query", ""),
                (packet.get("target_execution") or {}).get("query", ""),
                (packet.get("query_ir") or {}).get("target_query", ""),
            ):
                if isinstance(query, str) and query.strip():
                    yield query, source
            yield from _iter_packet_source_promql_queries(packet, source)


def _iter_packet_source_promql_queries(packet: dict[str, Any], source: str):
    query_ir = packet.get("query_ir") or {}
    source_language = str(query_ir.get("source_language") or packet.get("source_language") or "").lower()
    if source_language and source_language != "promql":
        return
    target_index = query_ir.get("target_index") if isinstance(query_ir, dict) else ""
    if not target_index:
        target_query = (
            packet.get("translated_query")
            or (packet.get("target_execution") or {}).get("query")
            or (query_ir or {}).get("target_query")
            or ""
        )
        target_index = _query_index(target_query) if isinstance(target_query, str) else ""
    if not target_index:
        return

    source_expressions = list(packet.get("source_queries") or [])
    if isinstance(packet.get("source_query"), str):
        source_expressions.append(packet["source_query"])
    if isinstance(query_ir, dict) and isinstance(query_ir.get("clean_expression"), str):
        source_expressions.append(query_ir["clean_expression"])
    elif isinstance(query_ir, dict) and isinstance(query_ir.get("source_expression"), str):
        source_expressions.append(query_ir["source_expression"])

    seen: set[str] = set()
    for expression in source_expressions:
        if not isinstance(expression, str):
            continue
        cleaned = expression.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        yield f"PROMQL index={target_index} step=1m value=({cleaned})", source


_ESQL_IDENTIFIER_PARAM_RE = re.compile(r"\?\?([A-Za-z_][A-Za-z0-9_]*)")
_CLASSIC_OPTIONS_CONTROL_TYPES = frozenset(
    {"options", "option", "options_list", "options_list_control"}
)
_CLASSIC_RANGE_CONTROL_TYPES = frozenset({"range", "range_slider", "range_slider_control"})
_ESQL_NON_FIELD_VARIABLE_TYPES = frozenset({"functions", "time_literal"})
_ESQL_CONTROL_BY_FIELD_RE = re.compile(rf"BY\s+({_IDENT_RE})", re.IGNORECASE)


def _extract_esql_values_bound_field(query: str) -> str:
    if not query:
        return ""
    match = _ESQL_CONTROL_BY_FIELD_RE.search(query)
    if not match:
        return ""
    field_name = _normalize_field(match.group(1))
    if not field_name or _should_skip_field(field_name):
        return ""
    return field_name


def _control_bound_field(control: Mapping[str, Any]) -> str:
    metadata = control.get("metadata")
    if isinstance(metadata, Mapping):
        bound = str(metadata.get("bound_field") or "").strip()
        if bound:
            return _normalize_field(bound)
    resolved = str(control.get("_resolved_field_name") or "").strip()
    if resolved:
        return _normalize_field(resolved)
    direct = _normalize_field(str(control.get("field_name") or control.get("field") or ""))
    if direct:
        return direct
    return _extract_esql_values_bound_field(str(control.get("query") or ""))


def _control_choice_values(control: Mapping[str, Any]) -> list[str]:
    collected: list[str] = []
    for key in (
        "defaults",
        "default",
        "selected_options",
        "available_options",
        "choices",
        "options",
    ):
        raw = control.get(key)
        if raw is None:
            continue
        if isinstance(raw, list):
            for item in raw:
                text = str(item or "").strip()
                if text and _is_literal_dimension_value(text):
                    _append_unique(collected, text)
        else:
            text = str(raw or "").strip()
            if text and _is_literal_dimension_value(text):
                _append_unique(collected, text)
    return collected


def _collect_non_field_param_defaults(controls: Any) -> dict[str, str]:
    defaults: dict[str, str] = {}
    if not isinstance(controls, list):
        return defaults
    for control in controls:
        if not isinstance(control, Mapping):
            continue
        variable_type = str(control.get("variable_type") or "").strip()
        if variable_type not in _ESQL_NON_FIELD_VARIABLE_TYPES:
            continue
        variable_name = str(control.get("variable_name") or "").strip()
        if not variable_name:
            continue
        choices = _control_choice_values(control)
        if choices:
            defaults[variable_name] = choices[0]
    return defaults


def _collect_non_field_param_names(controls: Any) -> set[str]:
    names: set[str] = set()
    if not isinstance(controls, list):
        return names
    for control in controls:
        if not isinstance(control, Mapping):
            continue
        variable_type = str(control.get("variable_type") or "").strip()
        if variable_type in _ESQL_NON_FIELD_VARIABLE_TYPES:
            variable_name = str(control.get("variable_name") or "").strip()
            if variable_name:
                names.add(variable_name)
    return names


def _collect_control_value_requirements(controls: Any) -> dict[str, list[str]]:
    requirements: dict[str, list[str]] = {}
    if not isinstance(controls, list):
        return requirements
    for control in controls:
        if not isinstance(control, Mapping):
            continue
        control_type = str(control.get("type") or "").lower()
        variable_type = str(control.get("variable_type") or "").strip()
        if variable_type in {"fields", *_ESQL_NON_FIELD_VARIABLE_TYPES}:
            continue
        if control_type in _CLASSIC_OPTIONS_CONTROL_TYPES | _CLASSIC_RANGE_CONTROL_TYPES:
            bound = _control_bound_field(control)
            if not bound or control_type in _CLASSIC_RANGE_CONTROL_TYPES:
                continue
            for value in _control_choice_values(control):
                _append_required(requirements, bound, value)
            continue
        if control.get("query") or variable_type in {"values", "multi_values"}:
            bound = _control_bound_field(control)
            if not bound:
                continue
            for value in _control_choice_values(control):
                _append_required(requirements, bound, value)
    return requirements


def _field_control_choices(controls: Any) -> dict[str, list[str]]:
    choices_by_name: dict[str, list[str]] = {}
    if not isinstance(controls, list):
        return choices_by_name
    for control in controls:
        if (
            not isinstance(control, dict)
            or control.get("type") != "esql"
            or control.get("variable_type") != "fields"
        ):
            continue
        name = str(control.get("variable_name") or "").strip()
        raw_choices = (
            control.get("choices")
            or control.get("available_options")
            or control.get("options")
            or []
        )
        choices = [
            str(choice).strip()
            for choice in raw_choices
            if str(choice or "").strip()
        ]
        if name and choices:
            choices_by_name[name] = choices
    return choices_by_name


def _expand_identifier_control_queries(
    query: str,
    choices_by_name: Mapping[str, Sequence[str]],
    *,
    skip_params: set[str] | None = None,
):
    names = [
        name
        for name in dict.fromkeys(_ESQL_IDENTIFIER_PARAM_RE.findall(query or ""))
        if choices_by_name.get(name)
    ]
    if not names:
        yield query
        return
    for selected in product(*(choices_by_name[name] for name in names)):
        rendered = query
        for name, field_name in zip(names, selected):
            rendered = re.sub(
                rf"\?\?{re.escape(name)}\b",
                lambda _match, value=field_name: value,
                rendered,
            )
        yield rendered


def _iter_dashboard_queries(
    node: Any,
    source: str,
    identifier_choices: Mapping[str, Sequence[str]] | None = None,
    skip_params: set[str] | None = None,
    param_defaults: Mapping[str, str] | None = None,
):
    """Walk a kb-dashboard-core dashboard document and yield ``(query, source)``.

    The document comes from :func:`_iter_artifact_dashboards`, i.e. from
    ``ir/*.ir.json`` via :meth:`DashboardIR.to_yaml_dict`. The walk is
    shape-driven rather than schema-driven so ``esql``/``lens`` presentation
    blocks are found at any nesting depth (dashboard, section, panel).
    """
    if isinstance(node, dict):
        controls = node.get("controls")
        scoped_choices = dict(identifier_choices or {})
        scoped_choices.update(_field_control_choices(controls))
        scoped_skip = set(skip_params or ())
        scoped_skip.update(_collect_non_field_param_names(controls))
        scoped_defaults = dict(param_defaults or {})
        scoped_defaults.update(_collect_non_field_param_defaults(controls))
        yield from _iter_dashboard_filter_queries(node, source)
        esql = node.get("esql")
        if isinstance(esql, dict) and isinstance(esql.get("query"), str):
            for query in _expand_identifier_control_queries(
                esql["query"],
                scoped_choices,
                skip_params=scoped_skip,
            ):
                yield _substitute_non_field_esql_params(query, scoped_defaults), source
        elif isinstance(esql, str):
            for query in _expand_identifier_control_queries(
                esql,
                scoped_choices,
                skip_params=scoped_skip,
            ):
                yield _substitute_non_field_esql_params(query, scoped_defaults), source
        lens_query = _lens_to_contract_query(node.get("lens"))
        if lens_query:
            yield _substitute_non_field_esql_params(lens_query, scoped_defaults), source
        for value in node.values():
            yield from _iter_dashboard_queries(
                value,
                source,
                scoped_choices,
                scoped_skip,
                scoped_defaults,
            )
    elif isinstance(node, list):
        for item in node:
            yield from _iter_dashboard_queries(
                item,
                source,
                identifier_choices,
                skip_params,
                param_defaults,
            )


def _query_index(query: str) -> str:
    first_line = next((line.strip() for line in query.splitlines() if line.strip()), "")
    if first_line.startswith("CONTROL ") or first_line.startswith("FILTER "):
        match = re.search(r"\bindex=(\S+)", first_line)
        return match.group(1) if match else "metrics-*"
    if first_line.startswith("RANGE "):
        match = re.search(r"\bindex=(\S+)", first_line)
        return match.group(1) if match else "metrics-*"
    if first_line.startswith("LENS "):
        match = re.search(r"\bindex=(\S+)", first_line)
        return match.group(1) if match else "metrics-*"
    match = re.match(r"^(?:FROM|TS)\s+(\S+)", first_line)
    if match:
        return match.group(1)
    match = re.match(r"^PROMQL\s+index=(\S+)", first_line)
    return match.group(1) if match else ""


def _extract_metrics(query: str) -> dict[str, str]:
    metrics: dict[str, str] = {}
    if query.startswith("LENS "):
        for field_name, aggregation in re.findall(r"\bmetric=([^\s]+)\s+agg=([^\s]+)", query):
            field_name = _normalize_field(field_name)
            if not _should_skip_field(field_name):
                metrics[field_name] = _classify_metric(field_name) if aggregation.lower() != "rate" else "counter"
        return metrics
    if query.startswith("PROMQL "):
        promql_line = query.split("\n", 1)[0]
        # Metrics wrapped in rate()/increase()/irate() are counters by Prometheus
        # convention. rate()/irate() are *counter-only* in PromQL (a gauge cannot be
        # rated), so they are an authoritative ("locked") counter signal that the
        # downstream ES|QL AVG_OVER_TIME/MAX_OVER_TIME gauge vote must not flip —
        # that gauge translation is itself a consequence of an earlier gauge guess.
        # increase() can be misused on a real gauge, so it stays a soft counter that
        # MAX_OVER_TIME may still downgrade.
        rate_locked = set()
        soft_counter = set()
        for m in re.finditer(r"\b(rate|increase|irate)\(([^)]+)\)", promql_line, re.IGNORECASE):
            func = m.group(1).lower()
            inner = m.group(2)
            for name_m in re.finditer(r"\b([A-Za-z_:][\w:.]+)(?=\s*(?:\{|\[|$))", inner):
                name = _normalize_field(name_m.group(1))
                if func in {"rate", "irate"}:
                    rate_locked.add(name)
                else:
                    soft_counter.add(name)
        for field_name in _extract_promql_metric_names(query):
            if field_name in rate_locked:
                metrics[field_name] = "counter_locked"
            elif field_name in soft_counter:
                metrics[field_name] = "counter"
            else:
                metrics[field_name] = _classify_metric(field_name)
        return metrics
    # FROM queries can only aggregate regular double or gauge_double fields — counter_double
    # is forbidden with AVG/SUM/etc. in FROM mode. Any field found in a FROM query must be
    # gauge (or plain double), never counter.
    is_from_query = query.startswith("FROM ")

    # Single-arg aggregations: SUM(field), AVG(field), ...
    pattern = re.compile(
        rf"\b(SUM|AVG|AVERAGE|MAX|MIN|MEDIAN|RATE|IRATE)\(\s*({_IDENT_RE})\s*\)"
        rf"|PERCENTILE\(\s*({_IDENT_RE})\s*,",
        re.IGNORECASE,
    )
    for match in pattern.finditer(query):
        function_name = (match.group(1) or "").upper()
        field_name = _normalize_field(match.group(2) or match.group(3) or "")
        if _should_skip_field(field_name):
            continue
        if is_from_query:
            # counter_double cannot be used with standard aggregations in FROM mode.
            metrics[field_name] = "gauge"
        else:
            metrics[field_name] = "counter" if function_name in {"RATE", "IRATE"} else _classify_metric(field_name)

    # Aggregations over expressions, e.g. MAX(node_boot_time_seconds * 1000) or
    # AVG(CASE((NOT (fstype RLIKE "tmpfs")), node_filesystem_device_error, 0)),
    # are common in generated ES|QL. Capture the real source fields inside the
    # *balanced* argument while ignoring function names and translator aliases.
    # A flat ``([^)]*)`` regex stops at the first ``)`` and so misses fields that
    # live after a nested ``CASE((...))`` (and harvests label names from the
    # predicate instead).
    for function_name, arg_text in _aggregation_arguments(query):
        if function_name not in {
            "SUM", "AVG", "AVERAGE", "MAX", "MIN", "MEDIAN",
            "RATE", "IRATE", "INCREASE", "MAX_OVER_TIME", "AVG_OVER_TIME", "PERCENTILE",
        }:
            continue
        for field_name in _extract_query_field_candidates(arg_text):
            if is_from_query:
                metrics[field_name] = "gauge"
            elif function_name in {"RATE", "IRATE", "INCREASE"}:
                metrics[field_name] = "counter"
            else:
                metrics[field_name] = _classify_metric(field_name)

    # Two-arg TSDB functions: IRATE(field, duration), RATE(field, dur), INCREASE(field, dur)
    # classify their first argument as counter.
    for m in re.finditer(
        rf"\b(?:IRATE|RATE|INCREASE)\(\s*({_IDENT_RE})\s*,",
        query,
        re.IGNORECASE,
    ):
        field_name = _normalize_field(m.group(1))
        if not _should_skip_field(field_name):
            metrics[field_name] = "counter"

    # MAX_OVER_TIME(field, dur) and AVG_OVER_TIME(field, dur) require gauge_double; mark as
    # gauge, overriding any counter classification from PROMQL verification packets that
    # misuse increase() on what are actually gauge metrics (e.g. node_netstat_*, node_vmstat_*).
    for m in re.finditer(
        rf"\b(?:MAX_OVER_TIME|AVG_OVER_TIME)\(\s*({_IDENT_RE})\s*,",
        query,
        re.IGNORECASE,
    ):
        field_name = _normalize_field(m.group(1))
        if not _should_skip_field(field_name):
            metrics[field_name] = "gauge"

    # Presence-only fields: a metric referenced solely via ``WHERE <field> IS
    # NOT NULL`` (common for "presence" panels) is never aggregated, so the
    # passes above miss it and the seeder omits the column — the live panel then
    # fails with ``Unknown column [<field>]``. Seed it as a gauge metric.
    # ``setdefault`` keeps any stronger classification an aggregation already gave
    # the field in this query; explicit label evidence later flips genuine
    # dimensions back via ``_apply_dimension_evidence``.
    derived_aliases = _eval_assigned_names(query) | _stats_derived_assigned_names(query)
    by_fields = set(_extract_group_fields(query))
    for field_name in _extract_is_not_null_fields(query):
        if field_name not in derived_aliases and field_name not in by_fields:
            metrics.setdefault(field_name, "gauge")

    # Drop derived ES|QL columns: anything assigned by ``EVAL <name> = ...`` is a
    # computed/legend alias (e.g. ``EVAL CPU = node_pressure_cpu_..._A``), not a
    # source index field, even when a later ``STATS CPU = MAX(CPU)`` re-aggregates
    # it. Seeding such an alias would invent a phantom metric the panel never reads.
    for alias in _eval_assigned_names(query):
        metrics.pop(alias, None)

    return metrics


_IS_NOT_NULL_RE = re.compile(rf"({_IDENT_RE})\s+IS\s+NOT\s+NULL\b", re.IGNORECASE)
_NEGATION_PAREN_TOKENS_RE = re.compile(r"\bNOT\b|\(|\)", re.IGNORECASE)


def _extract_is_not_null_fields(query: str) -> set[str]:
    """Fields referenced by a ``<field> IS NOT NULL`` presence predicate.

    These exist in the source index — the panel filters on their presence — so
    they must be seeded even when no aggregation reads them. Only ``IS NOT
    NULL`` (presence) is captured: a pure ``IS NULL`` (absence) filter wants the
    rows where the field is *missing*, and seeding it as an always-present gauge
    would make that panel match zero rows — a fresh false-negative. Quoted spans
    are blanked first so an ``IS NOT NULL`` substring inside a string literal
    does not seed a phantom field.
    """
    fields: set[str] = set()
    scan = re.sub(r'"[^"]*"', lambda m: " " * len(m.group(0)), query)
    for match in _IS_NOT_NULL_RE.finditer(scan):
        field_name = _normalize_field(match.group(1))
        if field_name and not _should_skip_field(field_name):
            fields.add(field_name)
    return fields


def _eval_assigned_names(query: str) -> set[str]:
    """Names introduced by ``EVAL`` assignments (left-hand sides) in an ES|QL query.

    Handles single and comma-separated assignments across an ``EVAL`` pipe stage:
    ``| EVAL a = x, b = y``. These are derived columns, never source fields.
    """
    names: set[str] = set()
    # Each EVAL stage runs until the next pipe or end of query.
    for stage in re.findall(r"(?:^|\|)\s*EVAL\s+(.+?)(?=\n\s*\||\|(?!\|)|$)", query, re.IGNORECASE | re.DOTALL):
        for assignment in _split_top_level(stage):
            lhs, sep, _rhs = assignment.partition("=")
            if not sep:
                continue
            name = _normalize_field(lhs.strip())
            if name:
                names.add(name)
    return names


def _stats_derived_assigned_names(query: str) -> set[str]:
    """Names introduced by ``STATS <alias> = ...`` assignments.

    A ``STATS`` left-hand side is often a source metric reused as the output
    column name (``m = AVG(m)``), so do not blanket-drop every alias. Only treat
    it as derived when the alias is absent from the right-hand side's source
    field candidates, which covers formula aliases such as ``q1 = AVG(redis...)``.
    """
    names: set[str] = set()
    for stage in re.findall(r"(?:^|\|)\s*STATS\s+(.+?)(?=\n\s*\||\|(?!\|)|$)", query, re.IGNORECASE | re.DOTALL):
        assignment_clause = _before_top_level_by(stage)
        for assignment in _split_top_level(assignment_clause):
            lhs, sep, rhs = assignment.partition("=")
            if not sep:
                continue
            name = _normalize_field(lhs.strip())
            if not name or _should_skip_field(name):
                continue
            rhs_fields = set(_extract_query_field_candidates(rhs))
            if name not in rhs_fields:
                names.add(name)
    return names


def _before_top_level_by(stage: str) -> str:
    """Return the part of a ``STATS`` stage before its top-level ``BY`` clause."""
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(stage):
        char = stage[i]
        if quote:
            if char == quote:
                quote = None
            i += 1
            continue
        if char in {'"', "'", "`"}:
            quote = char
            i += 1
            continue
        if char == "(":
            depth += 1
            i += 1
            continue
        if char == ")":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0 and stage[i : i + 2].upper() == "BY":
            before = stage[i - 1] if i > 0 else " "
            after = stage[i + 2] if i + 2 < len(stage) else " "
            if before.isspace() and after.isspace():
                return stage[:i].rstrip()
        i += 1
    return stage


_RATIO_RE = re.compile(
    rf"(?:[A-Za-z_]+\s*\()?\s*({_IDENT_RE})\s*\)?\s*/\s*(?:[A-Za-z_]+\s*\()?\s*({_IDENT_RE})\s*\)?"
)


def _extract_field_relationships(query: str, metric_names: set[str]) -> dict[str, list[dict[str, str]]]:
    """Find numeric relationships between metric fields in a query.

    Currently detects ratios (``A / B``, optionally aggregation-wrapped): the
    numerator ``A`` is recorded as bounded by denominator ``B`` so synthetic
    data keeps ``A <= B`` and the panel's percentage stays in range. Only pairs
    where both sides are known metric fields are recorded.
    """
    relationships: dict[str, list[dict[str, str]]] = {}
    for raw_num, raw_denom in _RATIO_RE.findall(query):
        numerator = _normalize_field(raw_num)
        denominator = _normalize_field(raw_denom)
        if numerator not in metric_names or denominator not in metric_names:
            continue
        if numerator == denominator:
            continue
        relation = {"type": "ratio_denominator", "field": denominator}
        bucket = relationships.setdefault(numerator, [])
        if relation not in bucket:
            bucket.append(relation)
    return relationships


def _extract_keyword_multifields(query: str) -> set[str]:
    """Base dimension names a query references via a ``.keyword`` sub-field.

    ``_normalize_field`` strips the ``.keyword`` suffix so dimensions collapse to
    their base name; this captures the suffixed form separately so the seeder can
    emit the aggregatable keyword multi-field that the query actually targets
    (e.g. ``deployment.environment.keyword``).
    """
    bases: set[str] = set()
    # _IDENT_RE allows dots, so anchor on a base that does NOT itself end in
    # ``.keyword`` and is followed by the literal suffix.
    for match in re.finditer(r"(`[^`]+`|[A-Za-z_][\w.-]*?)\.keyword\b", query):
        base = _normalize_field(match.group(1))
        if base and not _should_skip_field(base):
            bases.add(base)
    return bases


def _unwrap_scalar_casts(query: str) -> str:
    """Replace ``TO_STRING(field)`` / similar casts with the bare field name."""
    previous = None
    while previous != query:
        previous = query
        query = _SCALAR_CAST_RE.sub(r"\1", query)
    return query


def _extract_dimensions(query: str) -> set[str]:
    dimensions: set[str] = set()
    if query.startswith("CONTROL ") or query.startswith("FILTER "):
        match = re.search(r"\bfield=([^\s]+)", query)
        field_name = _normalize_field(match.group(1) if match else "")
        if not _should_skip_field(field_name):
            dimensions.add(field_name)
        return dimensions
    query = _unwrap_scalar_casts(query)
    metrics = set(_extract_metrics(query))
    derived_aliases = _eval_assigned_names(query) | _stats_derived_assigned_names(query)
    where_pattern = re.compile(
        rf"({_IDENT_RE})\s*(?:==|!=|>=|<=|>|<|NOT\s+LIKE|LIKE|NOT\s+RLIKE|RLIKE)\s*"
        rf"(?:\"|\(|-?\d|TRUE\b|FALSE\b|\?[A-Za-z_]\w*)",
        re.IGNORECASE | re.DOTALL,
    )

    for field_name in _extract_group_fields(query):
        _add_dimension(dimensions, field_name, metrics)

    for match in where_pattern.finditer(query):
        field_name = _normalize_field(match.group(1))
        if field_name not in derived_aliases:
            _add_dimension(dimensions, field_name, metrics)

    # COUNT_DISTINCT arguments are dimension fields being counted, not numeric metrics.
    for match in re.finditer(rf"\bCOUNT_DISTINCT\(\s*({_IDENT_RE})\s*\)", query, re.IGNORECASE):
        _add_dimension(dimensions, _normalize_field(match.group(1)), metrics)

    if query.startswith("PROMQL "):
        for field_name in _extract_promql_label_fields(query):
            _add_dimension(dimensions, field_name, metrics)
    values, patterns = _extract_required_filters(query)
    for field_name in set(values) | set(patterns):
        _add_dimension(dimensions, field_name, metrics)

    return dimensions


def _extract_group_fields(query: str) -> list[str]:
    fields: list[str] = []
    if query.startswith("LENS "):
        for field_name in re.findall(r"\bgroup=([^\s]+)", query):
            _append_unique(fields, _normalize_field(field_name))
        return [field for field in fields if not _should_skip_field(field)]
    if query.startswith("PROMQL "):
        promql_line = query.split("\n", 1)[0]
        for group_expr in re.findall(r"\bby\s*\(([^)]*)\)", promql_line, flags=re.IGNORECASE):
            for field_name in _split_top_level(group_expr):
                normalized = _normalize_field(field_name)
                if not _should_skip_field(normalized):
                    _append_unique(fields, normalized)
        # A panel whose series split comes from its ``legendFormat`` (e.g.
        # ``{{type}}``) carries no PromQL ``by (...)`` clause -- the translator
        # instead pulls each legend label out of the native ``_timeseries`` JSON
        # with ``| GROK _timeseries "...%{DATA:<label>}..."``. That GROK target is
        # the grouping dimension, so it must be a contract group field (otherwise
        # the seeder never seeds it and the migrated panel groups on an empty
        # field). Scope strictly to ``_timeseries`` extractions so label_replace
        # GROKs on other source columns are not mistaken for series dimensions.
        for label in _grok_timeseries_labels(query):
            if not _should_skip_field(label):
                _append_unique(fields, label)
        return fields
    by_pattern = re.compile(r"\bBY\b\s+(.+?)(?=\n\s*\||\s\|\s|$)", re.IGNORECASE | re.DOTALL)
    for match in by_pattern.finditer(query):
        for part in _split_top_level(match.group(1)):
            field_name = part.split("=", 1)[-1].strip() if "=" in part else part.strip()
            if field_name.startswith("??"):
                # Identifier controls are placeholders, not physical fields.
                # Their concrete field choices are collected from dashboard
                # controls by ``_iter_dashboard_filter_queries``.
                continue
            if "(" in field_name:
                continue
            normalized = _normalize_field(field_name)
            if not _should_skip_field(normalized):
                _append_unique(fields, normalized)
    return fields


def _grok_timeseries_labels(query: str) -> list[str]:
    """Return legend labels a passthrough query extracts from ``_timeseries``.

    Legend labels are pulled from the native ``_timeseries`` JSON by the
    translator with ``| GROK _timeseries "...%{DATA:<label>}..."`` (see
    ``panels._grok_label_extraction``). The ``%{...:<label>}`` capture name is
    the Grafana ``legendFormat`` label and thus the panel's grouping dimension.
    Matching per-line keeps each pipe's capture isolated.
    """
    labels: list[str] = []
    for line in (query or "").splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("| grok _timeseries"):
            continue
        match = re.search(r"%\{[A-Z]+:([A-Za-z_][\w.]*)\}", stripped)
        if match:
            _append_unique(labels, _normalize_field(match.group(1)))
    return labels


def _substitute_non_field_esql_params(
    query: str,
    param_defaults: Mapping[str, str],
) -> str:
    if not param_defaults:
        return query
    rendered = query
    for name, default in sorted(param_defaults.items()):
        replacement = str(default or "").strip()
        if not replacement:
            continue
        rendered = re.sub(
            rf"\?\?{re.escape(name)}\b",
            replacement,
            rendered,
        )
        rendered = re.sub(
            rf"(?<!\?)\?{re.escape(name)}\b",
            replacement,
            rendered,
        )
    return rendered


def _extract_control_fields(query: str) -> list[str]:
    if not query.startswith("CONTROL "):
        return []
    match = re.search(r"\bfield=([^\s]+)", query)
    field_name = _normalize_field(match.group(1) if match else "")
    if not field_name or _should_skip_field(field_name):
        return []
    return [field_name]


def _extract_required_filters(query: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    values: dict[str, list[str]] = {}
    patterns: dict[str, list[str]] = {}
    if query.startswith("FILTER "):
        field_match = re.search(r"\bfield=([^\s]+)", query)
        value_match = re.search(r"\bvalue=([^\s]+)", query)
        if field_match and value_match:
            _append_required(values, _normalize_field(field_match.group(1)), _decode_pseudo_value(value_match.group(1)))
        return values, patterns
    if query.startswith("PROMQL "):
        matcher_re = re.compile(r"([A-Za-z_][\w:.]*)\s*(=~|=|!=|!~)\s*\"([^\"]*)\"")
        for field_name, operator, value in matcher_re.findall(query):
            if operator in {"!=", "!~"}:
                continue
            target = patterns if "~" in operator else values
            _append_required(target, _normalize_field(field_name), value)
        return values, patterns
    query = _unwrap_scalar_casts(query)
    comparison_re = re.compile(
        rf"({_IDENT_RE})\s*(==|!=)\s*\"([^\"]*)\""
        rf"|({_IDENT_RE})\s*(NOT\s+RLIKE|RLIKE|NOT\s+LIKE|LIKE)\s*\"([^\"]*)\"",
        re.IGNORECASE,
    )
    for match in comparison_re.finditer(query):
        field_name = _normalize_field(match.group(1) or match.group(4) or "")
        operator = re.sub(r"\s+", " ", (match.group(2) or match.group(5) or "").upper())
        value = match.group(3) or match.group(6) or ""
        if not field_name or _should_skip_field(field_name):
            continue
        if operator in {"!=", "NOT LIKE", "NOT RLIKE"}:
            continue
        # A logically negated matcher (``NOT (field == "x")`` / ``NOT (field
        # RLIKE "y")``) excludes its value, so seeding it would synthesize only
        # rows the panel filters out. The Datadog ``LogNot`` path emits exactly
        # this shape for ``==`` too, so the guard must cover equality, not only
        # the regex/wildcard operators.
        if _is_negated_filter(query, match.start()):
            continue
        if operator == "RLIKE":
            # RLIKE values are regexes; keep them verbatim so the seeder's
            # regex instantiator (``_expand_single_pattern``) can synthesize a
            # fullmatching dimension value, exactly as it does for PromQL ``=~``.
            _append_required(patterns, field_name, value)
        elif operator == "LIKE":
            # LIKE uses ``*`` wildcards; strip them to the literal stem.
            _append_required(patterns, field_name, value.strip("*"))
        else:
            _append_required(values, field_name, value)
    for kql in re.findall(r'KQL\("([^"]*)"\)', query):
        for field_name, value in re.findall(r"([A-Za-z_@][\w.@-]*)\s*:\s*([A-Za-z0-9_./-]+)", kql):
            normalized = _normalize_field(field_name)
            if not normalized or _should_skip_field(normalized):
                continue
            _append_required(values, normalized, value)
    return values, patterns


def _is_negated_filter(query: str, field_start: int) -> bool:
    """Return whether the filter matcher at ``field_start`` is logically negated.

    Covers both the ``NOT (<field> ...)`` grouping that PromQL ``!~`` and Datadog
    ``NOT (...)`` emit, and a bare ``NOT <field> ...`` prefix. Negation must hold
    for *every* matcher inside a negated group, not only the first one
    (e.g. ``NOT (a RLIKE "x" OR a RLIKE "y")``).

    Quoted spans are blanked first (same convention as the metric-name scan
    elsewhere in this module) so a string literal containing ``NOT``/``(``/``)``
    can neither spoof a negation nor leave a frame open that suppresses a real,
    non-negated matcher later in the query.
    """
    scan = re.sub(r'"[^"]*"', lambda m: " " * len(m.group(0)), query[:field_start])
    stack: list[bool] = []
    pending_not_end = -1
    for match in _NEGATION_PAREN_TOKENS_RE.finditer(scan):
        token = match.group(0).upper()
        if token == "NOT":
            pending_not_end = match.end()
            continue
        if token == "(":
            is_negated = (
                pending_not_end != -1
                and not scan[pending_not_end:match.start()].strip()
            )
            stack.append(is_negated)
            pending_not_end = -1
            continue
        # Closing parenthesis.
        if stack:
            stack.pop()
        pending_not_end = -1
    if any(stack):
        return True
    # Bare ``NOT <field>``: a NOT with nothing but whitespace between it and the
    # matcher (no intervening paren or other predicate).
    return pending_not_end != -1 and not scan[pending_not_end:].strip()


def _add_dimension(dimensions: set[str], field_name: str, metrics: set[str]) -> None:
    normalized = _normalize_field(field_name)
    if not _should_skip_field(normalized) and normalized not in metrics:
        dimensions.add(normalized)


def _aggregation_arguments(query: str) -> list[tuple[str, str]]:
    """Yield ``(FUNCTION_NAME, balanced_argument_text)`` for every ``name(...)``.

    Unlike a flat ``name\\(([^)]*)\\)`` regex, this walks parentheses so the inner
    text of nested calls (e.g. ``CASE((...))`` inside ``AVG(...)``) is captured in
    full -- the source field after a nested predicate is no longer truncated.
    """
    out: list[tuple[str, str]] = []
    name_re = re.compile(r"([A-Za-z_][\w]*)\($")
    i = 0
    n = len(query)
    while i < n:
        if query[i] == "(":
            prefix = query[: i + 1]
            name_match = name_re.search(prefix)
            depth = 1
            j = i + 1
            while j < n and depth:
                if query[j] == "(":
                    depth += 1
                elif query[j] == ")":
                    depth -= 1
                j += 1
            if name_match:
                out.append((name_match.group(1).upper(), query[i + 1 : j - 1]))
            i += 1
        else:
            i += 1
    return out


def _split_top_level(value: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current))
    return parts


def _classify_metric(field_name: str) -> str:
    lowered = field_name.lower()
    if _GAUGE_OVERRIDE_RE.search(lowered):
        # Known gauges whose names contain a counter hint (``_total`` etc.) but
        # report a current level, not a monotonic count. Indexing these as
        # counters creates counter/gauge mapping ambiguities once a sibling field
        # of the same base is (correctly) a gauge.
        return "gauge"
    return "counter" if any(hint in lowered for hint in _COUNTER_HINTS) else "gauge"


def _extract_promql_metric_names(query: str) -> set[str]:
    # Only scan the PROMQL expression itself; subsequent "| EVAL …" pipe stages contain
    # ES|QL computed aliases (e.g. "device", "cpu") that are not real index metric fields.
    promql_line = query.split("\n", 1)[0]
    names: set[str] = set()
    excluded_fields = set(_extract_group_fields(promql_line)) | _extract_promql_label_fields(promql_line)
    # Blank quoted label-matcher values before the identifier scan. A regex value
    # like device=~"[a-z]+|nvme[0-9]+n[0-9]+|mmcblk[0-9]+" contains tokens
    # (``nvme``/``n``/``mmcblk``) each followed by ``[`` — the metric-name
    # lookahead would otherwise harvest them as phantom metric fields. Replace
    # each quoted span with same-length spaces so identifier offsets are stable.
    scan_line = re.sub(r'"[^"]*"', lambda m: " " * len(m.group(0)), promql_line)
    for match in re.finditer(r"\b([A-Za-z_:][\w:.]*)(?=\s*(?:\{|\[))", scan_line):
        field_name = _normalize_field(match.group(1))
        if not _should_skip_field(field_name) and field_name not in excluded_fields:
            names.add(field_name)
    expr = promql_line.split("value=", 1)[-1] if "value=" in promql_line else promql_line
    expr = re.sub(r"\{[^}]*\}", "", expr)
    expr = re.sub(r'"[^"]*"', "", expr)
    expr = re.sub(r"\[[^\]]*\]", "", expr)
    reserved = {
        "and",
        "avg",
        "bool",
        "by",
        "count",
        "count_values",
        "group",
        "increase",
        "irate",
        "label_join",
        "label_replace",
        "max",
        "min",
        "offset",
        "or",
        "rate",
        "scalar",
        "stddev",
        "stdvar",
        "sum",
        "topk",
        "unless",
        "without",
        "on",
        "ignoring",
        "group_left",
        "group_right",
    }
    # Negative lookbehind for `@` prevents bare `timestamp` from leaking out
    # of `@timestamp`. Negative lookbehind for word/dot characters keeps
    # the regex from re-matching the tail of dotted identifiers like
    # `service.name` (which would otherwise yield a phantom `name`).
    for match in re.finditer(r"(?<![@\w.])([A-Za-z_:][\w:.]*)\b(?!\s*\()", expr):
        field_name = _normalize_field(match.group(1))
        if (
            field_name.lower() not in reserved
            and not _should_skip_field(field_name)
            and field_name not in excluded_fields
        ):
            names.add(field_name)
    return names


_PROMQL_VECTOR_MATCHING_RE = re.compile(
    r"\b(?:on|ignoring|group_left|group_right)\s*\(([^)]*)\)",
    re.IGNORECASE,
)


def _extract_promql_label_fields(query: str) -> set[str]:
    fields: set[str] = set()
    # Restrict to the PROMQL expression line; EVAL/KEEP pipe stages follow "\n|" and
    # contain regex patterns with {…} that must not be treated as label selectors.
    search_text = query.split("\n", 1)[0] if query.startswith("PROMQL ") else query
    for matcher_block in re.findall(r"\{([^}]*)\}", search_text):
        for field_name in re.findall(r"([A-Za-z_][\w:.]*)\s*(?:=~|=|!=|!~)", matcher_block):
            normalized = _normalize_field(field_name)
            if not _should_skip_field(normalized):
                fields.add(normalized)
    for match in _PROMQL_VECTOR_MATCHING_RE.finditer(search_text):
        for part in _split_top_level(match.group(1)):
            normalized = _normalize_field(part)
            if normalized and not _should_skip_field(normalized):
                fields.add(normalized)
    return fields


def _max_lookback_seconds(query: str) -> int:
    max_seconds = 0
    for amount, unit in re.findall(r"NOW\(\)\s*-\s*(\d+)\s+([A-Za-z]+)", query):
        max_seconds = max(max_seconds, int(amount) * _LOOKBACK_SECONDS.get(unit.lower(), 0))
    for amount, unit in re.findall(r"\[(\d+)([smhdw])\]", query, flags=re.IGNORECASE):
        max_seconds = max(max_seconds, int(amount) * _promql_range_unit_seconds(unit))
    return max_seconds


def _promql_range_unit_seconds(unit: str) -> int:
    return {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 604800,
    }.get(unit.lower(), 0)


def _format_lookback(seconds: int) -> str:
    if seconds <= 0:
        return ""
    for unit, size in (("days", 86400), ("hours", 3600), ("minutes", 60)):
        if seconds % size == 0:
            amount = seconds // size
            label = unit[:-1] if amount == 1 else unit
            return f"{amount} {label}"
    return f"{seconds} seconds"


def _lookback_to_seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)\s+([A-Za-z]+)", value.strip())
    if not match:
        return 0
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if not unit.endswith("s"):
        unit = f"{unit}s"
    return amount * _LOOKBACK_SECONDS.get(unit, 0)


def _normalize_field(field_name: str) -> str:
    value = field_name.strip().rstrip("|").strip()
    if value.startswith("`") and value.endswith("`"):
        value = value[1:-1].replace("``", "`")
    if value.endswith(".keyword"):
        value = value.removesuffix(".keyword")
    return value


_QUERY_FIELD_RESERVED_WORDS = {
    "average",
    "avg",
    "avg_over_time",
    "case",
    "count",
    "date_diff",
    "false",
    "first",
    "if",
    "increase",
    "irate",
    "last",
    "max",
    "max_over_time",
    "median",
    "min",
    "now",
    "null",
    "percentile",
    "rate",
    "sum",
    "to_datetime",
    "true",
}


def _extract_query_field_candidates(expression: str) -> list[str]:
    # Drop quoted string literals first: comparison values such as
    # ``CASE((mode == "idle"), ...)`` are label *values*, not fields, and the
    # identifier regex below would otherwise harvest ``idle`` as a metric. A
    # backtick-quoted identifier IS a field, so only double/single quotes are
    # stripped here.
    expression = re.sub(r"\"[^\"]*\"|'[^']*'", " ", expression)
    fields: list[str] = []
    for match in re.finditer(r"`[^`]+`|[A-Za-z_@][\w.@-]*", expression):
        raw = match.group(0)
        if match.start() > 0 and expression[match.start() - 1].isdigit():
            # Duration literals such as ``1m`` / ``30s`` are not fields; the
            # regex sees the unit suffix as an identifier because it starts
            # with a letter.
            continue
        # Identifiers that are the left operand of a comparison / match operator
        # are label *predicates*, not numeric metrics: in
        # ``CASE((fstype RLIKE "tmpfs"), metric, 0)`` the ``fstype`` is a
        # dimension being filtered on, while ``metric`` is the real value.
        trailing = expression[match.end():]
        if re.match(r"\s*(?:==|!=|>=|<=|>|<|(?:NOT\s+)?R?LIKE\b)", trailing, re.IGNORECASE):
            continue
        field_name = _normalize_field(raw)
        if not field_name:
            continue
        if field_name == "@timestamp" or field_name.startswith("_"):
            continue
        if field_name.lower() in _QUERY_FIELD_RESERVED_WORDS:
            continue
        if _should_skip_field(field_name):
            continue
        _append_unique(fields, field_name)
    return fields


def _should_skip_field(field_name: str) -> bool:
    if field_name in _SKIP_FIELDS:
        return True
    if re.fullmatch(r"(?:query\d+(?:_\d+)*|value_\d+|[A-Z])", field_name):
        return True
    if _TRANSLATOR_INTERNAL_PREFIX_RE.match(field_name):
        return True
    return False


def _merge_field(
    fields: dict[str, dict[str, Any]],
    field_name: str,
    *,
    role: str,
    type_family: str,
    metric_kind: str,
    source: str,
    requires_native_promql: bool = False,
    keyword_multifield: bool = False,
) -> None:
    # ``counter_locked`` is an authoritative counter signal (source rate()/irate(),
    # which is counter-only in PromQL). It stores as ``counter`` but pins the kind so
    # a later AVG_OVER_TIME/MAX_OVER_TIME gauge vote cannot flip it.
    counter_locked = metric_kind == "counter_locked"
    if counter_locked:
        metric_kind = "counter"
    current = fields.setdefault(
        field_name,
        {
            "role": role,
            "type_family": type_family,
            "metric_kind": metric_kind,
            "sources": [],
        },
    )
    if current["role"] != role:
        current["role"] = "metric" if "metric" in {current["role"], role} else role
    if counter_locked:
        current["metric_kind"] = "counter"
        current["_counter_locked"] = True
    if metric_kind and not current.get("metric_kind"):
        current["metric_kind"] = metric_kind
    elif metric_kind == "counter" and current.get("metric_kind") == "gauge" and current.get("_counter_locked"):
        # A locked counter was previously stored as gauge by an out-of-order signal;
        # restore counter (the lock wins).
        current["metric_kind"] = "counter"
    elif metric_kind == "gauge" and current.get("metric_kind") == "counter" and not current.get("_counter_locked"):
        # ES|QL MAX_OVER_TIME(field) requires gauge_double and takes priority over a
        # *soft* PROMQL increase()-based counter classification. A locked counter
        # (source rate()/irate()) is exempt — that gauge translation is downstream of
        # an earlier gauge guess, not independent evidence.
        current["metric_kind"] = "gauge"
    if requires_native_promql:
        current["requires_native_promql"] = True
    if keyword_multifield:
        # A query referenced this dimension as ``<field>.keyword``. Real ES
        # Datadog/OTel mappings expose that aggregatable keyword sub-field, so
        # the seeded mapping must too or the column is unknown at query time.
        current["keyword_multifield"] = True
    _append_unique(current["sources"], source)


def _merge_required_map(target: dict[str, list[str]], incoming: dict[str, list[str]]) -> None:
    for field_name, values in incoming.items():
        bucket = target.setdefault(field_name, [])
        for value in values:
            _append_unique(bucket, value)


def _append_required(target: dict[str, list[str]], field_name: str, value: str) -> None:
    if not field_name or _should_skip_field(field_name):
        return
    if not _is_literal_dimension_value(value):
        # Unsubstituted template variables / migrator placeholders are not real
        # label values; skip them so the seeder uses coherent defaults instead.
        return
    bucket = target.setdefault(field_name, [])
    _append_unique(bucket, value)


def _lens_to_contract_query(lens: Any) -> str:
    if not isinstance(lens, dict):
        return ""
    parts = ["LENS", f"index={lens.get('data_view') or 'metrics-*'}"]

    def add_metric(config: Any) -> None:
        if isinstance(config, dict) and config.get("field"):
            parts.append(f"metric={config['field']}")
            parts.append(f"agg={config.get('aggregation', 'avg')}")

    add_metric(lens.get("primary"))
    metric = lens.get("metric")
    if isinstance(metric, dict):
        add_metric(metric.get("primary"))
    for metric_config in lens.get("metrics", []):
        add_metric(metric_config)

    def add_group(config: Any) -> None:
        if isinstance(config, dict) and config.get("field"):
            parts.append(f"group={config['field']}")

    add_group(lens.get("breakdown"))
    add_group(lens.get("dimension"))
    for group in lens.get("group_by", []):
        if isinstance(group, str):
            parts.append(f"group={group}")
        else:
            add_group(group)
    for group in lens.get("breakdowns", []):
        add_group(group)
    return " ".join(parts) if len(parts) > 2 else ""


def _iter_dashboard_filter_queries(node: dict[str, Any], source: str):
    controls = node.get("controls")
    filters = node.get("filters")
    if not isinstance(controls, list) and not isinstance(filters, list):
        return
    default_index = _first_control_index(controls) or "metrics-*"
    if isinstance(controls, list):
        for control in controls:
            if not isinstance(control, dict):
                continue
            if control.get("variable_type") == "fields":
                choices = (
                    control.get("choices")
                    or control.get("available_options")
                    or control.get("options")
                    or []
                )
                index = control.get("data_view") or control.get("data_view_id") or default_index
                for choice in choices:
                    field_name = str(choice or "").strip()
                    if field_name:
                        yield f"CONTROL index={index} field={field_name}", source
                continue
            control_type = str(control.get("type") or "").lower()
            if control_type in _CLASSIC_OPTIONS_CONTROL_TYPES | _CLASSIC_RANGE_CONTROL_TYPES:
                field_name = _control_bound_field(control)
                if not field_name:
                    continue
                index = control.get("data_view") or control.get("data_view_id") or default_index
                yield f"CONTROL index={index} field={field_name}", source
                if control_type in _CLASSIC_RANGE_CONTROL_TYPES:
                    numeric_values: list[float] = []
                    for raw in _control_choice_values(control):
                        try:
                            numeric_values.append(float(raw))
                        except ValueError:
                            continue
                    if len(numeric_values) >= 2:
                        low = min(numeric_values)
                        high = max(numeric_values)
                        yield (
                            f"RANGE index={index} field={field_name} min={low} max={high}",
                            source,
                        )
                continue
            field_name = control.get("field")
            bound = _control_bound_field(control) if not field_name else str(field_name)
            if bound:
                index = control.get("data_view") or control.get("data_view_id") or default_index
                yield f"CONTROL index={index} field={bound}", source
            query_text = str(control.get("query") or "").strip()
            if query_text:
                yield query_text, source
        for field_name, values in _collect_control_value_requirements(controls).items():
            for value in values:
                yield (
                    f"FILTER index={default_index} field={field_name} "
                    f"value={_encode_pseudo_value(value)}",
                    source,
                )
    if isinstance(filters, list):
        for dashboard_filter in filters:
            if not isinstance(dashboard_filter, dict):
                continue
            field_name = dashboard_filter.get("field")
            if not field_name:
                continue
            value = dashboard_filter.get("equals")
            if value is None:
                yield f"CONTROL index={default_index} field={field_name}", source
            else:
                yield f"FILTER index={dashboard_filter.get('data_view') or default_index} field={field_name} value={_encode_pseudo_value(str(value))}", source


def _first_control_index(controls: Any) -> str:
    if not isinstance(controls, list):
        return ""
    for control in controls:
        if isinstance(control, dict) and control.get("data_view"):
            return str(control["data_view"])
    return ""


def _encode_pseudo_value(value: str) -> str:
    return value.replace("%", "%25").replace(" ", "%20")


def _decode_pseudo_value(value: str) -> str:
    return value.replace("%20", " ").replace("%25", "%")


def _iter_schema_change_rows(artifact_path: Path):
    packets_path = _resolve_packets_path(artifact_path)
    ir_panel_index = _build_ir_panel_index(artifact_path)
    seen_keys: set[tuple[str, str]] = set()
    packet_entries: list[dict[str, Any]] = []
    if packets_path is not None:
        try:
            packet_entries = list(
                json.loads(packets_path.read_text(encoding="utf-8")).get("packets", [])
            )
        except Exception:
            packet_entries = []

    for packet in packet_entries:
        if not isinstance(packet, dict):
            continue
        dashboard = str(packet.get("dashboard") or "")
        panel = str(packet.get("panel") or "")
        source_expressions = list(packet.get("source_queries") or [])
        if isinstance(packet.get("source_query"), str):
            source_expressions.append(packet["source_query"])
        query_ir = packet.get("query_ir") or {}
        if isinstance(query_ir, dict) and isinstance(query_ir.get("source_expression"), str):
            source_expressions.append(query_ir["source_expression"])
        source_fields: list[str] = []
        for expression in source_expressions:
            for field_name in _extract_source_fields(str(expression)):
                _append_unique(source_fields, field_name)
        target_query = (
            packet.get("translated_query")
            or (packet.get("target_execution") or {}).get("query")
            or (query_ir or {}).get("target_query")
            or ""
        )
        target_metrics = set(_extract_metrics(target_query))
        target_dimensions = _extract_dimensions(target_query)
        target_stream = _query_index(target_query)

        ir_panel = ir_panel_index.get(_panel_key(dashboard, panel)) or ir_panel_index.get(
            _panel_key("", panel)
        )
        if ir_panel is not None:
            target_metrics |= ir_panel["metrics"]
            target_dimensions |= ir_panel["dimensions"]
            if not target_stream:
                target_stream = ir_panel["stream"]

        target_fields = sorted(target_metrics | target_dimensions)
        if not (source_fields or target_fields):
            continue
        seen_keys.add(_panel_key(dashboard, panel))
        yield {
            "dashboard": dashboard,
            "panel": panel,
            "source_fields": _clean_source_fields(source_fields),
            "target_stream": target_stream,
            "target_fields": target_fields,
        }

    for key, ir_panel in ir_panel_index.items():
        if key in seen_keys:
            continue
        target_fields = sorted(ir_panel["metrics"] | ir_panel["dimensions"])
        if not target_fields:
            continue
        yield {
            "dashboard": ir_panel["dashboard"],
            "panel": ir_panel["panel"],
            "source_fields": [],
            "target_stream": ir_panel["stream"],
            "target_fields": target_fields,
        }


def _resolve_packets_path(artifact_path: Path) -> Path | None:
    candidates: list[Path] = [artifact_path / "verification_packets.json"]
    if artifact_path.name in _ARTIFACT_SUBDIRS:
        candidates.append(artifact_path.parent / "verification_packets.json")
    for path in candidates:
        if path.exists():
            return path
    return None


def _build_ir_panel_index(artifact_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Index every panel's target metrics/dimensions/stream from the IR export.

    Keyed on ``(dashboard title, panel title)`` so
    :func:`_iter_schema_change_rows` can enrich a verification packet with the
    fields the emitted query actually references, and can still emit a row for
    a panel that has no packet at all.
    """
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for _source, payload in _iter_artifact_dashboards(
        artifact_path, caller="_build_ir_panel_index"
    ):
        for dashboard in payload.get("dashboards", []) or []:
            dashboard_title = str(
                dashboard.get("name") or dashboard.get("title") or ""
            )
            for panel in _flatten_dashboard_panels(dashboard.get("panels", []) or []):
                panel_title = str(panel.get("title") or "")
                metrics: set[str] = set()
                dimensions: set[str] = set()
                stream = ""
                lens = panel.get("lens")
                if isinstance(lens, dict):
                    lens_query = _lens_to_contract_query(lens)
                    if lens_query:
                        metrics |= set(_extract_metrics(lens_query))
                        dimensions |= _extract_dimensions(lens_query)
                        stream = _query_index(lens_query) or stream
                esql = panel.get("esql")
                if isinstance(esql, dict) and isinstance(esql.get("query"), str):
                    metrics |= set(_extract_metrics(esql["query"]))
                    dimensions |= _extract_dimensions(esql["query"])
                    if not stream:
                        stream = _query_index(esql["query"])
                if metrics or dimensions:
                    index[_panel_key(dashboard_title, panel_title)] = {
                        "dashboard": dashboard_title,
                        "panel": panel_title,
                        "metrics": metrics,
                        "dimensions": dimensions,
                        "stream": stream,
                    }
    return index


def _flatten_dashboard_panels(panels: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(panels, list):
        return
    for panel in panels:
        if not isinstance(panel, dict):
            continue
        yield panel
        section = panel.get("section")
        if isinstance(section, dict):
            yield from _flatten_dashboard_panels(section.get("panels"))


def _panel_key(dashboard: str, panel: str) -> tuple[str, str]:
    return (dashboard.strip(), panel.strip())


_DATADOG_AGGREGATOR_RE = re.compile(r"^(?:avg|sum|min|max|p\d+|count):", re.IGNORECASE)
_DATADOG_METRIC_QUERY_RE = re.compile(
    r"\b(?:avg|sum|min|max|p\d+|count)(?:\([^)]*\))?:([A-Za-z_][\w.]*)\{([^}]*)\}",
    re.IGNORECASE,
)
_DATADOG_LOG_FILTER_RE = re.compile(r"(?:^|[\s,(])([A-Za-z_][\w.-]*)\s*:\s*([^,\s)}]+)")
_NOISY_SOURCE_TOKENS = {
    "by",
    "and",
    "or",
    "not",
    "on",
    "ignoring",
    "group_left",
    "group_right",
    "without",
    "offset",
    "bool",
}

# Grafana global/template variables (https://grafana.com/docs/grafana/latest/dashboards/variables/)
# and well-known PromQL pseudo-labels. None of these are real producer
# fields; they are query-time substitutions or meta-attributes that the
# schema-report should never claim a producer must emit.
_GRAFANA_TEMPLATE_TOKENS = {
    "__name__",
    "__rate_interval",
    "__rate_interval_ms",
    "__interval",
    "__interval_ms",
    "__range",
    "__range_s",
    "__range_ms",
    "__from",
    "__to",
    "__user",
    "__org",
    "__dashboard",
    "__timeFilter",
    "__timefilter",
    "__auto",
    "__auto_interval",
    "aggregation_interval",
    "scrape_interval",
}


def _extract_source_fields(expression: str) -> list[str]:
    fields: list[str] = []
    # Datadog metric syntax: avg:metric.name{tag:value} by {tag}
    datadog_metric_matches = list(_DATADOG_METRIC_QUERY_RE.finditer(expression))
    for match in datadog_metric_matches:
        _append_unique(fields, match.group(1))
        scope = match.group(2)
        for tag in re.findall(r"([A-Za-z_][\w.]*)\s*:", scope):
            _append_unique(fields, tag)
    for group_expr in re.findall(r"\bby\s*\{([^}]*)\}", expression):
        for part in _split_top_level(group_expr):
            field_name = _normalize_field(part)
            if field_name and not _should_skip_field(field_name):
                _append_unique(fields, field_name)
    if datadog_metric_matches:
        return _clean_source_fields(fields)
    for match in _DATADOG_LOG_FILTER_RE.finditer(expression):
        field_name = _normalize_field(match.group(1))
        value = match.group(2)
        if value.startswith("$"):
            continue
        if field_name.lower() in _NOISY_SOURCE_TOKENS:
            continue
        _append_unique(fields, field_name)
    if fields:
        return _clean_source_fields(fields)
    # PromQL source expressions.
    for field_name in _extract_promql_metric_names(f"PROMQL index=metrics-* value=({expression})"):
        _append_unique(fields, field_name)
    for field_name in _extract_promql_label_fields(expression):
        _append_unique(fields, field_name)
    return _clean_source_fields(fields)


def _clean_source_fields(fields: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in fields:
        candidate = raw.strip().rstrip(".").strip()
        if not candidate:
            continue
        if _DATADOG_AGGREGATOR_RE.match(candidate):
            continue
        if candidate.lower() in _NOISY_SOURCE_TOKENS:
            continue
        if candidate in _GRAFANA_TEMPLATE_TOKENS:
            continue
        if candidate.startswith("__"):
            continue
        if _should_skip_field(candidate):
            continue
        _append_unique(cleaned, candidate)
    return sorted(cleaned)


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


__all__ = [
    "build_combined_telemetry_contract",
    "build_schema_change_report",
    "build_telemetry_contract",
    "count_declared_controls",
    "merge_metric_kind_overrides",
    "metric_kinds_from_field_caps",
    "metric_kinds_from_prometheus_metadata",
    "write_schema_report_artifacts",
    "write_telemetry_contract",
]
