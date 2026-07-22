# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Scaffold operator metric_map YAML from migration artifacts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

_METRIC_ROLES = frozenset({"metric", "aggregate"})
_PROMQL_METRIC_TOKEN_RE = re.compile(r"\b([A-Za-z_:][A-Za-z0-9_:]*)\s*(?=\{|\[)")


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    return payload if isinstance(payload, dict) else None


def _mapped_sources_from_contract(contract: dict[str, Any]) -> set[str]:
    mapped: set[str] = set()
    metric_map = contract.get("metric_map") or {}
    for entry in metric_map.get("applied") or []:
        if isinstance(entry, dict):
            source = str(entry.get("source", "") or "").strip()
            if source:
                mapped.add(source)
    for info in (contract.get("required_fields") or {}).values():
        if not isinstance(info, dict):
            continue
        mapped_from = info.get("mapped_from")
        if isinstance(mapped_from, str) and mapped_from.strip():
            mapped.add(mapped_from.strip())
        elif isinstance(mapped_from, list):
            mapped.update(str(item).strip() for item in mapped_from if str(item).strip())
    return mapped


def _source_metrics_from_contract(contract: dict[str, Any]) -> set[str]:
    metrics: set[str] = set()
    for info in (contract.get("required_fields") or {}).values():
        if not isinstance(info, dict):
            continue
        roles = {str(role) for role in (info.get("roles") or [])}
        if not roles & _METRIC_ROLES:
            continue
        for source in info.get("source_fields") or []:
            source_name = str(source or "").strip()
            if source_name:
                metrics.add(source_name)
    return metrics


def _source_metrics_from_manifest(manifest: dict[str, Any]) -> set[str]:
    metrics: set[str] = set()
    panel_lists = list(manifest.get("panels") or [])
    for dashboard in manifest.get("dashboards") or []:
        panel_lists.extend(dashboard.get("panels") or [])
    for panel in panel_lists:
        if not isinstance(panel, dict):
            continue
        query_ir = panel.get("query_ir") or {}
        if not isinstance(query_ir, dict):
            continue
        for key in ("source_metric", "metric"):
            value = str(query_ir.get(key, "") or "").strip()
            if value:
                metrics.add(value)
        expression = str(
            query_ir.get("source_expression", "")
            or query_ir.get("clean_expression", "")
            or panel.get("promql", "")
            or ""
        ).strip()
        if expression:
            metrics.update(match.group(1) for match in _PROMQL_METRIC_TOKEN_RE.finditer(expression))
    return metrics


def collect_unmapped_source_metrics(artifact_dir: str | Path) -> list[str]:
    """Return sorted source metric names that still need metric_map entries."""
    base = Path(artifact_dir)
    contracts = [
        payload
        for payload in (
            _load_json(base / "required_target_contract.json"),
            _load_json(base / "target_readiness_contract.json"),
        )
        if payload is not None
    ]
    manifest = _load_json(base / "migration_manifest.json")

    source_metrics: set[str] = set()
    mapped_sources: set[str] = set()
    for contract in contracts:
        source_metrics.update(_source_metrics_from_contract(contract))
        mapped_sources.update(_mapped_sources_from_contract(contract))
    if manifest is not None:
        source_metrics.update(_source_metrics_from_manifest(manifest))

    unmapped = sorted(name for name in source_metrics if name not in mapped_sources)
    return unmapped


def build_scaffold_yaml(artifact_dir: str | Path) -> str:
    """Build source-neutral metric_map scaffold YAML for unmapped metrics.

    Entries use empty ``target`` with ``provenance: scaffold``. They parse and
    load safely, but resolve as unapplied gaps until the operator fills each
    target (and optional Class-2 fields).
    """
    from .entries import SCAFFOLD_PROVENANCE

    unmapped = collect_unmapped_source_metrics(artifact_dir)
    payload: dict[str, Any] = {"metric_map": {}}
    for source in unmapped:
        payload["metric_map"][source] = {
            "target": "",
            "provenance": SCAFFOLD_PROVENANCE,
        }
    return yaml.safe_dump(payload, sort_keys=True, allow_unicode=True)
