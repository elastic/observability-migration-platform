# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Shared source-metric → target-field mapping building blocks.

Authoring maps (human or LLM) is outside this module. We parse, classify,
apply exact renames, and apply class-2 mappings when emitters can honor
transform / filter / scale semantics.
"""

from __future__ import annotations

from .bindings import MetricBinding, binding_from_result, plan_rate_transform
from .entries import (
    CLASS_EXACT,
    CLASS_NONE,
    CLASS_REQUIRES_TRANSFORM,
    SCAFFOLD_PROVENANCE,
    VALID_TRANSFORMS,
    MetricMapEntry,
    MetricMapResult,
    classify_metric_map_entry,
    normalize_metric_map,
    parse_metric_map_entry,
    resolve_metric_map,
)
from .files import load_metric_map_files, load_tag_map_files
from .recording_rules import looks_like_recording_rule_metric
from .reporting import (
    attach_metric_map_to_contract,
    build_metric_map_summary,
    metric_map_summary_from_tracker,
)
from .scaffold import build_scaffold_yaml, collect_unmapped_source_metrics

__all__ = [
    "CLASS_EXACT",
    "CLASS_NONE",
    "CLASS_REQUIRES_TRANSFORM",
    "SCAFFOLD_PROVENANCE",
    "VALID_TRANSFORMS",
    "MetricBinding",
    "MetricMapEntry",
    "MetricMapResult",
    "attach_metric_map_to_contract",
    "binding_from_result",
    "build_metric_map_summary",
    "build_scaffold_yaml",
    "classify_metric_map_entry",
    "collect_unmapped_source_metrics",
    "load_metric_map_files",
    "load_tag_map_files",
    "looks_like_recording_rule_metric",
    "metric_map_summary_from_tracker",
    "normalize_metric_map",
    "parse_metric_map_entry",
    "plan_rate_transform",
    "resolve_metric_map",
]
