# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Source-neutral metric_map / tag_map YAML file loading."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import yaml

from .entries import MetricMapEntry, normalize_metric_map

_MAP_KEYS = ("metric_map", "tag_map")


def _load_payloads(paths: Sequence[str] | None) -> list[tuple[Path, dict]]:
    """Read and shape-validate each map file once.

    A file must carry at least one of ``metric_map`` / ``tag_map`` at the top
    level. Later files override earlier files for duplicate keys.
    """
    payloads: list[tuple[Path, dict]] = []
    for raw_path in paths or []:
        path = Path(str(raw_path))
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise ValueError(
                f"Metric map file not found or unreadable: {path}"
            ) from exc
        except yaml.YAMLError as exc:
            raise ValueError(
                f"Invalid metric map file {path}: YAML parse error: {exc}"
            ) from exc
        if not isinstance(payload, dict) or not any(key in payload for key in _MAP_KEYS):
            raise ValueError(
                f"Invalid metric map file {path}: expected a top-level "
                "'metric_map' and/or 'tag_map' mapping"
            )
        payloads.append((path, payload))
    return payloads


def load_metric_map_files(paths: Sequence[str] | None) -> dict[str, MetricMapEntry]:
    """Load the ``metric_map`` entries from source-neutral map YAML files.

    File shape:

    ```yaml
    metric_map:
      source.metric: target.metric
    tag_map:            # optional; see load_tag_map_files
      source_tag: target.field
    ```

    Later files override earlier files for duplicate source metric keys.
    """
    entries: dict[str, MetricMapEntry] = {}
    for path, payload in _load_payloads(paths):
        metric_map = payload.get("metric_map") or {}
        if not isinstance(metric_map, dict):
            raise ValueError(
                f"Invalid metric map file {path}: top-level 'metric_map' must be a mapping"
            )
        entries.update(normalize_metric_map(metric_map))
    return entries


def load_tag_map_files(paths: Sequence[str] | None) -> dict[str, str]:
    """Load the optional ``tag_map`` entries from source-neutral map YAML files.

    ``tag_map`` renames a source tag/label/attribute name to a target
    Elasticsearch field (e.g. ``host: host.name``). Later files override
    earlier files for duplicate tag keys.
    """
    tags: dict[str, str] = {}
    for path, payload in _load_payloads(paths):
        tag_map = payload.get("tag_map") or {}
        if not isinstance(tag_map, dict):
            raise ValueError(
                f"Invalid metric map file {path}: top-level 'tag_map' must be a mapping"
            )
        for source, target in tag_map.items():
            if not isinstance(source, str) or not isinstance(target, str):
                raise ValueError(
                    f"Invalid metric map file {path}: 'tag_map' must map string tag "
                    "names to string field names"
                )
            if not source.strip() or not target.strip():
                raise ValueError(
                    f"Invalid metric map file {path}: 'tag_map' keys and values "
                    "must be non-empty strings"
                )
            tags[source] = target
    return tags
