# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Package-level entrypoints for the migration CLI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

from observability_migration.core.cli_contract import (
    ASSET_CHOICES,
    alert_output_dir,
    dashboard_output_dir,
    normalize_requested_assets,
)
from observability_migration.core.http import resolve_tls
from observability_migration.core.reporting.report import (
    MigrationResult,
    build_summary_view,
    mark_panel_requires_manual_after_failed_validation,
    mark_panel_requires_manual_after_validation,
    print_report,
    recompute_result_counts,
    save_detailed_report,
)
from observability_migration.core.reporting.summary_md import save_markdown_summary
from observability_migration.core.selection import (
    add_selection_arguments,
    apply_cli_selection,
    criteria_from_args,
)
from observability_migration.core.telemetry_contract import write_schema_report_artifacts
from observability_migration.targets.kibana.adapter import KibanaTargetAdapter
from observability_migration.targets.kibana.compile import (
    compile_all,
    compile_yaml,
    detect_space_id_from_kibana_url,
    kibana_url_for_space,
    lint_dashboard_yaml,
    sync_result_queries_to_yaml,
    validate_compiled_layout,
)
from observability_migration.targets.kibana.serverless import (
    delete_dashboards as serverless_delete_dashboards,
)
from observability_migration.targets.kibana.serverless import (
    ensure_migration_data_views,
)
from observability_migration.targets.kibana.serverless import (
    list_dashboards as serverless_list_dashboards,
)
from observability_migration.targets.kibana.smoke import run_smoke_report

from .alerts import (
    build_alert_migration_tasks,
    build_alert_summary,
    extract_alerts_from_dashboard,
)
from .annotations import build_annotations_summary, translate_annotations
from .assistant import apply_review_explanations
from .esql_validate import (
    _query_source_and_index,
    configure_es_auth,
    summarize_validation_records,
    validate_query_with_fixes,
    write_suggested_rule_pack,
)
from .extract import (
    extract_dashboards_from_files,
    extract_dashboards_from_grafana,
    selection_metadata_from_grafana_dashboard,
)
from .links import build_links_summary, translate_dashboard_links, translate_panel_links
from .local_ai import resolve_task_model
from .manifest import save_migration_manifest
from .panels import _dashboard_output_stem, _flatten_dashboard_panels, translate_dashboard
from .polish import apply_metadata_polish
from .preflight import (
    _collect_referenced_labels,
    _collect_referenced_metrics,
    build_dashboard_complexity,
    build_datasource_audit,
    build_preflight_report,
    build_target_contract_summary,
    build_target_schema_contract,
    probe_source_metric_inventory,
    probe_target_readiness,
    save_preflight_json,
    save_preflight_report,
)
from .rollout import build_rollout_plan, generate_review_queue, save_rollout_plan
from .rules import build_rule_catalog, load_python_plugins, load_rule_pack_files
from .runtime_features import (
    ESQL_NAMED_PARAM_BINDING,
    PROMQL_COMMAND_V0,
    PROMQL_LABEL_MATCHER_PARAMS,
    get_runtime_features,
    is_feature_supported,
    set_runtime_feature,
)
from .schema import SchemaResolver
from .smoke_integration import load_smoke_report, merge_smoke_into_results
from .transforms import build_redesign_tasks, build_transform_summary, extract_transformations
from .verification import annotate_results_with_verification, save_verification_packets

GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:3000")
GRAFANA_USER = os.getenv("GRAFANA_USER", "admin")
GRAFANA_PASS = os.getenv("GRAFANA_PASS", "admin")
KIBANA_URL = os.getenv("KIBANA_URL", "http://localhost:5601")
ES_URL = os.getenv("ES_URL", "")


def _env_truthy_default(name: str) -> bool:
    """Default for a store_true flag backed by an environment variable."""
    return str(os.getenv(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def _grafana_conn(args: argparse.Namespace) -> tuple[str, str, str]:
    """Resolve Grafana (url, user, pass), preferring CLI flags over env defaults.

    Falls back to the module-level env-derived globals when an argument is
    absent (e.g. an ``argparse.Namespace`` built without the new flags).
    """
    url = getattr(args, "grafana_url", None) or GRAFANA_URL
    user = getattr(args, "grafana_user", None) or GRAFANA_USER
    password = getattr(args, "grafana_pass", None) or GRAFANA_PASS
    return url, user, password


def _resolve_tls_from_args(args: argparse.Namespace) -> bool | str:
    """Resolve the requests ``verify`` setting from --ca-cert / --insecure args."""
    return resolve_tls(
        ca_cert=getattr(args, "ca_cert", "") or "",
        insecure=bool(getattr(args, "insecure", False)),
    )


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Grafana → Kibana migration pipeline")
    parser.add_argument(
        "--source",
        dest="source",
        choices=["api", "files"],
        default=None,
        help="Input mode alias: 'api' for live Grafana, 'files' for local JSON. Prefer --input-mode.",
    )
    parser.add_argument(
        "--input-mode",
        dest="input_mode",
        choices=["api", "files"],
        default=None,
        help="Input mode: 'api' for live Grafana, 'files' for local JSON.",
    )
    parser.add_argument(
        "--input-dir",
        default="infra/grafana/dashboards",
        help="Directory with Grafana JSON files (when source=files)",
    )
    parser.add_argument(
        "--output-dir",
        default="migration_output",
        help="Output directory for YAML and compiled NDJSON",
    )
    parser.add_argument(
        "--assets",
        choices=ASSET_CHOICES,
        default="dashboards",
        help="Asset family to migrate: dashboards only, alerts only, or both",
    )
    parser.add_argument(
        "--data-view",
        default="metrics-*",
        help="Elasticsearch data view / index pattern for migrated panels",
    )
    parser.add_argument(
        "--field-profile",
        default="otel",
        help="Target field mapping profile. Grafana currently supports 'otel' only.",
    )
    parser.add_argument(
        "--esql-index",
        default=None,
        help="Index or data stream pattern used inside generated ES|QL queries",
    )
    parser.add_argument(
        "--logs-index",
        default=None,
        help="Index or data stream pattern used for translated Loki / LogQL panels",
    )
    parser.add_argument(
        "--es-url",
        default=ES_URL,
        help="Elasticsearch URL for schema discovery and query validation",
    )
    parser.add_argument(
        "--es-api-key",
        default=os.getenv("ES_API_KEY", os.getenv("KEY", "")),
        help="API key for Elasticsearch (defaults to ES_API_KEY or KEY env var)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate generated ES|QL queries against Elasticsearch",
    )
    parser.add_argument(
        "--validate-narrow-limit",
        type=int,
        default=10,
        dest="validate_narrow_limit",
        help=(
            "Maximum number of concrete index candidates to probe when narrowing a wildcard "
            "index pattern during ES|QL validation (default: 10). Lower values reduce worst-case "
            "validation time per panel at the cost of fewer narrowing attempts."
        ),
    )
    parser.add_argument(
        "--validate-workers",
        type=int,
        default=int(os.getenv("OBS_MIGRATE_VALIDATE_WORKERS", "16")),
        dest="validate_workers",
        help=(
            "Number of concurrent ES|QL validation workers (default: 16). "
            "Use 1 for fully sequential validation."
        ),
    )
    parser.add_argument(
        "--rules-file",
        action="append",
        default=[],
        help="Optional YAML/JSON rule pack to extend simple mappings",
    )
    parser.add_argument(
        "--plugin",
        action="append",
        default=[],
        help="Optional Python plugin file exposing register(api)",
    )
    parser.add_argument(
        "--print-rule-catalog",
        action="store_true",
        help="Print the active rule registries and loaded rule-pack settings, then exit",
    )
    parser.add_argument(
        "--suggest-rule-pack-out",
        default=None,
        help="Write a suggested environment-specific rule pack from validation failures",
    )
    parser.add_argument(
        "--polish-metadata",
        action="store_true",
        help="Apply metadata polish to dashboard YAML (heuristics by default, optional local AI)",
    )
    parser.add_argument(
        "--local-ai-polish",
        action="store_true",
        help="When metadata polish is enabled, use a local OpenAI-compatible model if configured",
    )
    parser.add_argument(
        "--review-explanations",
        action="store_true",
        help="Generate reviewer-facing panel explanations (heuristics by default, optional local AI)",
    )
    parser.add_argument(
        "--local-ai-explanations",
        action="store_true",
        help="When reviewer explanations are enabled, use a local OpenAI-compatible model if configured",
    )
    parser.add_argument(
        "--local-ai-endpoint",
        default=os.getenv("LOCAL_AI_ENDPOINT", os.getenv("OPENAI_BASE_URL", "")),
        help="Base URL for a local OpenAI-compatible chat completions endpoint",
    )
    parser.add_argument(
        "--local-ai-model",
        default=os.getenv("LOCAL_AI_MODEL", os.getenv("OPENAI_MODEL", "")),
        help="Default model name for local AI tasks when task-specific models are not set",
    )
    parser.add_argument(
        "--local-ai-polish-model",
        default=os.getenv("LOCAL_AI_POLISH_MODEL", ""),
        help="Optional model override for metadata polish; defaults to a lighter local sibling when available",
    )
    parser.add_argument(
        "--local-ai-review-model",
        default=os.getenv("LOCAL_AI_REVIEW_MODEL", ""),
        help="Optional model override for reviewer explanations",
    )
    parser.add_argument(
        "--local-ai-api-key",
        default=os.getenv("LOCAL_AI_API_KEY", os.getenv("OPENAI_API_KEY", "")),
        help="API key for the local AI endpoint when required",
    )
    parser.add_argument(
        "--local-ai-timeout",
        type=int,
        default=20,
        help="Timeout in seconds for local AI requests",
    )
    parser.add_argument(
        "--prometheus-url",
        default=os.getenv("PROMETHEUS_URL", ""),
        help="Prometheus URL for live source-side query execution during verification",
    )
    parser.add_argument(
        "--loki-url",
        default=os.getenv("LOKI_URL", ""),
        help="Loki URL for live source-side query execution during verification",
    )
    native_promql_group = parser.add_mutually_exclusive_group()
    native_promql_group.add_argument(
        "--native-promql",
        dest="native_promql_flag",
        action="store_const",
        const="force_on",
        help=(
            "Force native PROMQL emission regardless of cluster support detection "
            "(for Elastic clusters with the ES|QL PROMQL command)."
        ),
    )
    native_promql_group.add_argument(
        "--no-native-promql",
        dest="native_promql_flag",
        action="store_const",
        const="force_off",
        help=(
            "Force ES|QL translation even when the cluster supports the PROMQL command "
            "(opt out of the auto-detected default)."
        ),
    )
    parser.set_defaults(native_promql_flag="auto")
    parser.add_argument(
        "--dataset-filter", default="",
        help="Explicit data_stream.dataset value for metrics dashboard filter "
             "(overrides the default 'prometheus'; cleared automatically when --native-promql is set)",
    )
    parser.add_argument(
        "--logs-dataset-filter", default="",
        help="Explicit data_stream.dataset value for logs dashboard filter",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Run preflight validation for customer readiness assessment (no upload, generates preflight report)",
    )
    parser.add_argument(
        "--smoke-report",
        default="",
        help="Legacy path to a pre-generated smoke report JSON to merge when not running integrated smoke",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="After upload, validate uploaded dashboards in Kibana and merge smoke results into verification",
    )
    parser.add_argument(
        "--browser-audit",
        action="store_true",
        help="With --smoke, scan uploaded dashboards for visible browser-side runtime errors",
    )
    parser.add_argument(
        "--capture-screenshots",
        action="store_true",
        help="With --smoke, capture dashboard screenshots during uploaded-dashboard validation",
    )
    parser.add_argument(
        "--smoke-output",
        default="",
        help="Optional path for the integrated post-upload smoke report JSON",
    )
    parser.add_argument(
        "--smoke-timeout",
        type=int,
        default=30,
        help="Timeout in seconds for integrated Kibana/Elasticsearch smoke requests",
    )
    parser.add_argument(
        "--time-from",
        default="now-1h",
        help="Dashboard time range start for integrated smoke validation",
    )
    parser.add_argument(
        "--time-to",
        default="now",
        help="Dashboard time range end for integrated smoke validation",
    )
    parser.add_argument(
        "--chrome-binary",
        default=os.getenv("CHROME_BINARY", ""),
        help="Optional Chrome/Chromium binary path for browser audit or screenshots",
    )
    parser.add_argument(
        "--shadow-space",
        default="",
        help="Kibana space ID for shadow deployment (rollout safety)",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload compiled dashboards to Kibana",
    )
    parser.add_argument(
        "--kibana-url",
        default=KIBANA_URL,
        help="Kibana URL for upload",
    )
    parser.add_argument(
        "--kibana-api-key",
        default=os.getenv("KIBANA_API_KEY", os.getenv("KEY", "")),
        help="Kibana API key for upload (defaults to KIBANA_API_KEY or KEY env var)",
    )
    parser.add_argument(
        "--ensure-data-views", action="store_true",
        help="Auto-create required data views in the target Kibana cluster before upload",
    )
    parser.add_argument(
        "--list-dashboards", action="store_true",
        help="List dashboards currently in the target Kibana cluster and exit",
    )
    parser.add_argument(
        "--delete-dashboards", default="",
        help="Comma-separated dashboard IDs to delete (overwrite with empty) from Kibana and exit",
    )
    parser.add_argument(
        "--fetch-alerts", action="store_true",
        help=(
            "Deprecated compatibility alias for alert-capable runs; prefer "
            "--assets alerts or --assets all."
        ),
    )
    parser.add_argument(
        "--alert-uids", default="",
        help=(
            "Comma-separated Grafana unified alert rule UIDs to migrate. "
            "When set, only the listed rules are extracted; all others are skipped. "
            "Only affects unified alerting rules (not legacy panel-embedded alerts)."
        ),
    )
    parser.add_argument(
        "--alert-folder", default="",
        help=(
            "Comma-separated Grafana folder UIDs. Only unified alert rules "
            "whose folderUID matches one of the supplied values are migrated. "
            "Combines with --alert-uids (AND logic)."
        ),
    )
    parser.add_argument(
        "--create-alert-rules", action="store_true",
        help=(
            "Create emitted Kibana alerting rules for alert-capable asset "
            "selection (--assets alerts, --assets all, or the deprecated "
            "--fetch-alerts alias). Rules are created disabled by default and "
            "tagged 'obs-migration'. Requires alert-capable asset selection, "
            "--kibana-url, and --kibana-api-key."
        ),
    )
    parser.add_argument(
        "--grafana-token", default=os.getenv("GRAFANA_TOKEN", ""),
        help="Grafana bearer token for API access (alternative to user/pass basic auth)",
    )
    parser.add_argument(
        "--grafana-url", default=GRAFANA_URL,
        help="Grafana base URL for API extraction (defaults to GRAFANA_URL env var)",
    )
    parser.add_argument(
        "--grafana-user", default=GRAFANA_USER,
        help="Grafana username for HTTP basic auth (defaults to GRAFANA_USER env var)",
    )
    parser.add_argument(
        "--grafana-pass", default=GRAFANA_PASS,
        help="Grafana password for HTTP basic auth (defaults to GRAFANA_PASS env var)",
    )
    parser.add_argument(
        "--ca-cert", default=os.getenv("OBS_MIGRATE_CA_CERT", ""),
        help=(
            "Path to a custom CA certificate (bundle) used to verify TLS for all "
            "outbound connections (Elasticsearch, Kibana, Grafana, Prometheus/Loki). "
            "Defaults to OBS_MIGRATE_CA_CERT env var."
        ),
    )
    parser.add_argument(
        "--insecure", action="store_true",
        default=_env_truthy_default("OBS_MIGRATE_INSECURE"),
        help=(
            "Disable TLS certificate verification for all outbound connections. "
            "Insecure — for testing or trusted migration environments only. "
            "Defaults to OBS_MIGRATE_INSECURE env var."
        ),
    )
    add_selection_arguments(parser)
    args = parser.parse_args(argv)
    if args.source and args.input_mode and args.source != args.input_mode:
        parser.error("--source and --input-mode must match when both are provided")
    input_mode = args.input_mode or args.source or "files"
    args.input_mode = input_mode
    args.source = input_mode
    return args


def _handle_list_dashboards(args):
    if not args.kibana_url:
        print("  ERROR: --kibana-url is required for --list-dashboards")
        return
    dashboards = serverless_list_dashboards(
        args.kibana_url,
        api_key=args.kibana_api_key,
        space_id=getattr(args, "shadow_space", ""),
        verify=_resolve_tls_from_args(args),
    )
    print(f"\n  Found {len(dashboards)} dashboard(s) in Kibana:\n")
    for d in dashboards:
        title = d.get("attributes", {}).get("title", "(untitled)")
        print(f"    {d.get('id', '???'):40s}  {title}")


def _handle_delete_dashboards(args):
    if not args.kibana_url:
        print("  ERROR: --kibana-url is required for --delete-dashboards")
        return
    ids = [i.strip() for i in args.delete_dashboards.split(",") if i.strip()]
    if not ids:
        print("  ERROR: provide comma-separated dashboard IDs")
        return
    result = serverless_delete_dashboards(
        args.kibana_url,
        ids,
        api_key=args.kibana_api_key,
        space_id=getattr(args, "shadow_space", ""),
        verify=_resolve_tls_from_args(args),
    )
    print(f"\n  Cleared {len(result['cleared'])} dashboard(s)")
    if result["failed"]:
        for f in result["failed"]:
            print(f"    FAILED: {f['id']}: {f['error'][:200]}")
    print(f"\n  Note: {result['note']}")


def _ensure_grafana_data_views(args):
    patterns: list[str] = []
    if args.data_view:
        patterns.append(args.data_view)
    esql_idx = getattr(args, "esql_index", "")
    if esql_idx and esql_idx != args.data_view:
        patterns.append(esql_idx)
    if not patterns:
        patterns = ["metrics-prometheus-*"]
    print(f"\n  Ensuring data views: {', '.join(patterns)}")
    try:
        created = ensure_migration_data_views(
            args.kibana_url,
            data_view_patterns=patterns,
            api_key=args.kibana_api_key,
            space_id=getattr(args, "shadow_space", ""),
            verify=_resolve_tls_from_args(args),
        )
        for dv in created:
            print(f"    OK: {dv.get('title', '???')} (id={dv.get('id', '???')})")
    except Exception as exc:
        print(f"    WARNING: data view creation failed: {exc}")


def _normalize_execution_flags(args: Any) -> tuple[bool, bool]:
    auto_enabled_upload = False
    auto_enabled_validate = False

    if (getattr(args, "browser_audit", False) or getattr(args, "capture_screenshots", False)) and not getattr(args, "smoke", False):
        print("  ERROR: --browser-audit and --capture-screenshots require --smoke")
        sys.exit(2)
    if getattr(args, "smoke", False) and getattr(args, "smoke_report", ""):
        print("  ERROR: --smoke-report cannot be combined with --smoke; use --smoke-output instead")
        sys.exit(2)
    if getattr(args, "preflight", False) and getattr(args, "smoke", False):
        print("  ERROR: --smoke cannot be combined with --preflight")
        sys.exit(2)
    if getattr(args, "smoke", False) and not getattr(args, "upload", False):
        args.upload = True
        auto_enabled_upload = True
    if getattr(args, "upload", False) and not getattr(args, "kibana_url", ""):
        print("  ERROR: --kibana-url is required when --upload is set")
        sys.exit(2)
    if getattr(args, "smoke", False) and not getattr(args, "es_url", ""):
        print("  ERROR: --es-url is required when --smoke is set")
        sys.exit(2)
    if getattr(args, "preflight", False):
        if getattr(args, "es_url", ""):
            args.validate = True
        args.upload = False

    return auto_enabled_upload, auto_enabled_validate


def _smoke_uploaded_dashboards(
    results: list[MigrationResult],
    output_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    uploaded_results = [result for result in results if result.uploaded]
    if not uploaded_results:
        print("\n  Smoke validation skipped: no dashboards uploaded successfully")
        return {"payload": {}, "output_path": "", "merge_summary": {}}

    smoke_output = Path(args.smoke_output) if args.smoke_output else output_dir / "uploaded_dashboard_smoke_report.json"
    dashboard_titles = [result.dashboard_title for result in uploaded_results if result.dashboard_title]

    print(f"\n  Smoke validating uploaded dashboards ({len(uploaded_results)})...")
    try:
        smoke_payload = run_smoke_report(
            kibana_url=args.kibana_url,
            es_url=args.es_url,
            kibana_api_key=args.kibana_api_key,
            es_api_key=args.es_api_key,
            space_id=args.shadow_space or "",
            output_path=smoke_output,
            screenshot_dir=str(output_dir / "dashboard_qa"),
            browser_audit_dir=str(output_dir / "browser_qa"),
            dashboard_titles=dashboard_titles,
            timeout=args.smoke_timeout,
            time_from=args.time_from,
            time_to=args.time_to,
            browser_audit=args.browser_audit,
            capture_screenshots=args.capture_screenshots,
            chrome_binary=args.chrome_binary,
            verify=_resolve_tls_from_args(args),
        )
    except Exception as exc:
        message = str(exc)
        print(f"    SMOKE FAILED: {message}")
        for result in uploaded_results:
            for panel_result in result.panel_results:
                if "smoke_failed" not in panel_result.runtime_rollups:
                    panel_result.runtime_rollups.append("smoke_failed")
                if args.browser_audit and "browser_failed" not in panel_result.runtime_rollups:
                    panel_result.runtime_rollups.append("browser_failed")
        return {"payload": {}, "output_path": str(smoke_output), "merge_summary": {}}

    merge_summary = merge_smoke_into_results(uploaded_results, smoke_payload)
    summary = smoke_payload.get("summary", {}) or {}
    print(
        "    Smoke summary: "
        f"{summary.get('runtime_error_panels', 0)} runtime error panel(s), "
        f"{summary.get('empty_panels', 0)} empty panel(s), "
        f"{summary.get('not_runtime_checked_panels', 0)} not runtime-checked panel(s), "
        f"{summary.get('dashboards_with_layout_issues', 0)} dashboard(s) with layout issues"
    )
    if args.browser_audit:
        print(
            "    Browser audit: "
            f"{summary.get('dashboards_with_browser_errors', 0)} dashboard(s) with visible errors"
        )
    if merge_summary.get("merged"):
        print(
            "    Smoke merge: "
            f"{merge_summary.get('smoke_failed', 0)} smoke_failed, "
            f"{merge_summary.get('browser_failed', 0)} browser_failed, "
            f"{merge_summary.get('empty_result', 0)} empty_result, "
            f"{merge_summary.get('not_runtime_checked', 0)} not_runtime_checked"
        )
    return {
        "payload": smoke_payload,
        "output_path": str(smoke_output),
        "merge_summary": merge_summary,
    }


def _build_dashboard_panel_index(dashboard):
    panel_index = {}
    for panel in _flatten_dashboard_panels(dashboard):
        panel_id = str(panel.get("id", "") or "")
        if panel_id:
            panel_index[panel_id] = panel
    return panel_index


def _collect_feature_gap_artifacts(dashboard_outputs, data_view):
    all_dashboard_links = []
    all_panel_links = {}
    all_annotations = []
    all_transform_tasks = []
    all_alert_tasks = []

    for result, yaml_path, dashboard in dashboard_outputs:
        if result.translation_error:
            continue
        result.yaml_path = str(yaml_path) if yaml_path is not None else ""
        dashboard_links = translate_dashboard_links(dashboard)
        annotations = translate_annotations(dashboard, data_view=data_view)
        alert_tasks = build_alert_migration_tasks(extract_alerts_from_dashboard(dashboard))

        result.dashboard_links = dashboard_links
        result.annotations = annotations
        result.alert_migration_tasks = alert_tasks

        panel_index = _build_dashboard_panel_index(dashboard)
        dashboard_panel_links = {}
        dashboard_transform_tasks = []
        for panel_result in getattr(result, "panel_results", []) or []:
            source_panel_id = str(getattr(panel_result, "source_panel_id", "") or "")
            panel_json = panel_index.get(source_panel_id)
            if not panel_json:
                continue

            panel_links = translate_panel_links(panel_json)
            panel_result.link_migrations = panel_links
            if panel_links:
                panel_key = source_panel_id or str(getattr(panel_result, "title", "") or "")
                dashboard_panel_links[panel_key] = panel_links
                for link in panel_links:
                    action = link.get("kibana_action", "")
                    description = link.get("description", link.get("title", ""))
                    note = f"Link: {description} [{action}]"
                    if description and note not in panel_result.notes:
                        panel_result.notes.append(note)

            transformation_entries = extract_transformations(panel_json)
            transformation_tasks = build_redesign_tasks(
                str(getattr(panel_result, "title", "")),
                str(getattr(result, "dashboard_title", "")),
                transformation_entries,
            )
            panel_result.transformation_redesign_tasks = transformation_tasks
            dashboard_transform_tasks.extend(transformation_tasks)

        result.feature_gap_summary = {
            "links": build_links_summary(dashboard_links, dashboard_panel_links),
            "annotations": build_annotations_summary(annotations),
            "transformation_redesign": build_transform_summary(dashboard_transform_tasks),
            "alert_migration": build_alert_summary(alert_tasks),
        }

        all_dashboard_links.extend(dashboard_links)
        all_panel_links.update(
            {
                f"{getattr(result, 'dashboard_uid', '')}:{panel_key}": links
                for panel_key, links in dashboard_panel_links.items()
            }
        )
        all_annotations.extend(annotations)
        all_transform_tasks.extend(dashboard_transform_tasks)
        all_alert_tasks.extend(alert_tasks)

    return {
        "dashboard_links": all_dashboard_links,
        "panel_links": all_panel_links,
        "annotations": all_annotations,
        "transform_tasks": all_transform_tasks,
        "alert_tasks": all_alert_tasks,
        "links_summary": build_links_summary(all_dashboard_links, all_panel_links),
        "annotations_summary": build_annotations_summary(all_annotations),
        "transform_summary": build_transform_summary(all_transform_tasks),
        "alert_summary": build_alert_summary(all_alert_tasks),
    }


def extract_dashboards_for_alerts(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.source == "api":
        url, user, password = _grafana_conn(args)
        return extract_dashboards_from_grafana(
            url,
            user,
            password,
            token=getattr(args, "grafana_token", "") or "",
            verify=_resolve_tls_from_args(args),
        )
    return extract_dashboards_from_files(args.input_dir)


_PROMQL_DETECTION_PROBE = (
    'PROMQL index=metrics-* step=1m '
    'start="2024-01-01T00:00:00Z" end="2024-01-01T01:00:00Z" '
    "value=(up)"
)

_PROMQL_LABEL_MATCHER_PARAM_PROBE = (
    'PROMQL index=metrics-* step=1m '
    'start="2024-01-01T00:00:00Z" end="2024-01-01T01:00:00Z" '
    "value=(up{job=?_job})"
)

# Self-contained probe for plain ES|QL named-parameter binding. It needs no
# real index or data — ``ROW`` synthesizes a row and the ``WHERE … == ?p`` /
# ``RLIKE ?p`` clause exercises exactly the named-parameter substitution the
# migrated ``WHERE field == ?var`` / ``RLIKE ?var`` filters rely on. A target
# that supports ES|QL named params returns HTTP 200; one that does not rejects
# the ``?p`` token at parse time (issue #132).
_ESQL_NAMED_PARAM_BINDING_PROBE = 'ROW probe = ?p | WHERE probe RLIKE ?p'
_ESQL_NAMED_PARAM_PROBE_VALUE = "__obs_migration_probe__"


def _es_headers(api_key: str | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
    return headers


def _detect_promql_support(
    es_url: str,
    api_key: str | None = None,
    timeout: float = 5.0,
    verify: bool | str = True,
) -> bool | None:
    """Probe the cluster to see if the ES|QL ``PROMQL`` source command is available.

    Returns ``True`` when the probe is accepted (HTTP 200 with ``columns``),
    ``False`` when the cluster reports that the command isn't supported, and
    ``None`` when the result is inconclusive (auth error or transport failure).
    The probe is best-effort and never raises.
    """
    if not es_url:
        return False
    url = es_url.rstrip("/") + "/_query"
    payload = {"query": _PROMQL_DETECTION_PROBE}
    try:
        response = requests.post(
            url,
            json=payload,
            headers=_es_headers(api_key),
            timeout=timeout,
            verify=verify,
        )
    except Exception as exc:
        print(f"  WARNING: PROMQL command detection failed ({exc.__class__.__name__}): {exc}")
        return None

    status = getattr(response, "status_code", 0)
    if status == 200:
        try:
            body = response.json()
        except Exception:
            body = {}
        columns = body.get("columns") if isinstance(body, dict) else None
        if isinstance(columns, list) and columns:
            return True
        return False

    body_text = ""
    try:
        body_text = (response.text or "").lower()
    except Exception:
        body_text = ""

    if status in (401, 403):
        print("  WARNING: PROMQL command detection skipped (auth error from cluster)")
        return None

    if status == 400:
        signals = ("no handler", "unknown command", "promql")
        if any(signal in body_text for signal in signals):
            return False
        return False

    return False


def _capability_payload_contains(payload: Any, capability: str) -> bool:
    if isinstance(payload, str):
        return payload == capability
    if isinstance(payload, dict):
        return any(_capability_payload_contains(value, capability) for value in payload.values())
    if isinstance(payload, list | tuple | set):
        return any(_capability_payload_contains(value, capability) for value in payload)
    return False


def _detect_promql_label_matcher_params(
    es_url: str,
    api_key: str | None = None,
    timeout: float = 5.0,
    verify: bool | str = True,
) -> dict[str, Any]:
    url = es_url.rstrip("/") + "/_query"
    payload = {
        "query": _PROMQL_LABEL_MATCHER_PARAM_PROBE,
        "params": [{"_job": "__obs_migration_probe__"}],
    }
    try:
        response = requests.post(
            url,
            json=payload,
            headers=_es_headers(api_key),
            timeout=timeout,
            verify=verify,
        )
    except Exception as exc:
        return {
            "supported": False,
            "source": "probe",
            "confidence": "inconclusive",
            "level": "syntax",
            "reason": f"target probe failed ({exc.__class__.__name__})",
        }

    status = getattr(response, "status_code", 0)
    body_text = ""
    try:
        body_text = response.text or ""
    except Exception:
        body_text = ""
    lower_text = body_text.lower()

    if status == 200:
        return {
            "supported": True,
            "source": "probe",
            "confidence": "verified",
            "level": "syntax",
            "reason": "target accepted PromQL label matcher params",
        }
    if status in (401, 403):
        return {
            "supported": False,
            "source": "probe",
            "confidence": "inconclusive",
            "level": "syntax",
            "reason": "target probe skipped due to auth error",
        }
    if "?_job" in lower_text and ("expecting string" in lower_text or "mismatched input" in lower_text):
        return {
            "supported": False,
            "source": "probe",
            "confidence": "verified",
            "level": "syntax",
            "reason": "target parser rejects PromQL label matcher params",
        }
    return {
        "supported": False,
        "source": "probe",
        "confidence": "inconclusive",
        "level": "syntax",
        "reason": f"target probe returned HTTP {status}",
    }


def _detect_esql_named_param_binding(
    es_url: str,
    api_key: str | None = None,
    timeout: float = 5.0,
    verify: bool | str = True,
) -> dict[str, Any]:
    """Probe whether the target binds plain ES|QL named parameters.

    Independent of the PROMQL command, so it is meaningful even on a
    ``--no-native-promql`` run. Returns a feature-state dict; an inconclusive
    or rejected probe leaves the feature unsupported so the engine keeps the
    safe fallback of dropping ``?var`` filters (issue #132).
    """
    if not es_url:
        return {}
    url = es_url.rstrip("/") + "/_query"
    payload = {
        "query": _ESQL_NAMED_PARAM_BINDING_PROBE,
        "params": [{"p": _ESQL_NAMED_PARAM_PROBE_VALUE}],
    }
    try:
        response = requests.post(
            url,
            json=payload,
            headers=_es_headers(api_key),
            timeout=timeout,
            verify=verify,
        )
    except Exception as exc:
        return {
            "supported": False,
            "source": "probe",
            "confidence": "inconclusive",
            "level": "syntax",
            "reason": f"target probe failed ({exc.__class__.__name__})",
        }

    status = getattr(response, "status_code", 0)
    if status == 200:
        return {
            "supported": True,
            "source": "probe",
            "confidence": "verified",
            "level": "syntax",
            "reason": "target accepted ES|QL named parameter binding",
        }
    if status in (401, 403):
        return {
            "supported": False,
            "source": "probe",
            "confidence": "inconclusive",
            "level": "syntax",
            "reason": "target probe skipped due to auth error",
        }
    return {
        "supported": False,
        "source": "probe",
        "confidence": "inconclusive",
        "level": "syntax",
        "reason": f"target probe returned HTTP {status}",
    }


def _detect_target_runtime_features(
    es_url: str,
    api_key: str | None = None,
    timeout: float = 5.0,
    verify: bool | str = True,
) -> dict[str, Any]:
    profile: dict[str, Any] = {}

    promql_supported = _detect_promql_support(es_url, api_key, timeout=timeout, verify=verify)
    set_runtime_feature(
        profile,
        PROMQL_COMMAND_V0,
        supported=promql_supported is True,
        source="probe",
        confidence="verified" if promql_supported is not None else "inconclusive",
        level="syntax",
        reason=(
            "target accepted the ES|QL PROMQL command"
            if promql_supported is True
            else "target did not verify ES|QL PROMQL command support"
        ),
    )

    if promql_supported is not True:
        set_runtime_feature(
            profile,
            PROMQL_LABEL_MATCHER_PARAMS,
            supported=False,
            source="probe",
            confidence="inconclusive" if promql_supported is None else "verified",
            level="syntax",
            reason="PromQL command support is unavailable on the target",
        )
        return profile

    headers = _es_headers(api_key)
    capabilities_url = es_url.rstrip("/") + "/_nodes/capabilities"
    try:
        response = requests.get(capabilities_url, headers=headers, timeout=timeout, verify=verify)
        if getattr(response, "status_code", 0) == 200:
            payload = response.json()
            if _capability_payload_contains(payload, PROMQL_LABEL_MATCHER_PARAMS):
                probe_state = _detect_promql_label_matcher_params(es_url, api_key, timeout, verify=verify)
                if probe_state.get("supported") is True:
                    set_runtime_feature(
                        profile,
                        PROMQL_LABEL_MATCHER_PARAMS,
                        supported=True,
                        source="capabilities+probe",
                        confidence="verified",
                        level="syntax",
                        reason="target capabilities advertise and probe confirms PromQL label matcher params",
                    )
                else:
                    profile[PROMQL_LABEL_MATCHER_PARAMS] = {
                        **probe_state,
                        "source": "capabilities+probe",
                        "reason": (
                            probe_state.get("reason")
                            or "target capabilities advertised PromQL label matcher params but probe did not confirm support"
                        ),
                    }
                return profile
    except Exception:
        pass

    profile[PROMQL_LABEL_MATCHER_PARAMS] = _detect_promql_label_matcher_params(es_url, api_key, timeout, verify=verify)
    return profile


def _runtime_feature_status_label(state: Any) -> str:
    if isinstance(state, bool):
        return "supported" if state else "unsupported"
    if not isinstance(state, dict):
        return "unknown"
    if state.get("supported") is True:
        return "supported"
    if state.get("confidence") == "inconclusive":
        return "inconclusive"
    return "unsupported"


def _print_promql_runtime_profile(runtime_features: dict[str, Any]) -> None:
    command_state = runtime_features.get(PROMQL_COMMAND_V0, {})
    label_state = runtime_features.get(PROMQL_LABEL_MATCHER_PARAMS, {})
    print("  Target PromQL profile:")
    print(f"    PROMQL command: {_runtime_feature_status_label(command_state)}")
    print(f"    PROMQL label matcher params: {_runtime_feature_status_label(label_state)}")
    if (
        is_feature_supported(runtime_features, PROMQL_COMMAND_V0)
        and not is_feature_supported(runtime_features, PROMQL_LABEL_MATCHER_PARAMS)
    ):
        print("    Label matcher params disabled; affected panels will use ES|QL translation")


def _resolve_native_promql(args: argparse.Namespace, runtime_features: dict[str, Any] | None = None) -> bool:
    """Resolve the effective ``native_promql`` setting for this run.

    Precedence:
      ``--no-native-promql`` (force_off) → False
      ``--native-promql``    (force_on)  → True
      otherwise (auto)                   → cluster auto-detection

    In ``auto`` mode native PROMQL is the default high-fidelity path. When an
    ES URL is configured we probe the target and fall back to ES|QL translation
    only if it reports the ``PROMQL`` command unsupported (or the probe is
    inconclusive). When no ES URL is configured there is no cluster to probe, so
    we optimistically default to native PROMQL; ``--no-native-promql`` is the
    opt-out.
    """
    mode = getattr(args, "native_promql_flag", "auto")
    if mode == "force_off":
        return False
    if mode == "force_on":
        return True
    es_url = getattr(args, "es_url", "") or ""
    if not es_url:
        print(
            "  No --es-url to probe; defaulting to native PROMQL "
            "(use --no-native-promql to force ES|QL translation)"
        )
        return True
    es_api_key = getattr(args, "es_api_key", "") or None
    runtime_features = runtime_features or _detect_target_runtime_features(
        es_url, es_api_key, verify=_resolve_tls_from_args(args)
    )
    if is_feature_supported(runtime_features, PROMQL_COMMAND_V0):
        print("  PROMQL ES|QL command detected on target; defaulting to --native-promql")
        return True
    command_state = runtime_features.get(PROMQL_COMMAND_V0, {})
    if isinstance(command_state, dict) and command_state.get("confidence") == "inconclusive":
        print("  PROMQL ES|QL command detection inconclusive (transport error); falling back to ES|QL translation")
        return False
    if isinstance(command_state, dict):
        print("  PROMQL ES|QL command not supported on target; falling back to ES|QL translation")
        return False
    print("  PROMQL ES|QL command detection inconclusive (transport error); falling back to ES|QL translation")
    return False


def _load_configured_rule_pack(args: argparse.Namespace):
    rule_pack = load_rule_pack_files(args.rules_file)
    if args.logs_index:
        rule_pack.logs_index = args.logs_index
    if args.dataset_filter:
        rule_pack.metrics_dataset_filter = args.dataset_filter
    if args.logs_dataset_filter:
        rule_pack.logs_dataset_filter = args.logs_dataset_filter
    load_python_plugins(args.plugin, rule_pack)
    return rule_pack


def _apply_native_promql_to_rule_pack(rule_pack, args: argparse.Namespace) -> None:
    """Resolve --native-promql/--no-native-promql/auto and apply to the pack.

    Separated from ``_load_configured_rule_pack`` so the offline
    ``--print-rule-catalog`` command doesn't trigger the cluster probe.

    When the user provided an explicit ``--dataset-filter`` it always wins,
    even if native PROMQL would otherwise clear the filter to ``""``. That
    preserves the pre-refactor behavior and respects an explicit user
    signal over the default-clearing behavior advertised in the
    ``--dataset-filter`` help text.
    """
    mode = getattr(args, "native_promql_flag", "auto")
    es_url = getattr(args, "es_url", "") or ""
    es_api_key = getattr(args, "es_api_key", "") or None
    verify = _resolve_tls_from_args(args)
    runtime_profile = None
    if mode != "force_off" and es_url:
        runtime_profile = _detect_target_runtime_features(es_url, es_api_key, verify=verify)
        rule_pack.runtime_features.update(runtime_profile)
        _print_promql_runtime_profile(runtime_profile)

    # ES|QL named-parameter binding (``WHERE field == ?var`` / ``RLIKE ?var``)
    # is a core ES|QL feature, independent of the PROMQL command, so it is
    # probed even on a deliberate --no-native-promql run. Without this the
    # pure-ES|QL path never learns the target can bind ``?var`` and silently
    # drops $var-driven label filters (issue #132).
    if es_url and ESQL_NAMED_PARAM_BINDING not in get_runtime_features(rule_pack):
        esql_state = _detect_esql_named_param_binding(es_url, es_api_key, verify=verify)
        get_runtime_features(rule_pack)[ESQL_NAMED_PARAM_BINDING] = esql_state
        print(
            "  Target ES|QL named-parameter binding: "
            f"{_runtime_feature_status_label(esql_state)}"
        )

    native = _resolve_native_promql(args, runtime_profile)
    if native:
        rule_pack.native_promql = True
        if not runtime_profile and not es_url:
            set_runtime_feature(
                rule_pack,
                PROMQL_COMMAND_V0,
                supported=True,
                source="default",
                confidence="unverified",
                reason="no --es-url configured; native PROMQL assumed for offline migration",
            )
        if not getattr(args, "dataset_filter", ""):
            rule_pack.metrics_dataset_filter = ""

    # Offline runs have no cluster to probe; ES|QL named-parameter binding is a
    # stable core feature, so assume it (mirroring the native-PROMQL offline
    # default above) rather than dropping $var label filters (issue #132).
    if not es_url and ESQL_NAMED_PARAM_BINDING not in get_runtime_features(rule_pack):
        set_runtime_feature(
            rule_pack,
            ESQL_NAMED_PARAM_BINDING,
            supported=True,
            source="default",
            confidence="unverified",
            reason="no --es-url configured; ES|QL named-parameter binding assumed for offline migration",
        )


def _build_dashboard_run_summary(
    output_dir: Path,
    *,
    results: list[MigrationResult],
    validation_summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "total": len(results),
        "translation_failed": sum(1 for r in results if r.translation_error),
        "artifacts_dir": str(output_dir),
        "validation_summary": validation_summary,
    }


def _run_validation_jobs(
    validation_jobs: list[tuple[Any, Any]],
    *,
    es_url: str,
    resolver: Any,
    es_api_key: str | None,
    narrow_limit: int,
    workers: int,
    verify: bool | str = True,
) -> list[tuple[Any, Any, dict[str, Any]]]:
    """Validate panel queries, optionally in parallel, preserving report order."""
    if not validation_jobs:
        return []

    if hasattr(resolver, "_discover_fields"):
        resolver._discover_fields()
    if hasattr(resolver, "_discover_concrete_indexes"):
        resolver._discover_concrete_indexes()

    unique_jobs: list[tuple[Any, Any]] = []
    unique_index_by_query: dict[str, int] = {}
    job_to_unique_index: list[int] = []
    for job in validation_jobs:
        query = str(getattr(job[1], "esql_query", "") or "")
        if query not in unique_index_by_query:
            unique_index_by_query[query] = len(unique_jobs)
            unique_jobs.append(job)
        job_to_unique_index.append(unique_index_by_query[query])

    worker_count = max(1, min(int(workers or 1), len(unique_jobs)))

    def run_one(job: tuple[Any, Any]) -> dict[str, Any]:
        _result, panel_result = job
        return validate_query_with_fixes(
            panel_result.esql_query,
            es_url,
            resolver,
            es_api_key=es_api_key,
            narrow_limit=narrow_limit,
            result_limit=1,
            verify=verify,
        )

    if worker_count == 1:
        unique_outputs = []
        for idx, job in enumerate(unique_jobs, start=1):
            unique_outputs.append(run_one(job))
            if idx % 25 == 0 or idx == len(unique_jobs):
                print(f"    validated {idx}/{len(unique_jobs)} unique queries", flush=True)
        return [
            (job[0], job[1], unique_outputs[job_to_unique_index[idx]])
            for idx, job in enumerate(validation_jobs)
        ]

    outputs: list[dict[str, Any] | None] = [None] * len(unique_jobs)
    print(
        f"    validating {len(unique_jobs)} unique queries "
        f"({len(validation_jobs)} panel queries) with {worker_count} workers",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            pool.submit(run_one, job): idx
            for idx, job in enumerate(unique_jobs)
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            idx = futures[future]
            outputs[idx] = future.result()
            if completed % 25 == 0 or completed == len(unique_jobs):
                print(f"    validated {completed}/{len(unique_jobs)} unique queries", flush=True)

    validation_outputs: list[tuple[Any, Any, dict[str, Any]]] = []
    for idx, job in enumerate(validation_jobs):
        output = outputs[job_to_unique_index[idx]]
        if output is not None:
            validation_outputs.append((job[0], job[1], output))
    return validation_outputs


def _write_run_summary(
    base_dir: Path,
    *,
    requested_assets: str,
    dashboard_summary: dict[str, Any] | None,
    alert_summary: dict[str, Any] | None,
) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)
    run_summary = {
        "requested_assets": requested_assets,
        "ran": {
            "dashboards": dashboard_summary is not None,
            "alerts": alert_summary is not None,
        },
    }
    if dashboard_summary is not None:
        run_summary["dashboards"] = dashboard_summary
    if alert_summary is not None:
        run_summary["alerts"] = alert_summary

    summary_path = base_dir / "run_summary.json"
    summary_path.write_text(json.dumps(run_summary, indent=2), encoding="utf-8")
    print(f"  Run summary: {summary_path}")


def _validate_field_profile(args: argparse.Namespace) -> None:
    if args.field_profile != "otel":
        print("Grafana supports --field-profile otel only", file=sys.stderr)
        raise SystemExit(2)


def _clear_dashboard_artifacts(yaml_dir: Path, compiled_dir: Path) -> int:
    removed = 0
    if yaml_dir.exists():
        for yaml_file in yaml_dir.glob("*.yaml"):
            yaml_file.unlink()
            removed += 1
    if compiled_dir.exists():
        for child in compiled_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
    return removed


def _lint_generated_yaml_files(yaml_files: list[Path]) -> tuple[bool, dict[str, tuple[bool, str]], str]:
    lint_results: dict[str, tuple[bool, str]] = {}
    failed_outputs = []
    all_ok = True
    for yaml_file in yaml_files:
        ok, output = lint_dashboard_yaml(yaml_file)
        lint_results[yaml_file.name] = (ok, output)
        if not ok:
            all_ok = False
            if output.strip():
                failed_outputs.append(output.strip())
    return all_ok, lint_results, "\n".join(failed_outputs)


def _compile_linted_yaml_files(
    yaml_files: list[Path],
    lint_results: dict[str, tuple[bool, str]],
    compiled_dir: Path,
) -> list[tuple[str, bool, str]]:
    compile_results = []
    for yaml_file in yaml_files:
        lint_ok, lint_output = lint_results.get(
            yaml_file.name,
            (False, "Dashboard YAML lint result missing."),
        )
        if not lint_ok:
            compile_results.append(
                (
                    yaml_file.name,
                    False,
                    "Dashboard YAML lint failed before compile.\n" + lint_output,
                )
            )
            continue
        out_dir = compiled_dir / yaml_file.stem
        out_dir.mkdir(parents=True, exist_ok=True)
        success, output = compile_yaml(yaml_file, out_dir)
        compile_results.append((yaml_file.name, success, output))
    return compile_results


def _validate_compiled_layout_after_compile(
    results: list[MigrationResult],
    compile_results: list[tuple[str, bool, str]],
    compiled_dir: Path,
) -> tuple[bool, str]:
    if not any(ok for _name, ok, _output in compile_results):
        return False, ""
    layout_ok, layout_output = validate_compiled_layout(compiled_dir)
    for result in results:
        result.layout_validated = True
        result.layout_error = "" if layout_ok else layout_output
    return layout_ok, layout_output


def _run_preflight_reporting(
    *,
    args: argparse.Namespace,
    results: list[Any],
    resolver: Any,
    base_dir: Path,
    validation_summary: dict[str, Any],
    validation_records: list[dict[str, Any]],
    verification_payload: dict[str, Any],
) -> dict[str, Any]:
    source_urls_configured = bool(
        getattr(args, "prometheus_url", "") or getattr(args, "loki_url", ""),
    )

    print("\n  Preflight probes...")
    verify = _resolve_tls_from_args(args)
    referenced_metrics = _collect_referenced_metrics(results)
    referenced_labels = _collect_referenced_labels(results)

    source_inventory = probe_source_metric_inventory(
        getattr(args, "prometheus_url", "") or "",
        required_metrics=referenced_metrics,
        required_labels=referenced_labels,
        verify=verify,
    )
    if source_inventory.get("status") == "ok":
        found = len(source_inventory.get("metrics_found", []))
        missing = len(source_inventory.get("metrics_missing", []))
        avail = len(source_inventory.get("available_metrics", []))
        print(
            f"    Source inventory: {avail} metrics in Prometheus, "
            f"{found} referenced found, {missing} referenced missing"
        )
    elif source_inventory.get("status") == "error":
        print(f"    Source inventory: error ({source_inventory.get('error', '')})")
    else:
        print("    Source inventory: not configured (pass --prometheus-url)")

    schema_contract = build_target_schema_contract(results, resolver)
    target_contract_summary = build_target_contract_summary(results)
    required_index_patterns = list(
        schema_contract.get("required_indexes", {}).keys(),
    )

    target_readiness = probe_target_readiness(
        args.es_url, required_index_patterns,
        es_api_key=args.es_api_key or None,
        verify=verify,
    )
    if target_readiness.get("cluster_health"):
        health = target_readiness["cluster_health"]
        tpl_count = sum(
            v.get("found", 0)
            for v in target_readiness.get("index_templates", {}).values()
        )
        ds_count = sum(
            v.get("found", 0)
            for v in target_readiness.get("data_streams", {}).values()
        )
        if health.get("unsupported"):
            print(
                f"    Target readiness: cluster {health.get('status', '?').upper()} "
                f"(cluster health API unavailable), {tpl_count} index templates, "
                f"{ds_count} data streams"
            )
        else:
            print(
                f"    Target readiness: cluster {health.get('status', '?').upper()}, "
                f"{health.get('number_of_data_nodes', '?')} data nodes, "
                f"{tpl_count} index templates, {ds_count} data streams"
            )
    elif target_readiness.get("status") != "not_configured":
        print(f"    Target readiness: errors ({target_readiness.get('errors', [])})")
    else:
        print("    Target readiness: not configured (pass --es-url)")

    datasource_audit = build_datasource_audit(results)
    ds_types = datasource_audit.get("datasource_types", {})
    if ds_types:
        parts = [f"{t}:{c}" for t, c in ds_types.items()]
        non_mig = datasource_audit.get("non_migratable_panels", 0)
        extra = f" ({non_mig} non-migratable)" if non_mig else ""
        print(f"    Datasource audit: {', '.join(parts)}{extra}")

    complexity_scores = build_dashboard_complexity(results)
    high = sum(1 for s in complexity_scores if s.get("complexity_score", 0) >= 50)
    if high:
        print(f"    Complexity: {high} dashboards scored >= 50 (high manual effort)")

    preflight_report = build_preflight_report(
        results,
        validation_summary,
        validation_records,
        verification_payload,
        schema_contract,
        target_contract_summary=target_contract_summary,
        source_urls_configured=source_urls_configured,
        target_url_configured=bool(args.es_url),
        source_inventory=source_inventory,
        target_readiness=target_readiness,
        datasource_audit=datasource_audit,
        complexity_scores=complexity_scores,
    )

    preflight_path = base_dir / "preflight_report.json"
    contract_path = base_dir / "required_target_contract.json"
    target_contract_path = base_dir / "target_query_contract_summary.json"
    save_preflight_report(preflight_report, preflight_path)
    save_preflight_json(schema_contract, contract_path)
    save_preflight_json(target_contract_summary, target_contract_path)
    print(f"  Preflight report: {preflight_path}")
    print(f"  Target schema contract: {contract_path}")
    print(f"  Target contract summary: {target_contract_path}")

    if args.suggest_rule_pack_out and validation_summary:
        write_suggested_rule_pack(args.suggest_rule_pack_out, validation_summary)
        print(f"  Suggested rule pack: {args.suggest_rule_pack_out}")

    action_summary = preflight_report.get("customer_action_summary", "")
    if action_summary:
        print(f"\n{action_summary}")

    return preflight_report


def _translate_dashboard_resilient(
    dashboard: dict,
    yaml_dir: Path,
    *,
    datasource_index: str,
    esql_index: str,
    rule_pack: Any,
    resolver: Any,
) -> tuple[MigrationResult, Any]:
    """Translate one dashboard; on unhandled exception return a stub result with translation_error set."""
    try:
        return translate_dashboard(
            dashboard,
            yaml_dir,
            datasource_index=datasource_index,
            esql_index=esql_index,
            rule_pack=rule_pack,
            resolver=resolver,
        )
    except Exception as exc:
        title = dashboard.get("title") or dashboard.get("_source_file") or "unknown"
        print(f"  ✗ {title}: translation error — {exc}")
        return (
            MigrationResult(
                dashboard_title=str(title),
                dashboard_uid=str(dashboard.get("uid") or ""),
                source_file=str(dashboard.get("_source_file") or ""),
                translation_error=traceback.format_exc(),
            ),
            None,
        )


def main(argv: list[str] | None = None):
    args = parse_args(argv)
    _validate_field_profile(args)
    selection = normalize_requested_assets(
        assets=args.assets,
        fetch_alerts=getattr(args, "fetch_alerts", False),
        fetch_monitors=False,
    )
    auto_enabled_upload = False
    auto_enabled_validate = False

    if args.list_dashboards:
        _handle_list_dashboards(args)
        return
    if args.delete_dashboards:
        _handle_delete_dashboards(args)
        return

    if selection.dashboards:
        auto_enabled_upload, auto_enabled_validate = _normalize_execution_flags(args)

    if args.print_rule_catalog:
        rule_pack = _load_configured_rule_pack(args)
        print(json.dumps(build_rule_catalog(rule_pack), indent=2))
        return

    root_output_dir = Path(args.output_dir)

    pipeline_label = "PREFLIGHT VALIDATION" if args.preflight else "MIGRATION PIPELINE"
    print("=" * 70)
    print(f"GRAFANA → KIBANA {pipeline_label}")
    print("=" * 70)
    if auto_enabled_upload:
        print("  Auto-enabled upload for smoke validation")
    if auto_enabled_validate:
        print("  Auto-enabled ES|QL validation for upload because --es-url was provided")

    if not selection.dashboards:
        if selection.alerts:
            print("\n  Dashboard migration: skipped (--assets alerts)")
            print("\n  Extracting alerts...")
            from .alert_pipeline import run_alert_pipeline

            raw_dashboards = extract_dashboards_for_alerts(args)
            alert_summary = run_alert_pipeline(
                args,
                output_dir=alert_output_dir(root_output_dir),
                raw_dashboards=raw_dashboards,
            ) or {
                "artifacts_dir": str(alert_output_dir(root_output_dir)),
            }
            _write_run_summary(
                root_output_dir,
                requested_assets=selection.label,
                dashboard_summary=None,
                alert_summary=alert_summary,
            )
        return

    rule_pack = _load_configured_rule_pack(args)
    _apply_native_promql_to_rule_pack(rule_pack, args)

    if args.es_api_key:
        configure_es_auth(args.es_api_key)

    verify = _resolve_tls_from_args(args)

    resolver = SchemaResolver(
        rule_pack,
        es_url=args.es_url or None,
        index_pattern=args.esql_index or args.data_view,
        es_api_key=args.es_api_key or None,
        verify=verify,
    )

    base_dir = dashboard_output_dir(root_output_dir)
    yaml_dir = base_dir / "yaml"
    compiled_dir = base_dir / "compiled"
    yaml_dir.mkdir(parents=True, exist_ok=True)
    compiled_dir.mkdir(parents=True, exist_ok=True)
    removed_stale_artifacts = _clear_dashboard_artifacts(yaml_dir, compiled_dir)
    if removed_stale_artifacts:
        print(f"\n  Removed {removed_stale_artifacts} stale dashboard artifact(s) from {base_dir}")

    default_ai_model = args.local_ai_model
    polish_ai_model = args.local_ai_polish_model or resolve_task_model("polish", args.local_ai_endpoint, default_ai_model)
    review_ai_model = args.local_ai_review_model or default_ai_model

    if args.es_url:
        print(f"\n  Schema discovery: {args.es_url}")
        resolver._discover_fields()
        discovery = resolver.discovery_status()
        if discovery["status"] == "ok":
            profile = resolver.schema_profile() or "generic/otel"
            print(
                f"  Discovered {discovery['field_count']} fields, "
                f"{len(resolver._discovered_mappings)} label mappings "
                f"(schema_profile={profile})"
            )
            if resolver.schema_profile() is None:
                print("  WARNING: no Prometheus schema profile detected; using OTel/pass-through fallbacks")
        elif discovery["status"] == "empty":
            print("  WARNING: schema discovery reached Elasticsearch but found no fields")
        elif discovery["status"] == "error":
            print(f"  WARNING: schema discovery failed: {discovery['error']}")
        else:
            print("  Schema discovery: offline mode")
    else:
        print("\n  Schema discovery: disabled (pass --es-url to enable)")

    print(f"\n[1/7] Extracting dashboards (source={args.source})...")
    grafana_url, grafana_user, grafana_pass = _grafana_conn(args)
    if args.source == "api":
        dashboards = extract_dashboards_from_grafana(
            grafana_url,
            grafana_user,
            grafana_pass,
            token=getattr(args, "grafana_token", "") or "",
            verify=verify,
        )
    else:
        dashboards = extract_dashboards_from_files(args.input_dir)
    if not dashboards:
        if args.source == "api":
            print(
                f"  ERROR: no dashboards found in Grafana at {grafana_url}.",
                file=sys.stderr,
            )
        else:
            print(
                f"  ERROR: no Grafana dashboards found under {args.input_dir}. "
                "Point --input-dir at a directory of Grafana dashboard JSON "
                "files (each with a top-level 'panels' or 'rows' key).",
                file=sys.stderr,
            )
        sys.exit(1)

    try:
        criteria = criteria_from_args(args)
    except ValueError as exc:
        print(f"  ERROR: invalid --select-updated-* value: {exc}", file=sys.stderr)
        sys.exit(1)
    dashboards = apply_cli_selection(
        dashboards,
        selection_metadata_from_grafana_dashboard,
        criteria,
        label="grafana dashboard",
        kind="dashboards",
    )
    if not criteria.is_empty and not dashboards:
        print(
            "  ERROR: no Grafana dashboards matched the --select-* criteria.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"  Found {len(dashboards)} dashboards")

    print("\n[2/7] Translating dashboards to YAML...")
    results = []
    dashboard_outputs = []
    for dashboard in dashboards:
        result, yaml_path = _translate_dashboard_resilient(
            dashboard,
            yaml_dir,
            datasource_index=args.data_view,
            esql_index=args.esql_index or args.data_view,
            rule_pack=rule_pack,
            resolver=resolver,
        )
        if result.translation_error:
            results.append(result)
            dashboard_outputs.append((result, yaml_path, dashboard))
            continue
        if args.polish_metadata:
            polish_summary = apply_metadata_polish(
                yaml_path,
                result,
                enable_ai=args.local_ai_polish,
                ai_endpoint=args.local_ai_endpoint,
                ai_model=polish_ai_model,
                ai_api_key=args.local_ai_api_key,
                timeout=args.local_ai_timeout,
            )
            if polish_summary.get("panel_titles") or polish_summary.get("control_labels") or polish_summary.get("notes"):
                note = "local AI" if polish_summary.get("mode") == "local_ai" else "heuristic"
                print(
                    f"    metadata polish ({note}): "
                    f"{len(polish_summary.get('panel_titles', {}))} panel title changes, "
                    f"{len(polish_summary.get('control_labels', {}))} control label changes"
                )
        results.append(result)
        dashboard_outputs.append((result, yaml_path, dashboard))
        status_icon = "✓" if result.not_feasible == 0 else "⚠"
        print(
            f"  {status_icon} {result.dashboard_title}: "
            f"{result.migrated}✓ {result.migrated_with_warnings}⚠ "
            f"{result.requires_manual}? {result.not_feasible}✗ "
            f"(of {result.total_panels} panels)"
        )

    feature_gap_artifacts = _collect_feature_gap_artifacts(dashboard_outputs, args.data_view)
    all_dashboard_links = feature_gap_artifacts["dashboard_links"]
    all_panel_links = feature_gap_artifacts["panel_links"]
    all_annotations = feature_gap_artifacts["annotations"]
    all_transform_tasks = feature_gap_artifacts["transform_tasks"]
    all_alert_tasks = feature_gap_artifacts["alert_tasks"]
    links_summary = feature_gap_artifacts["links_summary"]
    annotations_summary = feature_gap_artifacts["annotations_summary"]
    transform_summary = feature_gap_artifacts["transform_summary"]
    alert_summary = feature_gap_artifacts["alert_summary"]

    if any(v for v in links_summary.values() if v):
        print(
            f"  Links: {links_summary['dashboard_links']} dashboard, "
            f"{links_summary['panel_links']} panel "
            f"({links_summary['manual_wiring_needed']} need manual wiring)"
        )
    if annotations_summary.get("total"):
        print(
            f"  Annotations: {annotations_summary['total']} found "
            f"({annotations_summary.get('candidate_event_annotations', 0)} candidate event annotations, "
            f"{annotations_summary['manual_needed']} need manual setup)"
        )
    if transform_summary.get("total"):
        print(
            f"  Transformations: {transform_summary['total']} redesign tasks "
            f"(by complexity: {transform_summary.get('by_complexity', {})})"
        )
    if alert_summary.get("total"):
        print(
            f"  Alerts: {alert_summary['total']} rules to migrate "
            f"(suggested Kibana types: {alert_summary.get('by_kibana_type', {})})"
        )

    alert_run_summary = None
    if selection.alerts:
        print("\n  Extracting alerts...")
        from .alert_pipeline import run_alert_pipeline

        alert_run_summary = run_alert_pipeline(
            args,
            output_dir=alert_output_dir(root_output_dir),
            raw_dashboards=dashboards,
        ) or {
            "artifacts_dir": str(alert_output_dir(root_output_dir)),
        }

        from observability_migration.core.assets.alerting import build_alerting_ir_from_grafana
        from observability_migration.core.mapping import map_alerts_batch

        for result in results:
            existing_tasks = getattr(result, "alert_migration_tasks", []) or []
            result_alert_irs = []
            for task in existing_tasks:
                result_alert_irs.append(build_alerting_ir_from_grafana(task))
            result_mapping = map_alerts_batch(
                result_alert_irs,
                data_view=getattr(args, "data_view", "metrics-*"),
            )
            result.alert_results = [ir.to_dict() for ir in result_alert_irs]
            result_tiers = result_mapping["summary"]["by_automation_tier"]
            result.alert_summary = {
                "total": len(result.alert_results),
                "automated": result_tiers.get("automated", 0),
                "draft_review": result_tiers.get("draft_requires_review", 0),
                "manual_required": result_tiers.get("manual_required", 0),
                "by_kind": {},
            }

    validation_records = []
    validation_summary = {}
    if args.validate and args.es_url:
        print("\n[3/7] Validating ES|QL queries against Elasticsearch...", flush=True)
        passed = 0
        fixed = 0
        fixed_empty = 0
        failed = 0
        manualized_failed = 0
        validation_jobs = [
            (r, pr)
            for r in results
            for pr in r.panel_results
            if pr.esql_query
        ]
        total_queries = len(validation_jobs)
        validation_outputs = _run_validation_jobs(
            validation_jobs,
            es_url=args.es_url,
            resolver=resolver,
            es_api_key=args.es_api_key or None,
            narrow_limit=getattr(args, "validate_narrow_limit", 10),
            workers=getattr(args, "validate_workers", 4),
            verify=verify,
        )
        for r, pr, validation_result in validation_outputs:
            status = validation_result["status"]
            if status == "pass":
                passed += 1
            elif status == "fixed":
                fixed += 1
                pr.esql_query = validation_result["query"]
                if isinstance(pr.query_ir, dict):
                    pr.query_ir["target_query"] = pr.esql_query
                    _, fixed_index = _query_source_and_index(pr.esql_query)
                    if fixed_index:
                        pr.query_ir["target_index"] = fixed_index
            elif status == "fixed_empty":
                fixed_empty += 1
                pr.esql_query = validation_result["query"]
                if isinstance(pr.query_ir, dict):
                    pr.query_ir["target_query"] = pr.esql_query
                    _, fixed_index = _query_source_and_index(pr.esql_query)
                    if fixed_index:
                        pr.query_ir["target_index"] = fixed_index
                mark_panel_requires_manual_after_validation(pr, validation_result)
            elif status == "fail":
                failed += 1
                manualized_failed += 1
                mark_panel_requires_manual_after_failed_validation(pr, validation_result)

            record = {
                "dashboard": r.dashboard_title,
                "dashboard_uid": r.dashboard_uid,
                "panel": pr.title,
                "source_panel_id": pr.source_panel_id,
                "status": status,
                "query": validation_result["query"],
                "error": validation_result["error"],
                "fix_attempts": validation_result["fix_attempts"],
                "analysis": validation_result["analysis"],
            }
            validation_records.append(record)
        for r in results:
            recompute_result_counts(r)

        validation_summary = summarize_validation_records(validation_records)
        print(
            f"  Validated {total_queries} queries: "
            f"{passed} passed, {fixed} auto-fixed, {fixed_empty} manualized after empty fallback, "
            f"{failed} failed ({manualized_failed} replaced with upload-safe placeholders)"
        )
        if validation_summary.get("missing_labels"):
            top_labels = list(validation_summary["missing_labels"].items())[:5]
            print("  Top missing labels: " + ", ".join(f"{name} ({count})" for name, count in top_labels))
        if validation_summary.get("missing_metrics"):
            top_metrics = list(validation_summary["missing_metrics"].items())[:5]
            print("  Top missing metrics: " + ", ".join(f"{name} ({count})" for name, count in top_metrics))
        if validation_summary.get("counter_type_mismatches"):
            top_counters = list(validation_summary["counter_type_mismatches"].items())[:5]
            print("  Residual counter type mismatches: " + ", ".join(f"{name} ({count})" for name, count in top_counters))
        if validation_summary.get("empty_fallback_indexes"):
            top_fallbacks = list(validation_summary["empty_fallback_indexes"].items())[:5]
            print("  Empty fallback streams: " + ", ".join(f"{name} ({count})" for name, count in top_fallbacks))
        if args.suggest_rule_pack_out:
            write_suggested_rule_pack(args.suggest_rule_pack_out, validation_summary)
            print(f"  Suggested rule pack: {args.suggest_rule_pack_out}")
        for result, yaml_path, _dashboard in dashboard_outputs:
            if yaml_path is None:
                continue
            sync_result_queries_to_yaml(result, yaml_path)
    else:
        print("\n[3/7] Validation: skipped (pass --validate --es-url to enable)")

    yaml_files = sorted(yaml_dir.glob("*.yaml"))
    print("\n[4/7] Linting generated dashboard YAML...")
    yaml_lint_ok, yaml_lint_results, yaml_lint_output = _lint_generated_yaml_files(yaml_files)
    for result, yaml_path, _dashboard in dashboard_outputs:
        if yaml_path is None:
            continue
        result.yaml_linted = True
        lint_ok, lint_output = yaml_lint_results.get(
            Path(yaml_path).name,
            (False, "Dashboard YAML lint result missing."),
        )
        result.yaml_lint_error = "" if lint_ok else lint_output
    if yaml_lint_ok:
        print("  ✓ Dashboard YAML validation passed")
    else:
        print("  ✗ Dashboard YAML validation failed")
        for line in yaml_lint_output.strip().splitlines()[:20]:
            print(f"    {line}")

    compile_results = []
    layout_ok = False
    layout_output = ""
    if yaml_lint_ok:
        print("\n[5/7] Compiling YAML -> Kibana NDJSON via kb-dashboard-cli...")
        compile_results = compile_all(yaml_dir, compiled_dir)
    else:
        passing_lint = sum(1 for ok, _output in yaml_lint_results.values() if ok)
        if passing_lint:
            skipped_lint = len(yaml_files) - passing_lint
            print(
                "\n[5/7] Compiling YAML -> Kibana NDJSON via kb-dashboard-cli "
                f"(skipping {skipped_lint} lint-failed dashboard(s))..."
            )
        else:
            print("\n[5/7] Compiling YAML -> Kibana NDJSON via kb-dashboard-cli: skipped (no lint-passing dashboards)")
        compile_results = _compile_linted_yaml_files(yaml_files, yaml_lint_results, compiled_dir)

    compile_map = {Path(name).stem: (ok, output) for name, ok, output in compile_results}
    for result in results:
        dashboard_stem = _dashboard_output_stem(result.dashboard_title)
        if not result.translation_error:
            result.compiled_path = str(compiled_dir / dashboard_stem / "compiled_dashboards.ndjson")
        compiled_state = compile_map.get(dashboard_stem)
        if compiled_state:
            result.compiled = compiled_state[0]
            result.compile_error = "" if compiled_state[0] else compiled_state[1]
    for name, ok, _output in compile_results:
        icon = "✓" if ok else "✗"
        print(f"  {icon} {name}")

    if any(ok for _, ok, _ in compile_results):
        layout_ok, layout_output = _validate_compiled_layout_after_compile(
            results,
            compile_results,
            compiled_dir,
        )
        if layout_ok:
            print("  ✓ Compiled dashboard layout validation passed")
        else:
            print("  ✗ Compiled dashboard layout validation failed")
            for line in layout_output.strip().splitlines()[:20]:
                print(f"    {line}")

    target_space = detect_space_id_from_kibana_url(args.kibana_url) or "default"
    if args.upload and args.ensure_data_views:
        _ensure_grafana_data_views(args)
    if args.upload:
        print(f"\nUploading to Kibana at {args.kibana_url}...")
        upload_space = args.shadow_space or ""
        upload_kibana_url = kibana_url_for_space(args.kibana_url, upload_space)
        target_adapter = KibanaTargetAdapter()
        upload_blocker = ""
        if any(getattr(result, "layout_validated", None) and result.layout_error for result in results):
            upload_blocker = "Upload skipped because compiled dashboard layout validation failed."

        if upload_blocker:
            print(f"  ✗ {upload_blocker}")
            for result in results:
                result.upload_attempted = True
                result.uploaded = False
                result.upload_error = upload_blocker
        else:
            compiled_ok_stems = {Path(name).stem for name, ok, _output in compile_results if ok}
            for result, yaml_path, _dashboard in dashboard_outputs:
                result.upload_attempted = True
                if yaml_path is None:
                    continue
                if yaml_path.stem not in compiled_ok_stems:
                    result.uploaded = False
                    result.upload_error = "Upload skipped because this dashboard did not compile."
                    print(f"  - {yaml_path.name} skipped (dashboard did not compile)")
                    continue
                upload_result = target_adapter.upload_dashboard(
                    yaml_path,
                    compiled_dir / yaml_path.stem,
                    kibana_url=args.kibana_url,
                    space_id=upload_space,
                    kibana_api_key=args.kibana_api_key,
                    verify=verify,
                )
                ok = upload_result["success"]
                output = upload_result["output"]
                result.uploaded = ok
                result.upload_error = "" if ok else output
                result.uploaded_space = upload_space or target_space
                result.uploaded_kibana_url = upload_result.get("kibana_url", upload_kibana_url)
                icon = "✓" if ok else "✗"
                print(f"  {icon} {yaml_path.name}")
                if not ok:
                    for line in output.strip().splitlines()[:10]:
                        print(f"    {line}")

    smoke_merge_summary = {}
    integrated_smoke_output = ""
    if args.smoke:
        smoke_state = _smoke_uploaded_dashboards(results, base_dir, args)
        smoke_merge_summary = smoke_state.get("merge_summary", {}) or {}
        integrated_smoke_output = str(smoke_state.get("output_path", "") or "")
    elif args.smoke_report:
        smoke_data = load_smoke_report(args.smoke_report)
        if smoke_data:
            smoke_merge_summary = merge_smoke_into_results(results, smoke_data)
            if smoke_merge_summary.get("merged"):
                print(
                    f"  Smoke merge: {smoke_merge_summary['smoke_failed']} smoke_failed, "
                    f"{smoke_merge_summary['browser_failed']} browser_failed, "
                    f"{smoke_merge_summary['empty_result']} empty_result"
                )

    verification_payload = annotate_results_with_verification(
        results, validation_records,
        prometheus_url=getattr(args, "prometheus_url", "") or "",
        loki_url=getattr(args, "loki_url", "") or "",
        verify=verify,
    )
    review_summary = {}
    if args.review_explanations:
        review_summary = apply_review_explanations(
            results,
            verification_payload,
            enable_ai=args.local_ai_explanations,
            ai_endpoint=args.local_ai_endpoint,
            ai_model=review_ai_model,
            ai_api_key=args.local_ai_api_key,
            timeout=args.local_ai_timeout,
        )
        note = review_summary.get("mode", "heuristic")
        ai_request_suffix = ""
        if review_summary.get("ai_requests"):
            ai_request_suffix = (
                f", {review_summary.get('unique_ai_cases', 0)} unique cases "
                f"/ {review_summary.get('ai_requests', 0)} AI requests"
            )
        print(
            "  Reviewer explanations "
            f"({note}): {review_summary.get('panels', 0)} panels, "
            f"{review_summary.get('ai_panels', 0)} AI-assisted"
            f"{ai_request_suffix}"
        )
        for item in review_summary.get("notes", [])[:2]:
            print(f"    note: {item}")
    for result in results:
        result.runtime_features = dict(getattr(rule_pack, "runtime_features", {}) or {})
        result.verification_summary = {
            "green": sum(1 for pr in result.panel_results if (pr.verification_packet or {}).get("semantic_gate") == "Green"),
            "yellow": sum(1 for pr in result.panel_results if (pr.verification_packet or {}).get("semantic_gate") == "Yellow"),
            "red": sum(1 for pr in result.panel_results if (pr.verification_packet or {}).get("semantic_gate") == "Red"),
        }
        result.review_explanations = (
            {
                "panels": sum(1 for pr in result.panel_results if getattr(pr, "review_explanation", {})),
                "ai_panels": sum(
                    1
                    for pr in result.panel_results
                    if (getattr(pr, "review_explanation", {}) or {}).get("mode") == "local_ai"
                ),
            }
            if args.review_explanations
            else {}
        )

    print("\n[6/7] Generating report...")
    report_path = base_dir / "migration_report.json"
    manifest_path = base_dir / "migration_manifest.json"
    verification_path = base_dir / "verification_packets.json"
    save_detailed_report(results, compile_results, report_path, validation_summary, validation_records, verification_payload)
    save_migration_manifest(results, manifest_path)
    save_verification_packets(verification_payload, verification_path)
    try:
        schema_artifacts = write_schema_report_artifacts(base_dir)
    except Exception as exc:  # best-effort: never fail a migration on derived reports
        schema_artifacts = {}
        print(f"  Schema report: skipped ({exc})")
    print(f"  Detailed report: {report_path}")
    print(f"  Migration manifest: {manifest_path}")
    print(f"  Verification packets: {verification_path}")
    if schema_artifacts:
        print(f"  Schema change report saved: {schema_artifacts['schema_report']}")
        print(f"  Telemetry contract saved: {schema_artifacts['telemetry_contract']}")
    _write_run_summary(
        root_output_dir,
        requested_assets=selection.label,
        dashboard_summary=_build_dashboard_run_summary(
            base_dir,
            results=results,
            validation_summary=validation_summary,
        ),
        alert_summary=alert_run_summary,
    )

    if args.preflight:
        _run_preflight_reporting(
            args=args,
            results=results,
            resolver=resolver,
            base_dir=base_dir,
            validation_summary=validation_summary,
            validation_records=validation_records,
            verification_payload=verification_payload,
        )

    print("\n[7/7] Rollout plan & feature summaries...")
    rollout_plan = build_rollout_plan(
        results,
        target_space=target_space,
        shadow_space=args.shadow_space or "",
        output_dir=str(base_dir),
        smoke_report_path=integrated_smoke_output or args.smoke_report,
    )
    rollout_path = base_dir / "rollout_plan.json"
    save_rollout_plan(rollout_plan, rollout_path)
    print(f"  Rollout plan: {rollout_path}")

    review_queue = generate_review_queue(rollout_plan)
    if review_queue:
        top_risk = review_queue[:3]
        print(f"  Review queue ({len(review_queue)} dashboards, top risk):")
        for item in top_risk:
            gates = item["gates"]
            print(
                f"    {item['dashboard']}: risk={item['risk_score']} "
                f"(G:{gates['green']} Y:{gates['yellow']} R:{gates['red']})"
            )

    manifest_extras: dict[str, Any] = {}
    if all_dashboard_links or all_panel_links or all_annotations or all_transform_tasks or all_alert_tasks:
        manifest_extras = {
            "links": {
                "summary": links_summary,
                "dashboard_links": all_dashboard_links,
                "panel_links": all_panel_links,
            },
            "annotations": {
                "summary": annotations_summary,
                "items": all_annotations,
            },
            "transformation_redesign": {
                "summary": transform_summary,
                "tasks": all_transform_tasks,
            },
            "alert_migration": {
                "summary": alert_summary,
                "tasks": all_alert_tasks,
            },
        }
        extras_path = base_dir / "feature_gap_report.json"
        import json as _json
        with extras_path.open("w") as fh:
            _json.dump(manifest_extras, fh, indent=2)
        print(f"  Feature gap report: {extras_path}")

    try:
        rollout_run_id = (
            rollout_plan.get("run_id", "")
            if isinstance(rollout_plan, dict)
            else getattr(rollout_plan, "run_id", "")
        )
        summary_view = build_summary_view(
            results,
            compile_results,
            review_queue=review_queue,
            gap_data=manifest_extras,
            run_id=rollout_run_id,
        )
        summary_md_path = base_dir / "migration_summary.md"
        save_markdown_summary(summary_view, summary_md_path)
        print(f"  Migration summary: {summary_md_path}")
    except Exception as exc:  # best-effort: never fail a migration on the summary
        print(f"  Migration summary: skipped ({exc})")

    print_report(results, compile_results)

    if validation_records:
        failed_validations = [
            (record["panel"], record["error"])
            for record in validation_records
            if record["status"] == "fail"
        ]
        if failed_validations:
            print(f"\nVALIDATION FAILURES ({len(failed_validations)}):")
            for title, err in failed_validations[:20]:
                print(f"  {title}: {err[:120]}")

    if any(not ok for _, ok, _output in compile_results):
        raise SystemExit(1)


__all__ = ["main", "parse_args"]


if __name__ == "__main__":
    main()
