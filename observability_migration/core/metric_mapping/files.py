# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Source-neutral metric_map YAML file loading."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import yaml

from .entries import MetricMapEntry, normalize_metric_map


def load_metric_map_files(paths: Sequence[str] | None) -> dict[str, MetricMapEntry]:
    """Load source-neutral ``metric_map`` YAML files.

    File shape:

    ```yaml
    metric_map:
      source.metric: target.metric
    ```

    Later files override earlier files for duplicate source metric keys.
    """
    entries: dict[str, MetricMapEntry] = {}
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
        if not isinstance(payload, dict) or "metric_map" not in payload:
            raise ValueError(
                f"Invalid metric map file {path}: expected a top-level 'metric_map' mapping"
            )
        metric_map = payload.get("metric_map")
        if not isinstance(metric_map, dict):
            raise ValueError(
                f"Invalid metric map file {path}: top-level 'metric_map' must be a mapping"
            )
        entries.update(normalize_metric_map(metric_map))
    return entries
