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
        if info.get("keyword_multifield") and props.get(field_name, {}).get("type") == "keyword":
            # Mirror the aggregatable ``<field>.keyword`` sub-field that real ES
            # Datadog/OTel mappings expose, so queries referencing it resolve.
            props[field_name].setdefault("fields", {})["keyword"] = {
                "type": "keyword",
                "ignore_above": 1024,
            }

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


def _seed_metric_fields(
    doc: dict[str, Any],
    metric_fields: dict[str, dict[str, Any]],
    dimensions: dict[str, str],
    le_order: list[str],
    *,
    concrete_name: str,
    state_combo: tuple[int, int],
    combo_idx: int,
    effective_interval: int,
    hour: float,
    now_epoch: float,
    rng: random.Random,
    counter_state: dict[tuple[str, str, int], float],
) -> None:
    """Fill ``doc`` with a synthetic value for each metric field.

    Shared by the metrics and logs/traces paths: logs/traces streams can still
    carry numeric columns referenced by ``FROM`` aggregations or presence
    filters, and those must be seeded so queries do not hit empty or unknown
    columns. The Prometheus cumulative-histogram scaling is naturally a no-op off
    the metrics path because non-metric families carry no ``le`` dimension.
    """
    gauge_values: dict[str, float] = {}
    for field_name in _coherence_order(metric_fields):
        info = metric_fields[field_name]
        if info.get("metric_kind") == "counter":
            key = (concrete_name, field_name, state_combo)
            counter_state[key] = counter_state.get(key, float(10 + combo_idx))
            counter_state[key] += _counter_increment(field_name, effective_interval, hour, rng)
            value = counter_state[key]
            if field_name.endswith("_bucket") and "le" in dimensions:
                # Prometheus cumulative histogram: bucket(le=v) counts all
                # observations <= v, so it must be non-decreasing as le grows.
                # Scale the per-series counter by the le rank (1-based) so higher
                # buckets always dominate lower ones while each series stays
                # monotonic in time.
                rank, total = _le_rank(dimensions["le"], le_order)
                if total:
                    value *= rank / total
        else:
            denominator = (
                _ratio_denominator(info)
                or _static_invariant_denominator(field_name, metric_fields)
            )
            ceiling = gauge_values.get(denominator) if denominator else None
            value = _gauge_value(
                field_name, hour, combo_idx, rng,
                ceiling=ceiling, now_epoch=now_epoch,
            )
            gauge_values[field_name] = value
        doc[field_name] = round(value, 4)


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
        all_metric_fields = {
            field_name: info
            for field_name, info in (stream.get("fields") or {}).items()
            if info.get("role") == "metric"
        }
        # Split the stream into metric families that each only carry the
        # dimensions they actually co-occur with in a query. Without this, a
        # stream merged from many dashboards puts every metric and every
        # dimension into one document, so unrelated dimensions cross-contaminate
        # legends and the per-dimension cardinality collapses under the
        # combination cap. Falls back to the whole stream when no requirements
        # are recorded (preserving the unscoped behaviour).
        families = _metric_families(stream, all_metric_fields, max_combinations=max_combinations)

        previous_ts: datetime.datetime | None = None
        for ts in timestamps:
            hour = ts.hour + ts.minute / 60.0
            effective_interval = (
                int((ts - previous_ts).total_seconds())
                if previous_ts is not None
                else min(interval_sec, 60)
            )
            for family_idx, (metric_fields, combinations, le_order) in enumerate(families):
                for combo_idx, dimensions in enumerate(combinations):
                    # Namespace the counter/combo key by family so the same
                    # global combo index across families never aliases state.
                    state_combo = (family_idx, combo_idx)
                    doc: dict[str, Any] = {
                        "@timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "data_stream.type": stream_type,
                        "data_stream.dataset": dataset,
                        "data_stream.namespace": namespace,
                        **dimensions,
                    }
                    _seed_metric_fields(
                        doc,
                        metric_fields,
                        dimensions,
                        le_order,
                        concrete_name=concrete_name,
                        state_combo=state_combo,
                        combo_idx=combo_idx,
                        effective_interval=effective_interval,
                        hour=hour,
                        now_epoch=ts.timestamp(),
                        rng=rng,
                        counter_state=counter_state,
                    )
                    if not is_metrics:
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


_SEEDER_TEMPLATE_PREFIX = "telemetry-data-"


def _contract_index_patterns(contract: dict[str, Any]) -> list[str]:
    """Wildcard index patterns the contract's streams resolve against.

    A migrated dashboard queries a bare wildcard such as ``metrics-*`` /
    ``logs-*``; that is the surface that must be free of foreign, incompatibly
    mapped data for panels to resolve.
    """
    patterns: list[str] = []
    for index_pattern in (contract.get("streams") or {}):
        cleaned = index_pattern.strip()
        if not cleaned:
            continue
        # Reduce a concrete-ish pattern to its type wildcard (metrics-*/logs-*).
        prefix = cleaned.split("-", 1)[0]
        if prefix in {"metrics", "logs", "traces"}:
            _append_pattern(patterns, f"{prefix}-*")
        else:
            _append_pattern(patterns, cleaned)
    return patterns


def _append_pattern(patterns: list[str], value: str) -> None:
    if value and value not in patterns:
        patterns.append(value)


def purge_foreign_streams(
    contract: dict[str, Any],
    request: RequestFn,
) -> list[str]:
    """Delete data streams that overlap the contract's wildcards but were not
    created by this seeder.

    Migrated dashboards query bare wildcards (``metrics-*``/``logs-*``). Any
    leftover data stream that matches the same wildcard but carries a different
    mapping (e.g. an old parity/experiment stream) makes shared fields conflict
    across indices (``metric_conflicts_indices``), and ES then refuses to read
    those fields through the wildcard — panels silently return zero rows or
    fail. Seeder-owned streams (index template prefixed ``telemetry-data-``) are
    always preserved; everything else matching the wildcard is removed so the
    wildcard surface is internally consistent.

    Returns the list of deleted data stream names (empty when nothing matched).
    """
    deleted: list[str] = []
    seen: set[str] = set()
    for pattern in _contract_index_patterns(contract):
        listing = request("GET", f"/_data_stream/{pattern}", None, "application/json")
        for entry in (listing or {}).get("data_streams", []) or []:
            name = entry.get("name")
            template = str(entry.get("template") or "")
            if not name or name in seen:
                continue
            seen.add(name)
            if template.startswith(_SEEDER_TEMPLATE_PREFIX):
                continue  # seeder-owned; keep
            result = request("DELETE", f"/_data_stream/{name}", None, "application/json")
            _raise_on_error(result, f"delete foreign data stream {name}")
            deleted.append(name)
    return deleted


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


# Prometheus identity labels every scrape target carries (plus their common
# target-schema spellings). Used as the dimension fallback for metrics whose
# queries reference no dimensions, so seeded documents always satisfy TSDB
# routing.
_IDENTITY_FALLBACK_DIMENSIONS = ("instance", "job", "service.name", "host.name")


def _dotted_field_prefixes(field_names: Iterable[str]) -> set[str]:
    """Return the set of bare-name prefixes that have at least one dotted child."""
    return {
        name.split(".", 1)[0]
        for name in field_names
        if "." in name and not name.startswith("data_stream.")
    }


def _metric_families(
    stream: dict[str, Any],
    metric_fields: dict[str, dict[str, Any]],
    *,
    max_combinations: int,
) -> list[tuple[dict[str, dict[str, Any]], list[dict[str, str]], list[str]]]:
    """Split a stream into metric families scoped to co-occurring dimensions.

    Each family is ``(metric_fields_subset, combinations, le_order)``. A merged
    ``metrics-*`` stream lists every metric and every dimension from every
    dashboard; pairing them all in one document cross-contaminates legends and
    starves per-dimension cardinality. Using the per-query ``requirements`` we
    only pair a metric with the dimensions it actually appears with.

    With no ``requirements`` (or no metrics) the whole stream is one family, so
    behaviour is unchanged for callers that do not record requirements.
    """
    requirements = stream.get("requirements") or []
    if not requirements or not metric_fields:
        combos = _dimension_combinations(stream, max_combinations=max_combinations)
        return [(metric_fields, combos, _sorted_le_values(combos))]

    # Union the dimensions each metric co-occurs with across all requirements.
    metric_dims: dict[str, set[str]] = {name: set() for name in metric_fields}
    for requirement in requirements:
        dims = set(requirement.get("dimensions") or []) | set(requirement.get("group_fields") or [])
        for metric_name in requirement.get("metrics") or []:
            if metric_name in metric_dims:
                metric_dims[metric_name] |= dims
    # A ratio numerator must travel with its denominator (same document) and
    # share its dimensions so the bound holds per series.
    for metric_name, info in metric_fields.items():
        denominator = _ratio_denominator(info)
        if denominator and denominator in metric_dims:
            shared = metric_dims[metric_name] | metric_dims[denominator]
            metric_dims[metric_name] = shared
            metric_dims[denominator] = shared

    # A metric whose queries reference no dimensions at all (a bare ``up``)
    # must still seed documents that carry at least one
    # ``time_series_dimension`` value: TSDB routing rejects dimensionless
    # documents wholesale ("Error extracting routing: source didn't contain
    # any routing fields"), silently un-seeding the metric. Real Prometheus
    # series always carry identity labels, so fall back to the stream's
    # identity dimensions.
    identity_dims = {
        name
        for name in _IDENTITY_FALLBACK_DIMENSIONS
        if (stream.get("fields") or {}).get(name, {}).get("role") == "dimension"
    }
    if identity_dims:
        for metric_name, dims in metric_dims.items():
            if not dims:
                metric_dims[metric_name] = set(identity_dims)

    # Group metrics that share an identical dimension signature into one family.
    families_by_sig: dict[frozenset[str], list[str]] = {}
    for metric_name in sorted(metric_dims):
        sig = frozenset(metric_dims[metric_name])
        families_by_sig.setdefault(sig, []).append(metric_name)

    # Control/group dimensions that co-occur with no metric (e.g. a dashboard
    # variable like ``nodename``) would otherwise be mapped but never assigned a
    # value, leaving the Kibana control dropdown empty. Attach them to one
    # existing family at scope-build time so they get seeded -- WITHOUT changing
    # any metric's dimension signature (which would split a metric out of a
    # family it shares with a ratio sibling and break the ratio). The carrier is
    # the family with the most metrics (ties broken by sorted signature) so the
    # extra control values ride the largest doc set deterministically.
    orphan_dims = frozenset(
        dim
        for dim in (set(stream.get("control_fields") or []) | set(stream.get("group_fields") or []))
        if dim in (stream.get("fields") or {})
    ) - {dim for dims in metric_dims.values() for dim in dims}
    carrier_sig: frozenset[str] | None = None
    if orphan_dims and families_by_sig:
        carrier_sig = max(families_by_sig, key=lambda s: (len(families_by_sig[s]), sorted(s)))

    families: list[tuple[dict[str, dict[str, Any]], list[dict[str, str]], list[str]]] = []
    for sig, names in families_by_sig.items():
        family_metrics = {name: metric_fields[name] for name in names}
        scope_dims = sig | orphan_dims if sig == carrier_sig else sig
        scoped = _scoped_stream(stream, scope_dims, set(names))
        combos = _dimension_combinations(scoped, max_combinations=max_combinations)
        families.append((family_metrics, combos, _sorted_le_values(combos)))
    return families


def _scoped_stream(stream: dict[str, Any], dimensions: frozenset[str], metrics: set[str]) -> dict[str, Any]:
    """A shallow stream view restricted to *dimensions* and *metrics*."""
    fields = {
        name: info
        for name, info in (stream.get("fields") or {}).items()
        if name in metrics or name in dimensions
    }
    return {
        "fields": fields,
        "control_fields": [f for f in (stream.get("control_fields") or []) if f in dimensions],
        "group_fields": [f for f in (stream.get("group_fields") or []) if f in dimensions],
        "required_values": {k: v for k, v in (stream.get("required_values") or {}).items() if k in dimensions},
        "required_patterns": {k: v for k, v in (stream.get("required_patterns") or {}).items() if k in dimensions},
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
        values = [v for v in (required_values.get(field_name) or []) if _seedable_value(v)]
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
        sorted(set(control_fields) | set(group_fields) | set(required_values) | set(required_patterns)),
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


_LITERALISH_RE = re.compile(r"^[\w./:@-]+$")


def _seedable_value(value: str) -> bool:
    """False for exact required-values that are not real label values.

    Grafana template variables that the migrator relabels (``$host`` ->
    ``label_host``) reach the contract as exact ``required_values`` (not
    patterns). Seeding ``label_host:label_port`` verbatim produces a weird
    legend the panel query never matches, so drop it and let the field fall
    back to clean defaults.
    """
    if not value:
        return False
    return "label_" not in value


def _expand_patterns(field_name: str, patterns: list[str]) -> list[str]:
    values: list[str] = []
    for pattern in patterns:
        values.extend(_expand_single_pattern(field_name, pattern))
    return values


def _instantiate_regex(pattern: str) -> str | None:
    """Synthesize one concrete string that ``re.fullmatch``es ``pattern``.

    Handles the regex shapes Prometheus label matchers use in practice —
    character classes (``[a-z]+``, ``[0-9]+``, ``[A-Z]``), literal runs
    (``nvme``, ``eth``), and simple quantifiers (``+``/``*``/``?``/``{n}``).
    Returns ``None`` for anything it cannot confidently instantiate so the
    caller can fall back to a coherent default instead of guessing.

    A matching value is what makes a ``device=~"[a-z]+|nvme[0-9]+n[0-9]+"``
    panel actually find data — Prometheus ``=~`` is a *full* match, so a clean
    but non-matching stem (``device_1``) leaves the panel empty.
    """
    body = pattern.strip().removeprefix("^").removesuffix("$")
    if not body:
        return None
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        token: str
        if ch == "[":
            close = body.find("]", i + 1)
            if close == -1:
                return None
            cls = body[i + 1 : close]
            token = _sample_char_class(cls)
            if token is None:
                return None
            i = close + 1
        elif ch in "().|\\":
            # Groups / alternation / escapes / wildcards inside a single
            # alternative are beyond this lightweight instantiator.
            return None
        elif ch in "+*?":
            # Quantifier with no preceding atom — malformed for our purposes.
            return None
        else:
            token = ch
            i += 1
        # Apply a trailing quantifier to the atom we just consumed.
        if i < n and body[i] in "+*?":
            quant = body[i]
            i += 1
            if quant == "*":
                # Zero-or-more: keep one sample so the value is non-empty/stable.
                pass
            # '+' and '?' both keep exactly one occurrence of the atom.
        elif i < n and body[i] == "{":
            close = body.find("}", i + 1)
            if close == -1:
                return None
            i = close + 1  # honor the atom once; exact repetition count is not required to match a single char-class atom
        out.append(token)
    value = "".join(out)
    return value or None


def _sample_char_class(cls: str) -> str | None:
    """Return one representative character for a regex character class body
    (the text between ``[`` and ``]``). Supports common ranges; returns the
    first literal for enumerations and ``None`` for negations/unknowns."""
    if not cls or cls.startswith("^"):
        return None
    ranges = {
        "a-z": "a",
        "A-Z": "a",  # lowercased so values look like real device/interface names
        "0-9": "0",
        "a-zA-Z": "a",
        "a-z0-9": "a",
        "a-zA-Z0-9": "a",
        "0-9a-z": "a",
    }
    if cls in ranges:
        return ranges[cls]
    # ``[abc]`` enumeration: take the first non-range literal character.
    if "-" not in cls and "\\" not in cls:
        return cls[0]
    # A leading range we recognize (e.g. ``[a-z_]``) — sample its first range.
    for key, sample in ranges.items():
        if cls.startswith(key):
            return sample
    return None


def _expand_single_pattern(field_name: str, pattern: str) -> list[str]:
    cleaned = pattern.strip()
    if not cleaned:
        return []
    # Relabeled Grafana template variables (``$node`` -> ``label_node``) are not
    # real label values; seeding them produces series no panel query matches.
    if "label_" in cleaned:
        return _default_dimension_values(field_name, count=1)
    # Status-code classes (``2..`` / ``5xx``) resolve to a concrete code so the
    # generated docs satisfy dashboards filtering on a status family.
    if re.fullmatch(r"\d\.\.", cleaned) or re.fullmatch(r"\dxx", cleaned, re.IGNORECASE):
        return [f"{cleaned[0]}00"]
    # Pure wildcards carry no literal signal — fall back to a default sample.
    if set(cleaned) <= {".", "*", "+", "^", "$"}:
        return [_default_dimension_values(field_name, count=1)[0]]
    body = cleaned.removeprefix("^").removesuffix("$")
    # Unwrap a single enclosing (possibly non-capturing) group, e.g. ``(a|b)``.
    if body.startswith("(") and body.endswith(")"):
        inner = body[1:-1].removeprefix("?:")
        if ")" not in inner and "(" not in inner:
            body = inner
    # Alternation — Grafana multi-value template variables become ``a|b|c``.
    if "|" in body:
        alternatives = [alt.strip() for alt in body.split("|") if alt.strip()]
        if alternatives and all(_LITERALISH_RE.fullmatch(alt) for alt in alternatives):
            return _unique(alternatives)
        # Mixed/regex alternatives (e.g. ``[a-z]+|nvme[0-9]+n[0-9]+|mmcblk[0-9]+``):
        # synthesize one concrete value per alternative that actually fullmatches
        # the source matcher, so a ``device=~`` panel finds data. Verify each
        # against the *whole* pattern (Prometheus =~ is a full match); drop any
        # we cannot confidently instantiate. Only if none instantiate do we fall
        # back to coherent defaults (never leak the raw regex).
        try:
            compiled = re.compile(body)
        except re.error:
            compiled = None
        seeded: list[str] = []
        for alt in alternatives:
            value = _instantiate_regex(alt)
            if value and (compiled is None or compiled.fullmatch(value)):
                seeded.append(value)
        if seeded:
            return _unique(seeded)
        return _default_dimension_values(field_name, count=3)
    # Literal prefix followed by a trailing glob (``nginx-.*``) — emit distinct
    # concrete values rather than a single literal "sample".
    glob = re.fullmatch(r"(.+?)(?:\.\*|\.\+|\*)", body)
    if glob and _LITERALISH_RE.fullmatch(glob.group(1)):
        prefix = glob.group(1)
        return [f"{prefix}{idx}" for idx in range(3)]
    # Leading glob followed by a literal suffix (``.*irq``) — keep the suffix.
    suffix_glob = re.fullmatch(r"(?:\.\*|\.\+|\*)(.+)", body)
    if suffix_glob and _LITERALISH_RE.fullmatch(suffix_glob.group(1)):
        return [suffix_glob.group(1)]
    # Literal core flanked by globs (``.*Foo.*`` / ``.+Foo.+``). Preserve the
    # zero-width ``.*`` case, but add padding when ``.+`` requires a character.
    flanked = re.fullmatch(r"(\.\*|\.\+|\*)(.+?)(\.\*|\.\+|\*)", body)
    if flanked and _LITERALISH_RE.fullmatch(flanked.group(2)):
        left = "x" if flanked.group(1) == ".+" else ""
        right = "x" if flanked.group(3) == ".+" else ""
        return [f"{left}{flanked.group(2)}{right}"]
    # Plain literal: keep it only when it is genuinely literal. Any residual regex
    # metacharacter means we could not extract a clean value, so fall back to a
    # default rather than seed a value containing ``[``/``+``/``|`` etc.
    literal = body.strip("*")
    if literal and _LITERALISH_RE.fullmatch(literal):
        return [literal]
    # Bare regex (no alternation), e.g. ``[a-z]+`` or ``eth[0-9]+`` — synthesize a
    # concrete value that fullmatches it so the panel's =~ matcher finds data.
    if re.search(r"[\[\]+*?{}]", body):
        try:
            compiled = re.compile(body)
        except re.error:
            compiled = None
        value = _instantiate_regex(body)
        if value and (compiled is None or compiled.fullmatch(value)):
            return [value]
    return [_default_dimension_values(field_name, count=1)[0]]


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


def _ratio_denominator(info: dict[str, Any]) -> str | None:
    """Return the query-derived denominator field this metric is bounded by."""
    for relation in info.get("relationships") or []:
        if relation.get("type") == "ratio_denominator" and relation.get("field"):
            return relation["field"]
    return None


# Sibling upper-bound invariants that metric names/queries cannot express as a
# plain A / B ratio. Maps a metric-name suffix to the suffix of the sibling that
# bounds it from above (numerator <= denominator). Matched within the same
# metric family so the prefix (e.g. "node_memory_") is shared.
_STATIC_INVARIANT_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("_memavailable_bytes", "_memtotal_bytes"),
    ("_memfree_bytes", "_memtotal_bytes"),
    ("_swapfree_bytes", "_swaptotal_bytes"),
    ("_filesystem_avail_bytes", "_filesystem_size_bytes"),
    ("_filesystem_free_bytes", "_filesystem_size_bytes"),
    ("_boot_time_seconds", "_time_seconds"),
)


def _static_invariant_denominator(field_name: str, candidates: Iterable[str]) -> str | None:
    """Return the sibling field that bounds *field_name* from above, if present.

    Resolves a numerator suffix to its denominator suffix via
    ``_STATIC_INVARIANT_SUFFIXES`` and matches against the metrics actually
    present (*candidates*) on a shared-prefix basis so e.g.
    ``node_memory_MemAvailable_bytes`` binds to ``node_memory_MemTotal_bytes``.
    """
    lname = field_name.lower()
    for num_suffix, denom_suffix in _STATIC_INVARIANT_SUFFIXES:
        if not lname.endswith(num_suffix):
            continue
        prefix = lname[: -len(num_suffix)]
        for cand in candidates:
            lc = cand.lower()
            if lc == lname:
                continue
            if lc.endswith(denom_suffix) and lc[: -len(denom_suffix)] == prefix:
                return cand
    return None


def _coherence_order(metric_fields: dict[str, dict[str, Any]]) -> list[str]:
    """Order metric fields so each denominator precedes the metric it bounds.

    Combines query-derived ratio relationships with the static sibling-invariant
    table. Falls back to insertion order for unrelated fields, so generation is
    byte-identical to the unordered path when no relationships are present.
    Cycles are broken by the visited guard.
    """
    ordered: list[str] = []
    visited: set[str] = set()
    names = list(metric_fields)

    def denom_for(field_name: str) -> str | None:
        return (
            _ratio_denominator(metric_fields[field_name])
            or _static_invariant_denominator(field_name, names)
        )

    def visit(field_name: str) -> None:
        if field_name in visited or field_name not in metric_fields:
            return
        visited.add(field_name)
        denominator = denom_for(field_name)
        if denominator:
            visit(denominator)
        ordered.append(field_name)

    for field_name in names:
        visit(field_name)
    return ordered


def _counter_increment(field_name: str, interval_sec: int, hour: float, rng: random.Random) -> float:
    base_rate = 0.5 + (abs(hash(field_name)) % 40) / 10
    return max(0.1, base_rate * interval_sec * (0.5 + _diurnal(hour)) + rng.random())


def _le_value_sort_key(value: str) -> tuple[int, float, str]:
    """Sort key for a histogram ``le`` boundary.

    Numeric boundaries order naturally; ``+Inf`` (Prometheus' open top bucket)
    sorts last; anything non-numeric sorts after numbers but before ``+Inf`` so
    ordering is always total and deterministic.
    """
    text = str(value).strip()
    if text in {"+Inf", "Inf", "inf", "+inf"}:
        return (2, float("inf"), text)
    try:
        return (0, float(text), text)
    except ValueError:
        return (1, 0.0, text)


def _sorted_le_values(combinations: list[dict[str, str]]) -> list[str]:
    values = {combo["le"] for combo in combinations if "le" in combo}
    return sorted(values, key=_le_value_sort_key)


def _le_rank(value: str, le_order: list[str]) -> tuple[int, int]:
    """Return ``(1-based rank, total)`` of *value* within *le_order*."""
    if not le_order:
        return (0, 0)
    try:
        return (le_order.index(value) + 1, len(le_order))
    except ValueError:
        return (len(le_order), len(le_order))


_GIB = 1 << 30


@dataclasses.dataclass(frozen=True)
class ValueProfile:
    """Plausible value band for a gauge metric, derived from its name."""

    base: float
    span: float
    unit: str


def _value_profile(field_name: str) -> ValueProfile:
    """Classify a metric name into a physically plausible value band.

    First match wins. Unknown names fall back to the legacy band so existing
    seeded values for unrecognised metrics do not drift.
    """
    name = field_name.lower()
    salt = abs(hash(field_name))
    if name.endswith("_bytes") or name.endswith("_bytes_total"):
        # 8-64 GiB, stable per metric.
        return ValueProfile(base=float((8 + salt % 56) * _GIB), span=2.0 * _GIB, unit="bytes")
    if any(tok in name for tok in ("load1", "load5", "load15")):
        return ValueProfile(base=float(salt % 4), span=2.0, unit="load")
    if name.endswith("_celsius") or "_temp" in name:
        return ValueProfile(base=float(20 + salt % 40), span=10.0, unit="temperature")
    if name.endswith("_seconds") or "time_seconds" in name or "_timestamp" in name:
        # Band is applied relative to the document timestamp in _gauge_value.
        return ValueProfile(base=0.0, span=0.0, unit="epoch_seconds")
    if name.endswith("_percent"):
        return ValueProfile(base=float(salt % 60), span=20.0, unit="ratio")
    if name.endswith("_ratio") or "utilization" in name:
        return ValueProfile(base=(salt % 60) / 100.0, span=0.2, unit="ratio")
    return ValueProfile(base=float(10 + salt % 500), span=25.0, unit="generic")


def _gauge_value(
    field_name: str,
    hour: float,
    combo_idx: int,
    rng: random.Random,
    *,
    ceiling: float | None = None,
    now_epoch: float | None = None,
) -> float:
    profile = _value_profile(field_name)
    if profile.unit == "epoch_seconds":
        # Anchor near the document timestamp so sibling differences (now - boot)
        # are small positive uptimes. Deterministic per (field, combo).
        anchor = now_epoch if now_epoch is not None else 0.0
        offset = (abs(hash(field_name)) % (90 * 86400)) + combo_idx * 3600
        value = anchor - offset + 60 * _diurnal(hour)
        if ceiling is not None:
            value = min(value, ceiling)
        return value
    base = profile.base
    value = base + combo_idx * 3 + profile.span * _diurnal(hour) + rng.random()
    if ceiling is not None:
        # Keep a ratio numerator strictly below its denominator while preserving
        # a diurnal swing, so e.g. "used / total" stays a believable utilisation.
        fraction = 0.4 + 0.5 * _diurnal(hour)
        return min(value, ceiling * fraction)
    return value


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
    # Each document contributes two NDJSON lines (action + source), so the batch
    # holds this many docs. We use it to reconcile the per-item results below.
    attempted = len(lines) // 2
    items = result.get("items", []) if isinstance(result, dict) else []

    if not items:
        # The bulk request failed as a whole (HTTP 4xx/5xx -> error envelope, or
        # an empty body) and returned no per-item results. Counting only
        # ``items`` here would silently drop the batch and let the seed report
        # success while no data landed.
        if attempted > 1:
            # Most whole-batch failures are payload-size (413) or transient
            # throttling (429); both clear when the batch is smaller. Split and
            # retry so the data still lands instead of being written off.
            mid = (attempted // 2) * 2  # split on a doc boundary (2 lines/doc)
            _flush_into_summary(lines[:mid], request, summary)
            _flush_into_summary(lines[mid:], request, summary)
            return
        # A single document that still fails is a real, unrecoverable error.
        summary.errors += attempted
        if attempted and len(summary.error_samples) < 3:
            reason = ""
            if isinstance(result, dict):
                err = result.get("error")
                reason = (err.get("reason") if isinstance(err, dict) else str(err)) or ""
            summary.error_samples.append((reason or "bulk request returned no items")[:240])
        return

    ok = errors = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        operation = item.get("create") or item.get("index") or {}
        error = operation.get("error") if isinstance(operation, dict) else None
        if error:
            errors += 1
            if len(summary.error_samples) < 3:
                if isinstance(error, dict):
                    sample = str(error.get("reason") or error)
                else:
                    sample = str(error)
                summary.error_samples.append(sample[:240])
        else:
            ok += 1
    # Reconcile: if the server returned fewer items than we sent (truncated /
    # partial response), the missing docs are failures, not successes.
    missing = max(0, attempted - (ok + errors))
    summary.ok += ok
    summary.errors += errors + missing
    if missing and len(summary.error_samples) < 3:
        summary.error_samples.append(f"{missing} document(s) had no bulk result line")


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
    "purge_foreign_streams",
    "setup_templates_and_streams",
]
