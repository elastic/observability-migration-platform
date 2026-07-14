# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Unit tests for the interaction-audit verdict and evidence contract."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest

from observability_migration.targets.kibana.interaction_audit import (
    CapabilityCategory,
    EvidenceParseError,
    FailureClass,
    InteractionFinding,
    InteractionReport,
    InteractionResult,
    InteractionStatus,
    NetworkEvidence,
    PanelEvidence,
    check_network_contract,
    enrich_esql_response,
    match_noise_allowance,
    parse_esql_request,
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


def test_interaction_report_passes_for_kibana_only_capability_when_passed():
    report = InteractionReport(
        scenario="synthetic",
        results=[
            InteractionResult(
                "multi_value=one",
                InteractionStatus.PASS,
                capability=CapabilityCategory.KIBANA_ONLY,
            )
        ],
    )
    assert report.status == "pass"
    assert report.exit_code == 0


def test_interaction_report_skipped_migrated_live_remains_pass():
    report = InteractionReport(
        scenario="redis",
        results=[
            InteractionResult(
                "namespace=ns_1",
                InteractionStatus.SKIPPED,
                capability=CapabilityCategory.MIGRATED_LIVE,
            )
        ],
    )
    assert report.status == "pass"
    assert report.exit_code == 0


def test_interaction_report_explicit_warn_overrides_skipped_pass():
    report = InteractionReport(
        scenario="redis",
        results=[
            InteractionResult(
                "namespace=ns_1",
                InteractionStatus.SKIPPED,
                capability=CapabilityCategory.MIGRATED_LIVE,
            ),
            InteractionResult(
                "instance=redis_2",
                InteractionStatus.WARN,
                capability=CapabilityCategory.MIGRATED_LIVE,
            ),
        ],
    )
    assert report.status == "warn"
    assert report.exit_code == 0


def test_interaction_report_skipped_migration_gap_remains_warn():
    report = InteractionReport(
        scenario="synthetic",
        results=[
            InteractionResult(
                "function=AVG",
                InteractionStatus.SKIPPED,
                capability=CapabilityCategory.MIGRATION_GAP,
            )
        ],
    )
    assert report.status == "warn"
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


def test_redact_evidence_preserves_at_in_url_path_query_and_fragment():
    value = redact_evidence(
        {
            "url": "https://example.test/users/alice@corp/search?q=tag@prod#panel@1",
        }
    )
    assert value["url"] == "https://example.test/users/alice@corp/search?q=tag@prod#panel@1"


def test_redact_evidence_strips_userinfo_without_corrupting_path_at_signs():
    value = redact_evidence(
        {
            "url": "https://user:pass@example.test/users/alice@corp/search?q=tag@prod#panel@1",
        }
    )
    assert value["url"] == "https://example.test/users/alice@corp/search?q=tag@prod#panel@1"


def test_redact_evidence_redacts_contract_dataclasses_without_mutation():
    network = NetworkEvidence(
        endpoint="/internal/search/esql_async",
        method="POST",
        status=200,
        url="https://user:pass@example.test/api/search",
        headers={
            "Authorization": "ApiKey secret",
            "Content-Type": "application/json",
        },
    )
    result = InteractionResult(
        "instance=redis_2",
        InteractionStatus.FAIL,
        capability=CapabilityCategory.MIGRATED_LIVE,
        findings=[InteractionFinding(FailureClass.RENDER_ERROR, "panel failed")],
        network=[network],
        panels=[PanelEvidence(panel_id="panel-1", title="CPU", status="rendered")],
    )
    report = InteractionReport(scenario="redis", results=[result])

    redacted = redact_evidence(report)

    assert redacted["results"][0]["network"][0]["url"] == "https://example.test/api/search"
    assert redacted["results"][0]["network"][0]["headers"]["Authorization"] == "[REDACTED]"
    assert redacted["results"][0]["network"][0]["headers"]["Content-Type"] == "application/json"
    assert network.url == "https://user:pass@example.test/api/search"
    assert network.headers["Authorization"] == "ApiKey secret"


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


def test_match_noise_allowance_rejects_whitespace_only_rationale():
    allowance = {
        "endpoint": "/internal/security/user_profile",
        "method": "GET",
        "status": 404,
        "rationale": "   ",
    }
    assert match_noise_allowance("/internal/security/user_profile", "GET", 404, [allowance]) is None


def test_match_noise_allowance_returns_stripped_rationale():
    allowance = {
        "endpoint": "/internal/security/user_profile",
        "method": "GET",
        "status": 404,
        "rationale": "  optional profile feature absent locally  ",
    }
    rationale = match_noise_allowance("/internal/security/user_profile", "GET", 404, [allowance])
    assert rationale == "optional profile feature absent locally"


_KBN_CONTEXT = (
    "%7B%22type%22%3A%22application%22%2C%22name%22%3A%22dashboards%22%2C%22child%22%3A%7B"
    "%22type%22%3A%22lens%22%2C%22id%22%3A%22panel-7%22%2C%22description%22%3A%22Redis%20Memory%20Usage%22%7D%7D"
)


def test_parse_esql_request_extracts_panel_identity_from_headers():
    headers = {
        "x-kbn-context": _KBN_CONTEXT,
        "x-opaque-id": "kibana:application:dashboards:panel-7",
    }
    body = {
        "query": "FROM metrics-* | WHERE namespace == ?namespace | STATS value=MAX(redis_up)",
        "params": [{"namespace": "prod"}],
    }
    evidence = parse_esql_request(
        url="http://localhost:5601/internal/search/esql_async",
        method="POST",
        headers=headers,
        body=body,
    )
    assert evidence is not None
    assert evidence.panel_id == "panel-7"
    assert evidence.panel_title == "Redis Memory Usage"
    assert evidence.opaque_id == "kibana:application:dashboards:panel-7"
    assert evidence.params == {"namespace": "prod"}
    assert evidence.param_kinds == {"namespace": "value"}
    assert evidence.endpoint == "/internal/search/esql_async"
    assert evidence.method == "POST"
    assert evidence.status == 0
    assert evidence.query == body["query"]


def test_parse_esql_request_is_case_insensitive_for_method_and_headers():
    headers = {
        "X-Kbn-Context": _KBN_CONTEXT,
        "X-Opaque-Id": "opaque-1",
    }
    body = {"query": "FROM metrics-*", "params": {"namespace": "prod"}}
    evidence = parse_esql_request(
        url="http://localhost:5601/internal/search/esql",
        method="post",
        headers=headers,
        body=body,
    )
    assert evidence is not None
    assert evidence.panel_id == "panel-7"
    assert evidence.opaque_id == "opaque-1"
    assert evidence.method == "POST"


def test_parse_esql_request_degrades_malformed_context():
    headers = {"x-kbn-context": "not-json", "x-opaque-id": "opaque-1"}
    body = {"query": "FROM metrics-*"}
    evidence = parse_esql_request(
        url="http://localhost:5601/internal/search/esql_async",
        method="POST",
        headers=headers,
        body=body,
    )
    assert evidence is not None
    assert evidence.panel_id == ""
    assert evidence.panel_title == ""


def test_parse_esql_request_ignores_unrelated_endpoint_and_method():
    headers = {"x-kbn-context": _KBN_CONTEXT}
    body = {"query": "FROM metrics-*"}
    assert (
        parse_esql_request(
            url="http://localhost:5601/api/search?path=/internal/search/esql",
            method="POST",
            headers=headers,
            body=body,
        )
        is None
    )
    assert (
        parse_esql_request(
            url="http://internal/search/esql",
            method="POST",
            headers=headers,
            body=body,
        )
        is None
    )
    assert (
        parse_esql_request(
            url="http://localhost:5601/internal/search/esql_async",
            method="GET",
            headers=headers,
            body=body,
        )
        is None
    )


def test_parse_esql_request_parses_value_identifier_and_multi_value_params():
    body = {
        "query": "FROM metrics-* | WHERE namespace == ?namespace | STATS BY ??grouping",
        "params": [
            {"namespace": "prod"},
            {"tags": ["a", "b"]},
            {"grouping": "host.name"},
        ],
    }
    evidence = parse_esql_request(
        url="http://localhost:5601/internal/search/esql_async",
        method="POST",
        headers={},
        body=body,
    )
    assert evidence is not None
    assert evidence.params == {
        "namespace": "prod",
        "tags": ["a", "b"],
        "grouping": "host.name",
    }
    assert evidence.param_kinds == {
        "namespace": "value",
        "tags": "value",
        "grouping": "identifier",
    }


def test_parse_esql_request_infers_identifier_kind_from_plain_string_wire_format():
    body = {
        "query": "FROM metrics-* | STATS value=MAX(up) BY grouping=??grouping",
        "params": [{"grouping": "host.name"}],
    }
    evidence = parse_esql_request(
        url="http://localhost:5601/internal/search/esql_async",
        method="POST",
        headers={},
        body=body,
    )
    assert evidence is not None
    assert evidence.params == {"grouping": "host.name"}
    assert evidence.param_kinds == {"grouping": "identifier"}


def test_parse_esql_request_infers_value_kind_from_plain_string_wire_format():
    body = {
        "query": "FROM metrics-* | WHERE namespace == ?namespace",
        "params": [{"namespace": "prod"}],
    }
    evidence = parse_esql_request(
        url="http://localhost:5601/internal/search/esql_async",
        method="POST",
        headers={},
        body=body,
    )
    assert evidence is not None
    assert evidence.params == {"namespace": "prod"}
    assert evidence.param_kinds == {"namespace": "value"}


def test_parse_esql_request_infers_function_identifier_from_query_token():
    body = {
        "query": "FROM metrics-* | STATS value=??aggregate(field)",
        "params": [{"aggregate": "MAX"}],
    }
    evidence = parse_esql_request(
        url="http://localhost:5601/internal/search/esql_async",
        method="POST",
        headers={},
        body=body,
    )
    assert evidence is not None
    assert evidence.params == {"aggregate": "MAX"}
    assert evidence.param_kinds == {"aggregate": "identifier"}


def test_parse_esql_request_rejects_dual_value_and_identifier_query_tokens():
    with pytest.raises(EvidenceParseError, match="ambiguous dual query tokens"):
        parse_esql_request(
            url="http://localhost:5601/internal/search/esql_async",
            method="POST",
            headers={},
            body={
                "query": "FROM metrics-* | WHERE ?name == ??name",
                "params": [{"name": "prod"}],
            },
        )


def test_parse_esql_request_accepts_legacy_identifier_wrapper_when_compatible():
    body = {
        "query": "FROM metrics-* | STATS BY ??grouping",
        "params": [{"grouping": {"identifier": "host.name"}}],
    }
    evidence = parse_esql_request(
        url="http://localhost:5601/internal/search/esql_async",
        method="POST",
        headers={},
        body=body,
    )
    assert evidence is not None
    assert evidence.params == {"grouping": "host.name"}
    assert evidence.param_kinds == {"grouping": "identifier"}


def test_parse_esql_request_rejects_identifier_wrapper_with_value_query_token():
    with pytest.raises(EvidenceParseError, match="identifier wrapper conflicts with value token"):
        parse_esql_request(
            url="http://localhost:5601/internal/search/esql_async",
            method="POST",
            headers={},
            body={
                "query": "FROM metrics-* | WHERE grouping == ?grouping",
                "params": [{"grouping": {"identifier": "host.name"}}],
            },
        )


def test_parse_esql_request_rejects_non_string_query_value():
    with pytest.raises(EvidenceParseError, match="query must be a string"):
        parse_esql_request(
            url="http://localhost:5601/internal/search/esql_async",
            method="POST",
            headers={},
            body={"query": {"invalid": True}, "params": []},
        )


def test_parse_esql_request_allows_missing_query_for_graceful_capture():
    evidence = parse_esql_request(
        url="http://localhost:5601/internal/search/esql_async",
        method="POST",
        headers={},
        body={"params": [{"namespace": "prod"}]},
    )
    assert evidence is not None
    assert evidence.query == ""
    assert evidence.params == {"namespace": "prod"}
    assert evidence.param_kinds == {"namespace": "value"}


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"": "prod"}, "empty"),
        ([{"namespace": "prod"}, {"namespace": "prod"}], "duplicate"),
        ([{"namespace": True}], "boolean"),
        ([{"grouping": {"identifier": "host.name", "extra": "x"}}], "unsupported identifier"),
        ([{"grouping": {"identifier": ""}}], "identifier param must be a non-empty string"),
        ([{"bad": [True]}], "boolean list values"),
        ("not-params", "params must be"),
    ],
)
def test_parse_esql_request_rejects_malformed_params(params: object, message: str):
    with pytest.raises(EvidenceParseError, match=message):
        parse_esql_request(
            url="http://localhost:5601/internal/search/esql_async",
            method="POST",
            headers={},
            body={"query": "FROM metrics-*", "params": params},
        )


def test_parse_esql_request_copies_headers_without_mutation():
    headers = {"Authorization": "ApiKey secret", "x-kbn-context": _KBN_CONTEXT}
    body = {"query": "FROM metrics-*"}
    original = dict(headers)
    evidence = parse_esql_request(
        url="http://localhost:5601/internal/search/esql_async",
        method="POST",
        headers=headers,
        body=body,
    )
    assert evidence is not None
    assert headers == original
    assert evidence.headers == original
    evidence.headers["Authorization"] = "changed"
    assert headers["Authorization"] == "ApiKey secret"


def test_network_evidence_to_dict_includes_extended_fields_and_status_code():
    evidence = NetworkEvidence(
        endpoint="/internal/search/esql_async",
        method="POST",
        status=200,
        url="http://localhost:5601/internal/search/esql_async",
        query="FROM metrics-*",
        headers={"Content-Type": "application/json"},
        body={"query": "FROM metrics-*"},
        panel_id="panel-7",
        panel_title="Redis Memory Usage",
        opaque_id="opaque-1",
        params={"namespace": "prod"},
        param_kinds={"namespace": "value"},
        response_columns=("value", "namespace"),
        row_count=3,
        error="",
    )
    payload = evidence.to_dict()
    assert payload["status"] == 200
    assert payload["status_code"] == 200
    assert evidence.status_code == 200
    assert payload["panel_id"] == "panel-7"
    assert payload["panel_title"] == "Redis Memory Usage"
    assert payload["opaque_id"] == "opaque-1"
    assert payload["params"] == {"namespace": "prod"}
    assert payload["param_kinds"] == {"namespace": "value"}
    assert payload["response_columns"] == ["value", "namespace"]
    assert payload["row_count"] == 3
    assert payload["body"] == {"query": "FROM metrics-*"}


def _successful_evidence(**overrides: object) -> NetworkEvidence:
    base = {
        "endpoint": "/internal/search/esql_async",
        "method": "POST",
        "status": 200,
        "url": "http://localhost:5601/internal/search/esql_async",
        "query": "FROM metrics-* | WHERE namespace == ?namespace | STATS BY ??grouping",
        "panel_id": "panel-7",
        "params": {"namespace": "prod", "grouping": "host.name"},
        "param_kinds": {"namespace": "value", "grouping": "identifier"},
        "response_columns": ("value", "namespace", "host.name"),
        "row_count": 5,
    }
    base.update(overrides)
    return NetworkEvidence(**base)  # type: ignore[arg-type]


def test_check_network_contract_reports_missing_expected_request():
    findings = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[],
    )
    assert len(findings) == 1
    assert findings[0].failure_class == FailureClass.EXPECTED_REQUEST_MISSING
    assert "panel panel-7" in findings[0].detail


def test_check_network_contract_reports_unexpected_unaffected_panel_request():
    findings = check_network_contract(
        expected_panel_ids=[],
        unaffected_panel_ids=["panel-9"],
        evidence=[_successful_evidence(panel_id="panel-9")],
    )
    assert len(findings) == 1
    assert findings[0].failure_class == FailureClass.UNEXPECTED_PANEL_REQUEST
    assert "panel panel-9" in findings[0].detail


def test_check_network_contract_reports_server_and_query_errors():
    findings = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[
            NetworkEvidence(panel_id="panel-7", status=503),
            NetworkEvidence(panel_id="panel-7", status=400),
        ],
    )
    classes = [finding.failure_class for finding in findings]
    assert FailureClass.SERVER_ERROR in classes
    assert FailureClass.QUERY_CONTRACT_ERROR in classes
    assert FailureClass.EXPECTED_REQUEST_MISSING in classes


def test_check_network_contract_enforces_query_contains_and_not_contains():
    findings = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[
            _successful_evidence(
                query="FROM metrics-* | WHERE namespace == ?namespace | STATS value=MAX(redis_up)",
            )
        ],
        query_contains=["STATS value=MAX(redis_up)", "MISSING_FRAGMENT"],
        query_not_contains=["DROP TABLE"],
    )
    classes = [finding.failure_class for finding in findings]
    details = [finding.detail for finding in findings]
    assert all(failure_class == FailureClass.QUERY_CONTRACT_ERROR for failure_class in classes)
    assert any("MISSING_FRAGMENT" in detail for detail in details)
    assert not any("DROP TABLE" in detail for detail in details)


def test_check_network_contract_enforces_value_param_binding_and_token():
    findings = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[_successful_evidence()],
        expected_value_params={"namespace": "prod"},
    )
    assert findings == []

    wrong_value = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[_successful_evidence(params={"namespace": "staging"})],
        expected_value_params={"namespace": "prod"},
    )
    assert any("expected value 'prod'" in finding.detail for finding in wrong_value)

    missing_token = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[
            _successful_evidence(
                query="FROM metrics-* | WHERE namespace == ??namespace | STATS BY ??grouping",
            )
        ],
        expected_value_params={"namespace": "prod"},
    )
    assert any("missing value token ?namespace" in finding.detail for finding in missing_token)
    assert any("bound as identifier token ??namespace" in finding.detail for finding in missing_token)


def test_check_network_contract_enforces_multi_value_sequence() -> None:
    matching = _successful_evidence(
        query="FROM metrics-* | WHERE MV_CONTAINS(?services, service.name)",
        params={"services": ["api", "worker"]},
        param_kinds={"services": "value"},
    )
    findings = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[matching],
        expected_value_params={"services": ["api", "worker"]},
    )
    assert findings == []

    scalar = replace(
        matching,
        params={"services": "api"},
    )
    scalar_findings = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[scalar],
        expected_value_params={"services": ["api", "worker"]},
    )
    assert any(
        "expected value ['api', 'worker']" in finding.detail
        for finding in scalar_findings
    )

    normalized_scalar = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[replace(matching, params={"services": "api"})],
        expected_value_params={"services": ["api"]},
    )
    assert normalized_scalar == []


def test_query_not_contains_value_token_does_not_match_identifier_token():
    evidence = _successful_evidence(
        query="FROM metrics-* | STATS value=AVG(x) BY grouping=??grouping",
        params={"grouping": "host.name"},
        param_kinds={"grouping": "identifier"},
        response_columns=("value", "grouping"),
    )

    findings = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[evidence],
        query_not_contains=["?grouping"],
    )

    assert findings == []


def test_check_network_contract_enforces_identifier_param_binding_and_token():
    findings = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[_successful_evidence()],
        expected_identifier_params={"grouping": "host.name"},
    )
    assert findings == []

    wrong_kind = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[
            _successful_evidence(
                params={"namespace": "prod", "grouping": "host.name"},
                param_kinds={"namespace": "value", "grouping": "value"},
            )
        ],
        expected_identifier_params={"grouping": "host.name"},
    )
    assert any("expected identifier 'host.name'" in finding.detail for finding in wrong_kind)

    missing_token = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[
            _successful_evidence(
                query="FROM metrics-* | WHERE namespace == ?namespace | STATS BY ?grouping",
            )
        ],
        expected_identifier_params={"grouping": "host.name"},
    )
    assert any("missing identifier token ??grouping" in finding.detail for finding in missing_token)
    assert any("bound as value token ?grouping" in finding.detail for finding in missing_token)


def test_check_network_contract_enforces_columns_alias_and_minimum_rows():
    findings = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[_successful_evidence(response_columns=("value",), row_count=1)],
        required_columns=["namespace"],
        stable_alias="host.name",
        minimum_rows=3,
    )
    classes = [finding.failure_class for finding in findings]
    details = [finding.detail for finding in findings]
    assert all(failure_class == FailureClass.QUERY_CONTRACT_ERROR for failure_class in classes)
    assert any("missing response column 'namespace'" in detail for detail in details)
    assert any("missing stable alias column 'host.name'" in detail for detail in details)
    assert any("row_count 1 below minimum 3" in detail for detail in details)


def test_check_network_contract_passes_for_valid_expected_panel_response():
    findings = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=["panel-9"],
        evidence=[_successful_evidence()],
        query_contains=["FROM metrics-*"],
        query_not_contains=["DROP TABLE"],
        expected_value_params={"namespace": "prod"},
        expected_identifier_params={"grouping": "host.name"},
        required_columns=["value", "namespace"],
        stable_alias="host.name",
        minimum_rows=1,
    )
    assert findings == []


def test_check_network_contract_deduplicates_findings_deterministically():
    evidence = [
        _successful_evidence(
            query="FROM metrics-*",
            params={"namespace": "staging"},
            param_kinds={"namespace": "value", "grouping": "value"},
            response_columns=(),
            row_count=0,
        ),
        _successful_evidence(
            query="FROM metrics-*",
            params={"namespace": "staging"},
            param_kinds={"namespace": "value", "grouping": "value"},
            response_columns=(),
            row_count=0,
        ),
    ]
    findings = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=evidence,
        query_contains=["MISSING"],
        expected_value_params={"namespace": "prod"},
        expected_identifier_params={"grouping": "host.name"},
        required_columns=["namespace"],
        stable_alias="host.name",
        minimum_rows=2,
    )
    keys = [(finding.failure_class, finding.detail) for finding in findings]
    assert len(keys) == len(set(keys))
    assert len(findings) > 1


def test_check_network_contract_ignores_stale_wrong_success_when_later_attempt_matches():
    findings = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[
            _successful_evidence(
                params={"namespace": "staging"},
                param_kinds={"namespace": "value", "grouping": "identifier"},
            ),
            _successful_evidence(),
        ],
        expected_value_params={"namespace": "prod"},
        expected_identifier_params={"grouping": "host.name"},
    )
    assert findings == []


def test_check_network_contract_reports_latest_success_when_all_attempts_fail_contract():
    findings = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[
            _successful_evidence(
                query="FROM metrics-*",
                params={"namespace": "first"},
                param_kinds={"namespace": "value", "grouping": "value"},
            ),
            _successful_evidence(
                query="FROM metrics-*",
                params={"namespace": "latest"},
                param_kinds={"namespace": "value", "grouping": "value"},
            ),
        ],
        expected_value_params={"namespace": "prod"},
    )
    assert findings == [
        InteractionFinding(
            FailureClass.QUERY_CONTRACT_ERROR,
            "panel panel-7: param namespace expected value 'prod'",
        ),
        InteractionFinding(
            FailureClass.QUERY_CONTRACT_ERROR,
            "panel panel-7: query missing value token ?namespace",
        ),
    ]


def test_check_network_contract_framework_error_for_uncorrelated_successful_evidence():
    findings = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[_successful_evidence(panel_id="", status=200)],
    )
    assert findings == [
        InteractionFinding(
            FailureClass.FRAMEWORK_ERROR,
            "ES|QL response could not be correlated to a panel",
        ),
        InteractionFinding(
            FailureClass.EXPECTED_REQUEST_MISSING,
            "panel panel-7: expected ES|QL request missing",
        ),
    ]
    for finding in findings:
        assert "Authorization" not in finding.detail
        assert "headers" not in finding.detail
        assert "body" not in finding.detail


def test_check_network_contract_pending_status_does_not_count_as_success():
    findings = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[NetworkEvidence(panel_id="panel-7", status=0, query="FROM metrics-*")],
    )
    assert findings == [
        InteractionFinding(
            FailureClass.EXPECTED_REQUEST_MISSING,
            "panel panel-7: expected ES|QL request missing",
        )
    ]


def test_check_network_contract_overlapping_expected_and_unaffected_panel():
    findings = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=["panel-7"],
        evidence=[
            _successful_evidence(
                query="FROM metrics-*",
                params={"namespace": "staging"},
                param_kinds={"namespace": "value", "grouping": "value"},
            )
        ],
        expected_value_params={"namespace": "prod"},
    )
    assert findings == [
        InteractionFinding(
            FailureClass.UNEXPECTED_PANEL_REQUEST,
            "panel panel-7: unexpected successful ES|QL request",
        ),
        InteractionFinding(
            FailureClass.QUERY_CONTRACT_ERROR,
            "panel panel-7: param namespace expected value 'prod'",
        ),
        InteractionFinding(
            FailureClass.QUERY_CONTRACT_ERROR,
            "panel panel-7: query missing value token ?namespace",
        ),
    ]


def test_check_network_contract_rejects_similar_but_distinct_param_tokens():
    prefix_only_value = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[
            _successful_evidence(
                query="FROM metrics-* | WHERE namespace == ?namespace_extra",
                params={"namespace": "prod"},
                param_kinds={"namespace": "value", "grouping": "identifier"},
            )
        ],
        expected_value_params={"namespace": "prod"},
    )
    assert prefix_only_value == [
        InteractionFinding(
            FailureClass.QUERY_CONTRACT_ERROR,
            "panel panel-7: query missing value token ?namespace",
        )
    ]

    prefix_only_identifier = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[
            _successful_evidence(
                query="FROM metrics-* | STATS BY ??grouping_extra",
            )
        ],
        expected_identifier_params={"grouping": "host.name"},
    )
    assert prefix_only_identifier == [
        InteractionFinding(
            FailureClass.QUERY_CONTRACT_ERROR,
            "panel panel-7: query missing identifier token ??grouping",
        )
    ]

    exact_tokens = check_network_contract(
        expected_panel_ids=["panel-7"],
        unaffected_panel_ids=[],
        evidence=[_successful_evidence()],
        expected_value_params={"namespace": "prod"},
        expected_identifier_params={"grouping": "host.name"},
    )
    assert exact_tokens == []


def test_parse_esql_request_rejects_multi_key_params_list_entry():
    with pytest.raises(EvidenceParseError, match="single-key mapping"):
        parse_esql_request(
            url="http://localhost:5601/internal/search/esql_async",
            method="POST",
            headers={},
            body={"query": "FROM metrics-*", "params": [{"a": 1, "b": 2}]},
        )


def test_redact_evidence_redacts_extended_network_evidence_fields():
    network = NetworkEvidence(
        endpoint="/internal/search/esql_async",
        method="POST",
        status=200,
        url="https://user:pass@example.test/internal/search/esql_async",
        headers={
            "Authorization": "ApiKey secret",
            "cookie": "sid=secret",
            "Content-Type": "application/json",
        },
        panel_id="panel-7",
        opaque_id="opaque-1",
    )
    redacted = redact_evidence(network)
    assert redacted["url"] == "https://example.test/internal/search/esql_async"
    assert redacted["headers"]["Authorization"] == "[REDACTED]"
    assert redacted["headers"]["cookie"] == "[REDACTED]"
    assert redacted["headers"]["Content-Type"] == "application/json"
    assert network.headers["Authorization"] == "ApiKey secret"


def _request_evidence(**overrides: object) -> NetworkEvidence:
    base = {
        "endpoint": "/internal/search/esql_async",
        "method": "POST",
        "status": 0,
        "url": "http://localhost:5601/internal/search/esql_async",
        "query": "FROM metrics-*",
        "body": {"query": "FROM metrics-*"},
        "panel_id": "panel-7",
    }
    base.update(overrides)
    return NetworkEvidence(**base)  # type: ignore[arg-type]


def test_enrich_esql_response_parses_direct_body():
    evidence = _request_evidence()
    enriched = enrich_esql_response(
        evidence,
        status=200,
        body={
            "columns": [{"name": "value"}, {"name": "host.name"}],
            "values": [[1, "a"], [2, "b"], [3, "c"]],
        },
    )
    assert enriched.status == 200
    assert enriched.response_columns == ("value", "host.name")
    assert enriched.row_count == 3
    assert enriched.error == ""
    assert enriched.body == evidence.body


def test_enrich_esql_response_unwraps_kibana_envelopes():
    evidence = _request_evidence()
    enriched = enrich_esql_response(
        evidence,
        status=200,
        body={
            "rawResponse": {
                "response": {
                    "data": {
                        "columns": [{"name": "cpu"}],
                        "values": [[1.0], [2.0]],
                    }
                }
            }
        },
    )
    assert enriched.response_columns == ("cpu",)
    assert enriched.row_count == 2


def test_enrich_esql_response_uses_all_columns_when_drop_null_omits_columns():
    evidence = _request_evidence()
    enriched = enrich_esql_response(
        evidence,
        status=200,
        body={
            "all_columns": [{"name": "value"}],
            "columns": [],
            "values": [[]],
        },
    )

    assert enriched.response_columns == ("value",)
    assert enriched.row_count == 1


def test_enrich_esql_response_prefers_explicit_error_and_parses_body_error():
    evidence = _request_evidence()
    explicit = enrich_esql_response(
        evidence,
        status=400,
        body={"error": {"reason": "syntax error", "message": "ignored when explicit"}},
        error="explicit failure",
    )
    assert explicit.error == "explicit failure"

    parsed = enrich_esql_response(
        evidence,
        status=400,
        body={"error": {"reason": "bad query"}},
    )
    assert parsed.error == "bad query"


def test_enrich_esql_response_degrades_malformed_body_without_rows():
    evidence = _request_evidence(body={"query": "FROM metrics-*", "secret": "rows"})
    enriched = enrich_esql_response(
        evidence,
        status=200,
        body={"columns": [{"bad": True}, {"name": ""}], "values": "not-a-list"},
    )
    assert enriched.response_columns == ()
    assert enriched.row_count == -1
    assert "values" not in str(enriched.body)
    assert enriched.body == evidence.body


def test_enrich_esql_response_does_not_mutate_request_evidence():
    evidence = _request_evidence()
    original_body = evidence.body
    enriched = enrich_esql_response(
        evidence,
        status=200,
        body={"columns": [{"name": "value"}], "values": [[1]]},
    )
    assert evidence.status == 0
    assert evidence.response_columns == ()
    assert evidence.row_count == -1
    assert evidence.body == original_body
    assert enriched is not evidence


def test_enrich_esql_response_never_retains_response_values_in_body():
    evidence = _request_evidence()
    enriched = enrich_esql_response(
        evidence,
        status=200,
        body={
            "columns": [{"name": "value"}],
            "values": [[999], [888]],
        },
    )
    payload = enriched.to_dict()
    assert payload["body"] == {"query": "FROM metrics-*"}
    assert "999" not in str(payload)
    assert "888" not in str(payload)
