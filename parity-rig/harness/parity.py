"""PromQL-identity parity harness.

For each panel in the migrated dashboard:

1. Read the original PromQL (``query_ir.source_expression``).
2. Substitute Grafana template variables with concrete values.
3. Run the same PromQL string against:
   - Prometheus via ``/api/v1/query_range``
   - Elasticsearch via the ES|QL ``PROMQL`` source command
4. Align the result series by label set and compute per-bucket numeric
   error.

Where the panel was translated via the ES|QL fallback (because PromQL
isn't supported for that construct, e.g. ``or`` / ``histogram_quantile``),
we still run the original PromQL against Prometheus but execute the
*translated* ES|QL on the Elastic side and compare results.

Verdicts:

* ``PROMQL_IDENTITY`` — both sides ran the exact same PromQL.
* ``ESQL_FALLBACK`` — Elastic side ran ES|QL; PromQL ran on Prometheus.
* Plus a numeric verdict: ``STRICT_PASS`` (≤1 %), ``FUZZY_PASS`` (≤5 %),
  ``SHAPE_PASS`` (labels overlap, numerics diverge), ``FAIL_NO_OVERLAP``,
  ``ERROR``, ``SKIP``.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

PROM_URL = os.environ.get("PROM_URL", "http://localhost:29090")
ES_URL = os.environ["ELASTICSEARCH_ENDPOINT"].rstrip("/")
ES_KEY = os.environ["KEY"]
REPORT_PATH = os.environ.get(
    "REPORT_PATH",
    "/tmp/mig-to-kbn-e2e/parity-express-native2/dashboards/migration_report.json",
)
ESQL_INDEX = os.environ.get("ESQL_INDEX", "metrics-express.prometheus-parity")
WINDOW_MINUTES = int(os.environ.get("PARITY_WINDOW_MINUTES", "10"))
STEP_SECONDS = int(os.environ.get("PARITY_STEP_SECONDS", "60"))
OUTPUT_DIR = Path(
    os.environ.get(
        "OUTPUT_DIR",
        # Default to ``parity-rig/reports`` relative to this file
        # (parity-rig/harness/parity.py).
        str(Path(__file__).resolve().parent.parent / "reports"),
    )
)

ES_HEADERS = {"Authorization": f"ApiKey {ES_KEY}", "Content-Type": "application/json"}

PROMETHEUS_ONLY_LABELS = frozenset({
    "__name__", "instance", "job",
    "exported_instance", "exported_job",
    "cluster", "replica",  # Prometheus external_labels added on remote-write side
})

VARIABLE_DEFAULTS = {
    "instance": "express-1:3000",
    "node_exporter": "express-1:3000",
    "datasource": "parity-prom",
    # Variables introduced by the broader fixture set (k8s-views-global, the
    # canonical 1860 Node Exporter Full, etc.). The harness can't enumerate
    # values via Prometheus label_values, so we hard-code defaults that
    # match the parity-rig data we ingest. Panels referencing a variable
    # we don't recognise are skipped before query execution.
    "cluster": "parity-cluster",
    "job": "express-app",
    "node": "node-1:9100",
    "Filesystem": "/",
    "device": "eth0",
    "interval": "5m",
    "aggregation_interval": "5m",
}

# Mig-to-kbn fuses multi-target Grafana panels by joining each target's
# PromQL with ``|||``. The harness splits on this token and runs each
# target separately against Prometheus, then unions the resulting series
# before comparing against the (already-fused) ES|QL output.
MULTI_TARGET_SEPARATOR = "|||"

SKIPPABLE_TOKENS_RE = re.compile(
    r"\b(topk|bottomk|sort_desc|sort_asc|label_replace|vector|histogram_quantile)\b"
)


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
class PanelComparison:
    title: str
    promql_original: str = ""
    promql_run: str = ""
    esql: str = ""
    side_mode: str = ""  # "PROMQL_IDENTITY" or "ESQL_FALLBACK"
    prom_series_count: int = 0
    es_series_count: int = 0
    common_series_count: int = 0
    prom_only_series: list[str] = field(default_factory=list)
    es_only_series: list[str] = field(default_factory=list)
    compared_points: int = 0
    max_relative_error: float = 0.0
    mean_relative_error: float = 0.0
    promql_error: str = ""
    esql_error: str = ""
    notes: list[str] = field(default_factory=list)
    skipped_reason: str = ""

    @property
    def verdict(self) -> str:
        if self.skipped_reason:
            return "SKIP"
        if self.promql_error or self.esql_error:
            return "ERROR"
        if self.compared_points == 0:
            return "FAIL_NO_OVERLAP"
        if self.max_relative_error <= 0.01:
            return "STRICT_PASS"
        if self.max_relative_error <= 0.05:
            return "FUZZY_PASS"
        if self.common_series_count > 0:
            return "SHAPE_PASS"
        return "FAIL"


def expand_variables(promql: str) -> str:
    out = promql
    for var, default in VARIABLE_DEFAULTS.items():
        out = out.replace(f"${var}", default)
        out = out.replace(f"${{{var}}}", default)
    out = out.replace("$__rate_interval", "5m")
    out = out.replace("$__interval", "5m")
    out = out.replace("$__range", "15m")
    out = out.replace("$aggregation_interval", "5m")
    out = re.sub(r"=~\s*\"\.\*\"", '="express-1:3000"', out)
    return out


def run_promql_range(query: str, start: datetime, end: datetime, step: int) -> dict[str, Any]:
    params = {
        "query": query,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "step": str(step),
    }
    r = requests.get(f"{PROM_URL}/api/v1/query_range", params=params, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"prom {r.status_code}: {r.text[:300]}")
    return r.json()


def run_esql_promql(promql_expr: str, t_start: datetime, t_end: datetime, step: int) -> dict[str, Any]:
    """Run a raw PromQL string via Elasticsearch's ES|QL ``PROMQL`` command.

    This is the byte-for-byte identity path: same PromQL on both sides.
    """
    start_iso = t_start.isoformat().replace("+00:00", "Z")
    end_iso = t_end.isoformat().replace("+00:00", "Z")
    query = (
        f'PROMQL index={ESQL_INDEX} step={step}s '
        f'start="{start_iso}" end="{end_iso}" '
        f'value=({promql_expr})'
    )
    body = {"query": query}
    r = requests.post(f"{ES_URL}/_query", headers=ES_HEADERS, json=body, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"es-promql {r.status_code}: {r.text[:400]}")
    return r.json()


def _inject_time_filter(esql: str) -> str:
    """Inject ``WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend`` when needed.

    Both ``TS`` and ``FROM`` source queries need an explicit @timestamp range
    filter when executed via the REST API:

    * ``TS`` queries rely on Kibana's time-picker — without it, TBUCKET returns
      all historical data, exceeding ES|QL's row limit before the recent window.
    * ``FROM`` queries that use ``BUCKET(@timestamp, N, ?_tstart, ?_tend)``
      only align bucket *boundaries* to the params; they still scan all rows
      unless a ``WHERE @timestamp`` clause is present.

    The filter is injected as the first pipe after the source line.  It is a
    no-op if a ``WHERE @timestamp`` clause already exists.
    """
    # Skip if an explicit @timestamp range filter is already present.
    # BUCKET(@timestamp, ...) and TBUCKET(@timestamp, ...) are NOT time filters.
    if re.search(r"WHERE\s+@timestamp\s*(>=|<=|>|<)", esql, re.IGNORECASE):
        return esql

    lines = esql.split("\n")
    insert_after = -1
    for idx, line in enumerate(lines):
        stripped = line.strip().upper()
        if stripped.startswith("TS ") or stripped == "TS" or stripped.startswith("FROM "):
            insert_after = idx
            break
    if insert_after == -1:
        return esql
    time_filter = "| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend"
    return "\n".join(lines[: insert_after + 1] + [time_filter] + lines[insert_after + 1 :])


def run_esql_raw(esql: str, t_start: datetime, t_end: datetime) -> dict[str, Any]:
    params = [
        {"_tstart": t_start.isoformat().replace("+00:00", "Z")},
        {"_tend": t_end.isoformat().replace("+00:00", "Z")},
    ]
    # TS and FROM queries both need an explicit @timestamp filter when run via
    # the REST API (no Kibana time-picker context).  _inject_time_filter is a
    # no-op when the query already contains a WHERE @timestamp clause.
    esql = _inject_time_filter(esql)
    body = {"query": esql, "params": params}
    r = requests.post(f"{ES_URL}/_query", headers=ES_HEADERS, json=body, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"es {r.status_code}: {r.text[:400]}")
    return r.json()


def _drop_constants_and_promonly(
    raw: list[tuple[dict[str, str], list[tuple[float, float]]]],
) -> dict[SeriesKey, list[tuple[float, float]]]:
    raw = [
        (
            {k: v for k, v in d.items() if k not in PROMETHEUS_ONLY_LABELS},
            vs,
        )
        for d, vs in raw
    ]
    if not raw:
        return {}
    all_keys = set.intersection(*(set(d.keys()) for d, _ in raw))
    constants = {k for k in all_keys if len({d[k] for d, _ in raw}) == 1}
    out: dict[SeriesKey, list[tuple[float, float]]] = {}
    for d, vs in raw:
        scrubbed = {k: v for k, v in d.items() if k not in constants}
        out[SeriesKey(tuple(sorted(scrubbed.items())))] = vs
    return out


def normalize_prom_series(prom: dict[str, Any]) -> dict[SeriesKey, list[tuple[float, float]]]:
    raw = []
    for series in (prom.get("data") or {}).get("result", []):
        labels = dict(series.get("metric", {}))
        values = [(float(ts), float(val)) for ts, val in series.get("values", [])]
        raw.append((labels, values))
    return _drop_constants_and_promonly(raw)


def normalize_esql_promql_rows(esql_data: dict[str, Any]) -> dict[SeriesKey, list[tuple[float, float]]]:
    """Parse the ES|QL ``PROMQL`` command's output (``value`` / ``step`` /
    grouped labels OR a single ``_timeseries`` JSON column)."""
    columns = [c["name"] for c in esql_data.get("columns", [])]
    rows = esql_data.get("values", [])
    if not columns or not rows:
        return {}

    value_idx = step_idx = ts_json_idx = None
    label_idxs: list[tuple[int, str]] = []
    for i, name in enumerate(columns):
        if name in ("value",) or name.endswith("_value"):
            value_idx = i
        elif name == "step":
            step_idx = i
        elif name == "_timeseries":
            ts_json_idx = i
        elif name not in ("value", "step", "_timeseries"):
            # Treat any other column as a grouping label.
            label_idxs.append((i, name))
    # Fallback: maybe value column is first numeric.
    if value_idx is None:
        for i, _ in enumerate(columns):
            if step_idx == i or ts_json_idx == i:
                continue
            try:
                float(rows[0][i]) if rows[0][i] is not None else 0.0
                value_idx = i
                break
            except Exception:
                continue
    if value_idx is None or step_idx is None:
        return {}

    raw: list[tuple[dict[str, str], list[tuple[float, float]]]] = []
    bucket: dict[tuple[tuple[str, str], ...], list[tuple[float, float]]] = {}
    for row in rows:
        try:
            t = datetime.fromisoformat(str(row[step_idx]).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        try:
            v = float(row[value_idx]) if row[value_idx] is not None else None
        except (TypeError, ValueError):
            v = None
        if v is None:
            continue
        labels: dict[str, str] = {}
        if ts_json_idx is not None and row[ts_json_idx]:
            try:
                blob = json.loads(row[ts_json_idx])
                labels.update(blob.get("labels", {}))
            except Exception:
                pass
        for idx, name in label_idxs:
            if row[idx] is not None:
                labels[name] = str(row[idx])
        key = tuple(sorted(labels.items()))
        bucket.setdefault(key, []).append((t, v))
    raw = [(dict(k), v) for k, v in bucket.items()]
    return _drop_constants_and_promonly(raw)


def normalize_esql_translated(esql_data: dict[str, Any]) -> dict[SeriesKey, list[tuple[float, float]]]:
    """Parse a translated ES|QL query's output.

    The translator's ``STATS`` shape is
    ``STATS <metric> = <agg>(...) BY time_bucket = TBUCKET(...), <label1>, <label2>, ...``
    so the result columns end up as ``[<metric>, time_bucket, <label1>, <label2>, ...]``
    (the agg goes first, the BY columns follow). Subsequent rewrites can
    add an ``EVAL computed_value = ...`` and an ``EVAL legend = CONCAT(...)``
    which reorder the columns via ``KEEP``. We pick the metric column as
    the *numeric* leftover (preferring ``computed_value`` and ``*_value``
    when present) so labels that happen to come first in the KEEP order
    (e.g. ``status`` after a BY-promotion) are still treated as labels.
    """
    columns = [c["name"] for c in esql_data.get("columns", [])]
    column_types = [c.get("type", "") for c in esql_data.get("columns", [])]
    rows = esql_data.get("values", [])
    if not columns or not rows:
        return {}

    NUMERIC_TYPES = {"double", "long", "integer", "float", "unsigned_long"}

    # The translator's composite-legend rewrite adds an
    # ``EVAL legend = CONCAT(...)`` column for Kibana's Lens chart, which
    # is a display string derived from the underlying per-label columns
    # (also retained in KEEP). For parity comparison we ignore it
    # entirely and match on the real labels.
    DERIVED_DISPLAY_COLUMNS = {"legend"}

    time_idx = None
    candidate_indices: list[int] = []
    explicit_labels: list[tuple[int, str]] = []
    for i, name in enumerate(columns):
        lname = name.lower()
        if "time_bucket" in lname or lname == "@timestamp":
            time_idx = i
            continue
        if lname.startswith("labels.") or lname.startswith("prometheus.labels."):
            label_name = lname.split(".")[-1]
            # Mirror the Prometheus side: drop labels that Prometheus attaches
            # automatically (instance, job, cluster, replica) so series keys
            # match between the two stores.  This matters for native endpoint
            # panels that group BY labels.instance / labels.job.
            if label_name not in PROMETHEUS_ONLY_LABELS:
                explicit_labels.append((i, label_name))
            continue
        if lname in DERIVED_DISPLAY_COLUMNS:
            continue
        candidate_indices.append(i)

    metric_idx = None
    for i in candidate_indices:
        lname = columns[i].lower()
        if lname == "computed_value" or lname.endswith("_value"):
            metric_idx = i
            break
    if metric_idx is None:
        for i in candidate_indices:
            if column_types[i] in NUMERIC_TYPES:
                metric_idx = i
                break
    if metric_idx is None:
        # Fallback: pick the first non-time, non-labels.* column.
        for i in candidate_indices:
            metric_idx = i
            break

    label_idxs = list(explicit_labels)
    for i in candidate_indices:
        if i == metric_idx:
            continue
        label_idxs.append((i, columns[i]))

    if time_idx is None or metric_idx is None:
        return {}
    bucket: dict[tuple[tuple[str, str], ...], list[tuple[float, float]]] = {}
    for row in rows:
        try:
            t = datetime.fromisoformat(str(row[time_idx]).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        try:
            v = float(row[metric_idx]) if row[metric_idx] is not None else None
        except (TypeError, ValueError):
            v = None
        if v is None:
            continue
        labels = {name: str(row[idx]) for idx, name in label_idxs if row[idx] is not None}
        bucket.setdefault(tuple(sorted(labels.items())), []).append((t, v))
    raw = [(dict(k), v) for k, v in bucket.items()]
    return _drop_constants_and_promonly(raw)


def _project_prom_to_es_labels(
    prom: dict[SeriesKey, list[tuple[float, float]]],
    es: dict[SeriesKey, list[tuple[float, float]]],
) -> dict[SeriesKey, list[tuple[float, float]]]:
    """Project Prometheus series onto the label dimensions that ES uses.

    When the translated ES|QL groups by fewer labels than Prometheus (e.g. the
    Grafana legend omits ``status`` so the BY clause drops it), direct series
    matching produces zero common keys.  This function re-aggregates the
    Prometheus side by summing time-aligned values from all Prometheus series
    that share the same projected key so the comparison can proceed.
    """
    if not es or not prom:
        return prom
    es_label_names: set[str] = set()
    for key in es:
        for label_name, _ in key.labels:
            es_label_names.add(label_name)
    projected: dict[SeriesKey, list[tuple[float, float]]] = {}
    for key, values in prom.items():
        proj_labels = tuple(sorted((k, v) for k, v in key.labels if k in es_label_names))
        proj_key = SeriesKey(labels=proj_labels)
        projected.setdefault(proj_key, []).extend(values)
    return projected


def bucket_align(
    series: dict[SeriesKey, list[tuple[float, float]]],
    bucket_seconds: int,
) -> dict[SeriesKey, dict[int, float]]:
    return {
        key: {int(ts // bucket_seconds) * bucket_seconds: v for ts, v in vs}
        for key, vs in series.items()
    }


def compute_diff(
    prom: dict[SeriesKey, list[tuple[float, float]]],
    es: dict[SeriesKey, list[tuple[float, float]]],
    bucket_seconds: int,
) -> tuple[int, float, float, int, int, int]:
    pa = bucket_align(prom, bucket_seconds)
    ea = bucket_align(es, bucket_seconds)
    common = set(pa) & set(ea)
    # Drop boundary buckets per series (first + last bucket each side).
    def trim(buckets):
        out = {}
        for k, m in buckets.items():
            if len(m) <= 2:
                continue
            sorted_ts = sorted(m.keys())
            out[k] = {ts: m[ts] for ts in sorted_ts[1:-1]}
        return out
    pa_i = trim(pa)
    ea_i = trim(ea)
    rel_errors: list[float] = []
    points = 0
    for key in set(pa_i) & set(ea_i):
        for bts, pval in pa_i[key].items():
            eval_ = ea_i[key].get(bts)
            if eval_ is None:
                continue
            points += 1
            denom = max(abs(pval), abs(eval_), 1e-9)
            rel_errors.append(abs(pval - eval_) / denom)
    return (
        points,
        max(rel_errors, default=0.0),
        (sum(rel_errors) / len(rel_errors)) if rel_errors else 0.0,
        len(common),
        len(set(pa) - common),
        len(set(ea) - common),
    )


def compare_panel(panel: dict[str, Any], t_start: datetime, t_end: datetime) -> PanelComparison:
    title = panel.get("title", "")
    qir = panel.get("query_ir") or {}
    promql_original = qir.get("source_expression") or qir.get("clean_expression") or ""
    esql = (panel.get("esql") or "").strip()
    cmp_ = PanelComparison(title=title, promql_original=promql_original, esql=esql)

    if not esql:
        cmp_.skipped_reason = "no migrated ES|QL"
        return cmp_
    if not promql_original:
        cmp_.skipped_reason = "no source PromQL"
        return cmp_

    # Did the translator emit a native PROMQL command? If so, identity mode.
    is_native = esql.lstrip().upper().startswith("PROMQL")
    cmp_.side_mode = "PROMQL_IDENTITY" if is_native else "ESQL_FALLBACK"

    if SKIPPABLE_TOKENS_RE.search(promql_original) and not is_native:
        cmp_.skipped_reason = "PromQL construct without comparable ES|QL form"
        return cmp_

    promql_run_combined = expand_variables(promql_original)
    cmp_.promql_run = promql_run_combined

    # mig-to-kbn fuses multi-target panels by joining each target's PromQL
    # with ``|||``. Split and run each segment separately on Prometheus,
    # then union the results before comparing against the (already-fused)
    # ES|QL output.
    promql_segments = [
        seg.strip() for seg in promql_run_combined.split(MULTI_TARGET_SEPARATOR) if seg.strip()
    ]
    # Normalize histogram boundary label values: some Prometheus exporters
    # store le as "1.0"/"10.0" while Grafana dashboards use le="1"/"10".
    # Apply the same normalization that the translator emits so both sides
    # query the actual stored data rather than returning empty results.
    promql_segments = [
        re.sub(r'\ble=("|\')(\d+)\1', lambda m: f'le={m.group(1)}{m.group(2)}.0{m.group(1)}', seg)
        for seg in promql_segments
    ]
    if not promql_segments:
        cmp_.skipped_reason = "no PromQL segments after splitting"
        return cmp_

    # Skip panels that still contain unsubstituted Grafana variables; we
    # can't honestly compare what Prometheus would have returned.
    leftover = [s for s in promql_segments if "$" in s]
    if leftover:
        cmp_.skipped_reason = f"unsubstituted Grafana variables: {leftover[0][:80]}"
        return cmp_

    prom_norm: dict[SeriesKey, list[tuple[float, float]]] = {}
    try:
        for segment in promql_segments:
            seg_data = run_promql_range(segment, t_start, t_end, STEP_SECONDS)
            for key, values in normalize_prom_series(seg_data).items():
                # Concatenate values; if a series key appears in multiple
                # segments (rare), keep the earlier samples and append later
                # ones. The compute_diff step bucket-aligns so duplicates
                # within one bucket fold cleanly.
                prom_norm.setdefault(key, []).extend(values)
    except Exception as exc:
        cmp_.promql_error = str(exc)
        return cmp_

    try:
        if is_native:
            # The native-PROMQL ES|QL command parses the verbatim PromQL.
            # Send each segment as its own PROMQL call and union, mirroring
            # what we do on the Prom side.
            es_norm: dict[SeriesKey, list[tuple[float, float]]] = {}
            for segment in promql_segments:
                es_data = run_esql_promql(segment, t_start, t_end, STEP_SECONDS)
                for key, values in normalize_esql_promql_rows(es_data).items():
                    es_norm.setdefault(key, []).extend(values)
        else:
            es_data = run_esql_raw(esql, t_start, t_end)
            es_norm = normalize_esql_translated(es_data)
    except Exception as exc:
        cmp_.esql_error = str(exc)
        return cmp_

    cmp_.prom_series_count = len(prom_norm)
    cmp_.es_series_count = len(es_norm)
    common = set(prom_norm) & set(es_norm)
    # When the translated ES|QL groups by fewer label dimensions than Prometheus
    # (e.g. the Grafana legend drops 'status'), direct key matching yields zero
    # common series.  Re-aggregate Prometheus onto the ES label subset and retry.
    prom_for_diff = prom_norm
    if not common and prom_norm and es_norm:
        projected = _project_prom_to_es_labels(prom_norm, es_norm)
        reprojected_common = set(projected) & set(es_norm)
        if reprojected_common:
            prom_for_diff = projected
            common = reprojected_common
            cmp_.notes.append(
                f"Prometheus re-aggregated from {len(prom_norm)} to {len(projected)} series "
                f"by projecting onto ES label subset ({len(common)} common after projection)"
            )
    cmp_.common_series_count = len(common)
    cmp_.prom_only_series = [repr(k) for k in sorted(set(prom_for_diff) - common, key=str)[:3]]
    cmp_.es_only_series = [repr(k) for k in sorted(set(es_norm) - common, key=str)[:3]]

    points, rmax, rmean, _, _, _ = compute_diff(prom_for_diff, es_norm, STEP_SECONDS)
    cmp_.compared_points = points
    cmp_.max_relative_error = rmax
    cmp_.mean_relative_error = rmean
    return cmp_


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report = json.loads(Path(REPORT_PATH).read_text())

    end = datetime.now(UTC)
    start = end - timedelta(minutes=WINDOW_MINUTES)
    print(f"Parity window: {start.isoformat()} → {end.isoformat()} step={STEP_SECONDS}s")
    print(f"ES|QL index: {ESQL_INDEX}\n")

    cmps: list[PanelComparison] = []
    for d in report.get("dashboards", []):
        for panel in d.get("panels", []):
            cmps.append(compare_panel(panel, start, end))

    marker = {
        "STRICT_PASS": "✓",
        "FUZZY_PASS": "~",
        "SHAPE_PASS": "·",
        "FAIL_NO_OVERLAP": "✗",
        "FAIL": "✗",
        "ERROR": "!",
        "SKIP": "—",
    }
    counts: dict[str, int] = {}
    print("Per-panel verdicts:")
    for c in cmps:
        v = c.verdict
        counts[v] = counts.get(v, 0) + 1
        mode_tag = c.side_mode[:11] if c.side_mode else ""
        extra = ""
        if c.compared_points:
            extra = f" pts={c.compared_points} rel_err_max={c.max_relative_error:.3f}"
        elif c.skipped_reason:
            extra = f" :: {c.skipped_reason}"
        elif c.promql_error:
            extra = f" :: prom {c.promql_error[:60]}"
        elif c.esql_error:
            extra = f" :: es {c.esql_error[:60]}"
        print(
            f"  {marker.get(v, '?')} [{v:15s}][{mode_tag:11s}] {c.title:46s}"
            f" prom={c.prom_series_count:3d} es={c.es_series_count:3d} common={c.common_series_count:3d}{extra}"
        )

    print("\nVerdict summary:")
    for v, n in sorted(counts.items()):
        print(f"  {v:18s}: {n}")

    out_payload = {
        "window": {"start": start.isoformat(), "end": end.isoformat(), "step_seconds": STEP_SECONDS},
        "verdict_counts": counts,
        "panels": [{**c.__dict__, "verdict": c.verdict} for c in cmps],
    }
    (OUTPUT_DIR / "parity-report.json").write_text(json.dumps(out_payload, indent=2, default=str))
    print(f"\nReport: {OUTPUT_DIR / 'parity-report.json'}")


if __name__ == "__main__":
    main()
