# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""CLI entry point for the Datadog → Kibana migration tool.

Usage:
    python -m observability_migration.adapters.source.datadog.cli \\
        --source files \\
        --input-dir infra/datadog/dashboards \\
        --output-dir datadog_migration_output \\
        --field-profile otel \\
        --data-view "metrics-*"
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, cast

import requests

import observability_migration.targets.kibana.adapter  # noqa: F401
from observability_migration.adapters.source.grafana.esql_validate import (
    _query_source_and_index,
    summarize_validation_records,
    validate_query_with_fixes,
)
from observability_migration.adapters.source.grafana.rules import _append_unique
from observability_migration.core.cli_contract import (
    ASSET_CHOICES,
    alert_output_dir,
    dashboard_output_dir,
    normalize_requested_assets,
)
from observability_migration.core.http import resolve_tls
from observability_migration.core.interfaces.registries import target_registry
from observability_migration.core.interfaces.target_adapter import TargetAdapter
from observability_migration.core.reporting.summary_md import save_markdown_summary
from observability_migration.core.selection import (
    add_selection_arguments,
    apply_cli_selection,
    criteria_from_args,
)
from observability_migration.core.telemetry_contract import write_schema_report_artifacts
from observability_migration.targets.kibana.compile import validate_compiled_layout
from observability_migration.targets.kibana.smoke_integration import merge_smoke_into_results

from .extract import (
    extract_dashboards_from_api,
    extract_dashboards_from_files,
    load_credentials_from_env,
    selection_metadata_from_datadog_dashboard,
)
from .field_map import FieldMapProfile, load_profile
from .generate import generate_dashboard_yaml
from .manifest import save_migration_manifest
from .models import DashboardResult, NormalizedWidget, TranslationResult
from .normalize import normalize_dashboard
from .planner import plan_widget
from .preflight import (
    PreflightResult,
    build_target_readiness_contract,
    run_preflight,
    save_target_readiness_contract,
)
from .report import build_summary_view, print_report, save_detailed_report
from .rollout import build_rollout_plan, generate_review_queue, save_rollout_plan
from .translate import translate_widget
from .verification import annotate_results_with_verification, save_verification_packets


def _env_truthy_default(name: str) -> bool:
    return str(os.getenv(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_tls_from_args(args: argparse.Namespace) -> bool | str:
    return resolve_tls(
        ca_cert=getattr(args, "ca_cert", "") or "",
        insecure=bool(getattr(args, "insecure", False)),
    )


def _selection_criteria_or_exit(args: argparse.Namespace) -> Any:
    """Build SelectionCriteria from args, exiting 1 on an unparseable date."""
    try:
        return criteria_from_args(args)
    except ValueError as exc:
        print(f"  ERROR: invalid --select-updated-* value: {exc}", file=sys.stderr)
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    verify = _resolve_tls_from_args(args)
    selection = normalize_requested_assets(
        assets=args.assets,
        fetch_alerts=False,
        fetch_monitors=getattr(args, "fetch_monitors", False),
    )
    auto_enabled_upload = False

    if args.list_dashboards:
        target_adapter = target_registry.get("kibana")()
        _handle_list_dashboards(args, target_adapter, verify=verify)
        return
    if args.delete_dashboards:
        target_adapter = target_registry.get("kibana")()
        _handle_delete_dashboards(args, target_adapter, verify=verify)
        return

    compile_requested = False
    if selection.dashboards:
        if (args.browser_audit or args.capture_screenshots) and not args.smoke:
            print("  ERROR: --browser-audit and --capture-screenshots require --smoke")
            sys.exit(2)
        if args.smoke and not args.upload:
            args.upload = True
            auto_enabled_upload = True
        compile_requested = args.compile or args.upload

        if args.upload and not args.kibana_url:
            print("  ERROR: --kibana-url is required when --upload is set")
            sys.exit(2)
        if args.smoke and not args.es_url:
            print("  ERROR: --es-url is required when --smoke is set")
            sys.exit(2)
    dd_creds = load_credentials_from_env(args.env_file)

    print("\n  Datadog → Kibana Migration Tool v0.1.0")
    print(f"  Source: {args.source}")
    print(f"  Field profile: {args.field_profile}")
    print(f"  Output: {args.output_dir}\n")
    if auto_enabled_upload:
        print("  Smoke requested: auto-enabling upload step\n")
    if selection.dashboards and args.upload and not args.compile:
        print("  Upload requested: auto-enabling compile step\n")
    try:
        field_map = load_profile(args.field_profile)
    except ValueError as exc:
        print(f"  ERROR: {exc}")
        sys.exit(1)
    if args.data_view:
        field_map.metric_index = args.data_view
    if args.logs_index:
        field_map.logs_index = args.logs_index
    if args.dataset_filter:
        field_map.metrics_dataset_filter = args.dataset_filter
    if args.logs_dataset_filter:
        field_map.logs_dataset_filter = args.logs_dataset_filter
    if not field_map.metrics_dataset_filter:
        from .field_map import derive_dataset_from_index
        field_map.metrics_dataset_filter = derive_dataset_from_index(field_map.metric_index)
    if not field_map.logs_dataset_filter:
        from .field_map import derive_dataset_from_index
        field_map.logs_dataset_filter = derive_dataset_from_index(field_map.logs_index)
    _load_live_field_capabilities(field_map, args, verify=verify)
    base_dir = Path(args.output_dir)
    dashboards_dir = dashboard_output_dir(base_dir)
    alerts_dir = alert_output_dir(base_dir)

    dashboard_summary: dict[str, Any] | None = None
    if selection.dashboards:
        target_adapter = target_registry.get("kibana")()
        dashboard_summary = _run_dashboard_pipeline(
            args=args,
            field_map=field_map,
            output_dir=dashboards_dir,
            dd_creds=dd_creds,
            target_adapter=target_adapter,
            compile_requested=compile_requested,
            verify=verify,
        )

    alert_summary: dict[str, Any] | None = None
    if selection.alerts:
        print("\n  Extracting Datadog monitors...")
        from .alert_pipeline import run_alert_pipeline

        alert_summary = run_alert_pipeline(
            args,
            field_map=field_map,
            output_dir=alerts_dir,
            dd_creds=dd_creds,
        )

    _write_run_summary(
        base_dir,
        requested_assets=selection.label,
        dashboard_summary=dashboard_summary,
        alert_summary=alert_summary,
    )


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


def _run_dashboard_pipeline(
    *,
    args: argparse.Namespace,
    field_map: Any,
    output_dir: Path,
    dd_creds: dict[str, str],
    target_adapter: Any,
    compile_requested: bool,
    verify: bool | str = True,
) -> dict[str, Any]:
    raw_dashboards = _extract(args)
    if not raw_dashboards:
        print(
            f"  ERROR: no Datadog dashboards found under {args.input_dir}. "
            "Point --input-dir at a directory of Datadog dashboard JSON "
            "exports (each with a top-level 'widgets' key).",
            file=sys.stderr,
        )
        sys.exit(1)

    criteria = _selection_criteria_or_exit(args)
    raw_dashboards = apply_cli_selection(
        raw_dashboards,
        selection_metadata_from_datadog_dashboard,
        criteria,
        label="datadog dashboard",
        kind="dashboard(s)",
    )
    if not criteria.is_empty and not raw_dashboards:
        print(
            "  ERROR: no Datadog dashboards matched the --select-* criteria.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"  Found {len(raw_dashboards)} dashboard(s)\n")

    output_dir.mkdir(parents=True, exist_ok=True)
    yaml_dir = output_dir / "yaml"
    yaml_dir.mkdir(parents=True, exist_ok=True)
    removed_stale_artifacts = _clear_dashboard_artifacts(yaml_dir, output_dir / "compiled")
    if removed_stale_artifacts:
        print(f"  Removed {removed_stale_artifacts} stale dashboard artifact(s) from {output_dir}")

    all_results: list[DashboardResult] = []
    dashboard_outputs: list[tuple[DashboardResult, Any]] = []
    used_yaml_stems: set[str] = set()

    for raw in raw_dashboards:
        dashboard = normalize_dashboard(raw)
        print(f"  Processing: {dashboard.title} ({len(dashboard.widgets)} widgets)")

        total_count = len(dashboard.widgets)
        for widget in dashboard.widgets:
            total_count += len(widget.children)

        dashboard_result = DashboardResult(
            dashboard_id=dashboard.id,
            dashboard_title=dashboard.title,
            source_file=dashboard.source_file,
            total_widgets=total_count,
        )
        preflight_result = _run_dashboard_preflight(dashboard, field_map, args)
        if preflight_result is not None:
            dashboard_result.preflight_passed = preflight_result.passed
            dashboard_result.preflight_issues = [
                _preflight_issue_to_dict(issue) for issue in preflight_result.issues
            ]
            _print_preflight_summary(preflight_result)

        panel_results: list[TranslationResult] = []
        for widget in dashboard.widgets:
            panel_results.append(_translate_widget(widget, field_map, args))
            if widget.children:
                for child in widget.children:
                    panel_results.append(_translate_widget(child, field_map, args))

        dashboard_result.panel_results = panel_results
        dashboard_yaml = generate_dashboard_yaml(
            dashboard,
            panel_results,
            data_view=field_map.metric_index,
            metrics_dataset_filter=field_map.metrics_dataset_filter,
            logs_dataset_filter=field_map.logs_dataset_filter,
            logs_index=field_map.logs_index,
            field_map=field_map,
        )

        stem = _allocate_yaml_stem(
            title=dashboard.title,
            dashboard_id=dashboard.id,
            used_stems=used_yaml_stems,
        )
        yaml_path = yaml_dir / f"{stem}.yaml"
        yaml_path.write_text(dashboard_yaml, encoding="utf-8")
        dashboard_result.yaml_path = str(yaml_path)

        print(f"    YAML written: {yaml_path}")

        dashboard_result.recompute_counts()
        all_results.append(dashboard_result)
        dashboard_outputs.append((dashboard_result, dashboard))

    validation_records: list[dict[str, Any]] = []
    validation_summary: dict[str, Any] = {}
    if args.validate and args.es_url:
        validation_records, validation_summary = _validate_all_dashboards(
            dashboard_outputs,
            field_map,
            args,
            verify=verify,
        )
    elif args.validate:
        print("  Validation: skipped (pass --es-url to enable)")

    if compile_requested:
        _compile_all_dashboards(all_results, output_dir, target_adapter)
    if args.upload and args.ensure_data_views:
        _ensure_data_views(args, target_adapter, field_map, verify=verify)
    if args.upload:
        _upload_all_dashboards(all_results, output_dir, args, target_adapter, verify=verify)

    smoke_payload: dict[str, Any] = {}
    if args.smoke:
        smoke_payload = _smoke_uploaded_dashboards(
            all_results,
            output_dir,
            args,
            target_adapter,
            verify=verify,
        )

    # Source-side execution hits the live Datadog API per panel. Keep it
    # strictly opt-in (--source-execution): otherwise translation must stay
    # fully offline, even when DD_API_KEY/DD_APP_KEY happen to be in the
    # environment. Forwarding creds unconditionally makes large offline/
    # corpus runs block on api.datadoghq.com.
    source_execution_enabled = getattr(args, "source_execution", False)
    verification_payload = annotate_results_with_verification(
        all_results,
        validation_records,
        datadog_api_key=dd_creds.get("api_key", "") if source_execution_enabled else "",
        datadog_app_key=dd_creds.get("app_key", "") if source_execution_enabled else "",
        datadog_site=dd_creds.get("site", "datadoghq.com"),
        verify=verify,
    )
    for dashboard_result in all_results:
        dashboard_result.verification_summary = {
            "green": sum(
                1
                for panel_result in dashboard_result.panel_results
                if (panel_result.verification_packet or {}).get("semantic_gate") == "Green"
            ),
            "yellow": sum(
                1
                for panel_result in dashboard_result.panel_results
                if (panel_result.verification_packet or {}).get("semantic_gate") == "Yellow"
            ),
            "red": sum(
                1
                for panel_result in dashboard_result.panel_results
                if (panel_result.verification_packet or {}).get("semantic_gate") == "Red"
            ),
        }

    print_report(all_results)

    report_path = output_dir / "migration_report.json"
    manifest_path = output_dir / "migration_manifest.json"
    verification_path = output_dir / "verification_packets.json"
    readiness_contract_path = output_dir / "target_readiness_contract.json"
    rollout_path = output_dir / "rollout_plan.json"
    readiness_contract = build_target_readiness_contract(
        [dashboard for _, dashboard in dashboard_outputs],
        field_map,
    )
    save_detailed_report(
        all_results,
        str(report_path),
        validation_summary=validation_summary,
        validation_records=validation_records,
        smoke_payload=smoke_payload,
        verification_payload=verification_payload,
    )
    save_migration_manifest(all_results, manifest_path)
    save_verification_packets(verification_payload, verification_path)
    save_target_readiness_contract(readiness_contract, readiness_contract_path)
    try:
        schema_artifacts = write_schema_report_artifacts(output_dir)
    except Exception as exc:  # best-effort: never fail a migration on derived reports
        schema_artifacts = {}
        print(f"  Schema report: skipped ({exc})")
    rollout_plan = build_rollout_plan(
        all_results,
        target_space=args.space_id or "",
        output_dir=str(output_dir),
        smoke_report_path=(
            args.smoke_output or str(output_dir / "uploaded_dashboard_smoke_report.json")
        )
        if args.smoke
        else "",
    )
    save_rollout_plan(rollout_plan, rollout_path)
    print(f"  Verification packets saved: {verification_path}")
    print(f"  Target readiness contract saved: {readiness_contract_path}")
    if schema_artifacts:
        print(f"  Schema change report saved: {schema_artifacts['schema_report']}")
        print(f"  Telemetry contract saved: {schema_artifacts['telemetry_contract']}")
    print(f"  Migration manifest saved: {manifest_path}")
    print(f"  Rollout plan saved: {rollout_path}")
    review_queue = generate_review_queue(rollout_plan)
    if review_queue:
        top_risk = review_queue[:3]
        print("  Review queue (top risk):")
        for item in top_risk:
            gates = item["gates"]
            print(
                f"    {item['dashboard']}: risk={item['risk_score']} "
                f"(G:{gates['green']} Y:{gates['yellow']} R:{gates['red']})"
            )

    try:
        rollout_run_id = (
            rollout_plan.get("run_id", "")
            if isinstance(rollout_plan, dict)
            else getattr(rollout_plan, "run_id", "")
        )
        summary_view = build_summary_view(
            all_results,
            review_queue=review_queue,
            run_id=rollout_run_id,
        )
        summary_md_path = output_dir / "migration_summary.md"
        save_markdown_summary(summary_view, summary_md_path)
        print(f"  Migration summary saved: {summary_md_path}")
    except Exception as exc:  # best-effort: never fail a migration on the summary
        print(f"  Migration summary: skipped ({exc})")

    return {
        "total": len(all_results),
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
    print(f"  Run summary saved: {summary_path}")


def _translate_widget(
    widget: NormalizedWidget,
    field_map: Any,
    args: argparse.Namespace,
) -> TranslationResult:
    """Plan and translate a single widget."""
    plan = plan_widget(widget)
    return translate_widget(widget, plan, field_map)


def _extract(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Extract dashboards based on source type."""
    if args.source == "files":
        try:
            return extract_dashboards_from_files(args.input_dir)
        except FileNotFoundError:
            print(
                f"  ERROR: no Datadog dashboards found under {args.input_dir}. "
                "Point --input-dir at a directory of Datadog dashboard JSON "
                "exports (each with a top-level 'widgets' key).",
                file=sys.stderr,
            )
            sys.exit(1)

    if args.source == "api":
        creds = load_credentials_from_env(args.env_file)
        if not creds["api_key"] or not creds["app_key"]:
            print("  ERROR: DD_API_KEY and DD_APP_KEY must be set")
            sys.exit(1)
        dashboard_ids = args.dashboard_ids.split(",") if args.dashboard_ids else None
        return extract_dashboards_from_api(
            api_key=creds["api_key"],
            app_key=creds["app_key"],
            site=creds["site"],
            dashboard_ids=dashboard_ids,
            verify=_resolve_tls_from_args(args),
        )

    print(f"  ERROR: unknown source: {args.source}")
    sys.exit(1)


def _load_live_field_capabilities(
    field_map: Any,
    args: argparse.Namespace,
    *,
    verify: bool | str | None = None,
) -> None:
    """Populate live target field capabilities when Elasticsearch access is configured."""
    if not args.es_url:
        print("  Target field capabilities: offline mode (pass --es-url to enable)")
        return
    verify = _resolve_tls_from_args(args) if verify is None else verify
    try:
        counts = field_map.load_live_field_capabilities(
            args.es_url,
            es_api_key=args.es_api_key,
            verify=verify,
        )
    except Exception as exc:
        print(f"  WARNING: target field capability discovery failed: {exc}")
        return
    metric_fields = counts.get("metric_fields", 0)
    log_fields = counts.get("log_fields", 0)
    parts = [f"metrics={metric_fields}"]
    if field_map.logs_index:
        parts.append(f"logs={log_fields}")
    print(f"  Target field capabilities: loaded {' '.join(parts)}")


def _run_dashboard_preflight(
    dashboard: Any,
    field_map: Any,
    args: argparse.Namespace,
) -> PreflightResult | None:
    """Run Datadog preflight only when explicitly requested."""
    has_capabilities = bool(
        getattr(field_map, "field_caps", {})
        or getattr(field_map, "metric_field_caps", {})
        or getattr(field_map, "log_field_caps", {})
    )
    if not args.preflight:
        return None
    result = run_preflight(dashboard, field_map=field_map)
    if args.preflight and not has_capabilities and not result.issues:
        print("    Preflight: no target field capability data available")
        return None
    return result


def _print_preflight_summary(preflight: PreflightResult) -> None:
    block_count = len(preflight.blocking_issues)
    warn_count = len(preflight.warnings)
    info_count = len([issue for issue in preflight.issues if issue.level == "info"])
    status = "pass" if preflight.passed and not preflight.issues else "issues" if preflight.issues else "pass"
    print(f"    Preflight: {status}  Block: {block_count}  Warn: {warn_count}  Info: {info_count}")
    for issue in preflight.issues[:5]:
        prefix = issue.widget_id + ": " if issue.widget_id else ""
        print(f"      - [{issue.level}] {prefix}{issue.message}")
    if len(preflight.issues) > 5:
        print(f"      ... and {len(preflight.issues) - 5} more")


def _preflight_issue_to_dict(issue: Any) -> dict[str, str]:
    return {
        "level": issue.level,
        "category": issue.category,
        "message": issue.message,
        "widget_id": issue.widget_id,
        "field_name": issue.field_name,
    }


def _compile_all_dashboards(
    results: list[DashboardResult],
    output_dir: Path,
    target_adapter: Any,
) -> None:
    """Compile YAML to NDJSON using the shared Kibana target runtime."""
    compiled_dir = output_dir / "compiled"
    compiled_dir.mkdir(parents=True, exist_ok=True)

    for dr in results:
        if not dr.yaml_path:
            continue
        stem = Path(dr.yaml_path).stem
        out_dir = compiled_dir / stem
        out_dir.mkdir(parents=True, exist_ok=True)

        success, output = target_adapter.compile_dashboard(dr.yaml_path, out_dir)
        if success:
            dr.compiled = True
            dr.compiled_path = str(out_dir)
            print(f"    Compiled: {stem}")
            layout_ok, layout_output = validate_compiled_layout(out_dir)
            dr.layout_checked = True
            if layout_ok:
                dr.layout_error = ""
                print(f"    Layout validated: {stem}")
            else:
                dr.layout_error = layout_output[:500]
                print(f"    LAYOUT FAILED: {stem}: {layout_output[:200]}")
        else:
            dr.compile_error = output[:500]
            print(f"    COMPILE FAILED: {stem}: {output[:200]}")


class _DatadogValidationResolver:
    """Minimal resolver adapter for generic ES|QL validation fixes."""

    def __init__(
        self,
        field_map: FieldMapProfile,
        index_pattern: str,
        *,
        es_url: str = "",
        es_api_key: str = "",
        verify: bool | str = True,
    ) -> None:
        self._field_map = field_map
        self._index_pattern = index_pattern or field_map.metric_index
        self._es_url = es_url
        self._es_api_key = es_api_key
        self._verify = verify
        self._concrete_index_cache: list[str] | None = None

    def _context(self) -> str:
        if self._index_pattern == self._field_map.logs_index or self._index_pattern.startswith("logs-"):
            return "log"
        if self._index_pattern == self._field_map.metric_index or self._index_pattern.startswith("metrics-"):
            return "metric"
        return ""

    def _caps(self) -> dict[str, Any]:
        context = self._context()
        if context == "log":
            return cast(dict[str, Any], self._field_map.log_field_caps or self._field_map.field_caps)
        if context == "metric":
            return cast(dict[str, Any], self._field_map.metric_field_caps or self._field_map.field_caps)
        return cast(dict[str, Any], self._field_map.field_caps)

    def _candidate_fields(self, label: str) -> list[str]:
        candidates: list[str] = []
        context = self._context()
        mapped_tag = self._field_map.map_tag(label, context=context)
        if mapped_tag and mapped_tag != label:
            candidates.append(mapped_tag)
        mapped_log = self._field_map.map_log_field(label)
        if mapped_log and mapped_log not in candidates and mapped_log != label:
            candidates.append(mapped_log)
        mapped_metric = self._field_map.metric_map.get(label, "")
        if mapped_metric and mapped_metric not in candidates and mapped_metric != label:
            candidates.append(mapped_metric)
        return candidates

    def resolve_label(self, label: str) -> str:
        for candidate in self._candidate_fields(label):
            exists = self.field_exists(candidate)
            if exists is True or exists is None:
                return candidate
        return label

    def field_exists(self, field_name: str) -> bool | None:
        caps = self._caps()
        if not caps:
            return None
        return field_name in caps

    def _es_headers(self) -> dict[str, str]:
        headers = {}
        if self._es_api_key:
            headers["Authorization"] = f"ApiKey {self._es_api_key}"
        return headers

    def concrete_index_candidates(self) -> list[str]:
        if self._concrete_index_cache is not None:
            return list(self._concrete_index_cache)
        self._concrete_index_cache = []
        if not self._es_url:
            return []
        if not any(token in self._index_pattern for token in ("*", "?", ",")):
            self._concrete_index_cache = [self._index_pattern]
            return list(self._concrete_index_cache)
        try:
            resp = requests.get(
                f"{self._es_url}/_resolve/index/{self._index_pattern}",
                headers=self._es_headers(),
                timeout=10,
                verify=self._verify,
            )
            if resp.status_code != 200:
                return []
            body = resp.json()
            discovered: list[str] = []
            for bucket in ("data_streams", "indices"):
                for entry in body.get(bucket, []) or []:
                    name = entry.get("name")
                    if name and name not in discovered:
                        discovered.append(name)
            self._concrete_index_cache = discovered
        except Exception:
            pass
        return list(self._concrete_index_cache)


def _mark_widget_requires_manual_after_validation(
    result: TranslationResult,
    validation_result: dict[str, Any],
) -> None:
    result.status = "requires_manual"
    result.post_validation_action = "placeholder_empty_result"
    _append_unique(
        result.reasons,
        "Validation only succeeded on an empty fallback data stream",
    )
    _append_unique(
        result.warnings,
        "Validation only succeeded on an empty fallback data stream",
    )
    narrowed_to = (validation_result.get("analysis") or {}).get("narrowed_to_index", "")
    if narrowed_to:
        _append_unique(
            result.warnings,
            f"Fallback validation target `{narrowed_to}` returned zero rows; manual review required.",
        )
        result.post_validation_message = (
            f"Validation only succeeded on empty fallback stream `{narrowed_to}`; manual review required."
        )
    elif not result.post_validation_message:
        result.post_validation_message = "Validation only succeeded on an empty fallback stream; manual review required."
    if "empty fallback data stream during validation" not in result.semantic_losses:
        result.semantic_losses.append("empty fallback data stream during validation")


def _mark_widget_requires_manual_after_failed_validation(
    result: TranslationResult,
    validation_result: dict[str, Any],
) -> None:
    result.status = "requires_manual"
    result.post_validation_action = "placeholder_validation_failed"
    _append_unique(
        result.reasons,
        "Live ES|QL validation failed; uploaded placeholder instead of a broken runtime panel",
    )
    _append_unique(
        result.warnings,
        "Live ES|QL validation failed; uploaded placeholder instead of a broken runtime panel",
    )
    error = ((validation_result or {}).get("error") or "").splitlines()[0].strip()
    if error:
        _append_unique(result.warnings, f"Validation error: {error}")
        result.post_validation_message = f"Validation failed: {error}"
    elif not result.post_validation_message:
        result.post_validation_message = "Live ES|QL validation failed; manual review required."
    if "validation failure placeholder" not in result.semantic_losses:
        result.semantic_losses.append("validation failure placeholder")


def _rewrite_dashboard_yaml(
    dashboard: Any,
    result: DashboardResult,
    field_map: Any,
) -> None:
    if not result.yaml_path:
        return
    yaml_str = generate_dashboard_yaml(
        dashboard,
        result.panel_results,
        data_view=field_map.metric_index,
        metrics_dataset_filter=field_map.metrics_dataset_filter,
        logs_dataset_filter=field_map.logs_dataset_filter,
        logs_index=field_map.logs_index,
        field_map=field_map,
    )
    Path(result.yaml_path).write_text(yaml_str, encoding="utf-8")


def _validate_all_dashboards(
    dashboard_outputs: list[tuple[DashboardResult, Any]],
    field_map: Any,
    args: argparse.Namespace,
    *,
    verify: bool | str = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate emitted Datadog ES|QL queries against Elasticsearch."""
    print("\n  Validating ES|QL queries against Elasticsearch...")
    validation_records: list[dict[str, Any]] = []
    total_queries = 0
    passed = 0
    fixed = 0
    fixed_empty = 0
    failed = 0
    skipped = 0
    resolver_cache: dict[str, _DatadogValidationResolver] = {}

    for result, dashboard in dashboard_outputs:
        per_dashboard_counts: dict[str, int] = {}
        dashboard_changed = False
        for panel_result in result.panel_results:
            if not panel_result.esql_query:
                continue
            total_queries += 1
            _source_cmd, index_pattern = _query_source_and_index(panel_result.esql_query)
            cache_key = index_pattern or field_map.metric_index
            resolver = resolver_cache.get(cache_key)
            if resolver is None:
                resolver = _DatadogValidationResolver(
                    field_map,
                    cache_key,
                    es_url=args.es_url,
                    es_api_key=args.es_api_key or "",
                    verify=verify,
                )
                resolver_cache[cache_key] = resolver
            validation_result = validate_query_with_fixes(
                panel_result.esql_query,
                args.es_url,
                resolver,
                es_api_key=args.es_api_key or None,
                verify=verify,
            )
            status = validation_result["status"]
            per_dashboard_counts[status] = per_dashboard_counts.get(status, 0) + 1
            if status == "pass":
                passed += 1
            elif status == "fixed":
                fixed += 1
                panel_result.esql_query = validation_result["query"]
                dashboard_changed = True
            elif status == "fixed_empty":
                fixed_empty += 1
                panel_result.esql_query = validation_result["query"]
                _mark_widget_requires_manual_after_validation(panel_result, validation_result)
                dashboard_changed = True
            elif status == "fail":
                failed += 1
                _mark_widget_requires_manual_after_failed_validation(panel_result, validation_result)
                dashboard_changed = True
            elif status == "skip":
                skipped += 1

            validation_records.append(
                {
                    "dashboard": result.dashboard_title,
                    "dashboard_id": result.dashboard_id,
                    "widget": panel_result.title,
                    "widget_id": panel_result.widget_id,
                    "status": status,
                    "query": validation_result["query"],
                    "error": validation_result["error"],
                    "fix_attempts": validation_result["fix_attempts"],
                    "analysis": validation_result["analysis"],
                }
            )
        result.validation_summary = per_dashboard_counts
        result.recompute_counts()
        if dashboard_changed:
            _rewrite_dashboard_yaml(dashboard, result, field_map)

    validation_summary = summarize_validation_records(validation_records)
    print(
        f"  Validated {total_queries} queries: "
        f"{passed} passed, {fixed} auto-fixed, {fixed_empty} manualized after empty fallback, "
        f"{failed} failed, {skipped} skipped"
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
    return validation_records, validation_summary


def _handle_list_dashboards(
    args: argparse.Namespace,
    target_adapter: Any,
    *,
    verify: bool | str | None = None,
) -> None:
    if not args.kibana_url:
        print("  ERROR: --kibana-url is required for --list-dashboards")
        sys.exit(2)
    verify = _resolve_tls_from_args(args) if verify is None else verify
    dashboards = target_adapter.list_dashboards(
        args.kibana_url,
        api_key=args.kibana_api_key,
        space_id=args.space_id,
        verify=verify,
    )
    print(f"\n  Found {len(dashboards)} dashboard(s) in Kibana:\n")
    for d in dashboards:
        title = d.get("attributes", {}).get("title", "(untitled)")
        print(f"    {d.get('id', '???'):40s}  {title}")


def _handle_delete_dashboards(
    args: argparse.Namespace,
    target_adapter: Any,
    *,
    verify: bool | str | None = None,
) -> None:
    if not args.kibana_url:
        print("  ERROR: --kibana-url is required for --delete-dashboards")
        sys.exit(2)
    ids = [i.strip() for i in args.delete_dashboards.split(",") if i.strip()]
    if not ids:
        print("  ERROR: provide comma-separated dashboard IDs")
        sys.exit(2)
    verify = _resolve_tls_from_args(args) if verify is None else verify
    result = target_adapter.delete_dashboards(
        args.kibana_url,
        ids,
        api_key=args.kibana_api_key,
        space_id=args.space_id,
        verify=verify,
    )
    print(f"\n  Cleared {len(result['cleared'])} dashboard(s)")
    if result["failed"]:
        for f in result["failed"]:
            print(f"    FAILED: {f['id']}: {f['error'][:200]}")
    print(f"\n  Note: {result['note']}")


def _ensure_data_views(
    args: argparse.Namespace,
    target_adapter: Any,
    field_map: Any,
    *,
    verify: bool | str | None = None,
) -> None:
    patterns: list[str] = []
    if field_map.metric_index:
        patterns.append(field_map.metric_index)
    if field_map.logs_index:
        patterns.append(field_map.logs_index)
    if not patterns:
        patterns = ["metrics-*"]
    print(f"\n  Ensuring data views: {', '.join(patterns)}")
    verify = _resolve_tls_from_args(args) if verify is None else verify
    try:
        created = target_adapter.ensure_data_views(
            args.kibana_url,
            data_view_patterns=patterns,
            api_key=args.kibana_api_key,
            space_id=args.space_id,
            verify=verify,
        )
        for dv in created:
            print(f"    OK: {dv.get('title', '???')} (id={dv.get('id', '???')})")
    except Exception as exc:
        print(f"    WARNING: data view creation failed: {exc}")


def _upload_all_dashboards(
    results: list[DashboardResult],
    output_dir: Path,
    args: argparse.Namespace,
    target_adapter: Any,
    *,
    verify: bool | str | None = None,
) -> None:
    """Upload compiled dashboards to Kibana via the shared target runtime."""
    from observability_migration.targets.kibana.compile import (
        detect_space_id_from_kibana_url,
        kibana_url_for_space,
    )

    compiled_dir = output_dir / "compiled"
    compiled_dir.mkdir(parents=True, exist_ok=True)
    verify = _resolve_tls_from_args(args) if verify is None else verify
    target_space = detect_space_id_from_kibana_url(args.kibana_url) or "default"
    upload_space = args.space_id or ""
    upload_kibana_url = kibana_url_for_space(args.kibana_url, upload_space)

    print(f"\n  Uploading dashboards to {upload_kibana_url}")
    for dr in results:
        dr.upload_attempted = True
        stem = Path(dr.yaml_path).stem if dr.yaml_path else (dr.dashboard_title or dr.dashboard_id or "untitled")

        if not dr.yaml_path:
            dr.uploaded = False
            dr.upload_error = "Upload skipped because no YAML artifact was generated."
            print(f"    UPLOAD SKIPPED: {stem}: {dr.upload_error}")
            continue
        if not dr.compiled:
            dr.uploaded = False
            dr.upload_error = (
                "Upload skipped because compile failed."
                if dr.compile_error
                else "Upload skipped because compile did not run."
            )
            print(f"    UPLOAD SKIPPED: {stem}: {dr.upload_error}")
            continue
        if dr.layout_error:
            dr.uploaded = False
            dr.upload_error = f"Upload skipped because compiled layout validation failed: {dr.layout_error}"
            print(f"    UPLOAD SKIPPED: {stem}: {dr.upload_error[:200]}")
            continue

        out_dir = compiled_dir / Path(dr.yaml_path).stem
        out_dir.mkdir(parents=True, exist_ok=True)
        upload_result = target_adapter.upload_dashboard(
            dr.yaml_path,
            out_dir,
            kibana_url=args.kibana_url,
            space_id=upload_space,
            kibana_api_key=args.kibana_api_key,
            verify=verify,
        )
        dr.uploaded = upload_result["success"]
        dr.upload_error = "" if upload_result["success"] else upload_result["output"][:500]
        dr.uploaded_space = upload_space or target_space
        dr.uploaded_kibana_url = upload_result["kibana_url"]
        if upload_result["success"]:
            print(f"    Uploaded: {stem}")
        else:
            print(f"    UPLOAD FAILED: {stem}: {dr.upload_error[:200]}")


def _build_smoke_dashboard_indexes(
    smoke_payload: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_id: dict[str, dict[str, Any]] = {}
    by_title: dict[str, dict[str, Any]] = {}
    for dashboard in smoke_payload.get("dashboards", []):
        dashboard_id = str(dashboard.get("id", "") or "")
        dashboard_title = str(dashboard.get("title", "") or "")
        if dashboard_id and dashboard_id not in by_id:
            by_id[dashboard_id] = dashboard
        if dashboard_title and dashboard_title not in by_title:
            by_title[dashboard_title] = dashboard
    return by_id, by_title


def _apply_smoke_dashboard_state(
    result: DashboardResult,
    smoke_dashboard: dict[str, Any] | None,
    *,
    output_path: Path,
    browser_requested: bool,
) -> None:
    result.smoke_attempted = True
    result.smoke_report_path = str(output_path)
    result.browser_audit_attempted = browser_requested
    if not smoke_dashboard:
        result.smoke_status = "not_run"
        result.smoke_error = "Uploaded dashboard was not found in the smoke report."
        if browser_requested:
            result.browser_audit_status = "not_run"
            result.browser_audit_error = ""
        return

    result.kibana_saved_object_id = str(smoke_dashboard.get("id", "") or "")
    layout = smoke_dashboard.get("layout", {}) or {}
    overlaps = layout.get("overlaps", []) or []
    invalid_sizes = layout.get("invalid_sizes", []) or []
    out_of_bounds = layout.get("out_of_bounds", []) or []
    result.layout_checked = True
    if overlaps or invalid_sizes or out_of_bounds:
        result.layout_error = (
            f"{len(overlaps)} overlap(s), {len(invalid_sizes)} invalid size(s), "
            f"{len(out_of_bounds)} out-of-bounds panel(s)"
        )
    else:
        result.layout_error = ""

    status = str(smoke_dashboard.get("status", "") or "")
    if status == "clean":
        result.smoke_status = "pass"
        result.smoke_error = ""
    elif status in {"has_runtime_errors", "has_layout_issues"}:
        result.smoke_status = "fail"
        if status == "has_runtime_errors":
            result.smoke_error = f"{len(smoke_dashboard.get('failing_panels', []) or [])} panel runtime error(s)"
        else:
            result.smoke_error = result.layout_error or "Uploaded dashboard has layout issues."
    elif status == "has_empty_panels":
        result.smoke_status = "empty_result"
        result.smoke_error = f"{len(smoke_dashboard.get('empty_panels', []) or [])} empty panel(s)"
    elif status == "has_runtime_gaps":
        result.smoke_status = "not_runtime_checked"
        total_gaps = len(smoke_dashboard.get("not_runtime_checked_panels", []) or [])
        lens_by_design = len(smoke_dashboard.get("lens_by_design_panels", []) or [])
        unexpected_gaps = len(smoke_dashboard.get("unexpected_runtime_gap_panels", []) or [])
        if not lens_by_design and not unexpected_gaps:
            for panel in smoke_dashboard.get("not_runtime_checked_panels", []) or []:
                if panel.get("coverage_reason") == "lens_by_design":
                    lens_by_design += 1
                else:
                    unexpected_gaps += 1
        if lens_by_design and not unexpected_gaps:
            result.smoke_error = f"{lens_by_design} panel(s) still Lens by design"
        elif unexpected_gaps and not lens_by_design:
            result.smoke_error = f"{unexpected_gaps} unexpected runtime coverage gap panel(s)"
        else:
            result.smoke_error = (
                f"{total_gaps} panel(s) not runtime-checked "
                f"({lens_by_design} still Lens by design, {unexpected_gaps} unexpected gap(s))"
            )
    else:
        result.smoke_status = "not_run"
        result.smoke_error = ""

    browser_info = smoke_dashboard.get("browser_audit", {}) or {}
    browser_status = str(browser_info.get("status", "") or "not_requested")
    if browser_requested:
        if browser_status == "clean":
            result.browser_audit_status = "pass"
            result.browser_audit_error = ""
        elif browser_status in {"error", "failed"}:
            result.browser_audit_status = "fail"
            result.browser_audit_error = "; ".join(browser_info.get("issues", []) or []) or str(browser_info.get("error", "") or "")
        else:
            result.browser_audit_status = "not_run"
            result.browser_audit_error = str(browser_info.get("error", "") or "")


def _smoke_uploaded_dashboards(
    results: list[DashboardResult],
    output_dir: Path,
    args: argparse.Namespace,
    target_adapter: TargetAdapter,
    *,
    verify: bool | str | None = None,
) -> dict[str, Any]:
    uploaded_results = [result for result in results if result.uploaded]
    if not uploaded_results:
        print("\n  Smoke validation skipped: no dashboards uploaded successfully")
        return {}

    smoke_output = Path(args.smoke_output) if args.smoke_output else output_dir / "uploaded_dashboard_smoke_report.json"
    dashboard_titles = [result.dashboard_title for result in uploaded_results if result.dashboard_title]
    verify = _resolve_tls_from_args(args) if verify is None else verify

    print(f"\n  Smoke validating uploaded dashboards ({len(uploaded_results)})...")
    try:
        smoke_payload = target_adapter.smoke(
            kibana_url=args.kibana_url,
            es_url=args.es_url,
            kibana_api_key=args.kibana_api_key,
            es_api_key=args.es_api_key,
            space_id=args.space_id,
            output_path=smoke_output,
            screenshot_dir=str(output_dir / "dashboard_qa"),
            browser_audit_dir=str(output_dir / "browser_qa"),
            dashboard_titles=dashboard_titles,
            timeout=args.smoke_timeout,
            browser_audit=args.browser_audit,
            capture_screenshots=args.capture_screenshots,
            chrome_binary=args.chrome_binary,
            verify=verify,
        )
    except Exception as exc:
        message = str(exc)
        print(f"    SMOKE FAILED: {message}")
        for result in uploaded_results:
            result.smoke_attempted = True
            result.smoke_status = "fail"
            result.smoke_error = message
            result.smoke_report_path = str(smoke_output)
            result.browser_audit_attempted = args.browser_audit
            for panel_result in result.panel_results:
                _append_unique(panel_result.runtime_rollups, "smoke_failed")
            if args.browser_audit:
                result.browser_audit_status = "fail"
                result.browser_audit_error = message
                for panel_result in result.panel_results:
                    _append_unique(panel_result.runtime_rollups, "browser_failed")
        return {}

    merge_summary = merge_smoke_into_results(uploaded_results, smoke_payload)
    smoke_by_id, smoke_by_title = _build_smoke_dashboard_indexes(smoke_payload)
    for result in uploaded_results:
        smoke_dashboard = None
        if result.kibana_saved_object_id:
            smoke_dashboard = smoke_by_id.get(result.kibana_saved_object_id)
        if smoke_dashboard is None:
            smoke_dashboard = smoke_by_title.get(result.dashboard_title)
        if smoke_dashboard is None:
            for panel_result in result.panel_results:
                _append_unique(panel_result.runtime_rollups, "not_runtime_checked")
        _apply_smoke_dashboard_state(
            result,
            smoke_dashboard,
            output_path=smoke_output,
            browser_requested=args.browser_audit,
        )

    summary = smoke_payload.get("summary", {}) or {}
    lens_by_design = summary.get("lens_by_design_panels", 0)
    unexpected_runtime_gaps = summary.get("unexpected_runtime_gap_panels", 0)
    runtime_gap_suffix = ""
    if summary.get("not_runtime_checked_panels", 0):
        runtime_gap_suffix = (
            f" ({lens_by_design} still Lens by design, "
            f"{unexpected_runtime_gaps} unexpected gap(s))"
        )
    print(
        "    Smoke summary: "
        f"{summary.get('runtime_error_panels', 0)} runtime error panel(s), "
        f"{summary.get('empty_panels', 0)} empty panel(s), "
        f"{summary.get('not_runtime_checked_panels', 0)} not runtime-checked panel(s)"
        f"{runtime_gap_suffix}, "
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
    return cast(dict[str, Any], smoke_payload)


def _safe_filename(title: str) -> str:
    """Convert a dashboard title to a safe filename."""
    import re
    name = re.sub(r"[^\w\s-]", "", title).strip().lower()
    name = re.sub(r"[\s]+", "_", name)
    return name[:80] or "untitled"


def _allocate_yaml_stem(
    title: str,
    dashboard_id: str | None,
    used_stems: set[str],
) -> str:
    """Allocate a unique YAML stem to avoid filename collisions."""
    base = _safe_filename(title)
    if base not in used_stems:
        used_stems.add(base)
        return base

    raw_dashboard_id = str(dashboard_id or "").strip()
    if raw_dashboard_id:
        id_suffix = _safe_filename(raw_dashboard_id)
        id_candidate = f"{base}_{id_suffix[:24]}"
        if id_candidate not in used_stems:
            used_stems.add(id_candidate)
            return id_candidate

    index = 2
    while True:
        candidate = f"{base}_{index}"
        if candidate not in used_stems:
            used_stems.add(candidate)
            return candidate
        index += 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate Datadog dashboards to Kibana",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--source",
        dest="source",
        choices=["files", "api"],
        default=None,
        help="Input mode alias: 'files' (JSON exports) or 'api' (Datadog API). Prefer --input-mode.",
    )
    parser.add_argument(
        "--input-mode",
        dest="input_mode",
        choices=["files", "api"],
        default=None,
        help="Input mode: 'files' (JSON exports) or 'api' (Datadog API).",
    )
    parser.add_argument(
        "--input-dir", default="infra/datadog/dashboards",
        help="Directory with exported Datadog dashboard JSON files",
    )
    parser.add_argument(
        "--output-dir", default="datadog_migration_output",
        help="Output directory for generated YAML and reports",
    )
    parser.add_argument(
        "--assets",
        choices=ASSET_CHOICES,
        default="dashboards",
        help="Asset family to migrate: dashboards only, alerts only, or both",
    )
    parser.add_argument(
        "--field-profile", default="otel",
        help="Field mapping profile: otel, elastic_agent, prometheus, passthrough, or path to YAML",
    )
    parser.add_argument(
        "--data-view",
        default=None,
        help="Elasticsearch index pattern for metrics data (defaults from --field-profile)",
    )
    parser.add_argument(
        "--logs-index", default="",
        help="Elasticsearch index pattern for logs (defaults from profile)",
    )
    parser.add_argument(
        "--es-url",
        default=os.getenv("ELASTICSEARCH_ENDPOINT", os.getenv("ES_URL", "")),
        help="Elasticsearch URL for live target field discovery",
    )
    parser.add_argument(
        "--es-api-key",
        default=os.getenv("ES_API_KEY", os.getenv("KEY", "")),
        help="API key for Elasticsearch (defaults to ES_API_KEY or KEY env var)",
    )
    parser.add_argument(
        "--env-file", default="datadog_creds.env",
        help="Path to Datadog credentials .env file",
    )
    parser.add_argument(
        "--dashboard-ids", default="",
        help="Comma-separated Datadog dashboard IDs to fetch (API mode)",
    )
    parser.add_argument(
        "--compile",
        dest="compile",
        action="store_true",
        default=True,
        help="Compile YAML to NDJSON using kb-dashboard-cli (default)",
    )
    parser.add_argument(
        "--no-compile",
        dest="compile",
        action="store_false",
        help="Skip dashboard YAML compilation unless upload is requested",
    )
    parser.add_argument(
        "--validate", action="store_true",
        help="Validate emitted ES|QL against Elasticsearch before compile/upload",
    )
    parser.add_argument(
        "--source-execution", action="store_true",
        help=(
            "Execute each panel's source query against the live Datadog API "
            "to build source/target comparison verification packets. Requires "
            "DD_API_KEY/DD_APP_KEY (env or --env-file). Off by default: "
            "translation stays fully offline and never calls the Datadog API."
        ),
    )
    parser.add_argument(
        "--upload", action="store_true",
        help="Upload dashboards to Kibana after a successful compile pass",
    )
    parser.add_argument(
        "--smoke", action="store_true",
        help="Run post-upload smoke validation against the uploaded Kibana dashboards",
    )
    parser.add_argument(
        "--preflight", action="store_true",
        help="Run Datadog preflight checks before translation",
    )
    parser.add_argument(
        "--dataset-filter", default="",
        help="Explicit data_stream.dataset value for metrics dashboard filter "
             "(auto-derived from --data-view when possible)",
    )
    parser.add_argument(
        "--logs-dataset-filter", default="",
        help="Explicit data_stream.dataset value for logs dashboard filter "
             "(auto-derived from --logs-index when possible)",
    )
    parser.add_argument(
        "--kibana-url",
        default=os.getenv("KIBANA_ENDPOINT", os.getenv("KIBANA_URL", "")),
        help="Kibana URL for upload",
    )
    parser.add_argument(
        "--kibana-api-key",
        default=os.getenv("KIBANA_API_KEY", os.getenv("KEY", "")),
        help="API key for Kibana upload (defaults to KIBANA_API_KEY or KEY env var)",
    )
    parser.add_argument(
        "--space-id", default="",
        help="Optional Kibana space ID for upload",
    )
    parser.add_argument(
        "--smoke-output", default="",
        help="Optional path for the post-upload smoke report JSON",
    )
    parser.add_argument(
        "--smoke-timeout", type=int, default=30,
        help="Timeout in seconds for Kibana/Elasticsearch smoke requests",
    )
    parser.add_argument(
        "--browser-audit", action="store_true",
        help="During smoke validation, scan uploaded dashboards for visible browser-side runtime errors",
    )
    parser.add_argument(
        "--capture-screenshots", action="store_true",
        help="During smoke validation, capture dashboard screenshots",
    )
    parser.add_argument(
        "--chrome-binary", default=os.getenv("CHROME_BINARY", ""),
        help="Optional Chrome/Chromium binary path for browser audit or screenshots",
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
        "--fetch-monitors", action="store_true",
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
            "--fetch-monitors alias). Rules are created disabled by default "
            "and tagged 'obs-migration'. Requires alert-capable asset "
            "selection, --kibana-url, and --kibana-api-key."
        ),
    )
    parser.add_argument(
        "--monitor-ids", default="",
        help="Comma-separated Datadog monitor IDs to extract",
    )
    parser.add_argument(
        "--monitor-query", default="",
        help="Datadog monitor search query for filtered discovery",
    )
    parser.add_argument(
        "--ca-cert", default=os.getenv("OBS_MIGRATE_CA_CERT", ""),
        help=(
            "Path to a custom CA certificate (bundle) used to verify TLS for all "
            "outbound connections (Datadog, Elasticsearch, Kibana). "
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


if __name__ == "__main__":
    main()
