# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from observability_migration.core.assets.alerting import (
    build_alerting_ir_from_grafana,
    build_alerting_ir_from_grafana_unified,
)
from observability_migration.core.http import resolve_tls
from observability_migration.core.mapping import map_alerts_batch
from observability_migration.core.selection import (
    apply_cli_selection,
    criteria_from_args,
)
from observability_migration.targets.kibana.alerting import (
    create_rules_from_payloads,
    run_alerting_preflight,
    validate_rule_payload,
)

from .alerts import (
    build_alert_comparison_results,
    build_alert_migration_results,
    build_alert_migration_tasks,
    extract_alerts_from_dashboard,
)
from .extract import (
    extract_all_alerting_resources,
    extract_all_alerting_resources_from_files,
    filter_unified_alert_rules,
    selection_metadata_from_grafana_alert_rule,
    selection_metadata_from_grafana_dashboard,
)


def _selected_space_id(args) -> str:
    return getattr(args, "shadow_space", "") or getattr(args, "space_id", "") or ""


def _verify_from_args(args) -> bool | str:
    return resolve_tls(
        ca_cert=getattr(args, "ca_cert", "") or "",
        insecure=bool(getattr(args, "insecure", False)),
    )


def build_legacy_alert_tasks_from_dashboards(
    raw_dashboards: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for dashboard in raw_dashboards:
        tasks.extend(build_alert_migration_tasks(extract_alerts_from_dashboard(dashboard)))
    return tasks


def build_legacy_alert_irs_from_dashboards(
    raw_dashboards: list[dict[str, Any]],
) -> list[Any]:
    return [
        build_alerting_ir_from_grafana(task)
        for task in build_legacy_alert_tasks_from_dashboards(raw_dashboards)
    ]


def _alert_selection_filters(args) -> tuple[list[str] | None, list[str] | None]:
    """Parse --alert-uids and --alert-folder args into filter lists."""
    raw_uids = getattr(args, "alert_uids", "") or ""
    uids = [u.strip() for u in raw_uids.split(",") if u.strip()] if raw_uids else None
    raw_folders = getattr(args, "alert_folder", "") or ""
    folders = [f.strip() for f in raw_folders.split(",") if f.strip()] if raw_folders else None
    return uids, folders


def load_unified_alerting_resources(args) -> dict[str, Any]:
    grafana_token = getattr(args, "grafana_token", "") or os.getenv("GRAFANA_TOKEN", "")
    if getattr(args, "source", "files") == "api":
        grafana_url = getattr(args, "grafana_url", "") or os.getenv("GRAFANA_URL", "http://localhost:3000")
        grafana_user = getattr(args, "grafana_user", "") or os.getenv("GRAFANA_USER", "admin")
        grafana_password = getattr(args, "grafana_pass", "") or os.getenv("GRAFANA_PASS", "admin")
        resources = extract_all_alerting_resources(
            grafana_url,
            user=grafana_user,
            password=grafana_password,
            token=grafana_token,
            verify=_verify_from_args(args),
        )
    else:
        resources = extract_all_alerting_resources_from_files(getattr(args, "input_dir", ""))

    uids, folders = _alert_selection_filters(args)
    if uids is not None or folders is not None:
        original_count = len(resources.get("alert_rules", []) or [])
        resources = dict(resources)
        resources["alert_rules"] = filter_unified_alert_rules(
            resources.get("alert_rules") or [],
            uids=uids,
            folder_uids=folders,
        )
        filtered_count = len(resources["alert_rules"])
        if original_count != filtered_count:
            print(f"    Alert selection: {filtered_count} of {original_count} rules selected")
    return resources


def build_unified_alert_irs(unified_resources: dict[str, Any]) -> list[Any]:
    unified_rules = unified_resources.get("alert_rules", [])
    if not isinstance(unified_rules, list):
        return []
    datasource_map = unified_resources.get("datasources", {})
    if not isinstance(datasource_map, dict):
        datasource_map = {}
    return [
        build_alerting_ir_from_grafana_unified(rule, datasource_map=datasource_map)
        for rule in unified_rules
        if isinstance(rule, dict)
    ]


def build_payload_validation_lookup(
    args,
    mapping_batch: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    payload_validation_by_alert_id: dict[str, Any] = {}
    payload_preflight: dict[str, Any] | None = None
    if not getattr(args, "preflight", False):
        return payload_validation_by_alert_id, payload_preflight
    if not getattr(args, "kibana_url", ""):
        return payload_validation_by_alert_id, payload_preflight

    payload_preflight = run_alerting_preflight(
        args.kibana_url,
        api_key=getattr(args, "kibana_api_key", "") or "",
        space_id=_selected_space_id(args),
        verify=_verify_from_args(args),
    )
    for item in mapping_batch.get("results", []):
        payload = item.get("mapping", {}).get("rule_payload", {})
        if not payload:
            continue
        payload_validation_by_alert_id[str(item.get("alert_id", "") or "")] = validate_rule_payload(
            payload.get("rule_type_id", ""),
            payload.get("params", {}),
            payload_preflight,
        )
    return payload_validation_by_alert_id, payload_preflight


def write_alert_artifacts(
    *,
    output_dir: Path,
    raw_dashboards: list[dict[str, Any]],
    unified_resources: dict[str, Any],
    raw_alert_inputs: list[dict[str, Any]],
    alert_irs: list[Any],
    mapping_batch: dict[str, Any],
    payload_validation_by_alert_id: dict[str, Any] | None = None,
    total_legacy: int = 0,
    total_unified: int = 0,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw_alerts"

    has_unified_resources = any(
        unified_resources.get(key)
        for key in (
            "alert_rules",
            "contact_points",
            "notification_policies",
            "mute_timings",
            "templates",
            "datasources",
        )
    )
    if raw_dashboards or has_unified_resources:
        raw_dir.mkdir(parents=True, exist_ok=True)
        if raw_dashboards:
            raw_dashboards_path = raw_dir / "grafana_dashboards.json"
            with raw_dashboards_path.open("w", encoding="utf-8") as fh:
                json.dump(raw_dashboards, fh, indent=2)
            print(f"    Raw dashboard alerts saved: {raw_dashboards_path}")
        if has_unified_resources:
            for key, data in unified_resources.items():
                path = raw_dir / f"grafana_{key}.json"
                with path.open("w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2)
            unified_rules = unified_resources.get("alert_rules", [])
            contact_points = unified_resources.get("contact_points", [])
            mute_timings = unified_resources.get("mute_timings", [])
            print(f"    Raw alert artifacts saved to: {raw_dir}")
            print(f"    Unified alerting rules: {len(unified_rules) if isinstance(unified_rules, list) else 0}")
            print(f"    Contact points: {len(contact_points) if isinstance(contact_points, list) else 0}")
            print(
                "    Notification policies: "
                f"{'present' if unified_resources.get('notification_policies') else 'none'}"
            )
            print(f"    Mute timings: {len(mute_timings) if isinstance(mute_timings, list) else 0}")

    alert_results_path = output_dir / "alert_migration_results.json"
    with alert_results_path.open("w", encoding="utf-8") as fh:
        json.dump(
            build_alert_migration_results(
                alert_irs,
                total_alerts=len(alert_irs),
                total_legacy=total_legacy,
                total_unified=total_unified,
            ),
            fh,
            indent=2,
        )
    print(f"    Alert migration results: {alert_results_path}")

    alert_comparison_path = output_dir / "alert_comparison_results.json"
    with alert_comparison_path.open("w", encoding="utf-8") as fh:
        json.dump(
            build_alert_comparison_results(
                raw_alert_inputs,
                alert_irs,
                mapping_batch,
                payload_validation_by_alert_id=payload_validation_by_alert_id,
            ),
            fh,
            indent=2,
        )
    print(f"    Alert comparison results: {alert_comparison_path}")


def create_rules_if_requested(
    *,
    args,
    output_dir: Path,
    mapping_batch: dict[str, Any],
    payload_preflight: dict[str, Any] | None,
) -> None:
    if not getattr(args, "create_alert_rules", False):
        return
    if not getattr(args, "kibana_url", ""):
        print("    WARNING: --create-alert-rules ignored (requires --kibana-url)")
        return
    if not getattr(args, "kibana_api_key", ""):
        print("    WARNING: --create-alert-rules ignored (requires --kibana-api-key)")
        return

    print("\n  Creating Kibana alerting rules (disabled by default)...")
    rule_upload = create_rules_from_payloads(
        args.kibana_url,
        mapping_batch.get("results", []),
        api_key=getattr(args, "kibana_api_key", "") or "",
        space_id=_selected_space_id(args),
        preflight=payload_preflight,
        enabled=False,
        verify=_verify_from_args(args),
    )
    rule_upload_path = output_dir / "alert_rule_upload_results.json"
    with rule_upload_path.open("w", encoding="utf-8") as fh:
        json.dump(rule_upload, fh, indent=2)
    print(f"    Alert rule upload results: {rule_upload_path}")
    print(
        "    Created: {created}  Failed: {failed}  Skipped: {skipped}".format(
            **rule_upload["summary"],
        ),
    )
    if rule_upload.get("preflight_unreachable"):
        print("    WARNING: alerting preflight unreachable; no rules were created")
    if rule_upload["failed"]:
        for failure in rule_upload["failed"][:5]:
            print(
                f"      FAILED: {failure['name']} "
                f"({failure['rule_type_id']}): {failure['error'][:200]}"
            )


def run_alert_pipeline(
    args,
    *,
    output_dir: Path,
    raw_dashboards: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dashboards = list(raw_dashboards or [])

    try:
        criteria = criteria_from_args(args)
    except ValueError as exc:
        print(f"    ERROR: invalid --select-updated-* value: {exc}", file=sys.stderr)
        sys.exit(1)

    raw_dashboards = apply_cli_selection(
        raw_dashboards,
        selection_metadata_from_grafana_dashboard,
        criteria,
        label="grafana dashboard (alerts)",
        kind="dashboard(s) for alerts",
    )

    legacy_alert_tasks = build_legacy_alert_tasks_from_dashboards(raw_dashboards)
    legacy_alert_irs = [
        build_alerting_ir_from_grafana(task)
        for task in legacy_alert_tasks
    ]
    unified_resources = load_unified_alerting_resources(args)
    unified_rules = unified_resources.get("alert_rules", [])
    if isinstance(unified_rules, list) and not criteria.is_empty:
        datasource_map = unified_resources.get("datasources", {})
        if not isinstance(datasource_map, dict):
            datasource_map = {}
        filtered_rules = apply_cli_selection(
            unified_rules,
            lambda rule: selection_metadata_from_grafana_alert_rule(rule, datasource_map),
            criteria,
            label="grafana alert rule",
            kind="alert rule(s)",
        )
        unified_resources = {**unified_resources, "alert_rules": filtered_rules}
    unified_alert_irs = build_unified_alert_irs(unified_resources)
    all_alert_irs = legacy_alert_irs + unified_alert_irs

    mapping_batch = map_alerts_batch(
        all_alert_irs,
        data_view=getattr(args, "data_view", "metrics-*"),
    )
    payload_validation_by_alert_id, payload_preflight = build_payload_validation_lookup(
        args,
        mapping_batch,
    )

    unified_rules = unified_resources.get("alert_rules", [])
    if not isinstance(unified_rules, list):
        unified_rules = []
    raw_alert_inputs = list(legacy_alert_tasks) + [
        rule for rule in unified_rules if isinstance(rule, dict)
    ]

    write_alert_artifacts(
        output_dir=output_dir,
        raw_dashboards=raw_dashboards,
        unified_resources=unified_resources,
        raw_alert_inputs=raw_alert_inputs,
        alert_irs=all_alert_irs,
        mapping_batch=mapping_batch,
        payload_validation_by_alert_id=payload_validation_by_alert_id,
        total_legacy=len(legacy_alert_irs),
        total_unified=len(unified_alert_irs),
    )
    create_rules_if_requested(
        args=args,
        output_dir=output_dir,
        mapping_batch=mapping_batch,
        payload_preflight=payload_preflight,
    )

    by_tier = dict(mapping_batch.get("summary", {}).get("by_automation_tier", {}) or {})
    by_kind: dict[str, int] = {}
    for ir in all_alert_irs:
        by_kind[ir.kind] = by_kind.get(ir.kind, 0) + 1

    print(f"    Total: {len(all_alert_irs)} (legacy={len(legacy_alert_irs)}, unified={len(unified_alert_irs)})")
    print(f"    By tier: {by_tier}")
    if by_kind:
        print(f"    By kind: {by_kind}")
    return {
        "total": len(all_alert_irs),
        "artifacts_dir": str(output_dir),
        "by_automation_tier": by_tier,
        "by_kind": by_kind,
    }


__all__ = [
    "build_legacy_alert_irs_from_dashboards",
    "build_legacy_alert_tasks_from_dashboards",
    "build_unified_alert_irs",
    "create_rules_if_requested",
    "load_unified_alerting_resources",
    "run_alert_pipeline",
    "write_alert_artifacts",
]
