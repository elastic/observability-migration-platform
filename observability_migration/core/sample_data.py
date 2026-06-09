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
import time
from dataclasses import dataclass, field
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
    _SEEDER_TEMPLATE_PREFIX,
    IngestSummary,
    RequestFn,
    concrete_stream_name,
    generate_documents,
    ingest_documents,
    purge_foreign_streams,
    setup_templates_and_streams,
)


class NetworkError(RuntimeError):
    """Raised when the Elasticsearch endpoint cannot be reached at all."""


def make_es_request(
    es_url: str,
    api_key: str,
    *,
    verify: bool | str = True,
    timeout: int = 120,
    connect_timeout: float = 10.0,
    max_retries: int = 3,
    backoff_sec: float = 1.0,
) -> RequestFn:
    """Build a ``(method, path, body, content_type) -> dict`` ES request adapter.

    Routes through ``requests`` so the resolved ``verify`` value (system bundle,
    custom CA path, or ``False`` for --insecure) is applied uniformly. Raises
    ``NetworkError`` when the endpoint is unreachable; HTTP error responses are
    returned as parsed bodies so callers' ``_raise_on_error`` can surface them.

    Reliability: the request uses a ``(connect_timeout, read_timeout)`` tuple
    rather than a single scalar. A scalar ``requests`` timeout is a *per-read*
    deadline, not a total one -- a load balancer that trickles bytes resets the
    read timer indefinitely, so a stalled bulk can hang forever. The explicit
    read timeout makes that deterministic, and transient ``Timeout`` /
    ``ConnectionError`` failures are retried up to ``max_retries`` times with a
    linear backoff before raising ``NetworkError`` (so one stalled bulk neither
    hangs the seed nor fails it on a single blip).
    """
    base = es_url.rstrip("/")
    headers = {"Authorization": f"ApiKey {api_key}"}
    read_timeout = float(timeout)
    request_timeout = (float(connect_timeout), read_timeout)
    attempts = max(1, max_retries + 1)

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

        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = requests.request(
                    method, url, data=data, headers=send_headers,
                    verify=verify, timeout=request_timeout,
                )
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                # Transient: stalled read, dropped socket, or refused connect.
                # Retry a bounded number of times so a single blip doesn't sink
                # a long seed, but never loop forever.
                last_exc = exc
                if attempt < attempts - 1:
                    time.sleep(backoff_sec * (attempt + 1))
                    continue
                raise NetworkError(str(exc)) from exc
            except requests.exceptions.RequestException as exc:
                # Non-transient (malformed URL, TLS verify failure, etc.): do
                # not retry -- surface immediately.
                raise NetworkError(str(exc)) from exc
        else:  # pragma: no cover - defensive; loop either breaks or raises
            raise NetworkError(str(last_exc) if last_exc else "request failed")

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


@dataclass
class RemoveSummary:
    dry_run: bool
    deleted_streams: list[str] = field(default_factory=list)
    skipped_not_owned: list[str] = field(default_factory=list)
    deleted_templates: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _classify_stream(listing: Any, concrete: str) -> str:
    """Classify a ``GET /_data_stream/<name>`` response.

    Returns one of ``"owned"`` / ``"foreign"`` / ``"absent"`` / ``"unverifiable"``.
    Anything we cannot positively read is ``"unverifiable"`` so the caller can fail
    closed and never delete a stream whose ownership was not confirmed.
    """
    if isinstance(listing, dict) and listing.get("data_streams"):
        entry = next(
            (e for e in listing["data_streams"] if isinstance(e, dict) and e.get("name") == concrete),
            None,
        )
        if entry is None:
            return "absent"
        if str(entry.get("template") or "").startswith(_SEEDER_TEMPLATE_PREFIX):
            return "owned"
        return "foreign"
    status = None
    if isinstance(listing, dict):
        status = listing.get("status") or (listing.get("error") or {}).get("status")
    if status == 404:
        return "absent"
    if isinstance(listing, dict) and (listing.get("error") or status):
        return "unverifiable"
    return "absent"


def remove_sample_data(
    artifact_dirs: list[Path],
    request: RequestFn,
    *,
    dry_run: bool = True,
) -> RemoveSummary:
    """Delete only the data streams/templates this seeder created for the artifacts.

    A concrete data stream is deleted only when ownership is positively verified
    (its backing index template starts with ``telemetry-data-``). Foreign streams
    are skipped, and any stream whose ownership cannot be read (a non-404 GET error)
    is left untouched and reported as an error — the teardown fails closed so real
    data sharing the same wildcard is never deleted. ``dry_run`` reports the plan
    without writing.
    """
    contract = _build_contract(artifact_dirs, None)
    summary = RemoveSummary(dry_run=dry_run)
    for index_pattern, stream in sorted((contract.get("streams") or {}).items()):
        concrete = concrete_stream_name(index_pattern, stream)
        template = f"{_SEEDER_TEMPLATE_PREFIX}{concrete}"
        listing = request("GET", f"/_data_stream/{concrete}", None, "application/json")
        state = _classify_stream(listing, concrete)
        if state == "foreign":
            summary.skipped_not_owned.append(concrete)
            continue
        if state == "unverifiable":
            summary.errors.append(
                f"could not verify ownership of data stream {concrete}; skipped (not deleted)"
            )
            continue
        # state is "owned" or "absent": safe to remove seeder artifacts.
        if state == "owned":
            summary.deleted_streams.append(concrete)
        summary.deleted_templates.append(template)
        if not dry_run:
            if state == "owned":
                stream_res = request("DELETE", f"/_data_stream/{concrete}", None, "application/json")
                if isinstance(stream_res, dict) and stream_res.get("error"):
                    summary.errors.append(f"delete data stream {concrete}: {stream_res['error']}")
            tmpl_res = request("DELETE", f"/_index_template/{template}", None, "application/json")
            if isinstance(tmpl_res, dict) and tmpl_res.get("error"):
                summary.errors.append(f"delete index template {template}: {tmpl_res['error']}")
    return summary
