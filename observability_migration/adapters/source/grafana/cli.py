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
    reject_removed_surfaces,
)
from observability_migration.core.http import resolve_tls
from observability_migration.core.reporting.report import (
    MigrationResult,
    build_summary_view,
    mark_panel_migrated_with_missing_target_fields,
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
from observability_migration.core.verification.disposition import validation_failure_self_heals
from observability_migration.targets.kibana.adapter import KibanaTargetAdapter
from observability_migration.targets.kibana.alerting import exit_if_rule_creation_skipped
from observability_migration.targets.kibana.compile import (
    detect_space_id_from_kibana_url,
    kibana_url_for_space,
    sync_result_queries_to_ir,
)
from observability_migration.targets.kibana.dashboards_api import (
    dashboard_id_disambiguation_note,
    upload_warnings_from_reasons,
)
from observability_migration.targets.kibana.native_artifacts import (
    write_ir_artifact,
    write_native_artifact,
    write_native_artifact_index,
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
    validate_esql,
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
from .metrics_target_guidance import (
    MetricsTargetGuidance,
    assess_metrics_target,
    print_metrics_target_guidance,
)
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
from .rules import build_rule_catalog, load_python_plugins, load_rule_pack_files, resolve_pack_for_dashboard
from .runtime_features import (
    ESQL_NAMED_PARAM_BINDING,
    KIBANA_PROMQL_CONTROL_PARAMS,
    PROMQL_COMMAND_V0,
    PROMQL_HISTOGRAM_QUANTILE,
    PROMQL_LABEL_MATCHER_PARAMS,
    get_runtime_features,
    is_feature_supported,
    set_runtime_feature,
)
from .schema import SchemaResolver
from .smoke_integration import load_smoke_report, merge_smoke_into_results
from .transforms import (
    build_redesign_tasks,
    build_transform_summary,
    extract_transformations,
    mark_applied_transformations,
)
from .verification import annotate_results_with_verification, save_verification_packets

GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:3000")
GRAFANA_USER = os.getenv("GRAFANA_USER", "admin")
GRAFANA_PASS = os.getenv("GRAFANA_PASS", "admin")
KIBANA_URL = os.getenv("KIBANA_URL", "")
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
    reject_removed_surfaces(
        list(sys.argv[1:] if argv is None else argv), prog="grafana-migrate"
    )
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
        help=(
            "Target field mapping profile. Grafana supports 'otel', "
            "'prometheus_remote_write', 'prometheus_metrics', "
            "'prometheus_native', 'passthrough', and 'auto' (requires "
            "--es-url). Datadog uses a separate profile set via the "
            "unified CLI."
        ),
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
        "--control-schema",
        default="",
        help=(
            "Optional JSON control-schema fixture (field_cache/cooccurrence_cache) "
            "merged into schema discovery after --es-url probing"
        ),
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
        "--translation-mode",
        dest="translation_mode",
        choices=["auto", "native", "esql"],
        default="auto",
        help=(
            "Control the PromQL translation strategy (issue #158): "
            "'auto' (default) probes the target and uses native PROMQL when the "
            "ES|QL PROMQL command is available, otherwise falls back to ES|QL; "
            "'native' forces native PROMQL even if the probe is inconclusive "
            "(emits queries that error against a cluster lacking the command); "
            "'esql' disables native PROMQL entirely so every panel uses the ES|QL "
            "translator."
        ),
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
        "--metric-map-file",
        action="append",
        default=[],
        help=(
            "Source-neutral YAML file with top-level metric_map and/or tag_map "
            "entries (metric_map renames metric names; tag_map renames label names "
            "to ES fields). May be repeated; later files override earlier entries and "
            "loaded rule packs. When set with --translation-mode auto, selects ES|QL "
            "translation so the map applies."
        ),
    )
    parser.add_argument(
        "--plugin",
        action="append",
        default=[],
        help="Optional Python plugin file exposing register(api)",
    )
    parser.add_argument(
        "--no-curated-packs",
        action="store_true",
        default=False,
        dest="no_curated_packs",
        help=(
            "Disable automatic curated-pack loading for known Grafana community dashboards. "
            "By default, when a dashboard is identified by gnetId or exact title, a bundled "
            "curated pack is merged in automatically (user --rules-file always wins on collision). "
            "Set this flag to skip curated packs and use only the base rule pack."
        ),
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
    parser.add_argument(
        "--dataset-filter", default="",
        help="Explicit data_stream.dataset value for metrics dashboard filter "
             "(overrides the default 'prometheus'; cleared automatically when native PROMQL is used)",
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
        help="Upload dashboards to Kibana (typed Dashboards API by default)",
    )
    parser.add_argument(
        "--kibana-url",
        default=KIBANA_URL,
        help=(
            "Optional Kibana URL for target-version detection and upload "
            "(defaults to KIBANA_URL; required with --upload)"
        ),
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
            "--fetch-alerts alias). Both fully-automated and draft "
            "(review-required) translations are created disabled and tagged "
            "'obs-migration'; draft rules also get 'obs-migration-review' so "
            "they are easy to find and enable after inspection. Pass "
            "--no-draft-alert-rules to create only fully-automated rules. "
            "Requires alert-capable asset selection, --kibana-url, and "
            "--kibana-api-key."
        ),
    )
    parser.add_argument(
        "--no-draft-alert-rules", action="store_true",
        help=(
            "With --create-alert-rules, skip draft (review-required) "
            "translations and create only fully-automated rules. Draft rules "
            "are created by default."
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
    identifier_params_by_dashboard: dict[str, dict[str, str]] = {}
    for result in uploaded_results:
        defaults: dict[str, str] = {}
        for panel_result in result.panel_results:
            query_ir = panel_result.query_ir if isinstance(panel_result.query_ir, dict) else {}
            metadata = query_ir.get("metadata") if isinstance(query_ir.get("metadata"), dict) else {}
            raw_defaults = metadata.get("esql_identifier_param_defaults")
            if not isinstance(raw_defaults, dict):
                continue
            defaults.update(
                {
                    str(name): str(value)
                    for name, value in raw_defaults.items()
                    if name and value not in (None, "")
                }
            )
        if not defaults:
            continue
        for dashboard_key in (
            result.kibana_saved_object_id,
            result.dashboard_title,
        ):
            if dashboard_key:
                identifier_params_by_dashboard[dashboard_key] = dict(defaults)

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
            identifier_params_by_dashboard=identifier_params_by_dashboard,
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

    for result, artifact_stem, dashboard in dashboard_outputs:
        if result.translation_error:
            continue
        result.artifact_stem = str(artifact_stem or "")
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

            transformation_entries = mark_applied_transformations(
                extract_transformations(panel_json),
                getattr(panel_result, "applied_transform_indices", None),
            )
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

    # Only a precise parser/router rejection of the PROMQL command confirms it
    # is absent (verified False). Every other non-success response — a 400 that
    # is unrelated to the command, a transient 429/5xx, an endpoint quirk — is
    # inconclusive (None), so the optimistic native-PROMQL default is preserved
    # rather than forcing the lower-fidelity ES|QL fallback on a flaky probe
    # (issue #158).
    command_absent_signals = (
        "no handler",
        "unknown command",
        "mismatched input 'promql'",
    )
    if status in (400, 404) and any(signal in body_text for signal in command_absent_signals):
        return False

    print(f"  WARNING: PROMQL command detection inconclusive (HTTP {status})")
    return None


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

    Independent of the PROMQL command, so it is meaningful even when the
    cluster-wide ES|QL fallback is in effect. Returns a feature-state dict; an
    inconclusive or rejected probe leaves the feature unsupported so the engine
    keeps the safe fallback of dropping ``?var`` filters (issue #132).
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


#: Minimum Elasticsearch (major, minor) that evaluates histogram_quantile
#: natively (elastic/elasticsearch#150578, shipped in 9.5).
_HISTOGRAM_QUANTILE_MIN_ES = (9, 5)

#: Minimum Kibana (major, minor) that forwards dashboard control values into
#: named params inside an opaque ``PROMQL …`` expression
#: (elastic/kibana#271244 / kibana#271215, labeled v9.5.0). Older Kibana
#: (including 9.4) leaves ``?var`` unbound and panels must stay on ES|QL.
_KIBANA_PROMQL_CONTROL_PARAMS_MIN = (9, 5)


def _detect_es_version(
    es_url: str,
    api_key: str | None = None,
    timeout: float = 5.0,
    verify: bool | str = True,
) -> tuple[int, int] | None:
    """Return the target Elasticsearch ``(major, minor)`` version, or None.

    Reads ``version.number`` from the cluster root endpoint. Any transport
    error, non-200 status, or unparseable version yields None so callers treat
    the version as unknown (and keep the safe ES|QL fallback).
    """
    if not es_url:
        return None
    try:
        response = requests.get(
            es_url.rstrip("/") + "/",
            headers=_es_headers(api_key),
            timeout=timeout,
            verify=verify,
        )
    except Exception:
        return None
    if getattr(response, "status_code", 0) != 200:
        return None
    try:
        number = str(response.json().get("version", {}).get("number", "")).strip()
        parts = number.split(".")
        return (int(parts[0]), int(parts[1]))
    except (AttributeError, IndexError, ValueError):
        return None


def _kibana_headers(api_key: str | None = None) -> dict[str, str]:
    headers = {"kbn-xsrf": "true", "Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
    return headers


def _parse_major_minor_version(number: str) -> tuple[int, int] | None:
    text = str(number or "").strip()
    if not text:
        return None
    # Strip common suffixes: ``9.5.0-SNAPSHOT``, ``9.5.0+build``.
    for sep in ("-", "+"):
        if sep in text:
            text = text.split(sep, 1)[0]
    parts = text.split(".")
    try:
        return (int(parts[0]), int(parts[1]))
    except (IndexError, ValueError):
        return None


def _detect_kibana_version(
    kibana_url: str,
    api_key: str | None = None,
    timeout: float = 5.0,
    verify: bool | str = True,
) -> tuple[int, int] | None:
    """Return the target Kibana ``(major, minor)`` version, or None.

    Prefers ``GET /api/status`` → ``version.number``. Some local/dev builds
    omit that field, so fall back to ``GET /api/stats`` → ``kibana.version``.
    Any transport error, non-200, or unparseable payload yields None so callers
    keep the safe ES|QL fallback for control-bound PromQL panels.
    """
    if not kibana_url:
        return None
    base = kibana_url.rstrip("/")
    headers = _kibana_headers(api_key)

    try:
        response = requests.get(
            f"{base}/api/status",
            headers=headers,
            timeout=timeout,
            verify=verify,
        )
    except Exception:
        response = None
    if response is not None and getattr(response, "status_code", 0) == 200:
        try:
            payload = response.json()
            version = _parse_major_minor_version(
                str((payload.get("version") or {}).get("number") or "")
            )
            if version is not None:
                return version
        except (AttributeError, TypeError, ValueError):
            # Ignore malformed/unexpected /api/status payloads and
            # fall back to /api/stats version detection below.
            pass

    try:
        response = requests.get(
            f"{base}/api/stats",
            headers=headers,
            timeout=timeout,
            verify=verify,
        )
    except Exception:
        return None
    if getattr(response, "status_code", 0) != 200:
        return None
    try:
        payload = response.json()
        return _parse_major_minor_version(
            str((payload.get("kibana") or {}).get("version") or "")
        )
    except (AttributeError, TypeError, ValueError):
        return None


def _apply_kibana_promql_control_params_feature(
    rule_pack,
    *,
    kibana_url: str,
    api_key: str | None,
    verify: bool | str = True,
) -> None:
    """Prefer native PROMQL control binding; force ES|QL only on Kibana < 9.5.

    Kibana 9.5+ forwards dashboard controls into ``?var`` placeholders inside
    an opaque ``PROMQL`` command (elastic/kibana#271244). Older Kibana leaves
    those params unbound, so verified ``< 9.5`` keeps the ES|QL
    ``WHERE … RLIKE ?var`` path.

    When ``--kibana-url`` is absent or the version probe is inconclusive,
    optimistically enable native PROMQL (same posture as offline native PROMQL
    itself). Panels that still cannot stay native degrade to ES|QL through the
    existing translator / live-validator fallthroughs.
    """
    if not kibana_url:
        set_runtime_feature(
            rule_pack,
            KIBANA_PROMQL_CONTROL_PARAMS,
            supported=True,
            source="default",
            confidence="unverified",
            level="runtime",
            reason=(
                "no --kibana-url configured; prefer native PROMQL control "
                "binding (panels that cannot stay native fall through to ES|QL)"
            ),
        )
        print(
            "  Kibana inner-PROMQL control params: preferred "
            "(no --kibana-url; native PROMQL first, ES|QL fallthrough)"
        )
        return

    kibana_version = _detect_kibana_version(kibana_url, api_key, verify=verify)
    if kibana_version is None:
        set_runtime_feature(
            rule_pack,
            KIBANA_PROMQL_CONTROL_PARAMS,
            supported=True,
            source="probe",
            confidence="inconclusive",
            level="runtime",
            reason=(
                "Kibana version could not be determined; prefer native PROMQL "
                "control binding (panels that cannot stay native fall through "
                "to ES|QL)"
            ),
        )
        print(
            "  Kibana inner-PROMQL control params: preferred "
            "(Kibana version inconclusive; native PROMQL first, ES|QL fallthrough)"
        )
        return

    supported = kibana_version >= _KIBANA_PROMQL_CONTROL_PARAMS_MIN
    version_label = f"{kibana_version[0]}.{kibana_version[1]}"
    if supported:
        reason = (
            f"Kibana {version_label} forwards dashboard control params into "
            "inner PROMQL expressions"
        )
        print(f"  Kibana inner-PROMQL control params: enabled (Kibana {version_label})")
    else:
        reason = (
            f"Kibana {version_label} predates 9.5; control-bound PromQL panels "
            "use the ES|QL RLIKE binding path"
        )
        print(
            f"  Kibana inner-PROMQL control params: unsupported "
            f"(Kibana {version_label}; requires 9.5+)"
        )
    set_runtime_feature(
        rule_pack,
        KIBANA_PROMQL_CONTROL_PARAMS,
        supported=supported,
        source="probe",
        confidence="verified",
        level="runtime",
        reason=reason,
    )


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

    es_version = _detect_es_version(es_url, api_key, timeout=timeout, verify=verify)
    histogram_quantile_supported = (
        es_version is not None and es_version >= _HISTOGRAM_QUANTILE_MIN_ES
    )
    set_runtime_feature(
        profile,
        PROMQL_HISTOGRAM_QUANTILE,
        supported=histogram_quantile_supported,
        source="probe",
        confidence="verified" if es_version is not None else "inconclusive",
        level="syntax",
        reason=(
            f"target Elasticsearch {es_version[0]}.{es_version[1]} evaluates "
            "histogram_quantile natively"
            if histogram_quantile_supported
            else (
                f"target Elasticsearch {es_version[0]}.{es_version[1]} predates 9.5; "
                "histogram_quantile uses the ES|QL PERCENTILE() path"
                if es_version is not None
                else "target Elasticsearch version could not be determined"
            )
        ),
    )

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
    """Resolve whether this run emits native PROMQL.

    Migration always targets native PROMQL (the highest-fidelity path) and only
    falls back to ES|QL when the target cluster is *confirmed* to lack the
    ``PROMQL`` command. The user never declares intent (issue #158).

    When an ES URL is configured we probe the target:
      - command supported                → native PROMQL
      - command confirmed absent          → cluster-wide ES|QL fallback
      - probe inconclusive (transport/    → keep native PROMQL (optimistic
        auth error)                         default) and warn, so a transient
                                            failure doesn't route a capable
                                            cluster down the fallback path
    When no ES URL is configured there is no cluster to probe, so we
    optimistically default to native PROMQL.

    ``--translation-mode`` lets the user override the probe-driven default
    (issue #158): ``esql`` disables native PROMQL entirely, ``native`` forces it
    on (still probing only to warn when the command is confirmed absent), and
    ``auto`` keeps the probe behavior described above.

    When ``--metric-map-file`` is set and mode is still ``auto``, prefer ES|QL
    translation so exact metric renames actually apply (parity with Datadog).
    Explicit ``--translation-mode native`` still wins.
    """
    mode = str(getattr(args, "translation_mode", "auto") or "auto").lower()
    es_url = getattr(args, "es_url", "") or ""

    if mode == "esql":
        print(
            "  --translation-mode esql: native PROMQL disabled by user request; "
            "all panels use the ES|QL translator"
        )
        return False

    if mode == "auto" and getattr(args, "metric_map_file", None):
        print(
            "  --metric-map-file set: using ES|QL translation so metric_map applies "
            "(pass --translation-mode native to keep native PROMQL)"
        )
        return False

    if mode == "native":
        if es_url:
            es_api_key = getattr(args, "es_api_key", "") or None
            runtime_features = runtime_features or _detect_target_runtime_features(
                es_url, es_api_key, verify=_resolve_tls_from_args(args)
            )
            command_state = runtime_features.get(PROMQL_COMMAND_V0, {})
            confirmed_absent = (
                isinstance(command_state, dict)
                and command_state.get("supported") is False
                and command_state.get("confidence") == "verified"
            )
            if confirmed_absent:
                print(
                    "  WARNING: --translation-mode native, but the probe confirmed the "
                    "ES|QL PROMQL command is ABSENT on the target; emitted native "
                    "PROMQL queries will error at render time (explicit user choice)"
                )
        print("  --translation-mode native: forcing native PROMQL by user request")
        return True

    if not es_url:
        print("  No --es-url to probe; defaulting to native PROMQL")
        return True
    es_api_key = getattr(args, "es_api_key", "") or None
    runtime_features = runtime_features or _detect_target_runtime_features(
        es_url, es_api_key, verify=_resolve_tls_from_args(args)
    )
    if is_feature_supported(runtime_features, PROMQL_COMMAND_V0):
        print("  PROMQL ES|QL command detected on target; using native PROMQL")
        return True
    command_state = runtime_features.get(PROMQL_COMMAND_V0, {})
    confirmed_absent = (
        isinstance(command_state, dict)
        and command_state.get("supported") is False
        and command_state.get("confidence") == "verified"
    )
    if confirmed_absent:
        print("  PROMQL ES|QL command not supported on target; falling back to ES|QL translation")
        return False
    print(
        "  WARNING: PROMQL ES|QL command detection inconclusive (transport error); "
        "keeping native PROMQL (optimistic default)"
    )
    return True


def _load_configured_rule_pack(args: argparse.Namespace):
    rule_pack = load_rule_pack_files(args.rules_file)
    if getattr(args, "metric_map_file", None):
        from observability_migration.core.metric_mapping import (
            load_metric_map_files,
            load_tag_map_files,
        )

        rule_pack.metric_map.update(load_metric_map_files(args.metric_map_file))
        rule_pack.label_rewrites.update(load_tag_map_files(args.metric_map_file))
    if args.logs_index:
        rule_pack.logs_index = args.logs_index
    if args.dataset_filter:
        rule_pack.metrics_dataset_filter = args.dataset_filter
    if args.logs_dataset_filter:
        rule_pack.logs_dataset_filter = args.logs_dataset_filter
    load_python_plugins(args.plugin, rule_pack)
    return rule_pack


def _load_configured_rule_pack_or_exit(args: argparse.Namespace):
    try:
        return _load_configured_rule_pack(args)
    except ValueError as exc:
        print(f"  ERROR: {exc}")
        sys.exit(1)


def _attach_native_promql_validator(
    rule_pack, args: argparse.Namespace, *, verify: bool | str = True
) -> None:
    """Attach a cached live native-PROMQL parse validator to *rule_pack*.

    The validator is a ``callable(query) -> (ok, error)`` closing over
    ``validate_esql``; results are memoized by query string so a panel-level
    re-check of an identical native query costs at most one cluster round-trip.
    The panels-side gate (``_native_promql_query_survives_validation``) consults
    this and degrades a panel to ES|QL only on a parse rejection.
    """
    es_url = getattr(args, "es_url", "") or ""
    es_api_key = getattr(args, "es_api_key", "") or None
    if not es_url:
        return
    cache: dict[str, tuple[bool, str]] = {}

    def _native_promql_validator(query: str) -> tuple[bool, str]:
        if query in cache:
            return cache[query]
        try:
            ok, err = validate_esql(
                query,
                es_url,
                es_api_key=es_api_key,
                verify=verify,
            )
        except Exception:
            # A transport failure must never block migration; treat as "kept".
            ok, err = True, ""
        cache[query] = (ok, err or "")
        return cache[query]

    rule_pack.native_promql_validator = _native_promql_validator


def _print_native_validation_summary(rule_pack) -> None:
    """Print an observable summary of the per-run native live-validation gate.

    Only emits a line when the live validator actually ran (i.e. it was attached
    because ``--es-url`` was configured and native PROMQL is in effect). The
    counts come from ``rule_pack.native_validation_stats`` and reflect per-panel
    decisions: how many native queries were CHECKED against the target, how many
    DEGRADED to ES|QL on a parse rejection, and how many were KEPT native.
    """
    if getattr(rule_pack, "native_promql_validator", None) is None:
        return
    stats = getattr(rule_pack, "native_validation_stats", None) or {}
    checked = int(stats.get("checked", 0) or 0)
    degraded = int(stats.get("degraded", 0) or 0)
    kept = int(stats.get("kept", 0) or 0)
    if checked == 0:
        print(
            "  Native PROMQL live validation: 0 checked "
            "(no native PROMQL panels in this run)"
        )
        return
    print(
        f"  Native PROMQL live validation: {checked} checked, "
        f"{degraded} degraded to ES|QL (target parse rejection), {kept} kept"
    )


def _apply_native_promql_to_rule_pack(rule_pack, args: argparse.Namespace) -> None:
    """Probe the target and apply the native-PROMQL decision to the pack.

    Migration always targets native PROMQL with automatic ES|QL fallback; the
    user declares no intent (issue #158). Separated from
    ``_load_configured_rule_pack`` so the offline ``--print-rule-catalog``
    command doesn't trigger the cluster probe.

    When the user provided an explicit ``--dataset-filter`` it always wins,
    even if native PROMQL would otherwise clear the filter to ``""``. That
    preserves the pre-refactor behavior and respects an explicit user
    signal over the default-clearing behavior advertised in the
    ``--dataset-filter`` help text.
    """
    es_url = getattr(args, "es_url", "") or ""
    es_api_key = getattr(args, "es_api_key", "") or None
    verify = _resolve_tls_from_args(args)
    runtime_profile = None
    if es_url:
        runtime_profile = _detect_target_runtime_features(es_url, es_api_key, verify=verify)
        rule_pack.runtime_features.update(runtime_profile)
        _print_promql_runtime_profile(runtime_profile)

    # ES|QL named-parameter binding (``WHERE field == ?var`` / ``RLIKE ?var``)
    # is a core ES|QL feature, independent of the PROMQL command, so it is
    # probed even when the cluster-wide ES|QL fallback is in effect. Without
    # this the pure-ES|QL path never learns the target can bind ``?var`` and
    # silently drops $var-driven label filters (issue #132).
    if es_url and ESQL_NAMED_PARAM_BINDING not in get_runtime_features(rule_pack):
        esql_state = _detect_esql_named_param_binding(es_url, es_api_key, verify=verify)
        get_runtime_features(rule_pack)[ESQL_NAMED_PARAM_BINDING] = esql_state
        print(
            "  Target ES|QL named-parameter binding: "
            f"{_runtime_feature_status_label(esql_state)}"
        )
    kibana_url = getattr(args, "kibana_url", "") or ""
    kibana_api_key = getattr(args, "kibana_api_key", "") or None
    _apply_kibana_promql_control_params_feature(
        rule_pack,
        kibana_url=kibana_url,
        api_key=kibana_api_key,
        verify=verify,
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
            set_runtime_feature(
                rule_pack,
                PROMQL_LABEL_MATCHER_PARAMS,
                supported=True,
                source="default",
                confidence="unverified",
                reason=(
                    "no --es-url configured; PromQL label matcher params assumed "
                    "for offline migration"
                ),
            )
        if not getattr(args, "dataset_filter", ""):
            rule_pack.metrics_dataset_filter = ""
        # Attach the live native-PROMQL parse validator when a cluster is
        # configured so each built native query is probed; a parse rejection
        # degrades that panel to ES|QL. The closure is cached by query because
        # the panel-level gate may re-check identical queries; the per-panel
        # stats live on ``rule_pack.native_validation_stats`` (issue #158).
        #
        # Exception: an explicit ``--translation-mode native`` is a deliberate
        # "emit native PROMQL even if it errors at render time" request (the flag
        # already warns when the command is confirmed absent). Attaching the
        # degrading validator there would silently rewrite those panels to ES|QL,
        # contradicting the flag's contract — so skip it for forced native
        # (PR #234 review).
        forced_native = (getattr(args, "translation_mode", "auto") or "auto").lower() == "native"
        if es_url and not forced_native:
            rule_pack.native_validation_stats = {"checked": 0, "degraded": 0, "kept": 0}
            _attach_native_promql_validator(rule_pack, args, verify=verify)
    else:
        # ``--translation-mode esql`` (or a confirmed-absent probe) disables the
        # native path entirely; no validator and the ES|QL translator handles
        # every panel.
        rule_pack.native_promql = False
        rule_pack.native_promql_validator = None
        # Native path clears the default ``prometheus`` dataset filter because
        # it is wrong for OTel / mixed streams. ES|QL must do the same when the
        # operator did not pass ``--dataset-filter`` — otherwise OTel dashboards
        # bind ``data_stream.dataset: prometheus`` and render empty.
        if not getattr(args, "dataset_filter", ""):
            profile = str(getattr(args, "field_profile", "") or "").strip().lower()
            if profile in {"", "otel", "auto", "passthrough"}:
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
    field_discovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = {
        "total": len(results),
        "translation_failed": sum(1 for r in results if r.translation_error),
        "artifacts_dir": str(output_dir),
        "validation_summary": validation_summary,
    }
    if field_discovery is not None:
        summary["field_discovery"] = field_discovery
    return summary


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

    def identifier_defaults(panel_result: Any) -> dict[str, str]:
        query_ir = getattr(panel_result, "query_ir", None)
        if not isinstance(query_ir, dict):
            return {}
        metadata = query_ir.get("metadata")
        if not isinstance(metadata, dict):
            return {}
        defaults = metadata.get("esql_identifier_param_defaults")
        if not isinstance(defaults, dict):
            return {}
        return {
            str(name): str(value)
            for name, value in defaults.items()
            if name and value not in (None, "")
        }

    unique_jobs: list[tuple[Any, Any]] = []
    unique_index_by_signature: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
    job_to_unique_index: list[int] = []
    for job in validation_jobs:
        query = str(getattr(job[1], "esql_query", "") or "")
        defaults = identifier_defaults(job[1])
        signature = (query, tuple(sorted(defaults.items())))
        if signature not in unique_index_by_signature:
            unique_index_by_signature[signature] = len(unique_jobs)
            unique_jobs.append(job)
        job_to_unique_index.append(unique_index_by_signature[signature])

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
            identifier_params=identifier_defaults(panel_result),
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
    metrics_target: MetricsTargetGuidance | None = None,
) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)
    run_summary = {
        "requested_assets": requested_assets,
        "ran": {
            "dashboards": dashboard_summary is not None,
            "alerts": alert_summary is not None,
        },
    }
    if metrics_target is not None:
        # The banner scrolls past mid-run and CI only keeps the artifacts.
        run_summary["metrics_target"] = metrics_target.as_summary()
    if dashboard_summary is not None:
        run_summary["dashboards"] = dashboard_summary
    if alert_summary is not None:
        run_summary["alerts"] = alert_summary

    summary_path = base_dir / "run_summary.json"
    summary_path.write_text(json.dumps(run_summary, indent=2), encoding="utf-8")
    print(f"  Run summary: {summary_path}")


_GRAFANA_FIELD_PROFILES = (
    "otel",
    "prometheus_remote_write",
    "prometheus_metrics",
    "prometheus_native",
    "passthrough",
    "auto",
)


def _validate_field_profile(args: argparse.Namespace) -> None:
    if args.field_profile not in _GRAFANA_FIELD_PROFILES:
        print(
            "Grafana supports --field-profile "
            f"{' or '.join(_GRAFANA_FIELD_PROFILES)} only",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if args.field_profile == "auto" and not (args.es_url or "").strip():
        print(
            "Grafana --field-profile auto requires --es-url for live schema discovery",
            file=sys.stderr,
        )
        raise SystemExit(2)


def _build_dashboard_schema_resolver(
    args: argparse.Namespace,
    rule_pack,
    *,
    verify: bool | str,
) -> SchemaResolver:
    return SchemaResolver(
        rule_pack,
        es_url=args.es_url or None,
        index_pattern=args.esql_index or args.data_view,
        es_api_key=args.es_api_key or None,
        verify=verify,
        field_profile=args.field_profile,
    )


def _describe_exception(exc: BaseException) -> str:
    """Never return "" — an empty reason reads as "the target is fine"."""
    return str(exc).strip() or exc.__class__.__name__


def assess_metrics_target_from_args(
    args: argparse.Namespace, resolver: SchemaResolver | None
) -> MetricsTargetGuidance:
    """Assess migrate-first / mixed-wildcard / UI-vs-query index footguns (#284)."""
    concrete_streams: list[str] = []
    conflicts: list[str] = []
    discovery_error = ""
    target_missing = False
    es_url = str(getattr(args, "es_url", "") or "").strip()
    if resolver is not None and es_url:
        try:
            concrete_streams = list(resolver.concrete_index_candidates() or [])
            discovery_error = resolver.concrete_index_error()
            target_missing = bool(resolver.concrete_index_missing())
        except Exception as exc:
            discovery_error = _describe_exception(exc)
        try:
            conflicts = list(resolver.tsdb_conflict_fields() or [])
        except Exception:
            # Conflict detection is an extra hint on top of the stream list;
            # losing it should not suppress the rest of the guidance.
            conflicts = []
    return assess_metrics_target(
        data_view=getattr(args, "data_view", "") or "",
        esql_index=getattr(args, "esql_index", "") or "",
        es_url=es_url,
        concrete_streams=concrete_streams,
        tsdb_conflict_fields=conflicts,
        stream_discovery_error=discovery_error,
        target_missing=target_missing,
    )


def _print_metrics_target_operator_guidance(
    args: argparse.Namespace, resolver: SchemaResolver | None
) -> MetricsTargetGuidance:
    guidance = assess_metrics_target_from_args(args, resolver)
    print_metrics_target_guidance(guidance)
    return guidance


def _print_schema_discovery_status(
    resolver: SchemaResolver,
    *,
    field_profile: str,
) -> None:
    """Print discovery status without implying passthrough remapped fields."""
    discovery = resolver.discovery_status()
    summary = resolver.field_resolution_summary()
    if discovery["status"] == "ok":
        if field_profile == "passthrough":
            print(
                f"  Discovered {discovery['field_count']} fields "
                "(field_profile=passthrough; automatic mapping disabled)"
            )
            return
        print(
            f"  Discovered {discovery['field_count']} fields, "
            f"{len(resolver._discovered_mappings)} label mappings "
            f"(field_profile={field_profile})"
        )
        planned = summary.get("planned_schema_profile")
        detected = summary.get("detected_schema_profile")
        if planned is not None:
            print(f"  planned_schema_profile={planned}")
        if detected is not None:
            print(f"  detected_schema_profile={detected}")
        if summary.get("profile_mismatch"):
            print("  profile_mismatch=yes")
        auto_fallback = summary.get("auto_fallback")
        if auto_fallback is not None:
            print(f"  auto_fallback={auto_fallback}")
        profile = resolver.schema_profile() or "generic/otel"
        if planned is None and detected is None:
            print(f"  schema_profile={profile}")
        if resolver.schema_profile() is None and planned is None:
            print("  WARNING: no Prometheus schema profile detected; using OTel/pass-through fallbacks")
        for warning in summary.get("profile_warnings") or []:
            print(f"  WARNING: {warning}")
        guidance = summary.get("operator_guidance") or {}
        suggestion = str(guidance.get("suggested_field_profile") or "").strip()
        next_step = str(guidance.get("next_step") or "").strip()
        if suggestion:
            print(f"  suggested_field_profile={suggestion}")
        if next_step:
            print(f"  Next step: {next_step}")
    elif discovery["status"] == "empty":
        print("  WARNING: schema discovery reached Elasticsearch but found no fields")
    elif discovery["status"] == "error":
        print(f"  WARNING: schema discovery failed: {discovery['error']}")
    else:
        print("  Schema discovery: offline mode")


def _clear_dashboard_artifacts(
    base_dir: Path,
    *,
    native_dir: Path | None = None,
    ir_dir: Path | None = None,
) -> int:
    """Remove a previous run's dashboard artifacts from the output directory.

    Also sweeps the ``yaml/`` and ``compiled/`` directories a pre-native
    release left behind. The pipeline no longer produces either, and leaving
    stale artifacts next to fresh ``native/`` ones invites an operator to
    upload something this run never generated.
    """
    removed = 0
    for legacy_dir in (base_dir / "yaml", base_dir / "compiled"):
        if not legacy_dir.is_dir():
            continue
        for child in legacy_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        legacy_dir.rmdir()
    for artifact_dir, pattern in ((native_dir, "*.native.json"), (ir_dir, "*.ir.json")):
        if artifact_dir is not None and artifact_dir.exists():
            for artifact_file in artifact_dir.glob(pattern):
                artifact_file.unlink()
                removed += 1
            index_file = artifact_dir / "index.json"
            if index_file.exists():
                index_file.unlink()
                removed += 1
    return removed


def _apply_failed_validation_outcome(panel_result, validation_result):
    """Decide what to do with a panel whose live ES|QL validation failed.

    Missing target fields/indexes are a data-timing issue: the query is valid
    and the panel self-heals once telemetry arrives, so keep the visualization
    with a warning. A genuinely malformed query is broken regardless of data, so
    keep replacing it with a markdown placeholder (issue #154).

    Returns ``"self_heal"`` or ``"placeholder"``.
    """
    if validation_failure_self_heals(validation_result):
        mark_panel_migrated_with_missing_target_fields(panel_result, validation_result)
        return "self_heal"
    mark_panel_requires_manual_after_failed_validation(panel_result, validation_result)
    return "placeholder"


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
        unresolved = datasource_audit.get("unresolved_datasource_panels", 0)
        if unresolved:
            extra += f" ({unresolved} unresolved datasource variables)"
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
    *,
    datasource_index: str,
    esql_index: str,
    rule_pack: Any,
    resolver: Any,
    output_stem: str | None = None,
    id_disambiguator: str = "",
) -> MigrationResult:
    """Translate one dashboard; on unhandled exception return a stub result with translation_error set."""
    try:
        return translate_dashboard(
            dashboard,
            datasource_index=datasource_index,
            esql_index=esql_index,
            rule_pack=rule_pack,
            resolver=resolver,
            output_stem=output_stem,
            id_disambiguator=id_disambiguator,
        )
    except Exception as exc:
        title = dashboard.get("title") or dashboard.get("_source_file") or "unknown"
        print(f"  ✗ {title}: translation error — {exc}")
        return MigrationResult(
            dashboard_title=str(title),
            dashboard_uid=str(dashboard.get("uid") or ""),
            source_file=str(dashboard.get("_source_file") or ""),
            translation_error=traceback.format_exc(),
        )


def _allocate_dashboard_output_stem(
    *,
    title: str,
    dashboard_uid: str | None,
    used_stems: set[str],
) -> tuple[str, str]:
    """Allocate a unique dashboard artifact stem for one Grafana run.

    Returns ``(stem, id_disambiguator)``. The second value is empty unless the
    title collided with one already allocated, in which case it is the token
    that made the stem unique -- and the same token is appended to the Kibana
    dashboard id (see
    ``targets/kibana/dashboards_api.py::_stable_dashboard_id_from_ir``). Both
    come from one allocation so the artifact name and the dashboard id agree:
    ``shared_title_dash-beta`` <-> ``obs-migrate-shared-title-dash-beta``.
    """
    base = _dashboard_output_stem(title) or "untitled"
    if base not in used_stems:
        used_stems.add(base)
        return base, ""

    raw_uid = str(dashboard_uid or "").strip()
    if raw_uid:
        uid_suffix = _dashboard_output_stem(raw_uid)[:24]
        uid_candidate = f"{base}_{uid_suffix}"
        if uid_candidate not in used_stems:
            used_stems.add(uid_candidate)
            return uid_candidate, uid_suffix

    index = 2
    while True:
        candidate = f"{base}_{index}"
        if candidate not in used_stems:
            used_stems.add(candidate)
            return candidate, str(index)
        index += 1


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
        rule_pack = _load_configured_rule_pack_or_exit(args)
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
            # After the summary is written, so the operator keeps the artifacts.
            exit_if_rule_creation_skipped(alert_summary)
        return

    rule_pack = _load_configured_rule_pack_or_exit(args)
    _apply_native_promql_to_rule_pack(rule_pack, args)

    if args.es_api_key:
        configure_es_auth(args.es_api_key)

    verify = _resolve_tls_from_args(args)

    resolver = _build_dashboard_schema_resolver(
        args,
        rule_pack,
        verify=verify,
    )

    base_dir = dashboard_output_dir(root_output_dir)
    native_dir = base_dir / "native"
    ir_dir = base_dir / "ir"
    base_dir.mkdir(parents=True, exist_ok=True)
    removed_stale_artifacts = _clear_dashboard_artifacts(
        base_dir, native_dir=native_dir, ir_dir=ir_dir,
    )
    if removed_stale_artifacts:
        print(f"\n  Removed {removed_stale_artifacts} stale dashboard artifact(s) from {base_dir}")

    default_ai_model = args.local_ai_model
    polish_ai_model = args.local_ai_polish_model or resolve_task_model("polish", args.local_ai_endpoint, default_ai_model)
    review_ai_model = args.local_ai_review_model or default_ai_model

    if args.es_url:
        print(f"\n  Schema discovery: {args.es_url}")
        resolver._discover_fields()
        control_schema_path = str(getattr(args, "control_schema", "") or "").strip()
        if control_schema_path:
            schema_file = Path(control_schema_path)
            if not schema_file.is_file():
                raise FileNotFoundError(f"control schema not found: {schema_file}")
            schema_payload = json.loads(schema_file.read_text(encoding="utf-8"))
            resolver.merge_control_schema(schema_payload)
            print(f"  Merged control schema: {schema_file}")
        _print_schema_discovery_status(
            resolver,
            field_profile=args.field_profile,
        )
        metrics_target_guidance = _print_metrics_target_operator_guidance(args, resolver)
    else:
        print("\n  Schema discovery: disabled (pass --es-url to enable)")
        metrics_target_guidance = _print_metrics_target_operator_guidance(args, None)
        control_schema_path = str(getattr(args, "control_schema", "") or "").strip()
        if control_schema_path:
            schema_file = Path(control_schema_path)
            if not schema_file.is_file():
                raise FileNotFoundError(f"control schema not found: {schema_file}")
            schema_payload = json.loads(schema_file.read_text(encoding="utf-8"))
            resolver.merge_control_schema(schema_payload)
            print(f"  Merged offline control schema: {schema_file}")

    print(f"\n[1/5] Extracting dashboards (source={args.source})...")
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

    print("\n[2/5] Translating dashboards...")
    results = []
    # (result, artifact_stem, raw_dashboard); ``artifact_stem`` is None when
    # translation raised, which is what downstream stages skip on.
    dashboard_outputs = []
    used_dashboard_stems: set[str] = set()
    for dashboard in dashboards:
        output_stem, id_disambiguator = _allocate_dashboard_output_stem(
            title=str(dashboard.get("title") or ""),
            dashboard_uid=str(dashboard.get("uid") or ""),
            used_stems=used_dashboard_stems,
        )
        dashboard_pack = resolve_pack_for_dashboard(
            dashboard,
            rule_pack,
            no_curated=getattr(args, "no_curated_packs", False),
        )
        if dashboard_pack is not rule_pack:
            gnet_id = dashboard.get("gnetId", "?")
            print(f"  [curated pack] gnetId={gnet_id} — applying bundled curated rules")
            # The shared resolver was built with the base rule_pack; clone it so the
            # curated pack's label_candidates are visible to offline label resolution.
            dashboard_resolver = resolver.copy_with_pack(dashboard_pack)
        else:
            dashboard_resolver = resolver
        result = _translate_dashboard_resilient(
            dashboard,
            datasource_index=args.data_view,
            esql_index=args.esql_index or args.data_view,
            rule_pack=dashboard_pack,
            resolver=dashboard_resolver,
            output_stem=output_stem,
            id_disambiguator=id_disambiguator,
        )
        curated_pack_name = getattr(dashboard_pack, "_curated_pack_name", "")
        if curated_pack_name:
            result.curated_pack = curated_pack_name
        if result.translation_error:
            results.append(result)
            dashboard_outputs.append((result, None, dashboard))
            continue
        # The dashboard id no longer matches the plain title slug, so say so:
        # nothing else in the run output would explain where it went.
        disambiguation_note = dashboard_id_disambiguation_note(result.dashboard_ir)
        if disambiguation_note:
            print(f"  ⚠ {disambiguation_note}")
        if args.polish_metadata:
            polish_summary = apply_metadata_polish(
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
        dashboard_outputs.append((result, output_stem, dashboard))
        status_icon = "✓" if result.not_feasible == 0 else "⚠"
        print(
            f"  {status_icon} {result.dashboard_title}: "
            f"{result.migrated}✓ {result.migrated_with_warnings}⚠ "
            f"{result.requires_manual}? {result.not_feasible}✗ "
            f"(of {result.total_panels} panels)"
        )

    _print_native_validation_summary(rule_pack)

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
            resolver=resolver,
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
                resolver=resolver,
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
        print("\n[3/5] Verification-packet ES|QL validation against Elasticsearch...", flush=True)
        passed = 0
        fixed = 0
        fixed_empty = 0
        failed = 0
        manualized_failed = 0
        self_healing_failed = 0
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
                if _apply_failed_validation_outcome(pr, validation_result) == "self_heal":
                    self_healing_failed += 1
                else:
                    manualized_failed += 1

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
            f"{failed} failed ({self_healing_failed} kept as empty panels awaiting data, "
            f"{manualized_failed} replaced with upload-safe placeholders)"
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
        for result, artifact_stem, _dashboard in dashboard_outputs:
            if artifact_stem is None:
                continue
            sync_result_queries_to_ir(result)
    else:
        print(
            "\n[3/5] Verification-packet ES|QL validation: skipped "
            "(pass --validate --es-url to enable; this is distinct from the "
            "native PROMQL parse validation that runs with --es-url)"
        )

    print("\n[4/5] Writing native Dashboard-as-Code review artifacts...")
    native_index_entries: list[dict[str, Any]] = []
    for result, artifact_stem, _dashboard in dashboard_outputs:
        if artifact_stem is None or result.dashboard_ir is None or result.native_dashboard is None:
            continue
        stem = artifact_stem
        native_path = write_native_artifact(
            dashboard_ir=result.dashboard_ir,
            native_dashboard=result.native_dashboard,
            native_stats=result.native_dashboard_stats,
            native_dir=native_dir,
            stem=stem,
        )
        ir_path = write_ir_artifact(dashboard_ir=result.dashboard_ir, ir_dir=ir_dir, stem=stem)
        result.native_artifact_path = str(native_path)
        result.ir_artifact_path = str(ir_path)
        native_index_entries.append(
            {
                "stem": stem,
                "title": result.dashboard_title,
                "dashboard_id": result.native_dashboard.dashboard_id,
                "native_path": str(native_path.relative_to(base_dir)),
                "ir_path": str(ir_path.relative_to(base_dir)),
            }
        )
    if native_index_entries:
        write_native_artifact_index(native_dir, native_index_entries)
    print(f"  {len(native_index_entries)} dashboard(s) written to {native_dir}")

    target_space = detect_space_id_from_kibana_url(args.kibana_url) or "default"
    if args.upload and args.ensure_data_views:
        _ensure_grafana_data_views(args)
    if args.upload:
        print(f"\nUploading to Kibana at {args.kibana_url}...")
        upload_space = args.shadow_space or ""
        upload_kibana_url = kibana_url_for_space(args.kibana_url, upload_space)
        target_adapter = KibanaTargetAdapter()
        # Shared across the loop: a dashboard id reached twice would upsert over
        # an earlier dashboard and report "updated". See
        # ``dashboards_api._upload_native_api_payload``.
        uploaded_dashboard_ids: set[str] = set()
        for result, artifact_stem, _dashboard in dashboard_outputs:
            result.upload_attempted = True
            if artifact_stem is None:
                continue
            native_dashboard = getattr(result, "native_dashboard", None)
            if native_dashboard is None:
                result.uploaded = False
                result.upload_error = (
                    "Upload skipped because no native dashboard payload was generated."
                )
                print(f"  - {artifact_stem} skipped (no native payload)")
                continue
            upload_result = target_adapter.upload_dashboard(
                kibana_url=args.kibana_url,
                space_id=upload_space,
                kibana_api_key=args.kibana_api_key,
                es_url=args.es_url,
                es_api_key=args.es_api_key,
                verify=verify,
                native_dashboard=native_dashboard,
                native_dashboard_stats=getattr(result, "native_dashboard_stats", None),
                artifact_label=artifact_stem,
                seen_dashboard_ids=uploaded_dashboard_ids,
            )
            ok = upload_result["success"]
            output = upload_result["output"]
            result.uploaded = ok
            result.upload_error = "" if ok else output
            result.upload_warnings = upload_warnings_from_reasons(
                upload_result.get("unmapped_reasons", {})
            )
            result.upload_dropped_panels = list(
                upload_result.get("dropped_panels") or []
            )
            result.uploaded_space = upload_space or target_space
            result.uploaded_kibana_url = upload_result.get("kibana_url", upload_kibana_url)
            icon = "✓" if ok else "✗"
            print(f"  {icon} {artifact_stem}")
            if not ok:
                for line in output.strip().splitlines()[:10]:
                    print(f"    {line}")
            # Named per panel, not just counted: an HTTP 200 upload that
            # dropped panels is invisible unless the report says which ones.
            for dropped in result.upload_dropped_panels:
                reason = f": {str(dropped.get('reason') or '')[:300]}" if dropped.get("reason") else ""
                print(
                    f"    DROPPED PANEL {dropped.get('title') or '(untitled)'}{reason}",
                    file=sys.stderr,
                )
            for warning in result.upload_warnings:
                print(f"    warning: {warning}", file=sys.stderr)

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
        gate_panels = [
            pr for pr in result.panel_results
            if getattr(pr, "status", "") != "skipped"
            and getattr(pr, "grafana_type", "") != "row"
        ]
        result.verification_summary = {
            "green": sum(1 for pr in gate_panels if (pr.verification_packet or {}).get("semantic_gate") == "Green"),
            "yellow": sum(1 for pr in gate_panels if (pr.verification_packet or {}).get("semantic_gate") == "Yellow"),
            "red": sum(1 for pr in gate_panels if (pr.verification_packet or {}).get("semantic_gate") == "Red"),
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

    print("\n[5/5] Generating report...")
    field_discovery = resolver.field_resolution_summary()
    from observability_migration.core.metric_mapping.reporting import metric_map_summary_from_tracker

    metric_map_summary = metric_map_summary_from_tracker(resolver)
    report_path = base_dir / "migration_report.json"
    manifest_path = base_dir / "migration_manifest.json"
    verification_path = base_dir / "verification_packets.json"
    save_detailed_report(
        results,
        report_path,
        validation_summary,
        validation_records,
        verification_payload,
        field_discovery=field_discovery,
        metric_map_summary=metric_map_summary,
    )
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
            field_discovery=field_discovery,
        ),
        alert_summary=alert_run_summary,
        metrics_target=metrics_target_guidance,
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

    print("\n  Rollout plan & feature summaries...")
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
            review_queue=review_queue,
            gap_data=manifest_extras,
            run_id=rollout_run_id,
        )
        summary_md_path = base_dir / "migration_summary.md"
        save_markdown_summary(summary_view, summary_md_path)
        print(f"  Migration summary: {summary_md_path}")
    except Exception as exc:  # best-effort: never fail a migration on the summary
        print(f"  Migration summary: skipped ({exc})")

    print_report(results, field_discovery=field_discovery)

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

    # Last thing in the run: the full report is printed and every artifact is on
    # disk, but a requested --create-alert-rules that created nothing must not
    # exit 0.
    exit_if_rule_creation_skipped(alert_run_summary)


__all__ = ["main", "parse_args"]


if __name__ == "__main__":
    main()
