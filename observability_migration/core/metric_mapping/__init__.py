# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Shared source-metric → target-field mapping building blocks.

Authoring maps (human or LLM) is outside this module. We parse, classify,
apply exact renames, and refuse silent class-2 bare renames.
"""

from __future__ import annotations

from .entries import (
    CLASS_EXACT,
    CLASS_NONE,
    CLASS_REQUIRES_TRANSFORM,
    VALID_TRANSFORMS,
    MetricMapEntry,
    MetricMapResult,
    classify_metric_map_entry,
    normalize_metric_map,
    parse_metric_map_entry,
    resolve_metric_map,
)
from .files import load_metric_map_files

__all__ = [
    "CLASS_EXACT",
    "CLASS_NONE",
    "CLASS_REQUIRES_TRANSFORM",
    "VALID_TRANSFORMS",
    "MetricMapEntry",
    "MetricMapResult",
    "classify_metric_map_entry",
    "load_metric_map_files",
    "normalize_metric_map",
    "parse_metric_map_entry",
    "resolve_metric_map",
]
