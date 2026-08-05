# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Fetch/check the Kibana Dashboards API OpenAPI bundle.

The committed authority for offline CI and native-payload work is:

  docs/dashboards/kibana_dashboards_api.openapi.yaml

Use ``make check-native-schema`` to validate that pinned file. Use
``make refresh-native-schema`` (or this script without ``--check-only``) to
re-fetch from Elastic's hosted Dashboards API bundle and rewrite the pin.

Elastic's standard Kibana OpenAPI bundle may still contain redirect-only shells
while the full schemas are externally hosted, so this helper separates "fetch
the latest bundle" from "require the bundle to contain full request schemas".
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SCHEMA_URL = "https://dashboardsapispec.kibana.dev/dashboards-openapi.yaml"
DEFAULT_OUTPUT = Path("docs/dashboards/kibana_dashboards_api.openapi.yaml")
# Local path used by ``make check-native-schema`` so CI does not depend on the
# live external host remaining reachable or unchanged mid-PR.
COMMITTED_SCHEMA = DEFAULT_OUTPUT


def fetch_schema_text(url: str, *, timeout: int = 30) -> str:
    """Fetch a URL or local file path and return its text content."""
    if url.startswith("file://"):
        return Path(url[7:]).read_text(encoding="utf-8")
    if "://" not in url:
        return Path(url).read_text(encoding="utf-8")
    request = urllib.request.Request(url, headers={"User-Agent": "obs-migrate-schema-fetcher"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset)


def parse_schema(text: str) -> dict[str, Any]:
    """Parse a JSON/YAML OpenAPI document."""
    payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise ValueError("schema document is not a JSON/YAML object")
    return payload


def dashboard_paths(schema: dict[str, Any]) -> dict[str, Any]:
    """Return only `/api/dashboards*` path entries from an OpenAPI document."""
    paths = schema.get("paths")
    if not isinstance(paths, dict):
        return {}
    return {
        path: value
        for path, value in paths.items()
        if isinstance(path, str) and path.startswith("/api/dashboards")
    }


def _operation_has_request_schema(operation: Any) -> bool:
    if not isinstance(operation, dict):
        return False
    request_body = operation.get("requestBody")
    if not isinstance(request_body, dict):
        return False
    content = request_body.get("content")
    if not isinstance(content, dict):
        return False
    return any(isinstance(media, dict) and isinstance(media.get("schema"), dict) for media in content.values())


def has_full_dashboard_write_schema(schema: dict[str, Any]) -> bool:
    """Whether POST/PUT dashboard operations expose request-body schemas."""
    for path_item in dashboard_paths(schema).values():
        if not isinstance(path_item, dict):
            continue
        for method in ("post", "put"):
            if _operation_has_request_schema(path_item.get(method)):
                return True
    return False


def summarize(schema: dict[str, Any]) -> dict[str, Any]:
    paths = dashboard_paths(schema)
    operations = 0
    for path_item in paths.values():
        if isinstance(path_item, dict):
            operations += sum(1 for method in ("get", "post", "put", "delete", "patch") if method in path_item)
    return {
        "title": schema.get("info", {}).get("title") if isinstance(schema.get("info"), dict) else "",
        "version": schema.get("info", {}).get("version") if isinstance(schema.get("info"), dict) else "",
        "dashboard_paths": len(paths),
        "dashboard_operations": operations,
        "has_full_dashboard_write_schema": has_full_dashboard_write_schema(schema),
    }


def validate_dashboard_schema(schema: dict[str, Any], *, require_full_schema: bool = False) -> dict[str, Any]:
    summary = summarize(schema)
    if summary["dashboard_paths"] == 0:
        raise ValueError("OpenAPI document has no /api/dashboards paths")
    if require_full_schema and not summary["has_full_dashboard_write_schema"]:
        raise ValueError(
            "OpenAPI document has /api/dashboards paths but no POST/PUT request-body schema. "
            "Elastic's standard bundle may contain redirect-only shells while the technical-preview "
            "Dashboards API schemas are externally hosted; pass --url for the full external bundle "
            "or run Kibana's oas_docs `make api-docs-overlay-external` output."
        )
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch or validate the Kibana Dashboards API OpenAPI schema bundle. "
            f"The committed pin is {COMMITTED_SCHEMA}."
        ),
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("KIBANA_DASHBOARDS_API_SCHEMA_URL", DEFAULT_SCHEMA_URL),
        help=(
            "OpenAPI URL or local file path. Defaults to %(default)s for refresh; "
            "override with KIBANA_DASHBOARDS_API_SCHEMA_URL or this flag. "
            f"CI checks the committed pin via --url {COMMITTED_SCHEMA}."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Where to write the fetched OpenAPI YAML bundle (default: %(default)s).",
    )
    parser.add_argument("--timeout", type=int, default=30, help="Fetch timeout in seconds.")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Validate the schema from --url but do not write --output.",
    )
    parser.add_argument(
        "--require-full-schema",
        action="store_true",
        help="Fail unless POST/PUT /api/dashboards operations include request-body schemas.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        # Preserve upstream formatting on refresh (avoid yaml.safe_dump rewrites).
        text = fetch_schema_text(args.url, timeout=args.timeout)
        schema = parse_schema(text)
        summary = validate_dashboard_schema(schema, require_full_schema=args.require_full_schema)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not args.check_only:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if not text.endswith("\n"):
            text = f"{text}\n"
        args.output.write_text(text, encoding="utf-8")
        print(f"Wrote {args.output}")

    print(
        "Dashboards API schema: "
        f"{summary['dashboard_paths']} path(s), "
        f"{summary['dashboard_operations']} operation(s), "
        f"full_write_schema={summary['has_full_dashboard_write_schema']}"
    )
    if not summary["has_full_dashboard_write_schema"]:
        print(
            "NOTE: this bundle appears to contain redirect-only dashboard docs. "
            "Refresh from the full external Dashboards API bundle "
            f"(default {DEFAULT_SCHEMA_URL}) and commit {COMMITTED_SCHEMA}.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
