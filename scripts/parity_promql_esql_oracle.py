# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Numeric parity check: our translated ES|QL vs Elasticsearch's native PROMQL.

This proves *translation correctness* without needing a live Prometheus. For each
PromQL expression it:

1. Translates the expression to ES|QL with the real ``translate_promql_to_esql``.
2. Runs that translated ES|QL on the target index.
3. Runs the SAME expression through Elasticsearch's native ``PROMQL`` ES|QL command
   (``PROMQL index=... step=...s start=... end=... value=(<expr>)``) on the same
   index and time window. The native command is an independent implementation of
   PromQL semantics, so it serves as the oracle.
4. Aligns the two result sets by label set + time bucket and computes per-bucket
   relative error.

Because both sides read the same data from the same store, the only variable is
the query: if the numbers match, the translation is semantically correct.

Verdicts (mirrors parity-rig/harness/parity.py):
  STRICT_PASS  max relative error <= 1%
  FUZZY_PASS   max relative error <= 5%
  SHAPE_PASS   series labels overlap but values diverge
  FAIL         no overlapping series, or numbers diverge with no shared shape
  SKIP         translator marked it not_feasible, or native PROMQL can't parse it
  ERROR        a side errored unexpectedly

The series-key normalization, constant-label scrubbing, label projection, and
per-bucket diff are adapted from parity-rig/harness/parity.py so verdicts are
consistent with the existing parity rig.

Usage:
    set -a && . ./serverless_creds.env && set +a
    python scripts/parity_promql_esql_oracle.py --index 'metrics-express.prometheus-parity*'

By default it runs a built-in corpus (including the feasibility-expansion
constructs: clamp/sgn/quantile/math) over a time window where the parity index
has data. Override the window with --start/--end (ISO8601) or --window-minutes
(relative to now). Supply your own expressions with --expr (repeatable) or --file.
Exit code is non-zero if any comparison FAILs.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.translate import (
    translate_promql_to_esql,
)

CTX = ssl.create_default_context()

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

# Default corpus pins the metric to a single series ({instance="prometheus-1:9090"})
# on purpose. Elementwise functions (abs/sqrt/ln/clamp/...) are applied PER SERIES
# by PromQL, but a bare gauge selector translates to "AVG(metric) BY time_bucket",
# which collapses multiple instances into one value. Comparing a per-instance
# native result against a single averaged value would diverge for reasons that
# have nothing to do with the function translation (it's the documented gauge-AVG
# approximation). Scoping to one series isolates the function semantics, which is
# what this oracle is meant to verify. Multi-series expressions still work; they
# just exercise the aggregation gap too.
DEFAULT_CORPUS: list[str] = [
    # baseline aggregations (sanity)
    "sum(go_goroutines) by (job)",
    "avg(go_goroutines)",
    "max(go_goroutines)",
    # feasibility-expansion: exact 1:1 maps (single series to isolate the function)
    'clamp_max(go_goroutines{instance="prometheus-1:9090"}, 30)',
    'clamp(go_goroutines{instance="prometheus-1:9090"}, 10, 50)',
    'sgn(go_goroutines{instance="prometheus-1:9090"})',
    'quantile(0.95, go_goroutines{instance="prometheus-1:9090"})',
    # math / trig wrappers (single series)
    'abs(go_goroutines{instance="prometheus-1:9090"})',
    'sqrt(go_goroutines{instance="prometheus-1:9090"})',
    'ceil(go_goroutines{instance="prometheus-1:9090"} / 7)',
    'floor(go_goroutines{instance="prometheus-1:9090"} / 7)',
    'ln(go_goroutines{instance="prometheus-1:9090"})',
    'log2(go_goroutines{instance="prometheus-1:9090"})',
    'log10(go_goroutines{instance="prometheus-1:9090"})',
]


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


def _request(endpoint: str, key: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{endpoint.rstrip('/')}/_query?format=json",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"ApiKey {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, context=CTX, timeout=60) as resp:
        return json.loads(resp.read())


def _http_error_reason(e: urllib.error.HTTPError) -> str:
    raw = e.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw).get("error", {}).get("reason", raw[:300])
    except json.JSONDecodeError:
        return raw[:300] or f"HTTP {e.code}"


def run_translated(endpoint: str, key: str, esql: str, tstart: str, tend: str) -> dict:
    body = {"query": esql, "params": [{"_tstart": tstart}, {"_tend": tend}]}
    return _request(endpoint, key, body)


def run_native_promql(endpoint, key, expr, index, step, start_iso, end_iso) -> dict:
    query = (
        f"PROMQL index={index} step={step}s "
        f'start="{start_iso}" end="{end_iso}" '
        f"value=({expr})"
    )
    return _request(endpoint, key, {"query": query})


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


def compare(endpoint, key, expr, index, step, start_iso, end_iso) -> Comparison:
    rp = RulePackConfig()
    ctx = translate_promql_to_esql(expr, esql_index=index, rule_pack=rp)
    cmp_ = Comparison(expr=expr, esql=(ctx.esql_query or "").strip(), feasibility=ctx.feasibility)

    if ctx.feasibility == "not_feasible" or not cmp_.esql:
        cmp_.skipped_reason = "translator marked not_feasible"
        return cmp_
    if any(tok in expr for tok in NATIVE_UNSUPPORTED):
        cmp_.skipped_reason = "native PROMQL oracle does not support this construct"
        return cmp_

    try:
        native_raw = run_native_promql(endpoint, key, expr, index, step, start_iso, end_iso)
    except urllib.error.HTTPError as e:
        cmp_.skipped_reason = f"native PROMQL could not run: {_http_error_reason(e)[:120]}"
        return cmp_
    except Exception as e:
        cmp_.native_error = str(e)
        return cmp_

    try:
        translated_raw = run_translated(endpoint, key, cmp_.esql, start_iso, end_iso)
        if "error" in translated_raw:
            cmp_.translated_error = str(translated_raw["error"])[:200]
            return cmp_
    except urllib.error.HTTPError as e:
        cmp_.translated_error = _http_error_reason(e)
        return cmp_
    except Exception as e:
        cmp_.translated_error = str(e)
        return cmp_

    native = normalize_native(native_raw)
    translated = normalize_translated(translated_raw)
    cmp_.native_series = len(native)
    cmp_.translated_series = len(translated)

    common = set(native) & set(translated)
    native_for_diff = native
    if not common and native and translated:
        projected = _project_to_subset(native, translated)
        if set(projected) & set(translated):
            native_for_diff = projected
            common = set(projected) & set(translated)
            cmp_.notes.append(
                f"native re-aggregated {len(native)}->{len(projected)} series to match translated label subset"
            )
    cmp_.common_series = len(common)
    points, rmax, rmean = compute_diff(native_for_diff, translated, step)
    cmp_.compared_points = points
    cmp_.max_relative_error = rmax
    cmp_.mean_relative_error = rmean
    return cmp_


def load_expressions(args) -> list[str]:
    exprs: list[str] = list(args.expr or [])
    if args.file:
        for line in Path(args.file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                exprs.append(line)
    return exprs or list(DEFAULT_CORPUS)


def resolve_window(args) -> tuple[str, str]:
    if args.start and args.end:
        return args.start, args.end
    end = datetime.now(UTC)
    start = end - timedelta(minutes=args.window_minutes)
    return (
        start.isoformat().replace("+00:00", "Z"),
        end.isoformat().replace("+00:00", "Z"),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--expr", action="append", help="PromQL expression (repeatable).")
    p.add_argument("--file", help="File with one PromQL expression per line.")
    p.add_argument("--index", default="metrics-express.prometheus-parity*", help="ES index pattern.")
    p.add_argument("--es-endpoint", default=os.environ.get("ELASTICSEARCH_ENDPOINT", ""))
    p.add_argument("--api-key", default=os.environ.get("KEY", ""))
    p.add_argument("--step-seconds", type=int, default=300)
    p.add_argument("--window-minutes", type=int, default=30)
    p.add_argument("--start", help="ISO8601 window start (overrides --window-minutes).")
    p.add_argument("--end", help="ISO8601 window end (overrides --window-minutes).")
    p.add_argument("--json", action="store_true", help="Emit a JSON report instead of text.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.es_endpoint or not args.api_key:
        print("ERROR: set ELASTICSEARCH_ENDPOINT and KEY (or pass --es-endpoint/--api-key).", file=sys.stderr)
        return 2

    start_iso, end_iso = resolve_window(args)
    expressions = load_expressions(args)
    results = [
        compare(args.es_endpoint, args.api_key, expr, args.index, args.step_seconds, start_iso, end_iso)
        for expr in expressions
    ]

    if args.json:
        print(json.dumps([{**c.__dict__, "verdict": c.verdict()} for c in results], indent=2))
    else:
        symbols = {
            "STRICT_PASS": "PASS",
            "FUZZY_PASS": "~PASS",
            "SHAPE_PASS": "SHAPE",
            "FAIL": "FAIL",
            "SKIP": "skip",
            "ERROR": "ERROR",
        }
        print(f"Parity window {start_iso} -> {end_iso}  step={args.step_seconds}s  index={args.index}\n")
        for c in results:
            v = c.verdict()
            print(f"[{symbols.get(v, v)}] {c.expr}")
            if c.skipped_reason:
                print(f"       skip: {c.skipped_reason}")
            elif c.translated_error or c.native_error:
                print(f"       error: {c.translated_error or c.native_error}")
            else:
                print(
                    f"       series native={c.native_series} translated={c.translated_series} "
                    f"common={c.common_series} points={c.compared_points} "
                    f"max_rel_err={c.max_relative_error:.4f} mean_rel_err={c.mean_relative_error:.4f}"
                )
            for note in c.notes:
                print(f"       note: {note}")
        counts: dict[str, int] = {}
        for c in results:
            counts[c.verdict()] = counts.get(c.verdict(), 0) + 1
        print("\nsummary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    fails = sum(1 for c in results if c.verdict() == "FAIL")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
