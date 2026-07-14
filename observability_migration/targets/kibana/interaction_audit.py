# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Interaction-audit verdict and evidence contract for dashboard control testing.

Pure dataclasses and helpers that classify browser interaction outcomes,
aggregate scenario reports, redact sensitive evidence, and match narrow network
noise allowances. No browser driver or I/O dependencies.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit


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


class EvidenceParseError(ValueError):
    """Raised when ES|QL request evidence cannot be parsed safely."""


_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "x-elastic-api-key",
        "api_key",
    }
)

_ESQL_PATH_PREFIX = "/internal/search/esql"
_VALUE_PARAM_TOKEN = re.compile(r"(?<!\?)\?(?!\?)([A-Za-z_][A-Za-z0-9_]*)")
_IDENTIFIER_PARAM_TOKEN = re.compile(r"\?\?([A-Za-z_][A-Za-z0-9_]*)")


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


def _header_value(headers: Mapping[str, str], name: str) -> str:
    target = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == target:
            return str(value)
    return ""


def _copy_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(key): str(value) for key, value in headers.items()}


def _is_esql_endpoint(url: str) -> bool:
    path = urlsplit(url).path
    if not path.startswith(_ESQL_PATH_PREFIX):
        return False
    if path == _ESQL_PATH_PREFIX:
        return True
    suffix = path[len(_ESQL_PATH_PREFIX) :]
    return suffix.startswith(("/", "_"))


def _parse_kbn_context(raw: str) -> tuple[str, str]:
    if not raw:
        return "", ""
    try:
        decoded = unquote(raw)
        payload = json.loads(decoded)
    except (json.JSONDecodeError, TypeError, ValueError):
        return "", ""
    if not isinstance(payload, Mapping):
        return "", ""
    child = payload.get("child")
    if not isinstance(child, Mapping):
        return "", ""
    panel_id = str(child.get("id") or "")
    panel_title = str(child.get("description") or "")
    return panel_id, panel_title


def _param_token_sets(query: str) -> tuple[set[str], set[str]]:
    value_names = set(_VALUE_PARAM_TOKEN.findall(query))
    identifier_names = set(_IDENTIFIER_PARAM_TOKEN.findall(query))
    dual = value_names & identifier_names
    if dual:
        names = ", ".join(sorted(dual))
        raise EvidenceParseError(f"ambiguous dual query tokens for param(s): {names}")
    return value_names, identifier_names


def _kind_for_param_name(
    name: str,
    *,
    value_names: set[str],
    identifier_names: set[str],
) -> str:
    if name in identifier_names:
        return "identifier"
    if name in value_names:
        return "value"
    return "value"


def _merge_named_params(
    raw: object,
    query: str,
) -> tuple[dict[str, object], dict[str, str]]:
    """Merge wire-format params; infer identifier vs value kinds from query tokens.

    Live Kibana sends plain values such as ``[{"grouping": "host.name"}]``; the
    ``??grouping`` token marks identifier semantics. The legacy ``{"identifier": ...}``
    wrapper remains accepted when it does not conflict with a value token.
    """
    value_names, identifier_names = _param_token_sets(query)
    if raw is None:
        return {}, {}
    entries: list[tuple[str, object]]
    if isinstance(raw, Mapping):
        entries = [(str(name), value) for name, value in raw.items()]
    elif isinstance(raw, list):
        entries = []
        for item in raw:
            if not isinstance(item, Mapping) or len(item) != 1:
                raise EvidenceParseError("params entry must be a single-key mapping")
            name, value = next(iter(item.items()))
            entries.append((str(name), value))
    else:
        raise EvidenceParseError("params must be a mapping or list of single-key mappings")

    params: dict[str, object] = {}
    param_kinds: dict[str, str] = {}
    for name, value in entries:
        if not name:
            raise EvidenceParseError("param name must not be empty")
        if name in params:
            raise EvidenceParseError(f"duplicate param name: {name}")
        if isinstance(value, bool):
            raise EvidenceParseError(f"boolean param values are not supported: {name}")
        if isinstance(value, Mapping):
            if set(value.keys()) != {"identifier"}:
                raise EvidenceParseError(f"unsupported identifier wrapper for param: {name}")
            identifier = value.get("identifier")
            if not isinstance(identifier, str) or not identifier:
                raise EvidenceParseError(f"identifier param must be a non-empty string: {name}")
            if name in value_names:
                raise EvidenceParseError(
                    f"identifier wrapper conflicts with value token ?{name} for param: {name}"
                )
            params[name] = identifier
            param_kinds[name] = "identifier"
            continue
        if isinstance(value, list):
            if any(isinstance(item, bool) for item in value):
                raise EvidenceParseError(f"boolean list values are not supported: {name}")
            params[name] = list(value)
            param_kinds[name] = _kind_for_param_name(
                name,
                value_names=value_names,
                identifier_names=identifier_names,
            )
            continue
        if value is None or isinstance(value, (str, int, float)):
            params[name] = value
            param_kinds[name] = _kind_for_param_name(
                name,
                value_names=value_names,
                identifier_names=identifier_names,
            )
            continue
        raise EvidenceParseError(f"unsupported param value type for: {name}")

    return params, param_kinds


def _serializable_body(body: Mapping[str, object] | None) -> object:
    if body is None:
        return ""
    return _serialize_value(dict(body))


def parse_esql_request(
    *,
    url: str,
    method: str,
    headers: Mapping[str, str],
    body: Mapping[str, object] | None,
) -> NetworkEvidence | None:
    if method.casefold() != "post" or not _is_esql_endpoint(url):
        return None

    path = urlsplit(url).path
    panel_id, panel_title = _parse_kbn_context(_header_value(headers, "x-kbn-context"))
    opaque_id = _header_value(headers, "x-opaque-id")

    query = ""
    params: dict[str, object] = {}
    param_kinds: dict[str, str] = {}
    if body is not None:
        if "query" in body:
            raw_query = body.get("query")
            if raw_query is None:
                query = ""
            elif isinstance(raw_query, str):
                query = raw_query
            else:
                raise EvidenceParseError("query must be a string")
        if query:
            _param_token_sets(query)
        if "params" in body:
            params, param_kinds = _merge_named_params(body.get("params"), query)

    return NetworkEvidence(
        endpoint=path,
        method=method.upper(),
        status=0,
        url=url,
        query=query,
        headers=_copy_headers(headers),
        body=_serializable_body(body),
        panel_id=panel_id,
        panel_title=panel_title,
        opaque_id=opaque_id,
        params=params,
        param_kinds=param_kinds,
    )


def _is_successful_status(status: int) -> bool:
    return 200 <= status <= 299


_MAX_SAFE_ERROR_TEXT = 2048
_ESQL_ENVELOPE_KEYS = ("rawResponse", "response", "data")


def _bound_safe_text(value: object, *, limit: int = _MAX_SAFE_ERROR_TEXT) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return text[:limit]


def _unwrap_esql_response_body(body: object) -> object:
    current = body
    seen: set[int] = set()
    while isinstance(current, Mapping):
        marker_id = id(current)
        if marker_id in seen:
            break
        seen.add(marker_id)
        if any(key in current for key in ("columns", "values", "error")):
            return current
        unwrapped = False
        for key in _ESQL_ENVELOPE_KEYS:
            nested = current.get(key)
            if isinstance(nested, Mapping):
                current = nested
                unwrapped = True
                break
        if not unwrapped:
            return current
    return current


def _response_columns_from_body(body: Mapping[str, object]) -> tuple[str, ...]:
    raw_columns = body.get("columns")
    if not isinstance(raw_columns, list):
        return ()
    if not raw_columns and isinstance(body.get("all_columns"), list):
        raw_columns = body["all_columns"]
    names: list[str] = []
    for item in raw_columns:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(names)


def _row_count_from_body(body: Mapping[str, object]) -> int:
    values = body.get("values")
    if isinstance(values, list):
        return len(values)
    return -1


def _error_text_from_body(body: object) -> str:
    if not isinstance(body, Mapping):
        return ""
    error = body.get("error")
    if isinstance(error, Mapping):
        for key in ("reason", "message"):
            candidate = error.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return _bound_safe_text(candidate)
    if isinstance(error, str) and error.strip():
        return _bound_safe_text(error)
    return ""


def enrich_esql_response(
    evidence: NetworkEvidence,
    *,
    status: int,
    body: object,
    error: str = "",
) -> NetworkEvidence:
    """Return response-enriched network evidence without mutating the request."""
    unwrapped = _unwrap_esql_response_body(body)
    response_columns: tuple[str, ...] = ()
    row_count = -1
    resolved_error = _bound_safe_text(error)
    if isinstance(unwrapped, Mapping):
        response_columns = _response_columns_from_body(unwrapped)
        row_count = _row_count_from_body(unwrapped)
        if not resolved_error:
            resolved_error = _error_text_from_body(unwrapped)
    return replace(
        evidence,
        status=status,
        response_columns=response_columns,
        row_count=row_count,
        error=resolved_error,
    )


def _query_has_value_token(query: str, name: str) -> bool:
    return any(match == name for match in _VALUE_PARAM_TOKEN.findall(query))


def _query_has_identifier_token(query: str, name: str) -> bool:
    return any(match == name for match in _IDENTIFIER_PARAM_TOKEN.findall(query))


def _query_contains_fragment(query: str, fragment: str) -> bool:
    value_token = re.fullmatch(r"\?([A-Za-z_][A-Za-z0-9_]*)", fragment)
    if value_token is not None:
        return _query_has_value_token(query, value_token.group(1))
    identifier_token = re.fullmatch(r"\?\?([A-Za-z_][A-Za-z0-9_]*)", fragment)
    if identifier_token is not None:
        return _query_has_identifier_token(query, identifier_token.group(1))
    return fragment in query


def _append_finding(
    findings: list[InteractionFinding],
    seen: set[tuple[str, str]],
    failure_class: FailureClass,
    detail: str,
) -> None:
    key = (failure_class.value, detail)
    if key in seen:
        return
    seen.add(key)
    findings.append(InteractionFinding(failure_class, detail))


def _contract_violations_for_evidence(
    item: NetworkEvidence,
    panel_label: str,
    *,
    query_contains: Sequence[str],
    query_not_contains: Sequence[str],
    value_params: Mapping[str, object],
    identifier_params: Mapping[str, str],
    required_columns: Sequence[str],
    stable_alias: str,
    minimum_rows: int,
) -> list[InteractionFinding]:
    violations: list[InteractionFinding] = []
    for fragment in query_contains:
        if not _query_contains_fragment(item.query, fragment):
            violations.append(
                InteractionFinding(
                    FailureClass.QUERY_CONTRACT_ERROR,
                    f"panel {panel_label}: expected query fragment {fragment!r} not found",
                )
            )
    for fragment in query_not_contains:
        if _query_contains_fragment(item.query, fragment):
            violations.append(
                InteractionFinding(
                    FailureClass.QUERY_CONTRACT_ERROR,
                    f"panel {panel_label}: forbidden query fragment {fragment!r} found",
                )
            )

    for name, expected_value in value_params.items():
        actual_value = item.params.get(name)
        actual_kind = item.param_kinds.get(name)
        if actual_kind != "value" or actual_value != expected_value:
            violations.append(
                InteractionFinding(
                    FailureClass.QUERY_CONTRACT_ERROR,
                    f"panel {panel_label}: param {name} expected value {expected_value!r}",
                )
            )
        if not _query_has_value_token(item.query, name):
            violations.append(
                InteractionFinding(
                    FailureClass.QUERY_CONTRACT_ERROR,
                    f"panel {panel_label}: query missing value token ?{name}",
                )
            )
        if _query_has_identifier_token(item.query, name):
            violations.append(
                InteractionFinding(
                    FailureClass.QUERY_CONTRACT_ERROR,
                    f"panel {panel_label}: param {name} bound as identifier token ??{name}",
                )
            )

    for name, expected_identifier in identifier_params.items():
        actual_value = item.params.get(name)
        actual_kind = item.param_kinds.get(name)
        if actual_kind != "identifier" or actual_value != expected_identifier:
            violations.append(
                InteractionFinding(
                    FailureClass.QUERY_CONTRACT_ERROR,
                    f"panel {panel_label}: param {name} expected identifier {expected_identifier!r}",
                )
            )
        if not _query_has_identifier_token(item.query, name):
            violations.append(
                InteractionFinding(
                    FailureClass.QUERY_CONTRACT_ERROR,
                    f"panel {panel_label}: query missing identifier token ??{name}",
                )
            )
        if _query_has_value_token(item.query, name):
            violations.append(
                InteractionFinding(
                    FailureClass.QUERY_CONTRACT_ERROR,
                    f"panel {panel_label}: param {name} bound as value token ?{name}",
                )
            )

    for column in required_columns:
        if column not in item.response_columns:
            violations.append(
                InteractionFinding(
                    FailureClass.QUERY_CONTRACT_ERROR,
                    f"panel {panel_label}: missing response column {column!r}",
                )
            )

    if stable_alias and stable_alias not in item.response_columns:
        violations.append(
            InteractionFinding(
                FailureClass.QUERY_CONTRACT_ERROR,
                f"panel {panel_label}: missing stable alias column {stable_alias!r}",
            )
        )

    if minimum_rows > 0 and item.row_count < minimum_rows:
        violations.append(
            InteractionFinding(
                FailureClass.QUERY_CONTRACT_ERROR,
                f"panel {panel_label}: row_count {item.row_count} below minimum {minimum_rows}",
            )
        )

    return violations


def check_network_contract(
    *,
    expected_panel_ids: Collection[str],
    unaffected_panel_ids: Collection[str],
    evidence: Sequence[NetworkEvidence],
    query_contains: Sequence[str] = (),
    query_not_contains: Sequence[str] = (),
    expected_value_params: Mapping[str, object] | None = None,
    expected_identifier_params: Mapping[str, str] | None = None,
    required_columns: Sequence[str] = (),
    stable_alias: str = "",
    minimum_rows: int = 0,
) -> list[InteractionFinding]:
    findings: list[InteractionFinding] = []
    seen: set[tuple[str, str]] = set()
    expected = set(expected_panel_ids)
    unaffected = set(unaffected_panel_ids)
    value_params = dict(expected_value_params or {})
    identifier_params = dict(expected_identifier_params or {})

    successful_by_panel: dict[str, list[NetworkEvidence]] = {panel_id: [] for panel_id in expected}
    for item in evidence:
        if _is_successful_status(item.status):
            if not item.panel_id:
                _append_finding(
                    findings,
                    seen,
                    FailureClass.FRAMEWORK_ERROR,
                    "ES|QL response could not be correlated to a panel",
                )
            if item.panel_id in unaffected:
                _append_finding(
                    findings,
                    seen,
                    FailureClass.UNEXPECTED_PANEL_REQUEST,
                    f"panel {item.panel_id}: unexpected successful ES|QL request",
                )
            if item.panel_id in expected:
                successful_by_panel.setdefault(item.panel_id, []).append(item)
            continue

        if item.status == 0:
            continue

        if 500 <= item.status <= 599:
            panel_label = item.panel_id or "unknown"
            _append_finding(
                findings,
                seen,
                FailureClass.SERVER_ERROR,
                f"panel {panel_label}: server error status {item.status}",
            )
            continue

        if item.panel_id in expected:
            panel_label = item.panel_id or "unknown"
            _append_finding(
                findings,
                seen,
                FailureClass.QUERY_CONTRACT_ERROR,
                f"panel {panel_label}: non-success status {item.status}",
            )

    for panel_id in expected_panel_ids:
        panel_successes = successful_by_panel.get(panel_id, [])
        if not panel_successes:
            _append_finding(
                findings,
                seen,
                FailureClass.EXPECTED_REQUEST_MISSING,
                f"panel {panel_id}: expected ES|QL request missing",
            )
            continue

        matched = False
        for item in panel_successes:
            panel_label = item.panel_id or panel_id
            if not _contract_violations_for_evidence(
                item,
                panel_label,
                query_contains=query_contains,
                query_not_contains=query_not_contains,
                value_params=value_params,
                identifier_params=identifier_params,
                required_columns=required_columns,
                stable_alias=stable_alias,
                minimum_rows=minimum_rows,
            ):
                matched = True
                break

        if matched:
            continue

        latest = panel_successes[-1]
        panel_label = latest.panel_id or panel_id
        for violation in _contract_violations_for_evidence(
            latest,
            panel_label,
            query_contains=query_contains,
            query_not_contains=query_not_contains,
            value_params=value_params,
            identifier_params=identifier_params,
            required_columns=required_columns,
            stable_alias=stable_alias,
            minimum_rows=minimum_rows,
        ):
            _append_finding(findings, seen, violation.failure_class, violation.detail)

    return findings


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
    body: object = ""
    panel_id: str = ""
    panel_title: str = ""
    opaque_id: str = ""
    params: dict[str, object] = field(default_factory=dict)
    param_kinds: dict[str, str] = field(default_factory=dict)
    response_columns: tuple[str, ...] = ()
    row_count: int = -1
    error: str = ""

    @property
    def status_code(self) -> int:
        return self.status

    def to_dict(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "method": self.method,
            "status": self.status,
            "status_code": self.status_code,
            "url": self.url,
            "query": self.query,
            "headers": _serialize_value(self.headers),
            "body": _serialize_value(self.body),
            "panel_id": self.panel_id,
            "panel_title": self.panel_title,
            "opaque_id": self.opaque_id,
            "params": _serialize_value(self.params),
            "param_kinds": _serialize_value(self.param_kinds),
            "response_columns": list(self.response_columns),
            "row_count": self.row_count,
            "error": self.error,
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
    if "://" not in value:
        return value
    parts = urlsplit(value)
    if "@" not in parts.netloc:
        return value
    _userinfo, _separator, hostport = parts.netloc.rpartition("@")
    if not hostport:
        return value
    return urlunsplit((parts.scheme, hostport, parts.path, parts.query, parts.fragment))


_CONTRACT_EVIDENCE_TYPES = (
    InteractionFinding,
    NetworkEvidence,
    PanelEvidence,
    InteractionResult,
    InteractionReport,
)


def redact_evidence(value: Any) -> Any:
    if isinstance(value, _CONTRACT_EVIDENCE_TYPES):
        return redact_evidence(value.to_dict())
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
        if rationale is None:
            continue
        cleaned = str(rationale).strip()
        if not cleaned:
            continue
        return cleaned
    return None
