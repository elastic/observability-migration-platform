#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
#
# Lightweight Prometheus → Elasticsearch ingestion in prometheus_native format.
# Scrapes redis_exporter and writes docs to the metrics-redis.prometheus-default
# data stream using the metrics.* + labels.* layout that matches the built-in
# metrics-prometheus@mappings template (labels.* = passthrough TSDB dimension).
# This gives the migration pipeline the prometheus_native schema profile so
# TS queries, label controls, and RATE() all work correctly.

import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

EXPORTER_URL = "http://redis_exporter:9121/metrics"
ES_URL = "http://elasticsearch:9200"
INDEX = "metrics-redis.prometheus-default"
JOB = "redis_exporter"
INSTANCE = "redis:6379"
NAMESPACE = "default"
SCRAPE_INTERVAL = 10
RETRY_DELAY = 5

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


def build_bulk_body(groups: dict, timestamp: str) -> str:
    lines = []
    base_labels = {"instance": INSTANCE, "job": JOB, "namespace": NAMESPACE}
    for label_key, metrics in groups.items():
        if not metrics:
            continue
        extra = dict(label_key)
        doc = {
            "@timestamp": timestamp,
            "data_stream.type": "metrics",
            "data_stream.dataset": "redis.prometheus",
            "data_stream.namespace": NAMESPACE,
            "labels": {**base_labels, **extra},
            "metrics": metrics,
        }
        lines.append(json.dumps({"create": {"_index": INDEX}}))
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
    ok = sum(1 for item in result.get("items", []) if list(item.values())[0].get("status", 0) in (200, 201))
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
    print(f"Scraping {EXPORTER_URL} every {SCRAPE_INTERVAL}s → {INDEX}", flush=True)
    while True:
        try:
            resp = requests.get(EXPORTER_URL, timeout=10)
            resp.raise_for_status()
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            groups = parse_prometheus(resp.text)
            body = build_bulk_body(groups, ts)
            ok, err = bulk_index(body)
            print(f"{ts} scraped {len(groups)} series → indexed {ok}, errors {err}", flush=True)
        except Exception as exc:
            print(f"scrape error: {exc}", flush=True)
            time.sleep(RETRY_DELAY)
            continue
        time.sleep(SCRAPE_INTERVAL)


if __name__ == "__main__":
    main()
