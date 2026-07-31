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
]

_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


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
