# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from observability_migration.core.assets.alerting import build_alerting_ir_from_datadog
from observability_migration.core.http import resolve_tls
from observability_migration.core.mapping import AUTOMATED_TIER, map_alerts_batch
from observability_migration.core.selection import (
    apply_cli_selection,
    criteria_from_args,
)
from observability_migration.targets.kibana.alerting import (
    RULE_CREATION_NOT_REQUESTED,
    create_rules_from_payloads,
    rule_creation_attempted,
    rule_creation_capability_gap,
    rule_creation_not_requested,
    run_alerting_preflight,
    validate_rule_payload,
)

from .extract import (
    extract_monitors_from_api,
    extract_monitors_from_files,
    selection_metadata_from_datadog_monitor,
)
from .report import build_monitor_comparison_results, build_monitor_migration_results
from .verification import build_monitor_verification_lookup, validate_monitor_queries


def _verify_from_args(args) -> bool | str:
    return resolve_tls(
        ca_cert=getattr(args, "ca_cert", "") or "",
        insecure=bool(getattr(args, "insecure", False)),
    )


def run_alert_pipeline(
    args,
    *,
    field_map,
    output_dir: Path,
    dd_creds: dict[str, str],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_monitors = load_raw_monitors(args, dd_creds)
    try:
        criteria = criteria_from_args(args)
    except ValueError as exc:
        print(f"    ERROR: invalid --select-updated-* value: {exc}", file=sys.stderr)
        sys.exit(1)
    raw_monitors = apply_cli_selection(
        raw_monitors,
        selection_metadata_from_datadog_monitor,
        criteria,
        label="datadog monitor",
        kind="monitor(s)",
    )
    if not raw_monitors:
        print("    No monitors found")
        return {
            "total": 0,
            "artifacts_dir": str(output_dir),
            "by_automation_tier": {},
            "by_kind": {},
        }

    monitor_irs = [
        build_alerting_ir_from_datadog(monitor, field_map=field_map)
        for monitor in raw_monitors
    ]
    mapping_batch = map_alerts_batch(
        monitor_irs,
        data_view=getattr(args, "data_view", "metrics-*"),
    )
    monitor_validation_records = validate_monitor_queries(
        monitor_irs,
        es_url=args.es_url if getattr(args, "validate", False) else "",
        es_api_key=getattr(args, "es_api_key", "") or "",
        verify=_verify_from_args(args),
    )
    monitor_verification_lookup = build_monitor_verification_lookup(
        monitor_irs,
        monitor_validation_records,
    )
    payload_validation_by_alert_id, payload_preflight = build_payload_validation_lookup(
        args,
        mapping_batch,
    )
    write_monitor_artifacts(
        output_dir=output_dir,
        raw_monitors=raw_monitors,
        monitor_irs=monitor_irs,
        mapping_batch=mapping_batch,
        monitor_validation_records=monitor_validation_records,
        monitor_verification_lookup=monitor_verification_lookup,
        payload_validation_by_alert_id=payload_validation_by_alert_id,
    )
    rule_creation = create_rules_if_requested(
        args=args,
        output_dir=output_dir,
        mapping_batch=mapping_batch,
        payload_preflight=payload_preflight,
    )

    by_tier = dict(mapping_batch.get("summary", {}).get("by_automation_tier", {}) or {})
    by_kind: dict[str, int] = {}
    for ir in monitor_irs:
        by_kind[ir.kind] = by_kind.get(ir.kind, 0) + 1

    print(f"    Total: {len(monitor_irs)}")
    print(f"    By tier: {by_tier}")
    print(f"    By kind: {by_kind}")
    summary: dict[str, Any] = {
        "total": len(raw_monitors),
        "artifacts_dir": str(output_dir),
        "by_automation_tier": by_tier,
        "by_kind": by_kind,
    }
    # Only present when rules were actually asked for, so a run that never used
    # --create-alert-rules keeps the exact summary shape it had before.
    if rule_creation["status"] != RULE_CREATION_NOT_REQUESTED:
        summary["rule_creation"] = rule_creation
    return summary


def load_raw_monitors(args, dd_creds: dict[str, str]) -> list[dict[str, Any]]:
    raw_monitors: list[dict[str, Any]] = []
    if args.source == "api":
        if not dd_creds.get("api_key") or not dd_creds.get("app_key"):
            print("    WARNING: DD_API_KEY and DD_APP_KEY required for monitor extraction")
            return raw_monitors
        monitor_ids = [m.strip() for m in args.monitor_ids.split(",") if m.strip()] if args.monitor_ids else None
        monitor_query = getattr(args, "monitor_query", "") or ""
        return extract_monitors_from_api(
            api_key=dd_creds["api_key"],
            app_key=dd_creds["app_key"],
            site=dd_creds.get("site", "datadoghq.com"),
            monitor_ids=monitor_ids,
            monitor_query=monitor_query,
            verify=_verify_from_args(args),
        )

    monitor_dir = Path(args.input_dir) / "monitors"
    if monitor_dir.is_dir():
        return extract_monitors_from_files(str(monitor_dir))

    root = Path(args.input_dir)
    if any(root.glob("monitor*.json")):
        return extract_monitors_from_files(str(root))

    print(f"    No monitors directory at {monitor_dir}")
    return raw_monitors


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
        space_id=getattr(args, "space_id", "") or "",
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


def write_monitor_artifacts(
    *,
    output_dir: Path,
    raw_monitors: list[dict[str, Any]],
    monitor_irs: list[Any],
    mapping_batch: dict[str, Any],
    monitor_validation_records: list[dict[str, Any]],
    monitor_verification_lookup: dict[str, Any],
    payload_validation_by_alert_id: dict[str, Any] | None = None,
) -> None:
    raw_dir = output_dir / "raw_monitors"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / "datadog_monitors.json"
    with raw_path.open("w", encoding="utf-8") as fh:
        json.dump(raw_monitors, fh, indent=2)
    print(f"    Raw monitors saved: {raw_path} ({len(raw_monitors)} monitors)")

    monitor_results_path = output_dir / "monitor_migration_results.json"
    with monitor_results_path.open("w", encoding="utf-8") as fh:
        json.dump(build_monitor_migration_results(monitor_irs), fh, indent=2)
    print(f"    Monitor migration results: {monitor_results_path}")

    monitor_comparison = build_monitor_comparison_results(
        raw_monitors,
        monitor_irs,
        mapping_batch,
        payload_validation_by_alert_id=payload_validation_by_alert_id,
        verification_by_alert_id=monitor_verification_lookup,
    )
    monitor_comparison_path = output_dir / "monitor_comparison_results.json"
    with monitor_comparison_path.open("w", encoding="utf-8") as fh:
        json.dump(monitor_comparison, fh, indent=2)
    print(f"    Monitor comparison results: {monitor_comparison_path}")

    monitor_validation_summary: dict[str, int] = {}
    for record in monitor_validation_records:
        status = str(record.get("status", "not_run") or "not_run")
        monitor_validation_summary[status] = monitor_validation_summary.get(status, 0) + 1

    monitor_verification_path = output_dir / "monitor_verification_results.json"
    with monitor_verification_path.open("w", encoding="utf-8") as fh:
        json.dump(
            {
                "total": len(monitor_validation_records),
                "by_status": monitor_validation_summary,
                "records": monitor_validation_records,
                "by_alert_id": monitor_verification_lookup,
            },
            fh,
            indent=2,
        )
    print(f"    Monitor verification results: {monitor_verification_path}")


def create_rules_if_requested(
    *,
    args,
    output_dir: Path,
    mapping_batch: dict[str, Any],
    payload_preflight: dict[str, Any] | None,
) -> dict[str, Any]:
    """Create the emitted Kibana rules, and report whether that actually happened.

    Returns a ``rule_creation`` status document (see
    ``targets.kibana.alerting``). A requested creation that was skipped is
    reported on stderr and carried out to the caller so it can fail the run --
    it used to be a bare ``print`` with no exit-code consequence, which left an
    operator who asked for rules and got none looking at a green migration.
    """
    if not getattr(args, "create_alert_rules", False):
        return rule_creation_not_requested()
    gap = rule_creation_capability_gap(args)
    if gap is not None:
        print(f"    ERROR: {gap['headline']}", file=sys.stderr)
        # A skip artifact, so the output directory says "asked for, not done"
        # rather than staying silent about it.
        _write_rule_upload_results(output_dir, gap)
        return gap

    print("\n  Creating Kibana alerting rules (disabled by default)...")
    # --no-draft-alert-rules restricts creation to the fully-automated tier;
    # otherwise leave it None so the engine default (automated + draft) applies.
    creatable_tiers = (
        frozenset({AUTOMATED_TIER})
        if getattr(args, "no_draft_alert_rules", False)
        else None
    )
    rule_upload = create_rules_from_payloads(
        args.kibana_url,
        mapping_batch.get("results", []),
        api_key=getattr(args, "kibana_api_key", "") or "",
        space_id=getattr(args, "space_id", "") or "",
        preflight=payload_preflight,
        enabled=False,
        creatable_tiers=creatable_tiers,
        verify=_verify_from_args(args),
    )
    outcome = rule_creation_attempted(rule_upload)
    rule_upload_path = _write_rule_upload_results(output_dir, {**rule_upload, **outcome})
    print(f"    Monitor rule upload results: {rule_upload_path}")
    print(
        "    Created: {created}  Failed: {failed}  Skipped: {skipped}".format(
            **rule_upload["summary"],
        ),
    )
    if rule_upload.get("preflight_unreachable"):
        print(f"    ERROR: {outcome['headline']}", file=sys.stderr)
    if rule_upload["failed"]:
        for failure in rule_upload["failed"][:5]:
            print(
                f"      FAILED: {failure['name']} "
                f"({failure['rule_type_id']}): {failure['error'][:200]}"
            )
    return outcome


def _write_rule_upload_results(output_dir: Path, payload: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    rule_upload_path = output_dir / "monitor_rule_upload_results.json"
    with rule_upload_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return rule_upload_path
