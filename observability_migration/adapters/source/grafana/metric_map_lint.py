# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Fail Grafana metric_map targets that already carry the active profile prefix."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from observability_migration.core.metric_mapping.entries import MetricMapEntry

_NATIVE = "prometheus_native"
_METRICS = "prometheus_metrics"
_REMOTE_WRITE = "prometheus_remote_write"

_PREFIX_BY_PROFILE = {
    _NATIVE: "metrics.",
    _METRICS: "prometheus.metrics.",
    _REMOTE_WRITE: "prometheus.",
}

_REMOTE_WRITE_LEAVES = (".counter", ".rate", ".value")


def grafana_metric_map_prefix_errors(
    metric_map: Mapping[str, Any] | None,
    namespacing_profile: str | None,
) -> list[str]:
    """Return operator-facing error lines; empty means OK."""
    prefix = _PREFIX_BY_PROFILE.get(str(namespacing_profile or ""))
    if not prefix or not metric_map:
        return []
    errors: list[str] = []
    for source, raw in metric_map.items():
        source_name = str(source).strip()
        for _, target in _entry_targets(source_name, raw):
            error = _error_for_target(source_name, target, str(namespacing_profile), prefix)
            if error:
                errors.append(error)
    return errors


def raise_if_grafana_metric_map_prefix_errors(
    metric_map: Mapping[str, Any] | None,
    namespacing_profile: str | None,
) -> None:
    errors = grafana_metric_map_prefix_errors(metric_map, namespacing_profile)
    if errors:
        raise ValueError("\n".join(errors))


def _entry_targets(source: str, raw: Any) -> list[tuple[str, str]]:
    if isinstance(raw, MetricMapEntry):
        found: list[tuple[str, str]] = []
        if raw.target:
            found.append((source, raw.target))
        for variant in raw.variants:
            if variant.target:
                found.append((source, variant.target))
        return found
    if isinstance(raw, str):
        target = raw.strip()
        return [(source, target)] if target else []
    if isinstance(raw, Mapping):
        found = []
        target = str(raw.get("target") or "").strip()
        if target:
            found.append((source, target))
        for variant in raw.get("variants") or []:
            found.extend(_entry_targets(source, variant))
        return found
    return []


def _error_for_target(
    source: str,
    target: str,
    profile: str,
    prefix: str,
) -> str | None:
    if not target.startswith(prefix):
        return None
    suggested = _suggested_logical_name(target, profile, prefix)
    would_emit = _would_emit(target, profile)
    return (
        f"Grafana metric_map target {target!r} for source {source!r} already "
        f"uses the {profile} prefix; the profile would emit {would_emit!r}. "
        f"Use the logical name {suggested!r} instead."
    )


def _suggested_logical_name(target: str, profile: str, prefix: str) -> str:
    logical = target[len(prefix) :] if target.startswith(prefix) else target
    if profile == _REMOTE_WRITE:
        for leaf in _REMOTE_WRITE_LEAVES:
            if logical.endswith(leaf):
                return logical[: -len(leaf)]
    return logical


def _would_emit(target: str, profile: str) -> str:
    if profile == _NATIVE:
        return f"metrics.{target}"
    if profile == _METRICS:
        return f"prometheus.metrics.{target}"
    return f"prometheus.{target}.value"
