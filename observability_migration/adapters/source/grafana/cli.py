# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Package-level entrypoints for the migration CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from observability_migration.core.cli_contract import (
    ASSET_CHOICES,
    alert_output_dir,
    dashboard_output_dir,
    normalize_requested_assets,
)
from observability_migration.core.reporting.report import (
    MigrationResult,
    mark_panel_requires_manual_after_failed_validation,
    mark_panel_requires_manual_after_validation,
    print_report,
    recompute_result_counts,
    save_detailed_report,
)
from observability_migration.targets.kibana.compile import (
    compile_all,
    detect_space_id_from_kibana_url,
    kibana_url_for_space,
    lint_dashboard_yaml,
    sync_result_queries_to_yaml,
    upload_yaml,
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
from .extract import extract_dashboards_from_files, extract_dashboards_from_grafana
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
    build_target_schema_contract,
    probe_source_metric_inventory,
    probe_target_readiness,
    save_preflight_report,
    save_schema_contract,
)
from .rollout import build_rollout_plan, generate_review_queue, save_rollout_plan
from .rules import build_rule_catalog, load_python_plugins, load_rule_pack_files
from .schema import SchemaResolver
from .smoke_integration import load_smoke_report, merge_smoke_into_results
from .transforms import build_redesign_tasks, build_transform_summary, extract_transformations
from .verification import annotate_results_with_verification, save_verification_packets

GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:3000")
GRAFANA_USER = os.getenv("GRAFANA_USER", "admin")
GRAFANA_PASS = os.getenv("GRAFANA_PASS", "admin")
KIBANA_URL = os.getenv("KIBANA_URL", "http://localhost:5601")
ES_URL = os.getenv("ES_URL", "")


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Grafana → Kibana migration pipeline")
    parser.add_argument(
        "--source",
        choices=["api", "files"],
        default="files",
        help="Source: 'api' for live Grafana, 'files' for local JSON",
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
    parser.add_argument(
        "--native-promql",
        action="store_true",
        help="Emit native PROMQL source commands instead of translating to ES|QL (for Elastic Serverless with PromQL support)",
    )
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
    return parser.parse_args(argv)


def _handle_list_dashboards(args):
    if not args.kibana_url:
        print("  ERROR: --kibana-url is required for --list-dashboards")
        return
    dashboards = serverless_list_dashboards(
        args.kibana_url,
        api_key=args.kibana_api_key,
        space_id=getattr(args, "shadow_space", ""),
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
        result.yaml_path = str(yaml_path)
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
        return extract_dashboards_from_grafana(
            GRAFANA_URL,
            GRAFANA_USER,
            GRAFANA_PASS,
            token=getattr(args, "grafana_token", "") or "",
        )
    return extract_dashboards_from_files(args.input_dir)


def _load_configured_rule_pack(args: argparse.Namespace):
    rule_pack = load_rule_pack_files(args.rules_file)
    if args.logs_index:
        rule_pack.logs_index = args.logs_index
    if args.native_promql:
        rule_pack.native_promql = True
        rule_pack.metrics_dataset_filter = ""
    if args.dataset_filter:
        rule_pack.metrics_dataset_filter = args.dataset_filter
    if args.logs_dataset_filter:
        rule_pack.logs_dataset_filter = args.logs_dataset_filter
    load_python_plugins(args.plugin, rule_pack)
    return rule_pack


def _build_dashboard_run_summary(
    output_dir: Path,
    *,
    results: list[MigrationResult],
    validation_summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "total": len(results),
        "artifacts_dir": str(output_dir),
        "validation_summary": validation_summary,
    }


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

    if args.es_api_key:
        configure_es_auth(args.es_api_key)

    resolver = SchemaResolver(
        rule_pack,
        es_url=args.es_url or None,
        index_pattern=args.esql_index or args.data_view,
        es_api_key=args.es_api_key or None,
    )

    base_dir = dashboard_output_dir(root_output_dir)
    yaml_dir = base_dir / "yaml"
    compiled_dir = base_dir / "compiled"
    yaml_dir.mkdir(parents=True, exist_ok=True)
    compiled_dir.mkdir(parents=True, exist_ok=True)

    default_ai_model = args.local_ai_model
    polish_ai_model = args.local_ai_polish_model or resolve_task_model("polish", args.local_ai_endpoint, default_ai_model)
    review_ai_model = args.local_ai_review_model or default_ai_model

    if args.es_url:
        print(f"\n  Schema discovery: {args.es_url}")
        resolver._discover_fields()
        if resolver._field_cache:
            print(f"  Discovered {len(resolver._field_cache)} fields, {len(resolver._discovered_mappings)} label mappings")
        else:
            print("  Schema discovery: no fields found (offline mode)")
    else:
        print("\n  Schema discovery: disabled (pass --es-url to enable)")

    print(f"\n[1/7] Extracting dashboards (source={args.source})...")
    if args.source == "api":
        dashboards = extract_dashboards_from_grafana(
            GRAFANA_URL,
            GRAFANA_USER,
            GRAFANA_PASS,
            token=getattr(args, "grafana_token", "") or "",
        )
    else:
        dashboards = extract_dashboards_from_files(args.input_dir)
    print(f"  Found {len(dashboards)} dashboards")

    print("\n[2/7] Translating dashboards to YAML...")
    results = []
    dashboard_outputs = []
    for dashboard in dashboards:
        result, yaml_path = translate_dashboard(
            dashboard,
            yaml_dir,
            datasource_index=args.data_view,
            esql_index=args.esql_index or args.data_view,
            rule_pack=rule_pack,
            resolver=resolver,
        )
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
        print("\n[3/7] Validating ES|QL queries against Elasticsearch...")
        total_queries = 0
        passed = 0
        fixed = 0
        fixed_empty = 0
        failed = 0
        manualized_failed = 0
        for r in results:
            for pr in r.panel_results:
                if not pr.esql_query:
                    continue
                total_queries += 1
                validation_result = validate_query_with_fixes(
                    pr.esql_query, args.es_url, resolver,
                    es_api_key=args.es_api_key or None,
                )
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
            sync_result_queries_to_yaml(result, yaml_path)
    else:
        print("\n[3/7] Validation: skipped (pass --validate --es-url to enable)")

    yaml_files = sorted(yaml_dir.glob("*.yaml"))
    print("\n[4/7] Linting generated dashboard YAML...")
    yaml_lint_ok, yaml_lint_output = lint_dashboard_yaml(yaml_dir)
    for result in results:
        result.yaml_linted = True
        result.yaml_lint_error = "" if yaml_lint_ok else yaml_lint_output
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
        compile_results = [
            (
                yaml_file.name,
                False,
                "Dashboard YAML lint failed before compile.\n" + yaml_lint_output,
            )
            for yaml_file in yaml_files
        ]

    compile_map = {Path(name).stem: (ok, output) for name, ok, output in compile_results}
    for result in results:
        dashboard_stem = _dashboard_output_stem(result.dashboard_title)
        result.compiled_path = str(compiled_dir / dashboard_stem / "compiled_dashboards.ndjson")
        compiled_state = compile_map.get(dashboard_stem)
        if compiled_state:
            result.compiled = compiled_state[0]
            result.compile_error = "" if compiled_state[0] else compiled_state[1]
    for name, ok, _output in compile_results:
        icon = "✓" if ok else "✗"
        print(f"  {icon} {name}")

    if yaml_lint_ok and any(ok for _, ok, _ in compile_results):
        layout_ok, layout_output = validate_compiled_layout(compiled_dir)
        for result in results:
            result.layout_validated = True
            result.layout_error = "" if layout_ok else layout_output
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
        upload_blocker = ""
        if not yaml_lint_ok:
            upload_blocker = "Upload skipped because dashboard YAML lint failed."
        elif any(not ok for _, ok, _ in compile_results):
            upload_blocker = "Upload skipped because one or more dashboards failed to compile."
        elif any(getattr(result, "layout_validated", None) and result.layout_error for result in results):
            upload_blocker = "Upload skipped because compiled dashboard layout validation failed."

        if upload_blocker:
            print(f"  ✗ {upload_blocker}")
            for result in results:
                result.upload_attempted = True
                result.uploaded = False
                result.upload_error = upload_blocker
        else:
            for result, yaml_path, _dashboard in dashboard_outputs:
                result.upload_attempted = True
                ok, output = upload_yaml(
                    yaml_path,
                    compiled_dir / yaml_path.stem,
                    args.kibana_url,
                    space_id=upload_space,
                    kibana_api_key=args.kibana_api_key,
                )
                result.uploaded = ok
                result.upload_error = "" if ok else output
                result.uploaded_space = upload_space or target_space
                result.uploaded_kibana_url = upload_kibana_url
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
    print(f"  Detailed report: {report_path}")
    print(f"  Migration manifest: {manifest_path}")
    print(f"  Verification packets: {verification_path}")
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
        source_urls_configured = bool(
            getattr(args, "prometheus_url", "") or getattr(args, "loki_url", ""),
        )

        print("\n  Preflight probes...")
        referenced_metrics = _collect_referenced_metrics(results)
        referenced_labels = _collect_referenced_labels(results)

        source_inventory = probe_source_metric_inventory(
            getattr(args, "prometheus_url", "") or "",
            required_metrics=referenced_metrics,
            required_labels=referenced_labels,
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
        required_index_patterns = list(
            schema_contract.get("required_indexes", {}).keys(),
        )

        target_readiness = probe_target_readiness(
            args.es_url, required_index_patterns,
            es_api_key=args.es_api_key or None,
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
            source_urls_configured=source_urls_configured,
            target_url_configured=bool(args.es_url),
            source_inventory=source_inventory,
            target_readiness=target_readiness,
            datasource_audit=datasource_audit,
            complexity_scores=complexity_scores,
        )

        preflight_path = base_dir / "preflight_report.json"
        contract_path = base_dir / "required_target_contract.json"
        save_preflight_report(preflight_report, preflight_path)
        save_schema_contract(schema_contract, contract_path)
        print(f"  Preflight report: {preflight_path}")
        print(f"  Target schema contract: {contract_path}")

        if args.suggest_rule_pack_out and validation_summary:
            write_suggested_rule_pack(args.suggest_rule_pack_out, validation_summary)
            print(f"  Suggested rule pack: {args.suggest_rule_pack_out}")

        action_summary = preflight_report.get("customer_action_summary", "")
        if action_summary:
            print(f"\n{action_summary}")

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


__all__ = ["main", "parse_args"]


if __name__ == "__main__":
    main()
