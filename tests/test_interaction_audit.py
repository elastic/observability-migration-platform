# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Unit tests for the interaction-audit verdict and evidence contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from observability_migration.targets.kibana.interaction_audit import (
    CapabilityCategory,
    FailureClass,
    InteractionFinding,
    InteractionReport,
    InteractionResult,
    InteractionStatus,
    NetworkEvidence,
    PanelEvidence,
    match_noise_allowance,
    redact_evidence,
)


def test_interaction_report_aggregates_fail_over_warn():
    report = InteractionReport(
        scenario="redis",
        results=[
            InteractionResult(
                "namespace=ns_1",
                InteractionStatus.PASS,
                capability=CapabilityCategory.MIGRATED_LIVE,
            ),
            InteractionResult(
                "instance=redis_2",
                InteractionStatus.FAIL,
                capability=CapabilityCategory.MIGRATED_LIVE,
                findings=[InteractionFinding(FailureClass.RENDER_ERROR, "panel failed")],
            ),
        ],
    )
    assert report.status == "fail"
    assert report.exit_code == 1


def test_interaction_report_aggregates_warn_for_status_and_migration_gap():
    report = InteractionReport(
        scenario="synthetic",
        results=[
            InteractionResult(
                "function=AVG",
                InteractionStatus.WARN,
                capability=CapabilityCategory.MIGRATION_GAP,
            )
        ],
    )
    assert report.status == "warn"
    assert report.exit_code == 0


def test_interaction_report_warns_for_source_only_capability_even_when_passed():
    report = InteractionReport(
        scenario="redis",
        results=[
            InteractionResult(
                "datasource=prometheus",
                InteractionStatus.PASS,
                capability=CapabilityCategory.SOURCE_ONLY,
            )
        ],
    )
    assert report.status == "warn"
    assert report.exit_code == 0


def test_interaction_report_warns_for_migration_gap_capability_even_when_passed():
    report = InteractionReport(
        scenario="synthetic",
        results=[
            InteractionResult(
                "function=AVG",
                InteractionStatus.PASS,
                capability=CapabilityCategory.MIGRATION_GAP,
            )
        ],
    )
    assert report.status == "warn"
    assert report.exit_code == 0


def test_interaction_report_passes_when_all_migrated_live_pass():
    report = InteractionReport(
        scenario="redis",
        results=[
            InteractionResult(
                "namespace=ns_1",
                InteractionStatus.PASS,
                capability=CapabilityCategory.MIGRATED_LIVE,
            )
        ],
    )
    assert report.status == "pass"
    assert report.exit_code == 0


def test_interaction_report_to_dict_serializes_enums_recursively():
    finding = InteractionFinding(FailureClass.RENDER_ERROR, "panel failed")
    network = NetworkEvidence(endpoint="/internal/search/esql_async", method="POST", status=200)
    panel = PanelEvidence(panel_id="panel-1", title="CPU", status="rendered")
    result = InteractionResult(
        "instance=redis_2",
        InteractionStatus.FAIL,
        capability=CapabilityCategory.MIGRATED_LIVE,
        findings=[finding],
        network=[network],
        panels=[panel],
    )
    report = InteractionReport(scenario="redis", results=[result])
    payload = report.to_dict()

    assert payload["scenario"] == "redis"
    assert payload["status"] == "fail"
    assert payload["exit_code"] == 1
    assert payload["results"][0]["status"] == "fail"
    assert payload["results"][0]["name"] == "instance=redis_2"
    assert payload["results"][0]["capability"] == "migrated_live"
    assert payload["results"][0]["findings"][0]["failure_class"] == "render_error"
    assert payload["results"][0]["findings"][0]["detail"] == "panel failed"
    assert payload["results"][0]["network"][0]["endpoint"] == "/internal/search/esql_async"
    assert payload["results"][0]["panels"][0]["title"] == "CPU"


def test_redact_evidence_redacts_sensitive_keys_and_url_credentials():
    value = redact_evidence(
        {
            "authorization": "ApiKey secret",
            "cookie": "sid=secret",
            "url": "https://user:pass@example.test/api/search",
            "query": "FROM metrics-*",
        }
    )
    assert value["authorization"] == "[REDACTED]"
    assert value["cookie"] == "[REDACTED]"
    assert value["url"] == "https://example.test/api/search"
    assert value["query"] == "FROM metrics-*"


def test_redact_evidence_is_case_insensitive_and_nested():
    original = {
        "headers": {
            "Authorization": "ApiKey secret",
            "X-Elastic-Api-Key": "abc",
            "set-cookie": "sid=secret",
        },
        "nested": [{"api_key": "secret", "safe": "ok"}],
    }
    redacted = redact_evidence(original)

    assert redacted["headers"]["Authorization"] == "[REDACTED]"
    assert redacted["headers"]["X-Elastic-Api-Key"] == "[REDACTED]"
    assert redacted["headers"]["set-cookie"] == "[REDACTED]"
    assert redacted["nested"][0]["api_key"] == "[REDACTED]"
    assert redacted["nested"][0]["safe"] == "ok"
    assert original["headers"]["Authorization"] == "ApiKey secret"
    assert original["nested"][0]["api_key"] == "secret"


def test_redact_evidence_preserves_url_port_query_and_fragment():
    value = redact_evidence(
        {
            "url": "https://admin:secret@example.test:9200/api/search?q=cpu#panel-1",
        }
    )
    assert value["url"] == "https://example.test:9200/api/search?q=cpu#panel-1"


def test_redact_evidence_preserves_tuple_and_list_structure():
    original: tuple[Any, ...] = (
        {"cookie": "sid=secret"},
        [{"authorization": "Bearer token"}],
    )
    redacted = redact_evidence(original)

    assert isinstance(redacted, tuple)
    assert redacted[0]["cookie"] == "[REDACTED]"
    assert redacted[1][0]["authorization"] == "[REDACTED]"
    assert original[0]["cookie"] == "sid=secret"


def test_match_noise_allowance_returns_rationale_for_exact_match():
    allowances = [
        {
            "endpoint": "/internal/security/user_profile",
            "method": "GET",
            "status": 404,
            "rationale": "optional profile feature absent locally",
        }
    ]
    rationale = match_noise_allowance(
        "/internal/security/user_profile",
        "get",
        404,
        allowances,
    )
    assert rationale == "optional profile feature absent locally"


@dataclass
class _AllowanceObject:
    endpoint: str
    method: str
    status: int
    rationale: str


def test_match_noise_allowance_accepts_object_allowances():
    allowances = [
        _AllowanceObject(
            endpoint="/api/assistant",
            method="POST",
            status=403,
            rationale="assistant disabled in local stack",
        )
    ]
    rationale = match_noise_allowance("/api/assistant", "POST", 403, allowances)
    assert rationale == "assistant disabled in local stack"


def test_match_noise_allowance_requires_endpoint_method_and_status():
    allowance = {
        "endpoint": "/internal/security/user_profile",
        "method": "GET",
        "status": 404,
        "rationale": "optional profile feature absent locally",
    }
    assert match_noise_allowance("/other", "GET", 404, [allowance]) is None
    assert match_noise_allowance("/internal/security/user_profile", "POST", 404, [allowance]) is None
    assert match_noise_allowance("/internal/security/user_profile", "GET", 500, [allowance]) is None


def test_match_noise_allowance_rejects_empty_rationale():
    allowance = {
        "endpoint": "/internal/security/user_profile",
        "method": "GET",
        "status": 404,
        "rationale": "",
    }
    assert match_noise_allowance("/internal/security/user_profile", "GET", 404, [allowance]) is None
