# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Native Dashboard-as-Code review artifacts.

Persists the exact typed Kibana Dashboards API payload (and, optionally, the
semantic ``DashboardIR`` it was derived from) that ``migrate --upload``
otherwise only holds in memory. This restores the pre-typed-API
review-before-upload workflow -- inspect what will be uploaded before it is
uploaded -- without reviving the legacy YAML-to-NDJSON compile step: native
Dashboard-as-Code does not need compilation, it needs a stable, inspectable,
uploadable artifact. See ``docs/architecture/asset-model.md``.

Per dashboard, a migration run writes:

- ``native/<stem>.native.json`` -- the exact ``NativeDashboard.to_api_payload()``
  body plus mapping stats, wrapped in a small versioned envelope. This is
  what ``obs-migrate upload --artifact-dir ... --artifact-format native``
  sends to Kibana, unchanged (see ``dashboards_api.upload_native_artifact``).
- ``ir/<stem>.ir.json`` -- the semantic ``DashboardIR`` both the native
  payload and the on-disk YAML are derived from, for reviewers who want
  translator decisions rather than the final API shape.

And once per run:

- ``native/index.json`` -- a small index over every native artifact written
  in the run, so tooling does not need to scrape filenames.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

NATIVE_ARTIFACT_KIND = "native_dashboard"
IR_ARTIFACT_KIND = "dashboard_ir"
NATIVE_ARTIFACT_INDEX_KIND = "native_dashboard_index"
ARTIFACT_ENVELOPE_VERSION = 1

NATIVE_ARTIFACT_SUFFIX = ".native.json"
IR_ARTIFACT_SUFFIX = ".ir.json"
NATIVE_ARTIFACT_DIRNAME = "native"
IR_ARTIFACT_DIRNAME = "ir"
NATIVE_ARTIFACT_INDEX_NAME = "index.json"


def json_safe(value: Any) -> Any:
    """Recursively normalize a value tree for ``json.dumps``.

    ``DashboardIR.to_dict()`` builds on ``dataclasses.asdict``, which leaves
    ``AssetStatus`` (a ``str``-backed ``Enum``) members as enum instances
    rather than their plain string value, and never runs on values that
    are not themselves dataclass fields. This walks the resulting tree so
    callers never have to special-case enums -- or, defensively, a stray
    dataclass/set that slips in -- before serializing.
    """
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def build_native_artifact(
    *,
    dashboard_ir: Any,
    native_dashboard: Any,
    native_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``native/<stem>.native.json`` envelope for one dashboard.

    ``payload`` is exactly ``native_dashboard.to_api_payload()`` -- the same
    body ``migrate --upload`` sends immediately -- so a delayed
    ``obs-migrate upload --artifact-dir ... --artifact-format native`` run
    deploys byte-for-byte what a reviewer inspected on disk.
    """
    stats = native_stats if isinstance(native_stats, dict) else {}
    title = str(getattr(dashboard_ir, "title", "") or getattr(native_dashboard, "title", "") or "")
    dashboard_id = str(getattr(native_dashboard, "dashboard_id", "") or "")
    source_adapter = str(getattr(dashboard_ir, "source_adapter", "") or "")
    return {
        "kind": NATIVE_ARTIFACT_KIND,
        "version": ARTIFACT_ENVELOPE_VERSION,
        "dashboard_id": dashboard_id,
        "title": title,
        "source_adapter": source_adapter,
        "payload": native_dashboard.to_api_payload(),
        "mapping": {
            "mapped": int(stats.get("mapped", 0) or 0),
            "unmapped": int(stats.get("unmapped", 0) or 0),
            "sections": int(stats.get("sections", 0) or 0),
            "controls": int(stats.get("controls", 0) or 0),
            "reasons": dict(stats.get("reasons") or {}),
        },
    }


def build_ir_artifact(dashboard_ir: Any) -> dict[str, Any]:
    """Build the ``ir/<stem>.ir.json`` envelope for one dashboard."""
    title = str(getattr(dashboard_ir, "title", "") or "")
    source_adapter = str(getattr(dashboard_ir, "source_adapter", "") or "")
    to_dict = getattr(dashboard_ir, "to_dict", None)
    raw = to_dict() if callable(to_dict) else {}
    return {
        "kind": IR_ARTIFACT_KIND,
        "version": ARTIFACT_ENVELOPE_VERSION,
        "title": title,
        "source_adapter": source_adapter,
        "dashboard_ir": json_safe(raw),
    }


def write_native_artifact(
    *,
    dashboard_ir: Any,
    native_dashboard: Any,
    native_stats: dict[str, Any] | None,
    native_dir: Path,
    stem: str,
) -> Path:
    """Build and persist one dashboard's native artifact under ``native_dir``."""
    native_dir = Path(native_dir)
    native_dir.mkdir(parents=True, exist_ok=True)
    artifact = build_native_artifact(
        dashboard_ir=dashboard_ir,
        native_dashboard=native_dashboard,
        native_stats=native_stats,
    )
    path = native_dir / f"{stem}{NATIVE_ARTIFACT_SUFFIX}"
    path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    return path


def write_ir_artifact(*, dashboard_ir: Any, ir_dir: Path, stem: str) -> Path:
    """Build and persist one dashboard's IR export under ``ir_dir``."""
    ir_dir = Path(ir_dir)
    ir_dir.mkdir(parents=True, exist_ok=True)
    artifact = build_ir_artifact(dashboard_ir)
    path = ir_dir / f"{stem}{IR_ARTIFACT_SUFFIX}"
    path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    return path


def write_native_artifact_index(native_dir: Path, entries: list[dict[str, Any]]) -> Path:
    """Write ``native/index.json``: one row per dashboard emitted in the run.

    ``entries`` are small dicts (``stem``, ``title``, ``dashboard_id``,
    ``native_path``, ``ir_path``), expected to already be relative to the
    dashboard artifact root so the index stays portable if the run
    directory is moved or zipped for review.
    """
    native_dir = Path(native_dir)
    native_dir.mkdir(parents=True, exist_ok=True)
    index = {
        "kind": NATIVE_ARTIFACT_INDEX_KIND,
        "version": ARTIFACT_ENVELOPE_VERSION,
        "dashboards": list(entries),
    }
    path = native_dir / NATIVE_ARTIFACT_INDEX_NAME
    path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    return path


__all__ = [
    "ARTIFACT_ENVELOPE_VERSION",
    "IR_ARTIFACT_DIRNAME",
    "IR_ARTIFACT_KIND",
    "IR_ARTIFACT_SUFFIX",
    "NATIVE_ARTIFACT_DIRNAME",
    "NATIVE_ARTIFACT_INDEX_KIND",
    "NATIVE_ARTIFACT_INDEX_NAME",
    "NATIVE_ARTIFACT_KIND",
    "NATIVE_ARTIFACT_SUFFIX",
    "build_ir_artifact",
    "build_native_artifact",
    "json_safe",
    "write_ir_artifact",
    "write_native_artifact",
    "write_native_artifact_index",
]
