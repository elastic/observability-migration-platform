#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Generate a telemetry producer contract from migrated dashboard artifacts."""

from __future__ import annotations

import argparse
from pathlib import Path

from observability_migration.core.telemetry_contract import (
    build_combined_telemetry_contract,
    build_schema_change_report,
    build_telemetry_contract,
    write_telemetry_contract,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "artifact_dir",
        nargs="+",
        help="Dashboard artifact directory containing yaml/ and verification_packets.json. Repeat to merge multiple sources.",
    )
    parser.add_argument(
        "--output",
        default="telemetry_contract.json",
        help="Output JSON path",
    )
    parser.add_argument(
        "--schema-report",
        default="",
        help="Optional Markdown output path describing source-to-target schema changes",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    artifact_dirs = [Path(path) for path in args.artifact_dir]
    contract = (
        build_telemetry_contract(artifact_dirs[0])
        if len(artifact_dirs) == 1
        else build_combined_telemetry_contract(artifact_dirs)
    )
    write_telemetry_contract(contract, args.output)
    print(f"Telemetry contract written: {args.output}")
    if args.schema_report:
        report = build_schema_change_report(artifact_dirs)
        report_path = Path(args.schema_report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
        print(f"Schema change report written: {args.schema_report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
