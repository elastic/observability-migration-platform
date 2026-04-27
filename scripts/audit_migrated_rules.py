#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Audit migrated Kibana rules and optionally disable the enabled subset."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path = [path for path in sys.path if path != str(ROOT)]
sys.path.insert(0, str(ROOT))

from observability_migration.targets.kibana.alerting import audit_migrated_rules  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
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
    parser.add_argument("--per-page", type=int, default=100, help="Rules to fetch per page.")
    parser.add_argument("--max-pages", type=int, default=20, help="Maximum pages to fetch.")
    parser.add_argument(
        "--disable-enabled",
        action="store_true",
        help="Disable any migrated rules that are currently enabled.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.kibana_url:
        print("ERROR: Kibana URL is required (--kibana-url or KIBANA_ENDPOINT/KIBANA_URL).", file=sys.stderr)
        return 2
    if not args.api_key:
        print("ERROR: Kibana API key is required (--api-key or KIBANA_API_KEY/KEY).", file=sys.stderr)
        return 2

    result = audit_migrated_rules(
        args.kibana_url,
        api_key=args.api_key,
        space_id=args.space_id,
        per_page=args.per_page,
        max_pages=args.max_pages,
        disable_enabled=args.disable_enabled,
    )
    print(json.dumps(result, indent=2))

    if result.get("errors"):
        return 2
    if args.disable_enabled:
        return 0 if not result["remediation"]["failed_rule_ids"] else 1
    return 0 if not result["enabled_migrated_rule_ids"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
