# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Unified source-agnostic CLI entry point.

Orchestrates migrations by calling source adapters and shared
Kibana target runtime directly, without delegating to the
dedicated source CLIs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

import observability_migration.adapters.source.datadog.adapter
import observability_migration.adapters.source.grafana.adapter
import observability_migration.targets.kibana.adapter  # noqa: F401
from observability_migration.core.cli_contract import ASSET_CHOICES, normalize_requested_assets
from observability_migration.core.interfaces.registries import source_registry, target_registry

_UPLOAD_SHAPE_HELP = (
    "Accepted input shapes: a directory of .yaml files, a dashboard artifact "
    "dir with a 'yaml/' child (for example "
    "'migration_output/dashboards' or 'migration_output/dashboards/yaml'), "
    "or that artifact dir's sibling 'compiled/' directory (for example "
    "'migration_output/dashboards/compiled')."
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obs-migrate",
        description="Source-agnostic observability migration platform.",
    )
    sub = parser.add_subparsers(dest="command")

    migrate = sub.add_parser("migrate", help="Run a migration")
    migrate.add_argument(
        "--source", choices=source_registry.names(), required=True,
        help="Source vendor (grafana, datadog, ...)",
    )
    migrate.add_argument("--input-mode", default="files", choices=["files", "api"])
    migrate.add_argument("--input-dir", default=".")
    migrate.add_argument("--output-dir", default="migration_output")
    migrate.add_argument("--target", default="kibana")
    migrate.add_argument("--data-view", default="metrics-*")
    migrate.add_argument(
        "--assets",
        choices=ASSET_CHOICES,
        default="dashboards",
        help="Asset family to migrate: dashboards only, alerts only, or both",
    )
    migrate.add_argument("--esql-index", default="")
    migrate.add_argument("--logs-index", default="")
    native_promql_group = migrate.add_mutually_exclusive_group()
    native_promql_group.add_argument(
        "--native-promql",
        dest="native_promql_flag",
        action="store_const",
        const="force_on",
        help=(
            "Force native PROMQL emission regardless of cluster support detection "
            "(for Elastic clusters with the ES|QL PROMQL command). Forwarded to "
            "the underlying source adapter."
        ),
    )
    native_promql_group.add_argument(
        "--no-native-promql",
        dest="native_promql_flag",
        action="store_const",
        const="force_off",
        help=(
            "Force ES|QL translation even when the cluster supports the PROMQL command "
            "(opt out of the auto-detected default). Forwarded to the underlying "
            "source adapter."
        ),
    )
    migrate.set_defaults(native_promql_flag="auto")
    migrate.add_argument(
        "--fetch-alerts",
        action="store_true",
        help=(
            "Deprecated compatibility alias for alert-capable runs; "
            "prefer --assets alerts or --assets all."
        ),
    )
    migrate.add_argument(
        "--create-alert-rules", action="store_true",
        help=(
            "Create emitted Kibana alerting rules for alert-capable asset "
            "selection (--assets alerts, --assets all, or the deprecated "
            "--fetch-alerts alias). Rules are created disabled by default, "
            "tagged 'obs-migration'. Requires alert-capable asset selection, "
            "--kibana-url, and --kibana-api-key. Writes "
            "alert_rule_upload_results.json (Grafana) or "
            "monitor_rule_upload_results.json (Datadog) to the output "
            "directory."
        ),
    )
    migrate.add_argument("--grafana-token", default="",
                         help="Grafana API bearer token for alert extraction")
    migrate.add_argument("--monitor-ids", default="",
                         help="Comma-separated Datadog monitor IDs to extract (Datadog only)")
    migrate.add_argument("--monitor-query", default="",
                         help="Datadog monitor search query (Datadog only)")
    migrate.add_argument("--env-file", default="",
                         help="Path to credentials .env file (Datadog)")
    migrate.add_argument(
        "--field-profile",
        default="otel",
        help=(
            "Target field mapping profile. Defaults to 'otel' for all sources; "
            "Grafana currently supports 'otel' only, while Datadog also supports "
            "Datadog-specific built-ins and YAML profile files."
        ),
    )
    migrate.add_argument(
        "--compile", action="store_true",
        help=(
            "Compile generated YAML to NDJSON. Grafana always compiles as part of "
            "'obs-migrate migrate'; Datadog is now also compiled by default for "
            "parity. The flag is kept for compatibility with the dedicated source CLIs."
        ),
    )
    migrate.add_argument("--validate", action="store_true")
    migrate.add_argument("--upload", action="store_true")
    migrate.add_argument("--es-url", default="")
    migrate.add_argument("--es-api-key", default="")
    migrate.add_argument("--kibana-url", default="")
    migrate.add_argument("--kibana-api-key", default="")
    migrate.add_argument("--space-id", default="")
    migrate.add_argument("--rules-file", action="append", default=[])
    migrate.add_argument("--plugin", action="append", default=[])
    migrate.add_argument("--polish-metadata", action="store_true")
    migrate.add_argument("--preflight", action="store_true")
    migrate.add_argument("--dataset-filter", default="",
                         help="Explicit data_stream.dataset filter for metrics")
    migrate.add_argument("--logs-dataset-filter", default="",
                         help="Explicit data_stream.dataset filter for logs")
    migrate.add_argument("--smoke", action="store_true")
    migrate.add_argument("--browser-audit", action="store_true")
    migrate.add_argument("--capture-screenshots", action="store_true")
    migrate.add_argument("--smoke-output", default="")
    migrate.add_argument("--smoke-timeout", type=int, default=30)
    migrate.add_argument("--chrome-binary", default="")
    migrate.add_argument("--smoke-report", default="")

    compile_cmd = sub.add_parser("compile", help="Compile YAML to NDJSON")
    compile_cmd.add_argument("--yaml-dir", required=True, help="Directory with dashboard YAML files")
    compile_cmd.add_argument("--output-dir", required=True, help="Output directory for NDJSON")

    upload_cmd = sub.add_parser(
        "upload",
        help="Compile dashboard YAML to NDJSON and upload to Kibana",
        description=(
            "Compile dashboard YAML (via kb-dashboard-cli) and upload the resulting "
            f"NDJSON to Kibana. {_UPLOAD_SHAPE_HELP}"
        ),
    )
    upload_group = upload_cmd.add_mutually_exclusive_group(required=True)
    upload_group.add_argument(
        "--yaml-dir",
        help="Path to a dashboard YAML directory input for compile+upload. "
             f"{_UPLOAD_SHAPE_HELP}",
    )
    upload_group.add_argument(
        "--compiled-dir",
        help="[Deprecated alias for --yaml-dir] Kept for backward compatibility. "
             "May point at the dashboard artifact dir's sibling 'compiled/' directory "
             "(for example 'migration_output/dashboards/compiled'). Despite the name, "
             "this upload step recompiles YAML from the matching 'yaml/' directory; "
             "it does not consume pre-compiled NDJSON.",
    )
    upload_cmd.add_argument("--kibana-url", required=True)
    upload_cmd.add_argument("--kibana-api-key", default="")
    upload_cmd.add_argument("--space-id", default="")

    cluster_cmd = sub.add_parser("cluster", help="Manage target Kibana cluster")
    cluster_cmd.add_argument("action", choices=["list-dashboards", "ensure-data-views", "delete-dashboards", "detect-serverless"],
                             help="Cluster management action")
    cluster_cmd.add_argument("--kibana-url", required=True)
    cluster_cmd.add_argument("--kibana-api-key", default="")
    cluster_cmd.add_argument("--space-id", default="")
    cluster_cmd.add_argument("--dashboard-ids", default="",
                             help="Comma-separated dashboard IDs (for delete-dashboards)")
    cluster_cmd.add_argument("--data-view-patterns", default="metrics-*",
                             help="Comma-separated data view patterns (for ensure-data-views)")

    extensions_cmd = sub.add_parser("extensions", help="Show adapter extension points")
    extensions_cmd.add_argument(
        "--source", choices=source_registry.names(), required=True,
        help="Source vendor to inspect",
    )
    extensions_cmd.add_argument(
        "--format", choices=["json", "yaml"], default="json",
        help="Output format",
    )
    extensions_cmd.add_argument(
        "--template-only",
        action="store_true",
        help="Print only the starter extension template for the source adapter",
    )
    extensions_cmd.add_argument(
        "--template-out",
        default="",
        help="Write the starter extension template to a file",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    """Unified CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "migrate":
        _run_migrate(args)
    elif args.command == "compile":
        _run_compile(args)
    elif args.command == "upload":
        _run_upload(args)
    elif args.command == "extensions":
        _run_extensions(args)
    elif args.command == "cluster":
        _run_cluster(args)
    else:
        parser.print_help()
        sys.exit(1)


def _run_migrate(args: Any) -> None:
    """Orchestrate a migration through the adapter registry."""
    source = args.source

    if source == "grafana":
        _run_grafana_migration(args)
    elif source == "datadog":
        _run_datadog_migration(args)
    else:
        print(f"Source '{source}' is not yet supported.", file=sys.stderr)
        sys.exit(1)


def _run_grafana_migration(args: Any) -> None:
    """Run the Grafana migration pipeline directly."""
    from observability_migration.adapters.source.grafana.cli import main as grafana_main

    legacy_argv = [
        "--source", args.input_mode,
        "--input-dir", args.input_dir,
        "--output-dir", args.output_dir,
        "--data-view", args.data_view,
        "--field-profile", getattr(args, "field_profile", "otel"),
    ]
    requested_assets = getattr(args, "assets", None)
    if requested_assets is not None:
        selection = normalize_requested_assets(
            assets=requested_assets,
            fetch_alerts=getattr(args, "fetch_alerts", False),
            fetch_monitors=False,
        )
        legacy_argv.extend(["--assets", selection.label])
    if args.esql_index:
        legacy_argv.extend(["--esql-index", args.esql_index])
    if args.logs_index:
        legacy_argv.extend(["--logs-index", args.logs_index])
    mode = getattr(args, "native_promql_flag", "auto")
    if mode == "force_on":
        legacy_argv.append("--native-promql")
    elif mode == "force_off":
        legacy_argv.append("--no-native-promql")
    if args.validate:
        legacy_argv.append("--validate")
    if args.upload:
        legacy_argv.append("--upload")
    if args.es_url:
        legacy_argv.extend(["--es-url", args.es_url])
    if args.es_api_key:
        legacy_argv.extend(["--es-api-key", args.es_api_key])
    if args.kibana_url:
        legacy_argv.extend(["--kibana-url", args.kibana_url])
    if args.kibana_api_key:
        legacy_argv.extend(["--kibana-api-key", args.kibana_api_key])
    if getattr(args, "space_id", ""):
        legacy_argv.extend(["--shadow-space", args.space_id])
    for rf in args.rules_file:
        legacy_argv.extend(["--rules-file", rf])
    for pl in args.plugin:
        legacy_argv.extend(["--plugin", pl])
    if args.polish_metadata:
        legacy_argv.append("--polish-metadata")
    if args.preflight:
        legacy_argv.append("--preflight")
    if args.dataset_filter:
        legacy_argv.extend(["--dataset-filter", args.dataset_filter])
    if args.logs_dataset_filter:
        legacy_argv.extend(["--logs-dataset-filter", args.logs_dataset_filter])
    if args.smoke_report:
        legacy_argv.extend(["--smoke-report", args.smoke_report])
    if getattr(args, "create_alert_rules", False):
        legacy_argv.append("--create-alert-rules")
    if getattr(args, "grafana_token", ""):
        legacy_argv.extend(["--grafana-token", args.grafana_token])
    smoke_requested = (
        args.smoke
        or args.browser_audit
        or args.capture_screenshots
        or bool(args.smoke_output)
        or bool(args.chrome_binary)
    )
    if smoke_requested:
        if args.smoke:
            legacy_argv.append("--smoke")
        if args.browser_audit:
            legacy_argv.append("--browser-audit")
        if args.capture_screenshots:
            legacy_argv.append("--capture-screenshots")
        if args.smoke_output:
            legacy_argv.extend(["--smoke-output", args.smoke_output])
        legacy_argv.extend(["--smoke-timeout", str(args.smoke_timeout)])
        if args.chrome_binary:
            legacy_argv.extend(["--chrome-binary", args.chrome_binary])
    sys.argv = ["obs-migrate"] + legacy_argv
    grafana_main()


def _run_datadog_migration(args: Any) -> None:
    """Run the Datadog migration pipeline directly."""
    from observability_migration.adapters.source.datadog.cli import main as datadog_main

    legacy_argv = [
        "--source", args.input_mode,
        "--input-dir", args.input_dir,
        "--output-dir", args.output_dir,
        "--data-view", args.data_view,
        "--field-profile", args.field_profile,
    ]
    requested_assets = getattr(args, "assets", None)
    if requested_assets is not None:
        selection = normalize_requested_assets(
            assets=requested_assets,
            fetch_alerts=getattr(args, "fetch_alerts", False),
            fetch_monitors=False,
        )
        legacy_argv.extend(["--assets", selection.label])
    if args.logs_index:
        legacy_argv.extend(["--logs-index", args.logs_index])
    if args.es_url:
        legacy_argv.extend(["--es-url", args.es_url])
    if args.es_api_key:
        legacy_argv.extend(["--es-api-key", args.es_api_key])
    legacy_argv.append("--compile")
    if args.validate:
        legacy_argv.append("--validate")
    if args.upload:
        legacy_argv.append("--upload")
    if args.preflight:
        legacy_argv.append("--preflight")
    if args.dataset_filter:
        legacy_argv.extend(["--dataset-filter", args.dataset_filter])
    if args.logs_dataset_filter:
        legacy_argv.extend(["--logs-dataset-filter", args.logs_dataset_filter])
    if args.kibana_url:
        legacy_argv.extend(["--kibana-url", args.kibana_url])
    if args.kibana_api_key:
        legacy_argv.extend(["--kibana-api-key", args.kibana_api_key])
    if args.space_id:
        legacy_argv.extend(["--space-id", args.space_id])
    if getattr(args, "create_alert_rules", False):
        legacy_argv.append("--create-alert-rules")
    if getattr(args, "monitor_ids", ""):
        legacy_argv.extend(["--monitor-ids", args.monitor_ids])
    if getattr(args, "monitor_query", ""):
        legacy_argv.extend(["--monitor-query", args.monitor_query])
    if getattr(args, "env_file", ""):
        legacy_argv.extend(["--env-file", args.env_file])
    smoke_requested = (
        args.smoke
        or args.browser_audit
        or args.capture_screenshots
        or bool(args.smoke_output)
        or bool(args.chrome_binary)
    )
    if smoke_requested:
        if args.smoke:
            legacy_argv.append("--smoke")
        if args.browser_audit:
            legacy_argv.append("--browser-audit")
        if args.capture_screenshots:
            legacy_argv.append("--capture-screenshots")
        if args.smoke_output:
            legacy_argv.extend(["--smoke-output", args.smoke_output])
        legacy_argv.extend(["--smoke-timeout", str(args.smoke_timeout)])
        if args.chrome_binary:
            legacy_argv.extend(["--chrome-binary", args.chrome_binary])
    sys.argv = ["obs-migrate"] + legacy_argv
    datadog_main()


def _run_compile(args: Any) -> None:
    """Compile dashboard YAML to NDJSON using the shared Kibana target."""
    yaml_dir = Path(args.yaml_dir)
    output_dir = Path(args.output_dir)
    if not yaml_dir.is_dir():
        print(f"YAML directory not found: {yaml_dir}", file=sys.stderr)
        sys.exit(1)

    adapter = target_registry.get("kibana")()
    compile_payload = adapter.compile(yaml_dir, output_dir)
    results = compile_payload["compile_results"]
    ok = compile_payload["summary"]["compiled_ok"]
    total = compile_payload["summary"]["total"]
    print(f"\nCompiled {ok}/{total} dashboards to {output_dir}")
    for item in results:
        status = "OK" if item["success"] else "FAIL"
        print(f"  [{status}] {item['name']}")
        if not item["success"]:
            for line in item["output"].strip().splitlines()[:5]:
                print(f"         {line}")
    lint_status = compile_payload["yaml_lint"]["ok"]
    if lint_status is False:
        print("\nYAML lint failed:")
        for line in compile_payload["yaml_lint"]["output"].strip().splitlines()[:10]:
            print(f"  {line}")
    layout_status = compile_payload["layout"]["ok"]
    if layout_status is False:
        print("\nCompiled layout validation failed:")
        for line in compile_payload["layout"]["output"].strip().splitlines()[:10]:
            print(f"  {line}")
    if ok < total or lint_status is False or layout_status is False:
        sys.exit(1)


def _run_upload(args: Any) -> None:
    """Compile YAML dashboards and upload them to Kibana via kb-dashboard-cli."""
    raw_path = getattr(args, "yaml_dir", None) or getattr(args, "compiled_dir", None) or ""
    if getattr(args, "compiled_dir", None) and not getattr(args, "yaml_dir", None):
        print(
            "  NOTE: --compiled-dir is a deprecated alias for --yaml-dir. "
            "Upload recompiles YAML internally; prefer --yaml-dir in new scripts.",
            file=sys.stderr,
        )
    input_dir = Path(raw_path)
    if not input_dir.is_dir():
        print(f"Input directory not found: {input_dir}", file=sys.stderr)
        sys.exit(1)

    adapter = target_registry.get("kibana")()
    upload_payload = adapter.upload(
        input_dir,
        kibana_url=args.kibana_url,
        kibana_api_key=args.kibana_api_key,
        space_id=args.space_id,
    )
    if not upload_payload["records"]:
        print(
            f"No dashboard YAML files found under {input_dir}. "
            "Point --yaml-dir at a directory of .yaml files, a dashboard "
            "artifact dir containing 'yaml/' (e.g. "
            "'migration_output/dashboards' or "
            "'migration_output/dashboards/yaml'), or that dir's sibling "
            "'compiled/' directory (e.g. "
            "'migration_output/dashboards/compiled').",
            file=sys.stderr,
        )
        sys.exit(1)

    for item in upload_payload["records"]:
        status = "OK" if item["success"] else "FAIL"
        print(f"  [{status}] {item['yaml_file']}")
        if not item["success"]:
            print(f"         {item['output'][:200]}")
    if upload_payload["summary"]["uploaded_ok"] < upload_payload["summary"]["total"]:
        sys.exit(1)


def _run_extensions(args: Any) -> None:
    """Print the shared extension catalog for a source adapter."""
    adapter_cls = source_registry.get(args.source)
    adapter = adapter_cls()
    if args.template_out:
        template = adapter.build_extension_template()
        template_path = Path(args.template_out)
        template_path.parent.mkdir(parents=True, exist_ok=True)
        template_path.write_text(_serialize_data(template, args.format), encoding="utf-8")
        print(template_path)
        return
    if args.template_only:
        print(_serialize_data(adapter.build_extension_template(), args.format), end="")
        return

    catalog = adapter.build_extension_catalog()
    print(_serialize_data(catalog, args.format), end="")


def _run_cluster(args: Any) -> None:
    """Manage target Kibana cluster: list dashboards, create data views, etc."""
    from observability_migration.targets.kibana.serverless import (
        delete_dashboards,
        detect_serverless,
        ensure_migration_data_views,
        list_dashboards,
    )

    if args.action == "detect-serverless":
        is_sl = detect_serverless(
            args.kibana_url, api_key=args.kibana_api_key, space_id=args.space_id,
        )
        print(f"Serverless: {is_sl}")

    elif args.action == "list-dashboards":
        dashboards = list_dashboards(
            args.kibana_url, api_key=args.kibana_api_key, space_id=args.space_id,
        )
        print(f"\n  {len(dashboards)} dashboard(s):\n")
        for d in dashboards:
            title = d.get("attributes", {}).get("title", "(untitled)")
            print(f"    {d.get('id', '???'):40s}  {title}")

    elif args.action == "ensure-data-views":
        patterns = [p.strip() for p in args.data_view_patterns.split(",") if p.strip()]
        created = ensure_migration_data_views(
            args.kibana_url,
            data_view_patterns=patterns,
            api_key=args.kibana_api_key,
            space_id=args.space_id,
        )
        for dv in created:
            print(f"  OK: {dv.get('title', '???')} (id={dv.get('id', '???')})")

    elif args.action == "delete-dashboards":
        ids = [i.strip() for i in args.dashboard_ids.split(",") if i.strip()]
        if not ids:
            print("  ERROR: --dashboard-ids required", file=sys.stderr)
            sys.exit(2)
        result = delete_dashboards(
            args.kibana_url, ids,
            api_key=args.kibana_api_key, space_id=args.space_id,
        )
        print(f"  Cleared: {len(result['cleared'])}")
        for f in result.get("failed", []):
            print(f"  FAILED: {f['id']}: {f['error'][:200]}")
        print(f"\n  {result['note']}")


def _serialize_data(payload: dict[str, Any], output_format: str) -> str:
    if output_format == "yaml":
        return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    return json.dumps(payload, indent=2) + "\n"


if __name__ == "__main__":
    main()
