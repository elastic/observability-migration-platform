# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Unit tests for the alert / monitor offline correctness gate."""

from __future__ import annotations

from observability_migration.core.verification.alert_offline_gate import (
    AlertGateDisposition,
    AlertGateRuleId,
    check_alert_mapping,
    gate_bugs,
)


def _emitted_payload(**overrides):
    payload = {
        "rule_type_id": ".es-query",
        "name": "[migrated] cpu",
        "consumer": "stackAlerts",
        "schedule": {"interval": "1m"},
        "params": {
            "searchType": "esqlQuery",
            "esqlQuery": {"esql": "FROM metrics-* | STATS c = COUNT(*)"},
            "timeField": "@timestamp",
        },
        "actions": [],
        "enabled": False,
        "tags": ["obs-migration"],
    }
    payload.update(overrides)
    return {
        "rule_payload": payload,
        "automation_tier": "automated",
        "payload_emitted": True,
        "payload_status": "emitted",
        "valid": True,
    }


def test_emitted_payload_passes():
    findings = check_alert_mapping(_emitted_payload())
    assert gate_bugs(findings) == []
    assert any(f.disposition == AlertGateDisposition.ok for f in findings)


def test_enabled_true_is_real_bug():
    mapping = _emitted_payload(enabled=True)
    bugs = gate_bugs(check_alert_mapping(mapping))
    assert any(b.rule_id == AlertGateRuleId.ENABLED_TRUE for b in bugs)


def test_empty_query_on_emitted_is_real_bug():
    mapping = _emitted_payload()
    mapping["rule_payload"]["params"] = {"searchType": "esqlQuery", "esqlQuery": {"esql": ""}}
    bugs = gate_bugs(check_alert_mapping(mapping))
    assert any(b.rule_id == AlertGateRuleId.EMPTY_EMITTED_QUERY for b in bugs)


def test_manual_required_without_payload_is_expected():
    findings = check_alert_mapping(
        {
            "rule_payload": {},
            "automation_tier": "manual_required",
            "payload_emitted": False,
            "payload_status": "blocked_manual_review",
            "valid": False,
        }
    )
    assert gate_bugs(findings) == []
    assert any(f.disposition == AlertGateDisposition.expected_manual for f in findings)


def test_manual_required_with_emitted_payload_is_real_bug():
    bugs = gate_bugs(
        check_alert_mapping(
            {
                **_emitted_payload(),
                "automation_tier": "manual_required",
                "payload_status": "blocked_manual_review",
            }
        )
    )
    assert any(b.rule_id == AlertGateRuleId.MANUAL_COUNTED_AS_SUCCESS for b in bugs)


def test_parse_degraded_emitted_is_real_bug():
    bugs = gate_bugs(check_alert_mapping(_emitted_payload(), parse_degraded=True))
    assert any(b.rule_id == AlertGateRuleId.PARSE_DEGRADED_EMITTED for b in bugs)


def test_structural_oracle_nested_on_bad_esql():
    mapping = _emitted_payload()
    mapping["rule_payload"]["params"]["esqlQuery"]["esql"] = (
        "TS metrics-*\n"
        '| STATS a = SUM(IRATE(CASE((mode == "user"), m, NULL), 1m)), '
        "b = SUM(IRATE(other)) BY time_bucket = TBUCKET(5 minute)\n"
    )
    bugs = gate_bugs(check_alert_mapping(mapping))
    assert any(b.rule_id == AlertGateRuleId.STRUCTURAL_ESQL for b in bugs)


def test_native_promql_passthrough_skips_structural_rules():
    mapping = _emitted_payload()
    mapping["rule_payload"]["params"]["esqlQuery"]["esql"] = (
        'PROMQL("up == 0", "metrics-*") | WHERE value > 0'
    )
    assert gate_bugs(check_alert_mapping(mapping)) == []


def test_nonempty_actions_are_config_gap_not_bug():
    findings = check_alert_mapping(_emitted_payload(actions=[{"id": "email"}]))
    assert gate_bugs(findings) == []
    assert any(f.disposition == AlertGateDisposition.config_gap for f in findings)
