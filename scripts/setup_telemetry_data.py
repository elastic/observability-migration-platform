#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Set up source-agnostic telemetry data from migrated artifact requirements."""

from __future__ import annotations

import argparse
import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from observability_migration.core.telemetry_contract import (
    build_combined_telemetry_contract,
    build_telemetry_contract,
    merge_metric_kind_overrides,
    metric_kinds_from_prometheus_metadata,
)
from observability_migration.core.telemetry_data import (
    generate_documents,
    ingest_documents,
    setup_templates_and_streams,
)

ES_ENDPOINT = os.environ.get("ELASTICSEARCH_ENDPOINT", "")
API_KEY = os.environ.get("KEY", "")
HEADERS = {
    "Authorization": f"ApiKey {API_KEY}",
    "Content-Type": "application/json",
}
CTX = ssl.create_default_context()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "artifact_dir",
        nargs="*",
        help="Migrated dashboard artifact directory containing yaml/ and optional verification_packets.json. Repeat to combine sources.",
    )
    parser.add_argument(
        "--es-endpoint",
        default=os.environ.get("ELASTICSEARCH_ENDPOINT", ""),
        help="Elasticsearch endpoint URL",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("KEY", ""),
        help="Elasticsearch API key",
    )
    parser.add_argument(
        "--data-hours",
        type=float,
        default=float(os.environ.get("DATA_HOURS", "2")),
        help="Hours of synthetic data to generate",
    )
    parser.add_argument(
        "--interval-sec",
        type=int,
        default=int(os.environ.get("INTERVAL_SEC", "60")),
        help="Seconds between generated samples",
    )
    parser.add_argument(
        "--batch-docs",
        type=int,
        default=int(os.environ.get("BATCH_DOC_LIMIT", "5000")),
        help="Documents per bulk request",
    )
    parser.add_argument(
        "--no-recreate",
        action="store_true",
        help=(
            "Skip all index template and data stream operations. Use this when the "
            "target streams already exist with the desired mappings and you only "
            "want to ingest more synthetic documents."
        ),
    )
    parser.add_argument(
        "--max-combinations",
        type=int,
        default=int(os.environ.get("MAX_COMBINATIONS", "12")),
        help=(
            "Maximum number of dimension combinations to emit per stream per "
            "timestamp. Lower this for very high-cardinality contracts."
        ),
    )
    parser.add_argument(
        "--rules-file",
        action="append",
        default=[],
        help=(
            "Rule-pack YAML/JSON file providing authoritative metric_kinds "
            "(counter/gauge) overrides. Repeat to layer multiple packs."
        ),
    )
    parser.add_argument(
        "--prometheus-url",
        default=os.environ.get("PROMETHEUS_URL", ""),
        help=(
            "Optional live Prometheus base URL. When set, /api/v1/metadata is "
            "queried for ground-truth metric types. Rule-pack overrides win over "
            "live metadata."
        ),
    )
    return parser.parse_args(argv)


def _fetch_prometheus_metadata(prometheus_url: str) -> dict[str, Any]:
    """Fetch Prometheus ``/api/v1/metadata``; return ``{}`` on any failure."""
    url = f"{prometheus_url.rstrip('/')}/api/v1/metadata"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, context=CTX, timeout=30) as resp:
            payload = resp.read()
        return json.loads(payload) if payload else {}
    except (urllib.error.URLError, json.JSONDecodeError, ValueError):
        return {}


def load_metric_kind_overrides(
    rules_files: list[str] | None,
    prometheus_url: str = "",
) -> dict[str, str]:
    """Build an authoritative metric-kind override map.

    Composes (most authoritative first) rule-pack ``metric_kinds`` and, when a
    Prometheus URL is given, live ``/api/v1/metadata`` types. Returns an empty
    map when no source yields anything so the contract falls back to inference.
    """
    rule_pack_kinds: dict[str, str] = {}
    if rules_files:
        from observability_migration.adapters.source.grafana.rules import load_rule_pack_files

        pack = load_rule_pack_files(rules_files)
        rule_pack_kinds = dict(getattr(pack, "metric_kinds", {}) or {})

    metadata_kinds: dict[str, str] = {}
    if prometheus_url:
        metadata_kinds = metric_kinds_from_prometheus_metadata(
            _fetch_prometheus_metadata(prometheus_url)
        )

    return merge_metric_kind_overrides(rule_pack_kinds, metadata_kinds)


def es_request(
    method: str,
    path: str,
    body: Any | None = None,
    content_type: str = "application/json",
) -> dict[str, Any]:
    url = f"{ES_ENDPOINT.rstrip('/')}{path}"
    data = None
    if body is not None:
        data = json.dumps(body).encode() if isinstance(body, dict) else body
    headers = {**HEADERS}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=120) as resp:
            payload = resp.read()
            return json.loads(payload) if payload else {"acknowledged": True}
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode(errors="replace")
        if exc.code == 404 and method == "DELETE":
            return {"acknowledged": True}
        print(f"  HTTP {exc.code}: {payload[:300]}")
        try:
            return json.loads(payload) if payload else {"error": {"status": exc.code}}
        except json.JSONDecodeError:
            return {"error": {"status": exc.code, "reason": payload[:300]}}
    except urllib.error.URLError as exc:
        raise NetworkError(str(exc.reason)) from exc


class NetworkError(RuntimeError):
    """Raised when the Elasticsearch endpoint cannot be reached at all."""


def main(argv: list[str] | None = None) -> int:
    global ES_ENDPOINT
    global API_KEY
    global HEADERS

    args = parse_args(argv)
    ES_ENDPOINT = args.es_endpoint
    API_KEY = args.api_key
    HEADERS = {
        "Authorization": f"ApiKey {API_KEY}",
        "Content-Type": "application/json",
    }

    raw_dirs = list(args.artifact_dir or [])
    if not raw_dirs and os.environ.get("DASHBOARD_YAML_DIR", ""):
        raw_dirs = [os.environ["DASHBOARD_YAML_DIR"]]
    if not raw_dirs:
        print("ERROR: artifact_dir or DASHBOARD_YAML_DIR must be provided")
        return 1

    artifact_dirs: list[Path] = []
    seen_paths: set[Path] = set()
    for raw in raw_dirs:
        path = Path(raw).resolve()
        if not path.exists():
            print(f"ERROR: artifact directory does not exist: {raw}")
            return 1
        if path in seen_paths:
            print(f"WARN: ignoring duplicate artifact directory: {raw}")
            continue
        seen_paths.add(path)
        artifact_dirs.append(Path(raw))

    if args.data_hours <= 0:
        print("ERROR: --data-hours must be greater than 0")
        return 1
    if args.interval_sec <= 0:
        print("ERROR: --interval-sec must be greater than 0")
        return 1
    if args.max_combinations <= 0:
        print("ERROR: --max-combinations must be greater than 0")
        return 1

    if not ES_ENDPOINT or not API_KEY:
        print("ERROR: ELASTICSEARCH_ENDPOINT and KEY must be set (or pass --es-endpoint/--api-key)")
        return 1

    metric_kind_overrides = load_metric_kind_overrides(args.rules_file, args.prometheus_url)
    contract = (
        build_telemetry_contract(artifact_dirs[0], metric_kind_overrides=metric_kind_overrides)
        if len(artifact_dirs) == 1
        else build_combined_telemetry_contract(
            artifact_dirs, metric_kind_overrides=metric_kind_overrides
        )
    )
    streams = contract.get("streams") or {}
    if not streams:
        print(f"ERROR: no telemetry requirements discovered in {', '.join(str(path) for path in artifact_dirs)}")
        return 1

    print("=== Common Telemetry Data Setup ===")
    print(f"Artifact dirs: {', '.join(str(path) for path in artifact_dirs)}")
    print(f"Streams: {len(streams)}")
    print("Stream field counts:")
    for stream_name, stream in sorted(streams.items()):
        print(f"  {stream_name}: {len(stream.get('fields') or {})} fields")

    try:
        if args.no_recreate:
            print("Skipping index template and data stream creation (--no-recreate)")
        else:
            setup_templates_and_streams(contract, es_request, recreate=True)
        summary = ingest_documents(
            generate_documents(
                contract,
                data_hours=args.data_hours,
                interval_sec=args.interval_sec,
                max_combinations=args.max_combinations,
            ),
            es_request,
            batch_docs=args.batch_docs,
        )
    except NetworkError as exc:
        print(f"Setup failed: cannot reach Elasticsearch endpoint: {exc}")
        return 1
    except RuntimeError as exc:
        print(f"Setup failed: {exc}")
        return 1

    print("Documents ingested per stream:")
    for stream_name, count in sorted(summary.docs_per_stream.items()):
        print(f"  {stream_name}: {count} docs")
    print(f"Ingested documents: {summary.ok}, errors: {summary.errors}")
    for sample in summary.error_samples:
        print(f"  ingest error sample: {sample}")
    if summary.errors:
        print("Setup failed: bulk ingest reported errors")
        return 1
    print("Setup complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
