#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
#
# Lightweight Prometheus → Elasticsearch ingestion in prometheus_native format.
# Scrapes each configured exporter and writes docs to its own
# metrics-<dataset>-default data stream using the metrics.* + labels.* layout
# that matches the built-in metrics-prometheus@mappings template
# (labels.* = passthrough TSDB dimension). This gives the migration pipeline the
# prometheus_native schema profile so TS queries, label controls, and RATE() all
# work correctly.
#
# Multiple targets exist so the numeric parity gate has real data for more than
# one exporter. Comparing a node_exporter dashboard against a redis-only index
# yields DATA_GAP for every panel, which looks like coverage but verifies
# nothing; the metric names have to actually be present to be compared.

import json
import re
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime

import requests

ES_URL = "http://elasticsearch:9200"
NAMESPACE = "default"
SCRAPE_INTERVAL = 10
RETRY_DELAY = 5

# (exporter URL, dataset, job label, instance label)
TARGETS = [
    ("http://redis_exporter:9121/metrics", "redis.prometheus", "redis_exporter", "redis:6379"),
    ("http://node_exporter:9100/metrics", "node.prometheus", "node_exporter", "node:9100"),
    ("http://mysqld_exporter:9104/metrics", "mysql.prometheus", "mysqld_exporter", "mysql:3306"),
    ("http://postgres_exporter:9187/metrics", "postgres.prometheus", "postgres_exporter", "postgres:5432"),
]

_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')
_TYPE_RE = re.compile(r"^#\s*TYPE\s+(\S+)\s+(\S+)")


def parse_metric_types(text: str) -> dict:
    """Metric name -> Prometheus type, from the exposition's ``# TYPE`` lines.

    Elasticsearch infers counter-vs-gauge from the ``_total`` suffix when a
    mapping does not say otherwise. Real exporters do not all follow that
    convention: postgres_exporter declares ``pg_stat_database_tup_fetched`` as a
    counter, and without the declaration it lands as a gauge, so every
    ``irate()`` panel over it fails with "first argument of [IRATE(...)] must be
    [counter_long, counter_integer or counter_double]". Honouring ``# TYPE``
    makes the rig behave like a faithful ingest instead of manufacturing a
    counter-typing gap the real world would not have.
    """
    out = {}
    for line in text.splitlines():
        m = _TYPE_RE.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2).lower()
    return out


def ensure_index_template(dataset: str, metric_types: dict) -> None:
    """Declare counter/gauge typing for this dataset before its first document."""
    props = {}
    for name, kind in sorted(metric_types.items()):
        if kind not in ("counter", "gauge"):
            continue
        props[name] = {"type": "double", "time_series_metric": kind}
    if not props:
        return
    # Compose on the built-in metrics component templates rather than replacing
    # them: an index template does not merge with a lower-priority one, and
    # re-declaring TSDB settings here fails with "[index.mode=time_series]
    # requires a non-empty [index.routing_path]". Composing keeps the stock
    # passthrough/dimension setup and adds only the metric typing.
    body = {
        "index_patterns": [f"metrics-{dataset}-*"],
        "data_stream": {},
        "priority": 500,
        # ``index.mode`` must be set here: none of the composed component
        # templates set it, so omitting it yields a standard index and every
        # query fails with "is not a time series index. Use FROM command
        # instead". The routing path comes from the ``labels`` passthrough in
        # ``metrics-prometheus@mappings``, so that component must be composed and
        # ``labels`` must NOT be redefined below -- redefining it without
        # ``time_series_dimension`` leaves TSDB with no routing path and the
        # template is rejected outright.
        "composed_of": [
            "metrics-prometheus@mappings",
            "metrics-prometheus@settings",
            "metrics@mappings",
            "data-streams@mappings",
            "metrics@settings",
        ],
        "template": {
            "settings": {"index.mode": "time_series"},
            "mappings": {"properties": {"metrics": {"properties": props}}},
        },
    }
    try:
        resp = requests.put(
            f"{ES_URL}/_index_template/rig-{dataset.replace('.', '-')}",
            json=body, timeout=20,
        )
        if resp.status_code >= 300:
            print(f"  template {dataset}: HTTP {resp.status_code} {resp.text[:160]}", flush=True)
        else:
            counters = sum(1 for v in props.values() if v["time_series_metric"] == "counter")
            print(f"  template {dataset}: {len(props)} fields ({counters} counters)", flush=True)
    except Exception as exc:
        print(f"  template {dataset}: {exc}", flush=True)


def parse_prometheus(text: str) -> dict:
    """Parse Prometheus text format. Returns {(labels_frozen): {metric: value}}."""
    groups: dict = defaultdict(dict)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Skip histogram/summary component metrics (le, quantile labels)
        # — only track the gauge/counter leaf metrics used by the dashboard.
        try:
            if " " not in line:
                continue
            parts = line.rsplit(" ", 1)
            if len(parts) != 2:
                continue
            name_labels, raw_value = parts
            try:
                value = float(raw_value)
            except ValueError:
                continue
            # Skip NaN / Inf — not useful for TSDB
            if value != value or value in (float("inf"), float("-inf")):
                continue

            labels: dict[str, str] = {}
            if "{" in name_labels:
                name, label_block = name_labels.split("{", 1)
                label_block = label_block.rstrip("}")
                for k, v in _LABEL_RE.findall(label_block):
                    labels[k] = v
            else:
                name = name_labels

            name = name.strip()
            if not name:
                continue

            label_key = tuple(sorted(labels.items()))
            groups[label_key][name] = value
        except Exception:
            continue
    return groups


def build_bulk_body(groups: dict, timestamp: str, dataset: str, job: str, instance: str) -> str:
    lines = []
    index = f"metrics-{dataset}-{NAMESPACE}"
    base_labels = {"instance": instance, "job": job, "namespace": NAMESPACE}
    for label_key, metrics in groups.items():
        if not metrics:
            continue
        extra = dict(label_key)
        doc = {
            "@timestamp": timestamp,
            "data_stream.type": "metrics",
            "data_stream.dataset": dataset,
            "data_stream.namespace": NAMESPACE,
            "labels": {**base_labels, **extra},
            "metrics": metrics,
        }
        lines.append(json.dumps({"create": {"_index": index}}))
        lines.append(json.dumps(doc))
    return "\n".join(lines) + "\n"


def bulk_index(body: str) -> tuple[int, int]:
    if not body.strip():
        return 0, 0
    resp = requests.post(
        f"{ES_URL}/_bulk",
        data=body.encode(),
        headers={"Content-Type": "application/x-ndjson"},
        timeout=15,
    )
    result = resp.json()
    ok = sum(1 for item in result.get("items", []) if next(iter(item.values())).get("status", 0) in (200, 201))
    err = len(result.get("items", [])) - ok
    return ok, err


def wait_for_es(url: str, max_wait: int = 120) -> None:
    print(f"Waiting for Elasticsearch at {url}...", flush=True)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            r = requests.get(f"{url}/_cluster/health", timeout=5)
            if r.status_code == 200 and r.json().get("status") in ("green", "yellow"):
                print("Elasticsearch ready.", flush=True)
                return
        except Exception:
            pass
        time.sleep(3)
    print("ERROR: Elasticsearch did not become ready in time.", flush=True)
    sys.exit(1)


def main() -> None:
    wait_for_es(ES_URL)
    # Declare counter/gauge typing from each exporter's own ``# TYPE`` lines
    # before the first document creates the data stream with inferred mappings.
    for url, dataset, _job, _instance in TARGETS:
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            ensure_index_template(dataset, parse_metric_types(resp.text))
        except Exception as exc:
            print(f"  template {dataset}: could not scrape for types: {exc}", flush=True)
    for url, dataset, _job, _instance in TARGETS:
        print(f"Scraping {url} every {SCRAPE_INTERVAL}s → metrics-{dataset}-{NAMESPACE}", flush=True)
    while True:
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        for url, dataset, job, instance in TARGETS:
            # One target being down must not stop the others: the redis pack is
            # validated from this same rig and cannot regress because a
            # secondary exporter is unhealthy.
            try:
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                groups = parse_prometheus(resp.text)
                body = build_bulk_body(groups, ts, dataset, job, instance)
                ok, err = bulk_index(body)
                print(f"{ts} {dataset}: {len(groups)} series → indexed {ok}, errors {err}", flush=True)
            except Exception as exc:
                print(f"{ts} {dataset}: scrape error: {exc}", flush=True)
        time.sleep(SCRAPE_INTERVAL)


if __name__ == "__main__":
    main()
