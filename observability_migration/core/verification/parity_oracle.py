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
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

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
    fail_reason: str = ""
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
    keep: frozenset[str] = frozenset(),
) -> dict[SeriesKey, list[tuple[float, float]]]:
    raw = [({k: v for k, v in d.items() if k not in PROMETHEUS_ONLY_LABELS or k in keep}, vs) for d, vs in raw]
    if not raw:
        return {}
    all_keys = set.intersection(*(set(d.keys()) for d, _ in raw)) if raw else set()
    constants = {k for k in all_keys if len({d[k] for d, _ in raw}) == 1}
    out: dict[SeriesKey, list[tuple[float, float]]] = {}
    for d, vs in raw:
        scrubbed = {k: v for k, v in d.items() if k not in constants}
        out[SeriesKey(tuple(sorted(scrubbed.items())))] = vs
    return out


def normalize_native(data: dict, keep_labels: frozenset[str] = frozenset()) -> dict[SeriesKey, list[tuple[float, float]]]:
    """Parse native PROMQL output: columns value/step/<labels>.

    Native PROMQL may return the series labels either as broken-out columns or
    packed into a single ``_timeseries`` JSON column (the TS form). Both must be
    decoded -- ignoring ``_timeseries`` collapses every grouped series into one
    empty-key series, which can never intersect the translated side (which does
    decode it), turning correct grouped panels into false FAILs.

    ``keep_labels`` exempts specific canonical label names from the
    PROMETHEUS_ONLY scrub (used when the translated side's legend extraction
    keys series by a normally-scrubbed label such as ``service.name``).
    """
    columns = [c["name"] for c in data.get("columns", [])]
    rows = data.get("values", [])
    if not columns or not rows:
        return {}
    value_idx = step_idx = timeseries_idx = None
    label_idxs: list[tuple[int, str]] = []
    for i, name in enumerate(columns):
        if name == "value" or name.endswith("_value"):
            value_idx = i
        elif name == "step":
            step_idx = i
        elif name == "_timeseries":
            timeseries_idx = i
        else:
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
        if timeseries_idx is not None:
            labels.update(_decode_timeseries_labels(row[timeseries_idx], keep=keep_labels))
        bucket.setdefault(tuple(sorted(labels.items())), []).append((t, v))
    return _drop_constants([(dict(k), v) for k, v in bucket.items()], keep=keep_labels)


def normalize_translated(
    data: dict,
    value_column: str | None = None,
    ignore_columns: frozenset[str] = frozenset(),
) -> dict[SeriesKey, list[tuple[float, float]]]:
    """Parse translated ES|QL output: metric col + time_bucket + label cols.

    ``value_column`` pins the metric column by name (per-target comparison of
    a merged multi-target panel); ``ignore_columns`` excludes the sibling
    targets' value columns so they are neither picked as the metric nor
    misread as series labels."""
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
        if name in ignore_columns:
            continue
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
    if value_column is not None:
        if value_column not in columns:
            return {}
        metric_idx = columns.index(value_column)
    if metric_idx is None:
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


def _flatten_label_payload(labels: dict, prefix: str = "") -> dict[str, str]:
    """Flatten nested label objects to dotted paths (``k8s.cluster.name``).

    ``_timeseries`` cells can carry nested OTel resource attributes; dotted
    paths keep them comparable with broken-out columns and with the
    translator's flattened label fallback (see ``_decode_label_blob``)."""
    out: dict[str, str] = {}
    for name, value in (labels or {}).items():
        path = f"{prefix}.{name}" if prefix else str(name)
        if isinstance(value, dict):
            out.update(_flatten_label_payload(value, path))
        elif value is not None:
            out[path] = str(value)
    return out


def _decode_timeseries_labels(raw, keep: frozenset[str] = frozenset()) -> dict[str, str]:
    """Extract comparable series labels from a TS ``_timeseries`` JSON cell.

    Flattens nested label objects to dotted paths, canonicalizes OTel field
    names back to Prometheus names and scrubs the auto-attached
    PROMETHEUS_ONLY_LABELS so keys align with the native side. Canonical names
    in ``keep`` are exempt from the scrub.
    """
    if not raw:
        return {}
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    labels = payload.get("labels", payload) if isinstance(payload, dict) else {}
    out: dict[str, str] = {}
    for name, value in _flatten_label_payload(labels).items():
        canon = _canonical_label(name)
        if canon in PROMETHEUS_ONLY_LABELS and canon not in keep:
            continue
        out[canon] = value
    return out


# A flattened ``_timeseries`` blob produced by the translator's label-extraction
# fallback usually starts with the metric name pair (``__name__`` sorts first),
# but some series omit ``__name__`` from ``_timeseries`` entirely.
_BLOB_SIGNATURE = "__name__:"


def _looks_like_label_blob(value: str) -> bool:
    """Heuristic for the flatten-fallback blob: ``name:value, name:value, ...``.

    Either the canonical ``__name__:`` prefix, or a multi-pair shape where
    every comma-separated part carries a colon. Decoding additionally requires
    an anchor match against native label names (see ``_decode_label_blob``),
    so a colon-bearing real label value cannot be misread as a blob."""
    if value.startswith(_BLOB_SIGNATURE):
        return True
    if ", " not in value:
        return False
    return all(":" in part for part in value.split(", "))


def _flattened_label_names(raw: dict) -> set[str]:
    """All (dotted, pre-canonicalization) label names a native response carries.

    Used to resolve the colon ambiguity when decoding a flattened label blob:
    ``instance:host:9100`` is the label ``instance`` with a colon-bearing
    value, while ``k8s:cluster:name:c1`` is the nested path
    ``k8s.cluster.name`` - only the names observed on the native side can
    tell the two apart."""
    names: set[str] = set()
    columns = [c["name"] for c in (raw or {}).get("columns", [])]
    ts_idx = None
    for i, name in enumerate(columns):
        if name == "_timeseries":
            ts_idx = i
        elif name not in {"value", "step"} and not name.endswith("_value"):
            names.add(name)
    if ts_idx is not None:
        for row in (raw or {}).get("values", []):
            cell = row[ts_idx]
            if not cell:
                continue
            try:
                payload = json.loads(cell) if isinstance(cell, str) else cell
            except (TypeError, ValueError):
                continue
            labels = payload.get("labels", payload) if isinstance(payload, dict) else {}
            names.update(_flatten_label_payload(labels).keys())
    return names


def _decode_label_blob(blob: str, label_names: set[str]) -> dict[str, str] | None:
    """Decode a flattened ``_timeseries`` blob back into label pairs.

    The translator's fallback strips ``{}"`` from the ``_timeseries`` JSON and
    spaces the commas: ``{"a":"b","k8s":{"cluster":{"name":"x"}}}`` becomes
    ``a:b, k8s:cluster:name:x``. Known native label names (colon-joined for
    nested paths) anchor each item; unmatched items fall back to splitting on
    the first colon. Returns None when the text does not look like a blob, or
    when no item is anchored by a native label name (a colon-bearing real
    label value must not be misread as a blob)."""
    if not _looks_like_label_blob(blob):
        return None
    anchors = sorted((name.replace(".", ":") for name in label_names), key=len, reverse=True)
    decoded: dict[str, str] = {}
    anchored = 0
    for part in blob.split(", "):
        if ":" not in part:
            return None
        name = value = None
        for anchor in anchors:
            if part.startswith(anchor + ":"):
                name = anchor.replace(":", ".")
                value = part[len(anchor) + 1:]
                anchored += 1
                break
        if name is None:
            name, _, value = part.partition(":")
        decoded[name] = value
    if not anchored and not blob.startswith(_BLOB_SIGNATURE):
        return None
    return decoded


def _align_blob_label_keys(
    translated: dict[SeriesKey, list[tuple[float, float]]],
    native_raw: dict,
) -> tuple[dict[SeriesKey, list[tuple[float, float]]], bool]:
    """Re-key translated series whose label values are flattened blobs.

    When a panel's grouping label is absent from the data, the translated
    query's label-extraction fallback emits the whole flattened
    ``_timeseries`` JSON as the group value, so its series keys can never
    intersect the native side's decoded labels (a false compared_points=0
    FAIL). Decode the blob back into real label pairs - canonicalized and
    scrubbed like any other label set - so per-series comparison works.
    Returns ``(series, decoded_any)``."""
    if not any(
        _looks_like_label_blob(value)
        for key in translated
        for _, value in key.labels
    ):
        return translated, False
    label_names = _flattened_label_names(native_raw)
    rebuilt: list[tuple[dict[str, str], list[tuple[float, float]]]] = []
    decoded_any = False
    for key, values in translated.items():
        labels: dict[str, str] = {}
        for name, value in key.labels:
            decoded = _decode_label_blob(value, label_names) if _looks_like_label_blob(value) else None
            if decoded is None:
                labels[name] = value
                continue
            decoded_any = True
            for blob_name, blob_value in decoded.items():
                canon = _canonical_label(blob_name)
                if canon not in PROMETHEUS_ONLY_LABELS:
                    labels[canon] = blob_value
        rebuilt.append((labels, values))
    if not decoded_any:
        return translated, False
    return _drop_constants(rebuilt), True


def _label_values_by_name(series: dict[SeriesKey, list[tuple[float, float]]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for key in series:
        for name, value in key.labels:
            out.setdefault(name, set()).add(value)
    return out


def _rename_labels(
    series: dict[SeriesKey, list[tuple[float, float]]],
    renames: dict[str, str],
) -> dict[SeriesKey, list[tuple[float, float]]]:
    rebuilt: dict[SeriesKey, list[tuple[float, float]]] = {}
    for key, values in series.items():
        labels = tuple(sorted((renames.get(n, n), v) for n, v in key.labels))
        rebuilt[SeriesKey(labels)] = values
    return rebuilt


def _filter_series_by_label(
    series: dict[SeriesKey, list[tuple[float, float]]],
    label: str,
    value: str,
) -> dict[SeriesKey, list[tuple[float, float]]]:
    """Scope a translated response to one target of a same-metric collapse.

    Keeps only series whose key carries ``(label, value)`` - the raw or
    canonical label name both match - and drops that label from the kept keys
    so they can align with a native side that ran the sub-query with the
    matcher applied (where the label is constant and already scrubbed)."""
    names = {label, _canonical_label(label)}
    out: dict[SeriesKey, list[tuple[float, float]]] = {}
    for key, values in series.items():
        labels = dict(key.labels)
        matched = False
        for name in list(labels):
            if name in names and labels[name] == value:
                matched = True
                labels.pop(name)
        if matched:
            out[SeriesKey(tuple(sorted(labels.items())))] = values
    return out


def _align_label_names_by_values(
    native: dict[SeriesKey, list[tuple[float, float]]],
    translated: dict[SeriesKey, list[tuple[float, float]]],
) -> tuple[dict[SeriesKey, list[tuple[float, float]]], dict[str, str]]:
    """Rename translated label names whose value sets exactly match one native label's.

    The translated legend extraction names its output column after the panel's
    label (e.g. ``name``); on data where that label is absent, the extracted
    values can come from a differently-named native label (the extraction regex
    scans the whole ``_timeseries`` JSON). When the values prove the
    correspondence - exact value-set equality, unique on both sides - rename
    the translated label so per-series comparison can proceed.
    Returns ``(series, renames)``."""
    native_values = _label_values_by_name(native)
    translated_values = _label_values_by_name(translated)
    renames: dict[str, str] = {}
    for tname, tset in translated_values.items():
        if tname in native_values:
            continue
        candidates = [n for n, s in native_values.items() if s == tset and n not in translated_values]
        if len(candidates) == 1:
            renames[tname] = candidates[0]
    if not renames:
        return translated, {}
    return _rename_labels(translated, renames), renames


def _raw_label_values(raw: dict) -> dict[str, set[str]]:
    """Pre-scrub label name -> value set for a raw native response.

    Includes the PROMETHEUS_ONLY labels the normalizers scrub, so the
    comparator can recognize a translated legend label that extracted values
    from one of them (e.g. ``service.name``)."""
    out: dict[str, set[str]] = {}
    columns = [c["name"] for c in (raw or {}).get("columns", [])]
    ts_idx = None
    label_cols: list[tuple[int, str]] = []
    for i, name in enumerate(columns):
        if name == "_timeseries":
            ts_idx = i
        elif name not in {"value", "step"} and not name.endswith("_value"):
            label_cols.append((i, name))
    for row in (raw or {}).get("values", []):
        for i, name in label_cols:
            if row[i] is not None:
                out.setdefault(name, set()).add(str(row[i]))
        if ts_idx is not None and row[ts_idx]:
            try:
                payload = json.loads(row[ts_idx]) if isinstance(row[ts_idx], str) else row[ts_idx]
            except (TypeError, ValueError):
                continue
            labels = payload.get("labels", payload) if isinstance(payload, dict) else {}
            for name, value in _flatten_label_payload(labels).items():
                out.setdefault(name, set()).add(value)
    return out


def _align_translated_to_scrubbed_native_label(
    native_raw: dict,
    translated: dict[SeriesKey, list[tuple[float, float]]],
) -> tuple[dict[SeriesKey, list[tuple[float, float]]], dict[SeriesKey, list[tuple[float, float]]] | None, dict[str, str]]:
    """Second-chance alignment: key both sides by a normally-scrubbed label.

    The legend extraction regex scans the whole ``_timeseries`` JSON and can
    capture a label the comparator scrubs (e.g. ``service.name``, canonicalized
    to ``job``). The extracted values still map 1:1 to series, so when a
    translated label's value set exactly matches one scrubbed native label,
    re-normalize the native side keeping that label and rename the translated
    label to it. Returns ``(translated, native_or_None, renames)``."""
    raw_values = _raw_label_values(native_raw)
    translated_values = _label_values_by_name(translated)
    renames: dict[str, str] = {}
    keep: set[str] = set()
    for tname, tset in translated_values.items():
        candidates = sorted(
            {_canonical_label(n) for n, s in raw_values.items()
             if s == tset and _canonical_label(n) in PROMETHEUS_ONLY_LABELS}
        )
        if len(candidates) == 1:
            renames[tname] = candidates[0]
            keep.add(candidates[0])
    if not renames:
        return translated, None, {}
    native = normalize_native(native_raw, keep_labels=frozenset(keep))
    return _rename_labels(translated, renames), native, renames


# A pure string-literal ``| EVAL <name> = "..."`` stage: the translated form of
# a static Grafana legend. On multi-series data the constant label collapses
# every underlying series into one interleaved stream.
_STATIC_LEGEND_EVAL_RE = re.compile(
    r'\|\s*EVAL\s+[A-Za-z_][A-Za-z0-9_]*\s*=\s*"(?:[^"\\]|\\.)*"\s*(?:$|[\n|])'
)


def _has_static_legend_label(esql: str) -> bool:
    return bool(_STATIC_LEGEND_EVAL_RE.search(esql or ""))


def _project_to_subset(
    a: dict[SeriesKey, list[tuple[float, float]]],
    b: dict[SeriesKey, list[tuple[float, float]]],
    reducer: str = "sum",
) -> dict[SeriesKey, list[tuple[float, float]]]:
    """Re-aggregate ``a`` onto the label dimensions used by ``b``.

    ``reducer`` must match the outer aggregation the translated query applied
    when it grouped by those labels. Summing native series onto a label subset
    that the translated query AVERAGED reads N* too high (N = native series
    collapsing into one subset key), which is the dominant source of false
    SHAPE_PASS-at-~0.99 verdicts on grouped gauge panels.
    """
    if not a or not b:
        return a
    b_labels: set[str] = set()
    for key in b:
        for name, _ in key.labels:
            b_labels.add(name)
    grouped: dict[SeriesKey, dict[float, list[float]]] = {}
    for key, values in a.items():
        sub = tuple(sorted((n, v) for n, v in key.labels if n in b_labels))
        acc = grouped.setdefault(SeriesKey(sub), {})
        for ts, val in values:
            acc.setdefault(ts, []).append(val)
    projected: dict[SeriesKey, list[tuple[float, float]]] = {}
    for key, tsmap in grouped.items():
        projected[key] = sorted((ts, _reduce_values(vals, reducer)) for ts, vals in tsmap.items())
    return projected


def _reduce_values(values: list[float], reducer: str) -> float:
    if not values:
        return 0.0
    if reducer == "avg":
        return sum(values) / len(values)
    if reducer == "max":
        return max(values)
    if reducer == "min":
        return min(values)
    return sum(values)


# Outer aggregation in the emitted ES|QL ``| STATS <alias> = <AGG>(...) BY ...``.
# Determines how native series must be collapsed when projecting onto the
# translated label subset so the comparison is apples-to-apples.
_TRANSLATED_REDUCER_RE = re.compile(
    r"\|\s*STATS\s+[A-Za-z_][A-Za-z0-9_.]*\s*=\s*(?P<agg>AVG|SUM|MAX|MIN|COUNT)\s*\(",
    re.IGNORECASE,
)


def _translated_reducer(esql: str) -> str:
    """Return the outer STATS aggregation ('sum' default) of an ES|QL query."""
    match = _TRANSLATED_REDUCER_RE.search(esql or "")
    if not match:
        return "sum"
    agg = match.group("agg").lower()
    return agg if agg in {"avg", "max", "min", "sum"} else "sum"


def _is_promql_passthrough(esql: str) -> bool:
    """True when the translated query's leading command is native ``PROMQL``.

    These queries return native-shaped columns (``value``/``step``/
    ``_timeseries``), so the comparison must decode them with ``normalize_native``
    rather than the ES|QL ``normalize_translated`` (which keys off ``time_bucket``).
    """
    return bool(re.match(r"^\s*PROMQL\b", esql or "", re.IGNORECASE))


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

# Grafana range/interval macros -> a concrete duration the native PROMQL parser
# accepts. The oracle only needs a *runnable* window; exact width does not change
# whether the translated and native series line up (both use the same seeded data
# over the same compare window), so a single sensible default is fine.
_DEFAULT_RANGE = "5m"
_RANGE_MACRO_RE = re.compile(
    r"\$__rate_interval|\$__interval|\$__range|\$__auto_interval_\w+|\$interval", re.IGNORECASE
)
# A ``[ ... ]`` range selector whose contents are not a plain duration (i.e. it
# embeds a template variable like ``[$myrange]`` or a subquery ``[$r:$s]``).
_VAR_RANGE_SELECTOR_RE = re.compile(r"\[\s*\$[^\]]*\]")
# One label matcher inside a ``{...}`` selector: name (op) "value".
_MATCHER_RE = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<op>=~|!~|!=|=)\s*\"(?P<val>(?:[^\"\\]|\\.)*)\""
)


def _strip_variable_matchers(expr: str) -> str:
    """Drop label matchers whose value contains a Grafana ``$variable``.

    ``apache_uptime{job="$job", instance="$instance"}`` matches no seeded series
    because nothing has a label equal to the literal ``$job``. The translated
    side carries no such filter, so the faithful oracle comparison is against the
    metric with the variable matchers removed (static matchers are preserved).
    Selectors left empty collapse to the bare metric name.
    """
    out: list[str] = []
    pos = 0
    for brace in re.finditer(r"\{[^{}]*\}", expr):
        out.append(expr[pos:brace.start()])
        inner = brace.group(0)[1:-1]
        kept = [
            m.group(0)
            for m in _MATCHER_RE.finditer(inner)
            if "$" not in m.group("val")
        ]
        out.append("{" + ", ".join(kept) + "}" if kept else "")
        pos = brace.end()
    out.append(expr[pos:])
    return "".join(out)


# A translated panel that ends by collapsing every bucket into a single row
# (Grafana stat / single-value panel): ``STATS time_bucket = MAX(time_bucket), ...``.
# Its output is one scalar, so there is no time series to diff against the native
# range vector -- comparing point-wise is meaningless and produces a false FAIL.
_SINGLE_VALUE_REDUCTION_RE = re.compile(
    r"STATS\s+time_bucket\s*=\s*(?:MAX|MIN|LAST|FIRST|AVG|SUM)\s*\(\s*time_bucket\s*\)",
    re.IGNORECASE,
)

_STATS_BY_RE = re.compile(r"^\s*STATS\b(?P<body>.*)$", re.IGNORECASE | re.DOTALL)


def _stats_groups_by_time_bucket(stats_command: str) -> bool:
    """True when a single ``STATS ...`` command groups ``BY`` a clause that
    contains ``time_bucket`` (i.e. it still yields a per-bucket time series).
    A ``STATS`` with no ``BY`` at all, or a ``BY`` over dimensions only, does
    not — it produces a single row per group (a stat snapshot)."""
    match = _STATS_BY_RE.match(stats_command.strip())
    if not match:
        return False
    body = match.group("body")
    # The ``BY`` keyword splits aggregations from grouping; only the grouping
    # side establishes the series shape. Match ``BY`` as a standalone keyword.
    by_split = re.split(r"\bBY\b", body, maxsplit=1, flags=re.IGNORECASE)
    if len(by_split) < 2:
        return False  # no BY clause -> collapses to one row
    grouping = by_split[1]
    return bool(re.search(r"\btime_bucket\b", grouping, re.IGNORECASE))


def _split_top_level_commas(text: str) -> list[str]:
    """Split on commas that are not nested inside parentheses."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


_SCALAR_ASSIGN_RE = re.compile(
    r"^(?P<alias>`[^`]+`|[A-Za-z_][\w.]*)\s*=\s*(?P<func>MAX|LAST)\s*\(\s*(?P<arg>[^)]*)\)$",
    re.IGNORECASE,
)


def _terminal_scalar_reduction(esql: str) -> dict[str, str] | None:
    """Parse a terminal stat reduction the oracle can mirror on the native side.

    Supported shapes (per column): ``alias = MAX(alias)`` (window max -
    Grafana's dominant stat reduction, including the ``time_bucket =
    MAX(time_bucket)`` companion) and ``alias = LAST(alias, time_bucket)``
    (value at the latest bucket). Anything else - COUNT/COUNT_DISTINCT over
    rows, scalar arithmetic, grouped tables (``BY <dim>``) - returns None so
    the honest single-value SKIP applies; a false FAIL from an approximated
    reduction would be worse."""
    stages = [stage.strip() for stage in (esql or "").split("|")]
    stats_stages = [stage for stage in stages if re.match(r"(?i)^STATS\b", stage)]
    if not stats_stages:
        return None
    terminal = stats_stages[-1]
    body_match = _STATS_BY_RE.match(terminal)
    if not body_match:
        return None
    body = body_match.group("body")
    if re.search(r"\bBY\b", body, re.IGNORECASE):
        return None
    reductions: dict[str, str] = {}
    for part in _split_top_level_commas(body):
        match = _SCALAR_ASSIGN_RE.match(part)
        if not match:
            return None
        alias = match.group("alias").strip("`")
        func = match.group("func").upper()
        args = [a.strip().strip("`") for a in match.group("arg").split(",")]
        if func == "MAX":
            if args != [alias]:
                return None
            reductions[alias] = "max"
        else:
            if args != [alias, "time_bucket"]:
                return None
            reductions[alias] = "last"
    value_columns = [name for name in reductions if name != "time_bucket"]
    if not value_columns:
        return None
    return reductions


def _compare_single_value(
    cmp_: Comparison,
    native_raw: dict,
    translated_raw: dict,
    reductions: dict[str, str],
    value_column: str | None,
) -> Comparison:
    """Scalar comparison for stat panels: reduce the native series with the
    same terminal reducer the translated query applied and diff the scalars."""
    fallback = "translated panel reduces to a single value (stat panel); no time series to compare"
    columns = [c["name"] for c in translated_raw.get("columns", [])]
    rows = translated_raw.get("values", [])
    value_columns = [name for name in reductions if name != "time_bucket"]
    target_column = value_column or (value_columns[0] if len(value_columns) == 1 else None)
    if target_column is None or target_column not in columns or len(rows) != 1:
        cmp_.skipped_reason = fallback
        return cmp_
    translated_value = rows[0][columns.index(target_column)]
    native = normalize_native(native_raw)
    cmp_.native_series = len(native)
    cmp_.translated_series = 1 if translated_value is not None else 0
    if translated_value is None and not native:
        cmp_.skipped_reason = (
            "no data on either side in the compare window; seed sample data or "
            "align --window-minutes/--step-seconds with the dashboard"
        )
        return cmp_
    if not native:
        cmp_.skipped_reason = (
            "native oracle returned no series; translated returned a scalar - "
            "no reference data to verify against"
        )
        return cmp_
    if translated_value is None:
        cmp_.fail_reason = "translated stat query returned no value; native returned data"
        return cmp_
    # Multiple native series first collapse with the inner per-bucket reducer
    # (mirrors what the translated pipeline did before its terminal stage).
    if len(native) > 1:
        inner = _translated_reducer(cmp_.esql)
        native = _project_to_subset(native, {SeriesKey(()): [(0.0, 0.0)]}, reducer=inner)
        cmp_.notes.append(f"native series collapsed with inner reducer ({inner}) before scalar reduction")
    points = sorted(p for series in native.values() for p in series)
    if not points:
        cmp_.skipped_reason = fallback
        return cmp_
    reducer = reductions[target_column]
    native_value = points[-1][1] if reducer == "last" else max(v for _, v in points)
    cmp_.common_series = 1
    cmp_.compared_points = 1
    translated_value = float(translated_value)
    cmp_.max_relative_error = abs(native_value - translated_value) / max(
        abs(native_value), abs(translated_value), 1e-9
    )
    cmp_.mean_relative_error = cmp_.max_relative_error
    cmp_.notes.append(
        f"single-value comparison ({reducer}): translated {translated_value} "
        f"vs native {native_value}"
    )
    return cmp_


def is_single_value_reduction(esql: str) -> bool:
    """True when the emitted ES|QL reduces the series to a single (stat) value
    instead of a per-time-bucket range series.

    Grafana stat / single-stat / gauge panels translate to ES|QL whose terminal
    aggregation drops the time dimension — e.g. a trailing
    ``STATS time_bucket = MAX(time_bucket)`` collapse, a bare
    ``STATS m = COUNT_DISTINCT(cpu)`` cardinality, a ``time()``-style
    ``DATE_DIFF(..., NOW())`` uptime scalar, or a per-bucket STATS followed by a
    terminal ``STATS ... BY <dimension>`` (no ``time_bucket``). There is no time
    series to diff point-wise against the native range vector, so the oracle must
    SKIP rather than emit a false FAIL.

    The shape rule: split the pipeline into ``|`` stages; if there is at least
    one ``STATS`` stage and the *last* one does not group ``BY time_bucket``, the
    result is a single value (per group). Queries with no ``STATS`` at all are
    left to point-wise comparison.
    """
    text = esql or ""
    # ``ROW constant_value = 2.0`` (a constant PromQL panel) emits a single
    # literal row with no time dimension at all.
    if re.match(r"(?i)^\s*ROW\b", text):
        return True
    if _SINGLE_VALUE_REDUCTION_RE.search(text):
        return True
    stages = [stage.strip() for stage in text.split("|")]
    stats_stages = [stage for stage in stages if re.match(r"(?i)^STATS\b", stage)]
    if not stats_stages:
        return False
    # The terminal aggregation governs the output shape.
    return not _stats_groups_by_time_bucket(stats_stages[-1])


def sanitize_source_for_oracle(expr: str, step: int) -> str:
    """Make a Grafana source PromQL expression runnable by native PROMQL.

    Grafana panel queries embed template variables (``$job``, ``$node``) and
    range macros (``$__rate_interval``) that Grafana interpolates at view time.
    Fed verbatim to native PROMQL they either fail to parse or match zero series,
    which would make every templated panel an unwinnable FAIL regardless of
    translation quality. Normalize them so the oracle exercises the same data the
    translated ES|QL does:

    * variable-valued label matchers are dropped (static ones preserved);
    * range/interval macros and ``[$var]`` selectors become a concrete duration;
    * any residual bare ``$var`` is removed defensively.
    """
    if "$" not in expr:
        return expr
    result = _strip_variable_matchers(expr)
    result = _RANGE_MACRO_RE.sub(_DEFAULT_RANGE, result)
    result = _VAR_RANGE_SELECTOR_RE.sub(f"[{_DEFAULT_RANGE}]", result)
    # Any leftover ${var} / $var not inside a matcher (e.g. used as a scalar):
    # remove it so the expression at least parses. Capture-group backrefs ($1)
    # and the special $__ macros have already been handled above.
    result = re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::[^}]*)?\}", "", result)
    result = re.sub(r"\$(?!\d)[A-Za-z_][A-Za-z0-9_]*", "", result)
    return result


def _run_query(request, query: str, params: list | None = None) -> dict:
    body: dict = {"query": query}
    if params is not None:
        body["params"] = params
    return request("POST", "/_query?format=json", body, "application/json")


def run_translated(request, esql: str, tstart: str, tend: str) -> dict:
    return _run_query(request, esql, params=[{"_tstart": tstart}, {"_tend": tend}])


def run_native_promql(request, expr: str, index: str, step: int, start_iso: str, end_iso: str) -> dict:
    query = f'PROMQL index={index} step={step}s start="{start_iso}" end="{end_iso}" value=({expr})'
    return _run_query(request, query)


def native_promql_available(request, index: str) -> bool:
    """Probe whether the target ES supports the native PROMQL command."""
    end = datetime.now(UTC)
    start = end - timedelta(minutes=5)
    res = run_native_promql(
        request, "1", index, 60,
        start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z"),
    )
    return not (isinstance(res, dict) and res.get("error"))


def compare_panel(request, *, source_query: str, translated_query: str, index: str,
                  step: int, start_iso: str, end_iso: str,
                  translated_value_column: str | None = None,
                  translated_ignore_columns: frozenset[str] = frozenset(),
                  translated_label_filter: tuple[str, str] | None = None) -> Comparison:
    """Compare an emitted ES|QL panel query against native PROMQL of its source.

    ``translated_value_column``/``translated_ignore_columns`` scope the
    comparison to one output column of a merged multi-target panel (per-target
    provenance), ignoring the sibling targets' value columns.
    ``translated_label_filter`` scopes it to one label value of a same-metric
    collapsed panel instead (targets map to BY-column values there).

    In-band ES errors fail closed (SKIP for native, ERROR for translated). Transport
    failures are NOT caught here: a NetworkError from the injected ``request`` (e.g. an
    unreachable cluster) propagates to the caller, which handles it at the CLI boundary.
    """
    cmp_ = Comparison(expr=source_query, esql=(translated_query or "").strip())
    if not cmp_.esql:
        cmp_.skipped_reason = "no translated ES|QL on this panel"
        return cmp_
    if " ||| " in source_query:
        # Multi-target panels join sub-queries with " ||| " (panels.py) and
        # translate to ONE merged ES|QL whose columns can reorder or drop
        # sub-queries; without per-target provenance there is nothing the
        # single-query oracle can honestly compare.
        sub_queries = source_query.count(" ||| ") + 1
        cmp_.skipped_reason = (
            f"multi-query panel ({sub_queries} sub-queries merged into one ES|QL); "
            "the native oracle compares single queries, and per-target comparison "
            "requires per-target provenance in the verification packet"
        )
        return cmp_
    if any(tok in source_query for tok in NATIVE_UNSUPPORTED):
        cmp_.skipped_reason = "native PROMQL oracle does not support this construct"
        return cmp_
    scalar_reductions = None
    if is_single_value_reduction(cmp_.esql):
        # Mirrorable terminal reductions (window MAX / latest-bucket LAST) are
        # compared as scalars below; everything else keeps the honest SKIP.
        scalar_reductions = _terminal_scalar_reduction(cmp_.esql)
        if scalar_reductions is None:
            cmp_.skipped_reason = "translated panel reduces to a single value (stat panel); no time series to compare"
            return cmp_

    # Strip Grafana template vars / range macros so native PROMQL runs against the
    # same series the translated ES|QL does (a literal ``$job`` matches nothing).
    native_query = sanitize_source_for_oracle(source_query, step)
    if native_query != source_query:
        cmp_.notes.append("source sanitized for oracle (template vars / range macros resolved)")
    native_raw = run_native_promql(request, native_query, index, step, start_iso, end_iso)
    if isinstance(native_raw, dict) and native_raw.get("error"):
        cmp_.skipped_reason = f"native PROMQL could not run: {str(native_raw['error'])[:120]}"
        return cmp_

    translated_raw = run_translated(request, cmp_.esql, start_iso, end_iso)
    if isinstance(translated_raw, dict) and translated_raw.get("error"):
        cmp_.translated_error = str(translated_raw["error"])[:200]
        return cmp_

    if scalar_reductions is not None:
        return _compare_single_value(
            cmp_, native_raw, translated_raw, scalar_reductions, translated_value_column
        )

    native = normalize_native(native_raw)
    # A translated query that is itself a native ``PROMQL ...`` command (the
    # native-passthrough degrade path) emits native-shaped ``step``/``value``/
    # ``_timeseries`` columns rather than ES|QL ``time_bucket``. Parsing it with
    # normalize_translated yields 0 series (no time_bucket) -> a false cmp=0 FAIL,
    # so decode it with the native parser instead.
    if _is_promql_passthrough(cmp_.esql):
        translated = normalize_native(translated_raw)
        cmp_.notes.append("translated query is a native PROMQL passthrough; parsed with the native normalizer")
    else:
        translated = normalize_translated(
            translated_raw,
            value_column=translated_value_column,
            ignore_columns=translated_ignore_columns,
        )
    translated, blob_decoded = _align_blob_label_keys(translated, native_raw)
    if blob_decoded:
        cmp_.notes.append(
            "translated label fallback blob decoded into label pairs (panel grouping "
            "label absent from the data); series keys re-aligned with the native side"
        )
    if translated_label_filter and translated:
        translated = _filter_series_by_label(translated, *translated_label_filter)
        cmp_.notes.append(
            f"translated response scoped to {translated_label_filter[0]}="
            f"{translated_label_filter[1]!r} (same-metric collapsed target)"
        )
    if native and translated and not (set(native) & set(translated)):
        translated, renames = _align_label_names_by_values(native, translated)
        if renames:
            cmp_.notes.append(
                "translated label name(s) re-aligned by matching value sets: "
                + ", ".join(f"{t}->{n}" for t, n in sorted(renames.items()))
            )
    if native and translated and not (set(native) & set(translated)):
        translated, rekeyed_native, renames = _align_translated_to_scrubbed_native_label(
            native_raw, translated
        )
        if rekeyed_native is not None:
            native = rekeyed_native
            cmp_.notes.append(
                "translated legend label(s) carry values of normally-scrubbed native "
                "label(s); both sides re-keyed by them: "
                + ", ".join(f"{t}->{n}" for t, n in sorted(renames.items()))
            )
    cmp_.native_series = len(native)
    cmp_.translated_series = len(translated)
    if not native and not translated:
        # Nothing was compared on either side; FAIL would read as a translation
        # defect when the only proven fact is the absence of data.
        cmp_.skipped_reason = (
            "no data on either side in the compare window; seed sample data or "
            "align --window-minutes/--step-seconds with the dashboard"
        )
        return cmp_
    if cmp_.translated_series == 1 and cmp_.native_series > 1 and _has_static_legend_label(cmp_.esql):
        # A static legend collapses every underlying series into one
        # interleaved stream; any per-series (or projected) diff against it
        # measures the collapse, not the translation.
        cmp_.skipped_reason = (
            f"translated query assigns a static legend label, collapsing {cmp_.native_series} "
            "native series into one stream; per-series comparison impossible on multi-series "
            "data (panel variables resolved to match-all for the oracle)"
        )
        return cmp_
    common = set(native) & set(translated)
    native_for_diff = native
    translated_for_diff = translated
    if not common and native and translated:
        reducer = _translated_reducer(cmp_.esql)
        projected = _project_to_subset(native, translated, reducer=reducer)
        if set(projected) & set(translated):
            native_for_diff = projected
            common = set(projected) & set(translated)
            cmp_.notes.append(
                f"native re-aggregated {len(native)}->{len(projected)} series ({reducer}) "
                "to match translated label subset"
            )
    if not common and native and translated:
        # Reverse direction: a global-aggregate source (variable matcher
        # stripped by the oracle) versus a translated panel that keeps the
        # variable's dimension grouped for its Kibana control. Re-aggregating
        # the translated partitions onto the native label subset is exact for
        # multiplicity-invariant-safe reducers (sum of partition sums is the
        # global sum); an AVG of partition AVGs is not, so leave those alone.
        reducer = _translated_reducer(cmp_.esql)
        if reducer in {"sum", "max", "min"}:
            projected = _project_to_subset(translated, native, reducer=reducer)
            if set(projected) & set(native):
                translated_for_diff = projected
                common = set(native) & set(projected)
                cmp_.notes.append(
                    f"translated re-aggregated {len(translated)}->{len(projected)} series ({reducer}) "
                    "to match native label subset"
                )
    cmp_.common_series = len(common)
    points, rmax, rmean = compute_diff(native_for_diff, translated_for_diff, step)
    cmp_.compared_points = points
    cmp_.max_relative_error = rmax
    cmp_.mean_relative_error = rmean
    if cmp_.common_series == 0:
        if not translated:
            cmp_.fail_reason = (
                f"translated query returned no series; native returned {cmp_.native_series}"
            )
        elif not native:
            # The oracle ran without error but produced no reference data
            # (e.g. instant-vector arithmetic that matches nothing on this
            # data set); that cannot prove the translation wrong.
            cmp_.skipped_reason = (
                f"native oracle returned no series; translated returned "
                f"{cmp_.translated_series} - no reference data to verify against"
            )
        else:
            cmp_.fail_reason = (
                f"series keys did not align (native {cmp_.native_series}, "
                f"translated {cmp_.translated_series} series)"
            )
    elif cmp_.compared_points == 0:
        cmp_.fail_reason = "no overlapping time buckets between native and translated points"
    return cmp_
