# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Interaction-audit verdict and evidence contract for dashboard control testing.

Pure dataclasses and helpers that classify browser interaction outcomes,
aggregate scenario reports, redact sensitive evidence, and match narrow network
noise allowances. No browser driver or I/O dependencies.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class InteractionStatus(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIPPED = "skipped"


class CapabilityCategory(str, Enum):
    MIGRATED_LIVE = "migrated_live"
    KIBANA_ONLY = "kibana_only"
    SOURCE_ONLY = "source_only"
    MIGRATION_GAP = "migration_gap"


class FailureClass(str, Enum):
    INTERACTION_REGRESSION = "interaction_regression"
    QUERY_CONTRACT_ERROR = "query_contract_error"
    COVERAGE_GAP = "coverage_gap"
    CONTROL_NOT_FOUND = "control_not_found"
    OPTION_NOT_FOUND = "option_not_found"
    SELECTION_DID_NOT_STICK = "selection_did_not_stick"
    EXPECTED_REQUEST_MISSING = "expected_request_missing"
    UNEXPECTED_PANEL_REQUEST = "unexpected_panel_request"
    QUERY_CONTRACT_MISMATCH = "query_contract_mismatch"
    RENDER_ERROR = "render_error"
    UNEXPECTED_EMPTY = "unexpected_empty"
    DATA_GAP = "data_gap"
    FIELD_GAP = "field_gap"
    CONSOLE_ERROR = "console_error"
    SERVER_ERROR = "server_error"
    SETTLE_TIMEOUT = "settle_timeout"
    FRAMEWORK_ERROR = "framework_error"


_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "x-elastic-api-key",
        "api_key",
    }
)


def _serialize_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        serialized = [_serialize_value(item) for item in value]
        return tuple(serialized) if isinstance(value, tuple) else serialized
    return value


def _aggregate_status(results: Sequence[InteractionResult]) -> str:
    if any(result.status == InteractionStatus.FAIL for result in results):
        return "fail"
    if any(
        result.status == InteractionStatus.WARN
        or result.capability
        in (CapabilityCategory.SOURCE_ONLY, CapabilityCategory.MIGRATION_GAP)
        for result in results
    ):
        return "warn"
    return "pass"


@dataclass
class InteractionFinding:
    failure_class: FailureClass
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "failure_class": self.failure_class.value,
            "detail": self.detail,
        }


@dataclass
class NetworkEvidence:
    endpoint: str = ""
    method: str = ""
    status: int = 0
    url: str = ""
    query: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "method": self.method,
            "status": self.status,
            "url": self.url,
            "query": self.query,
            "headers": _serialize_value(self.headers),
            "body": self.body,
        }


@dataclass
class PanelEvidence:
    panel_id: str = ""
    title: str = ""
    status: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "panel_id": self.panel_id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class InteractionResult:
    name: str
    status: InteractionStatus
    capability: CapabilityCategory = CapabilityCategory.MIGRATED_LIVE
    findings: list[InteractionFinding] = field(default_factory=list)
    network: list[NetworkEvidence] = field(default_factory=list)
    panels: list[PanelEvidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status.value,
            "capability": self.capability.value,
            "findings": [finding.to_dict() for finding in self.findings],
            "network": [item.to_dict() for item in self.network],
            "panels": [item.to_dict() for item in self.panels],
        }


@dataclass
class InteractionReport:
    scenario: str
    results: list[InteractionResult] = field(default_factory=list)

    @property
    def status(self) -> str:
        return _aggregate_status(self.results)

    @property
    def exit_code(self) -> int:
        return 1 if self.status == "fail" else 0

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario": self.scenario,
            "status": self.status,
            "exit_code": self.exit_code,
            "results": [result.to_dict() for result in self.results],
        }


def _redact_url(value: str) -> str:
    if "://" not in value or "@" not in value:
        return value
    scheme, remainder = value.split("://", 1)
    credentials, _, location = remainder.rpartition("@")
    if not credentials or credentials == location:
        return value
    return f"{scheme}://{location}"


def redact_evidence(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]"
            if str(key).casefold() in _SENSITIVE_KEYS
            else redact_evidence(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_evidence(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_evidence(item) for item in value)
    if isinstance(value, str):
        return _redact_url(value)
    return value


def _allowance_value(allowance: Mapping[str, Any] | object, name: str) -> Any:
    if isinstance(allowance, Mapping):
        return allowance.get(name)
    return getattr(allowance, name, None)


def match_noise_allowance(
    endpoint: str,
    method: str,
    status: int,
    allowances: Sequence[Mapping[str, Any] | object],
) -> str | None:
    normalized_method = method.casefold()
    for allowance in allowances:
        allowance_endpoint = _allowance_value(allowance, "endpoint")
        allowance_method = _allowance_value(allowance, "method")
        allowance_status = _allowance_value(allowance, "status")
        rationale = _allowance_value(allowance, "rationale")
        if allowance_endpoint != endpoint:
            continue
        if str(allowance_method).casefold() != normalized_method:
            continue
        if allowance_status != status:
            continue
        if not rationale:
            continue
        return str(rationale)
    return None
