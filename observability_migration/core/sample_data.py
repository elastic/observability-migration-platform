# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Package-native synthetic-data seeding/removal orchestration.

Wraps the primitives in ``core.telemetry_data`` and the contract builders in
``core.telemetry_contract`` so the ``obs-migrate seed-sample-data`` /
``remove-sample-data`` subcommands (and the thin ``scripts/setup_telemetry_data.py``
shim) share one implementation. ES traffic goes through a ``requests`` adapter
that honors the shared ``resolve_tls`` policy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

from observability_migration.core.telemetry_contract import (
    build_combined_telemetry_contract,
    build_telemetry_contract,
    merge_metric_kind_overrides,
    metric_kinds_from_prometheus_metadata,
)
from observability_migration.core.telemetry_data import (
    IngestSummary,
    RequestFn,
    generate_documents,
    ingest_documents,
    purge_foreign_streams,
    setup_templates_and_streams,
)


class NetworkError(RuntimeError):
    """Raised when the Elasticsearch endpoint cannot be reached at all."""


def make_es_request(es_url: str, api_key: str, *, verify: bool | str = True, timeout: int = 120) -> RequestFn:
    """Build a ``(method, path, body, content_type) -> dict`` ES request adapter.

    Routes through ``requests`` so the resolved ``verify`` value (system bundle,
    custom CA path, or ``False`` for --insecure) is applied uniformly. Raises
    ``NetworkError`` when the endpoint is unreachable; HTTP error responses are
    returned as parsed bodies so callers' ``_raise_on_error`` can surface them.
    """
    base = es_url.rstrip("/")
    headers = {"Authorization": f"ApiKey {api_key}"}

    def request(method: str, path: str, body: Any | None = None, content_type: str = "application/json") -> dict[str, Any]:
        url = f"{base}{path}"
        data: bytes | None = None
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                data = bytes(body)
            elif isinstance(body, str):
                data = body.encode()
            else:
                data = json.dumps(body).encode()
        send_headers = dict(headers)
        if content_type:
            send_headers["Content-Type"] = content_type
        try:
            resp = requests.request(method, url, data=data, headers=send_headers, verify=verify, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            raise NetworkError(str(exc)) from exc
        if resp.status_code == 404 and method == "DELETE":
            return {"acknowledged": True}
        text = resp.text
        if not text:
            return {"acknowledged": True} if resp.ok else {"error": {"status": resp.status_code}}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"error": {"status": resp.status_code, "reason": text[:300]}}
        if isinstance(parsed, dict):
            return parsed
        return {"response": parsed}

    return request


def _fetch_prometheus_metadata(prometheus_url: str, *, verify: bool | str = True) -> dict[str, Any]:
    """Fetch Prometheus ``/api/v1/metadata``; return ``{}`` on any failure."""
    url = f"{prometheus_url.rstrip('/')}/api/v1/metadata"
    try:
        resp = requests.get(url, verify=verify, timeout=30)
        return resp.json() if resp.ok and resp.text else {}
    except (requests.RequestException, ValueError):
        return {}


def load_metric_kind_overrides(
    rules_files: list[str] | None,
    prometheus_url: str = "",
    *,
    verify: bool | str = True,
) -> dict[str, str]:
    """Build an authoritative metric-kind override map.

    Composes (most authoritative first) rule-pack ``metric_kinds`` and, when a
    Prometheus URL is given, live ``/api/v1/metadata`` types. Returns an empty
    map when no source yields anything so the contract falls back to inference.
    """
    rule_pack_kinds: dict[str, str] = {}
    if rules_files:
        from observability_migration.adapters.source.grafana.rules import load_rule_pack_files

        pack = load_rule_pack_files(rules_files)
        rule_pack_kinds = dict(getattr(pack, "metric_kinds", {}) or {})

    metadata_kinds: dict[str, str] = {}
    if prometheus_url:
        metadata_kinds = metric_kinds_from_prometheus_metadata(
            _fetch_prometheus_metadata(prometheus_url, verify=verify)
        )

    return merge_metric_kind_overrides(rule_pack_kinds, metadata_kinds)


def _build_contract(artifact_dirs: list[Path], metric_kind_overrides: dict[str, str] | None) -> dict[str, Any]:
    if len(artifact_dirs) == 1:
        return build_telemetry_contract(artifact_dirs[0], metric_kind_overrides=metric_kind_overrides)
    return build_combined_telemetry_contract(artifact_dirs, metric_kind_overrides=metric_kind_overrides)


def seed_sample_data(
    artifact_dirs: list[Path],
    request: RequestFn,
    *,
    data_hours: float,
    interval_sec: int,
    batch_docs: int,
    max_combinations: int,
    no_recreate: bool = False,
    purge_foreign: bool = False,
    metric_kind_overrides: dict[str, str] | None = None,
) -> IngestSummary:
    """Build a contract from artifacts, set up streams, and ingest synthetic docs."""
    contract = _build_contract(artifact_dirs, metric_kind_overrides)
    streams = contract.get("streams") or {}
    if not streams:
        raise RuntimeError("no telemetry requirements discovered in the artifact directories")
    if purge_foreign:
        purge_foreign_streams(contract, request)
    if not no_recreate:
        setup_templates_and_streams(contract, request, recreate=True)
    return ingest_documents(
        generate_documents(
            contract,
            data_hours=data_hours,
            interval_sec=interval_sec,
            max_combinations=max_combinations,
        ),
        request,
        batch_docs=batch_docs,
    )
