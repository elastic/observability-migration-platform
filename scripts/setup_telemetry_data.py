#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Set up source-agnostic telemetry data from migrated artifact requirements.

Thin shim over ``observability_migration.core.sample_data``: this script keeps the
historical positional / ``DASHBOARD_YAML_DIR`` CLI surface, but the contract build,
stream setup, document generation, ingest, and ES/TLS handling all live in the
library (shared with ``obs-migrate seed-sample-data``). Prefer the subcommand for
new use; this entry point remains for existing automation.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from observability_migration.core.sample_data import (
    NetworkError,
    load_metric_kind_overrides,
    make_es_request,
    seed_sample_data,
)


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
        "--purge-foreign-streams",
        action="store_true",
        help=(
            "Before seeding, delete any data stream that overlaps the contract's "
            "wildcards (e.g. metrics-*/logs-*) but was NOT created by this seeder. "
            "Leftover experiment/parity streams with incompatible mappings make "
            "shared fields conflict across indices, so panels querying the wildcard "
            "return zero rows. Seeder-owned (telemetry-data-*) streams are kept."
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    es_endpoint = args.es_endpoint
    api_key = args.api_key

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
    if not es_endpoint or not api_key:
        print("ERROR: ELASTICSEARCH_ENDPOINT and KEY must be set (or pass --es-endpoint/--api-key)")
        return 1

    overrides = load_metric_kind_overrides(args.rules_file, args.prometheus_url)
    request = make_es_request(es_endpoint, api_key)

    print("=== Common Telemetry Data Setup ===")
    print(f"Artifact dirs: {', '.join(str(path) for path in artifact_dirs)}")
    try:
        summary = seed_sample_data(
            artifact_dirs,
            request,
            data_hours=args.data_hours,
            interval_sec=args.interval_sec,
            batch_docs=args.batch_docs,
            max_combinations=args.max_combinations,
            no_recreate=args.no_recreate,
            purge_foreign=args.purge_foreign_streams,
            metric_kind_overrides=overrides,
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
