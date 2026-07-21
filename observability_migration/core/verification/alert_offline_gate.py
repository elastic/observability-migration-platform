# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Offline correctness gate for alert / monitor → Kibana rule translations.

Separate from the dashboard ES|QL structural harness: alerts use automation
tiers + ``payload_status`` rather than panel ``feasibility``. This gate hard-
fails only on ``real_bug`` findings so ``manual_required`` / blocked / draft
review stay visible without being counted as migration success (issue #301 PR2).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from observability_migration.core.verification.translation_oracle import (
    StructuralFinding,
    StructuralRuleId,
    check_esql_structure,
    structural_errors,
)


class AlertGateDisposition(str, Enum):
    ok = "ok"
    expected_manual = "expected_manual"
    draft_review = "draft_review"
    real_bug = "real_bug"
    config_gap = "config_gap"


class AlertGateRuleId(str, Enum):
    ENABLED_TRUE = "ENABLED_TRUE"
    EMPTY_EMITTED_QUERY = "EMPTY_EMITTED_QUERY"
    MISSING_REQUIRED_FIELDS = "MISSING_REQUIRED_FIELDS"
    EMITTED_WITH_BLOCKED_STATUS = "EMITTED_WITH_BLOCKED_STATUS"
    MANUAL_COUNTED_AS_SUCCESS = "MANUAL_COUNTED_AS_SUCCESS"
    PARSE_DEGRADED_EMITTED = "PARSE_DEGRADED_EMITTED"
    STRUCTURAL_ESQL = "STRUCTURAL_ESQL"


@dataclass(frozen=True)
class AlertGateFinding:
    rule_id: AlertGateRuleId
    disposition: AlertGateDisposition
    message: str
    evidence: dict[str, Any]


def gate_bugs(findings: list[AlertGateFinding]) -> list[AlertGateFinding]:
    """Findings that must fail CI (translator / emission safety bugs)."""
    return [f for f in findings if f.disposition == AlertGateDisposition.real_bug]


def extract_esql_from_payload(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return ""
    esql_block = params.get("esqlQuery") or {}
    if isinstance(esql_block, dict):
        return str(esql_block.get("esql") or "").strip()
    return ""


def check_alert_mapping(
    mapping: dict[str, Any],
    *,
    source_name: str = "",
    alert_name: str = "",
    parse_degraded: bool = False,
) -> list[AlertGateFinding]:
    """Inspect one ``map_alert_to_kibana_payload`` / batch mapping result.

    ``mapping`` is the dict returned by ``map_alert_to_kibana_payload`` (or the
    ``mapping`` object nested under ``map_alerts_batch`` results).
    """
    findings: list[AlertGateFinding] = []
    payload = mapping.get("rule_payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    tier = str(mapping.get("automation_tier") or "")
    payload_status = str(mapping.get("payload_status") or "")
    payload_emitted = bool(mapping.get("payload_emitted"))
    valid = bool(mapping.get("valid"))
    evidence_base = {
        "source": source_name,
        "alert_name": alert_name,
        "automation_tier": tier,
        "payload_status": payload_status,
        "payload_emitted": payload_emitted,
        "valid": valid,
    }

    # --- Enablement safety ---
    if payload and payload.get("enabled") is True:
        findings.append(
            AlertGateFinding(
                rule_id=AlertGateRuleId.ENABLED_TRUE,
                disposition=AlertGateDisposition.real_bug,
                message="Emitted Kibana rule payload has enabled=True (must ship disabled)",
                evidence=evidence_base,
            )
        )

    # --- Manual / blocked must not look like success ---
    if tier == "manual_required":
        if payload_emitted or (payload and valid):
            findings.append(
                AlertGateFinding(
                    rule_id=AlertGateRuleId.MANUAL_COUNTED_AS_SUCCESS,
                    disposition=AlertGateDisposition.real_bug,
                    message="manual_required alert emitted a success-shaped payload",
                    evidence=evidence_base,
                )
            )
        else:
            findings.append(
                AlertGateFinding(
                    rule_id=AlertGateRuleId.MANUAL_COUNTED_AS_SUCCESS,
                    disposition=AlertGateDisposition.expected_manual,
                    message="manual_required with no emitted payload (expected)",
                    evidence=evidence_base,
                )
            )
        return findings

    if payload_status.startswith("blocked_") and payload_emitted:
        findings.append(
            AlertGateFinding(
                rule_id=AlertGateRuleId.EMITTED_WITH_BLOCKED_STATUS,
                disposition=AlertGateDisposition.real_bug,
                message=f"payload_status={payload_status} but payload_emitted=True",
                evidence=evidence_base,
            )
        )

    if parse_degraded and payload_status == "emitted":
        findings.append(
            AlertGateFinding(
                rule_id=AlertGateRuleId.PARSE_DEGRADED_EMITTED,
                disposition=AlertGateDisposition.real_bug,
                message="parse_degraded translation emitted a Kibana rule payload",
                evidence=evidence_base,
            )
        )

    if payload_status != "emitted":
        if tier == "draft_requires_review" and not payload_emitted:
            findings.append(
                AlertGateFinding(
                    rule_id=AlertGateRuleId.EMPTY_EMITTED_QUERY,
                    disposition=AlertGateDisposition.draft_review,
                    message="draft_requires_review without emitted payload",
                    evidence=evidence_base,
                )
            )
        return findings

    # --- Emitted payload required fields ---
    missing: list[str] = []
    for key in ("rule_type_id", "name", "consumer", "schedule", "params", "tags"):
        if key not in payload or payload.get(key) in (None, "", {}):
            missing.append(key)
    schedule = payload.get("schedule") or {}
    if isinstance(schedule, dict) and not schedule.get("interval"):
        missing.append("schedule.interval")
    params = payload.get("params") or {}
    if not isinstance(params, dict) or not params:
        missing.append("params")
    if "actions" not in payload:
        missing.append("actions")
    elif payload.get("actions") not in ([], None):
        # Connectors are placeholders today; non-empty actions without review is a
        # config gap unless they are intentionally empty. Non-list is a bug.
        if not isinstance(payload.get("actions"), list):
            findings.append(
                AlertGateFinding(
                    rule_id=AlertGateRuleId.MISSING_REQUIRED_FIELDS,
                    disposition=AlertGateDisposition.real_bug,
                    message="payload.actions must be a list (empty placeholder)",
                    evidence={**evidence_base, "actions": payload.get("actions")},
                )
            )
        else:
            findings.append(
                AlertGateFinding(
                    rule_id=AlertGateRuleId.MISSING_REQUIRED_FIELDS,
                    disposition=AlertGateDisposition.config_gap,
                    message="payload.actions is non-empty; connectors need target config",
                    evidence={**evidence_base, "action_count": len(payload["actions"])},
                )
            )

    if missing:
        findings.append(
            AlertGateFinding(
                rule_id=AlertGateRuleId.MISSING_REQUIRED_FIELDS,
                disposition=AlertGateDisposition.real_bug,
                message=f"Emitted payload missing required fields: {', '.join(missing)}",
                evidence={**evidence_base, "missing": missing},
            )
        )

    query = extract_esql_from_payload(payload)
    if not query:
        findings.append(
            AlertGateFinding(
                rule_id=AlertGateRuleId.EMPTY_EMITTED_QUERY,
                disposition=AlertGateDisposition.real_bug,
                message="payload_status=emitted but params.esqlQuery.esql is empty",
                evidence=evidence_base,
            )
        )
        return findings

    # --- ES|QL structural legality (skip native PROMQL passthrough) ---
    structural = structural_errors(check_esql_structure(query))
    for finding in structural:
        findings.append(
            AlertGateFinding(
                rule_id=AlertGateRuleId.STRUCTURAL_ESQL,
                disposition=AlertGateDisposition.real_bug,
                message=f"ES|QL structural oracle: {finding.rule_id.value}: {finding.message}",
                evidence={
                    **evidence_base,
                    "structural_rule": finding.rule_id.value,
                    "structural_evidence": finding.evidence,
                },
            )
        )

    if not gate_bugs(findings) and tier == "draft_requires_review":
        findings.append(
            AlertGateFinding(
                rule_id=AlertGateRuleId.EMPTY_EMITTED_QUERY,
                disposition=AlertGateDisposition.draft_review,
                message="draft_requires_review payload emitted (review required, not a gate bug)",
                evidence=evidence_base,
            )
        )
    elif not gate_bugs(findings) and not findings:
        findings.append(
            AlertGateFinding(
                rule_id=AlertGateRuleId.EMPTY_EMITTED_QUERY,
                disposition=AlertGateDisposition.ok,
                message="emitted payload passed offline gate",
                evidence=evidence_base,
            )
        )

    return findings


def check_alert_batch(
    batch_result: dict[str, Any],
    *,
    source_name: str = "",
    parse_degraded_by_id: dict[str, bool] | None = None,
) -> list[AlertGateFinding]:
    """Run ``check_alert_mapping`` over ``map_alerts_batch`` results."""
    findings: list[AlertGateFinding] = []
    degraded_lookup = parse_degraded_by_id or {}
    for item in batch_result.get("results") or []:
        mapping = item.get("mapping") or item
        name = str(item.get("name") or "")
        alert_id = str(item.get("alert_id") or "")
        parse_degraded = bool(degraded_lookup.get(alert_id) or degraded_lookup.get(name))
        findings.extend(
            check_alert_mapping(
                mapping,
                source_name=source_name,
                alert_name=name,
                parse_degraded=parse_degraded,
            )
        )
    return findings


# Re-export structural types for tests that assert nested oracle rules.
__all__ = [
    "AlertGateDisposition",
    "AlertGateFinding",
    "AlertGateRuleId",
    "StructuralFinding",
    "StructuralRuleId",
    "check_alert_batch",
    "check_alert_mapping",
    "extract_esql_from_payload",
    "gate_bugs",
]
