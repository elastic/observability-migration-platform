#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Generate producer-side routing config that maps source labels to target fields.

Emits OpenTelemetry Collector, Prometheus remote-write relabel, and/or Elastic
Agent configuration so live telemetry producers ship the same field names the
migrated dashboards expect.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from observability_migration.adapters.source.grafana.routing_artifacts import (
    forward_label_mapping,
    generate_routing_artifacts,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rules-file",
        action="append",
        default=[],
        help="Rule-pack YAML/JSON providing label_rewrites overrides. Repeatable.",
    )
    parser.add_argument(
        "--schema-profile",
        choices=["prometheus_native", "prometheus_remote_write", "otel"],
        default=None,
        help="Target schema profile. Omit to emit all formats.",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Directory to write artifacts into. Omit to print to stdout.",
    )
    return parser.parse_args(argv)


def build_routing_artifacts(
    *,
    rules_files: list[str] | None,
    schema_profile: str | None,
) -> dict[str, str]:
    """Build the routing artifact file map from rule-pack label rewrites."""
    label_rewrites: dict[str, str] = {}
    if rules_files:
        from observability_migration.adapters.source.grafana.rules import load_rule_pack_files

        pack = load_rule_pack_files(rules_files)
        label_rewrites = dict(pack.label_rewrites or {})
    forward = forward_label_mapping(label_rewrites=label_rewrites)
    return generate_routing_artifacts(forward, schema_profile=schema_profile)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    artifacts = build_routing_artifacts(
        rules_files=args.rules_file, schema_profile=args.schema_profile
    )
    if not artifacts:
        print("No source→target label renames found; nothing to emit.")
        return 0
    if args.out_dir:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in artifacts.items():
            (out_dir / filename).write_text(content, encoding="utf-8")
            print(f"Wrote {out_dir / filename}")
    else:
        for filename, content in artifacts.items():
            print(f"# === {filename} ===")
            print(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
