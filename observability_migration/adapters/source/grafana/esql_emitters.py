# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Registry of Grafana PromQL → ES|QL emitter paths for the translation harness."""

from __future__ import annotations

GRAFANA_ESQL_EMITTERS: tuple[str, ...] = (
    "single_target_formula",
    "join_family_ratio",
    "shared_measure_pipeline",
    "pretranslated_xy_merge",
    "same_metric_collapse",
)

EMITTER_HELPER_SYMBOLS: dict[str, str] = {
    "single_target_formula": "_build_formula_plan",
    "join_family_ratio": "join_family_rule",
    "shared_measure_pipeline": "_build_shared_measure_pipeline",
    "pretranslated_xy_merge": "_merge_pretranslated_xy_queries",
    "same_metric_collapse": "_try_collapse_same_metric_targets",
}
