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
    """Parse native PROMQL output: columns value/step/<labels>.

    Native PROMQL may return the series labels either as broken-out columns or
    packed into a single ``_timeseries`` JSON column (the TS form). Both must be
    decoded -- ignoring ``_timeseries`` collapses every grouped series into one
    empty-key series, which can never intersect the translated side (which does
    decode it), turning correct grouped panels into false FAILs.
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
            labels.update(_decode_timeseries_labels(row[timeseries_idx]))
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


def is_single_value_reduction(esql: str) -> bool:
    """True when the emitted ES|QL reduces the series to a single (stat) value."""
    return bool(_SINGLE_VALUE_REDUCTION_RE.search(esql or ""))


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
                  step: int, start_iso: str, end_iso: str) -> Comparison:
    """Compare an emitted ES|QL panel query against native PROMQL of its source.

    In-band ES errors fail closed (SKIP for native, ERROR for translated). Transport
    failures are NOT caught here: a NetworkError from the injected ``request`` (e.g. an
    unreachable cluster) propagates to the caller, which handles it at the CLI boundary.
    """
    cmp_ = Comparison(expr=source_query, esql=(translated_query or "").strip())
    if not cmp_.esql:
        cmp_.skipped_reason = "no translated ES|QL on this panel"
        return cmp_
    if any(tok in source_query for tok in NATIVE_UNSUPPORTED):
        cmp_.skipped_reason = "native PROMQL oracle does not support this construct"
        return cmp_
    if is_single_value_reduction(cmp_.esql):
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

    native = normalize_native(native_raw)
    translated = normalize_translated(translated_raw)
    cmp_.native_series = len(native)
    cmp_.translated_series = len(translated)
    common = set(native) & set(translated)
    native_for_diff = native
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
    cmp_.common_series = len(common)
    points, rmax, rmean = compute_diff(native_for_diff, translated, step)
    cmp_.compared_points = points
    cmp_.max_relative_error = rmax
    cmp_.mean_relative_error = rmean
    return cmp_
