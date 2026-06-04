#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Verify emitted alert-rule payloads upload to Kibana disabled by default."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path = [path for path in sys.path if path != str(ROOT)]
sys.path.insert(0, str(ROOT))

from observability_migration.core.http import resolve_tls  # noqa: E402, I001

# create_rule / run_alerting_preflight are imported here (not only used inline)
# so existing tests can patch them on this module to intercept the round trip.
from observability_migration.targets.kibana.alerting import (  # noqa: E402
    collect_emitted_rule_payloads,
    create_rule,
    run_alerting_preflight,
    verify_emitted_rule_uploads,
)


DEFAULT_COMPARISON_PATHS = [
    ROOT / "examples/alerting/generated/grafana/alerts/alert_comparison_results.json",
    ROOT / "examples/alerting/generated/datadog/alerts/monitor_comparison_results.json",
]
DEFAULT_NAME_PREFIX = "[verification "


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_emitted_payloads(paths: list[Path]) -> list[dict[str, Any]]:
    reports = [_load_json(path) for path in paths]
    return collect_emitted_rule_payloads(*reports)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--comparison",
        dest="comparison_paths",
        action="append",
        default=[],
        help="Comparison JSON path to read. May be provided multiple times.",
    )
    parser.add_argument(
        "--kibana-url",
        default=os.getenv("KIBANA_ENDPOINT", os.getenv("KIBANA_URL", "")),
        help="Kibana URL (defaults to KIBANA_ENDPOINT or KIBANA_URL).",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("KIBANA_API_KEY", os.getenv("KEY", "")),
        help="Kibana API key (defaults to KIBANA_API_KEY or KEY).",
    )
    parser.add_argument("--space-id", default="", help="Optional Kibana space ID.")
    parser.add_argument("--limit", type=int, default=0, help="Optional max number of emitted payloads to verify.")
    parser.add_argument("--keep-rules", action="store_true", help="Keep verification rules instead of deleting them.")
    parser.add_argument(
        "--name-prefix",
        default=DEFAULT_NAME_PREFIX,
        help="Prefix for temporary verification rule names.",
    )
    parser.add_argument(
        "--ca-cert",
        default=os.getenv("OBS_MIGRATE_CA_CERT", ""),
        help="Path to a custom CA certificate (bundle) used to verify TLS. Defaults to OBS_MIGRATE_CA_CERT.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        default=str(os.getenv("OBS_MIGRATE_INSECURE", "") or "").strip().lower() in {"1", "true", "yes", "on"},
        help="Disable TLS certificate verification. Defaults to OBS_MIGRATE_INSECURE.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    comparison_paths = [Path(path) for path in args.comparison_paths] if args.comparison_paths else DEFAULT_COMPARISON_PATHS

    if not args.kibana_url:
        print("ERROR: Kibana URL is required (--kibana-url or KIBANA_ENDPOINT/KIBANA_URL).", file=sys.stderr)
        return 2
    if not args.api_key:
        print("ERROR: Kibana API key is required (--api-key or KIBANA_API_KEY/KEY).", file=sys.stderr)
        return 2

    missing = [str(path) for path in comparison_paths if not path.exists()]
    if missing:
        print(json.dumps({"error": "missing_comparison_files", "paths": missing}, indent=2))
        return 2

    payloads = _load_emitted_payloads(comparison_paths)
    if args.limit > 0:
        payloads = payloads[: args.limit]
    if not payloads:
        print(json.dumps({"error": "no_emitted_rule_payloads"}, indent=2))
        return 2

    verify = resolve_tls(ca_cert=args.ca_cert, insecure=bool(args.insecure))
    preflight = run_alerting_preflight(
        args.kibana_url,
        api_key=args.api_key,
        space_id=args.space_id,
        verify=verify,
    )
    summary = verify_emitted_rule_uploads(
        args.kibana_url,
        payloads,
        api_key=args.api_key,
        space_id=args.space_id,
        keep_rules=bool(args.keep_rules),
        name_prefix=args.name_prefix,
        preflight=preflight,
        verify=verify,
        # Inject the script-module bindings so test patches on this module
        # (run_alerting_preflight / create_rule) still intercept the round trip.
        create_rule_fn=create_rule,
    )
    summary = {
        "comparison_paths": [
            str(path.relative_to(ROOT)) if path.is_absolute() and path.is_relative_to(ROOT) else str(path)
            for path in comparison_paths
        ],
        **summary,
    }
    print(json.dumps(summary, indent=2))

    if summary.get("error") == "preflight_unreachable":
        return 2
    if (
        summary["creation_errors"]
        or summary["enabled_true_in_create_response"]
        or summary["enabled_true_in_rule_listing"]
        or summary["cleanup"]["failed_rule_ids"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
