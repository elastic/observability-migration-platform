# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""PromQL <-> ES|QL native-oracle parity (package-native, TLS-aware).

Lifted from scripts/parity_promql_esql_oracle.py so a pip-installed user can prove
translation correctness: run the emitted ES|QL and the original PromQL through
Elasticsearch's own native PROMQL command on the same data and diff per bucket.
ES traffic goes through the shared make_es_request adapter (honors resolve_tls).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

# Labels Prometheus/remote-write attach automatically; scrub so series keys match
# between the translated output and the native PROMQL output.
PROMETHEUS_ONLY_LABELS = frozenset(
    {"__name__", "instance", "job", "exported_instance", "exported_job", "cluster", "replica"}
)

# The translator rewrites well-known Prometheus labels to their OTel/ECS field names
# (e.g. ``job`` -> ``service.name``). Canonicalize the translated side back to the
# Prometheus names so series keys align with the native PROMQL output (and so the
# PROMETHEUS_ONLY_LABELS scrub applies symmetrically to both sides).
OTEL_TO_PROM_LABELS = {
    "service.name": "job",
    "service.instance.id": "instance",
    "k8s.namespace.name": "namespace",
    "k8s.pod.name": "pod",
    "host.name": "instance",
}


def _canonical_label(name: str) -> str:
    return OTEL_TO_PROM_LABELS.get(name, OTEL_TO_PROM_LABELS.get(name.lower(), name))


@dataclass
class SeriesKey:
    labels: tuple[tuple[str, str], ...]

    def __hash__(self) -> int:
        return hash(self.labels)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SeriesKey) and self.labels == other.labels

    def __repr__(self) -> str:
        return "{" + ", ".join(f"{k}={v}" for k, v in self.labels) + "}"


@dataclass
class Comparison:
    expr: str
    esql: str = ""
    feasibility: str = ""
    skipped_reason: str = ""
    translated_error: str = ""
    native_error: str = ""
    translated_series: int = 0
    native_series: int = 0
    common_series: int = 0
    compared_points: int = 0
    max_relative_error: float = 0.0
    mean_relative_error: float = 0.0
    notes: list[str] = field(default_factory=list)

    def verdict(self) -> str:
        if self.skipped_reason:
            return "SKIP"
        if self.translated_error or self.native_error:
            return "ERROR"
        if self.common_series == 0:
            return "FAIL"
        if self.compared_points == 0:
            return "FAIL"
        if self.max_relative_error <= 0.01:
            return "STRICT_PASS"
        if self.max_relative_error <= 0.05:
            return "FUZZY_PASS"
        return "SHAPE_PASS" if self.common_series else "FAIL"


def _drop_constants(
    raw: list[tuple[dict[str, str], list[tuple[float, float]]]],
) -> dict[SeriesKey, list[tuple[float, float]]]:
    raw = [({k: v for k, v in d.items() if k not in PROMETHEUS_ONLY_LABELS}, vs) for d, vs in raw]
    if not raw:
        return {}
    all_keys = set.intersection(*(set(d.keys()) for d, _ in raw)) if raw else set()
    constants = {k for k in all_keys if len({d[k] for d, _ in raw}) == 1}
    out: dict[SeriesKey, list[tuple[float, float]]] = {}
    for d, vs in raw:
        scrubbed = {k: v for k, v in d.items() if k not in constants}
        out[SeriesKey(tuple(sorted(scrubbed.items())))] = vs
    return out


def normalize_native(data: dict) -> dict[SeriesKey, list[tuple[float, float]]]:
    """Parse native PROMQL output: columns value/step/<labels>."""
    columns = [c["name"] for c in data.get("columns", [])]
    rows = data.get("values", [])
    if not columns or not rows:
        return {}
    value_idx = step_idx = None
    label_idxs: list[tuple[int, str]] = []
    for i, name in enumerate(columns):
        if name == "value" or name.endswith("_value"):
            value_idx = i
        elif name == "step":
            step_idx = i
        elif name != "_timeseries":
            label_idxs.append((i, name))
    if value_idx is None or step_idx is None:
        return {}
    bucket: dict[tuple[tuple[str, str], ...], list[tuple[float, float]]] = {}
    for row in rows:
        try:
            t = datetime.fromisoformat(str(row[step_idx]).replace("Z", "+00:00")).timestamp()
            v = float(row[value_idx]) if row[value_idx] is not None else None
        except (TypeError, ValueError):
            continue
        if v is None:
            continue
        labels = {name: str(row[idx]) for idx, name in label_idxs if row[idx] is not None}
        bucket.setdefault(tuple(sorted(labels.items())), []).append((t, v))
    return _drop_constants([(dict(k), v) for k, v in bucket.items()])


def normalize_translated(data: dict) -> dict[SeriesKey, list[tuple[float, float]]]:
    """Parse translated ES|QL output: metric col + time_bucket + label cols."""
    columns = [c["name"] for c in data.get("columns", [])]
    column_types = [c.get("type", "") for c in data.get("columns", [])]
    rows = data.get("values", [])
    if not columns or not rows:
        return {}
    numeric = {"double", "long", "integer", "float", "unsigned_long"}
    time_idx = None
    timeseries_idx = None
    candidates: list[int] = []
    explicit_labels: list[tuple[int, str]] = []
    for i, name in enumerate(columns):
        lname = name.lower()
        if "time_bucket" in lname or lname == "@timestamp":
            time_idx = i
            continue
        if lname == "_timeseries":
            # TS direct-gauge (STATS field = field BY TBUCKET) carries the series
            # dimensions here as a JSON label set instead of broken-out columns.
            timeseries_idx = i
            continue
        if lname.startswith("labels.") or lname.startswith("prometheus.labels."):
            label_name = _canonical_label(lname.split(".")[-1])
            if label_name not in PROMETHEUS_ONLY_LABELS:
                explicit_labels.append((i, label_name))
            continue
        if lname == "legend":
            continue
        candidates.append(i)
    metric_idx = None
    for i in candidates:
        lname = columns[i].lower()
        if lname == "computed_value" or lname.endswith("_value"):
            metric_idx = i
            break
    if metric_idx is None:
        for i in candidates:
            if column_types[i] in numeric:
                metric_idx = i
                break
    if metric_idx is None and candidates:
        metric_idx = candidates[0]
    if time_idx is None or metric_idx is None:
        return {}
    # Bare label columns (e.g. ``service.name``) are canonicalized to Prometheus names
    # and scrubbed symmetrically with the native side.
    label_idxs = list(explicit_labels)
    for i in candidates:
        if i == metric_idx:
            continue
        canon = _canonical_label(columns[i])
        if canon not in PROMETHEUS_ONLY_LABELS:
            label_idxs.append((i, canon))
    bucket: dict[tuple[tuple[str, str], ...], list[tuple[float, float]]] = {}
    for row in rows:
        try:
            t = datetime.fromisoformat(str(row[time_idx]).replace("Z", "+00:00")).timestamp()
            v = float(row[metric_idx]) if row[metric_idx] is not None else None
        except (TypeError, ValueError):
            continue
        if v is None:
            continue
        labels = {name: str(row[idx]) for idx, name in label_idxs if row[idx] is not None}
        if timeseries_idx is not None:
            labels.update(_decode_timeseries_labels(row[timeseries_idx]))
        bucket.setdefault(tuple(sorted(labels.items())), []).append((t, v))
    return _drop_constants([(dict(k), v) for k, v in bucket.items()])


def _decode_timeseries_labels(raw) -> dict[str, str]:
    """Extract comparable series labels from a TS ``_timeseries`` JSON cell.

    Canonicalizes OTel field names back to Prometheus names and scrubs the
    auto-attached PROMETHEUS_ONLY_LABELS so keys align with the native side.
    """
    if not raw:
        return {}
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    labels = payload.get("labels", payload) if isinstance(payload, dict) else {}
    out: dict[str, str] = {}
    for name, value in (labels or {}).items():
        canon = _canonical_label(str(name))
        if canon in PROMETHEUS_ONLY_LABELS or value is None:
            continue
        out[canon] = str(value)
    return out


def _project_to_subset(
    a: dict[SeriesKey, list[tuple[float, float]]],
    b: dict[SeriesKey, list[tuple[float, float]]],
) -> dict[SeriesKey, list[tuple[float, float]]]:
    """Re-aggregate ``a`` onto the label dimensions used by ``b`` (sum-align)."""
    if not a or not b:
        return a
    b_labels: set[str] = set()
    for key in b:
        for name, _ in key.labels:
            b_labels.add(name)
    projected: dict[SeriesKey, list[tuple[float, float]]] = {}
    summed: dict[SeriesKey, dict[float, float]] = {}
    for key, values in a.items():
        sub = tuple(sorted((n, v) for n, v in key.labels if n in b_labels))
        acc = summed.setdefault(SeriesKey(sub), {})
        for ts, val in values:
            acc[ts] = acc.get(ts, 0.0) + val
    for key, tsmap in summed.items():
        projected[key] = sorted(tsmap.items())
    return projected


def _bucket_align(series, step):
    return {key: {int(ts // step) * step: v for ts, v in vs} for key, vs in series.items()}


def compute_diff(a, b, step) -> tuple[int, float, float]:
    aa, bb = _bucket_align(a, step), _bucket_align(b, step)

    def trim(buckets):
        out = {}
        for k, m in buckets.items():
            if len(m) <= 2:
                continue
            keys = sorted(m)
            out[k] = {ts: m[ts] for ts in keys[1:-1]}
        return out

    ai, bi = trim(aa), trim(bb)
    rel: list[float] = []
    for key in set(ai) & set(bi):
        for bts, av in ai[key].items():
            bv = bi[key].get(bts)
            if bv is None:
                continue
            denom = max(abs(av), abs(bv), 1e-9)
            rel.append(abs(av - bv) / denom)
    return (
        len(rel),
        max(rel, default=0.0),
        (sum(rel) / len(rel)) if rel else 0.0,
    )


# PromQL constructs the native PROMQL command does not parse / we don't compare.
NATIVE_UNSUPPORTED = ("label_replace", "label_join")
