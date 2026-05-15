# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from dataclasses import dataclass

from observability_migration.core.assets.query import QueryIR
from observability_migration.core.assets.target_query_contract import (
    FieldRequirement,
    TargetQueryContract,
)


@dataclass
class RuntimeCapabilities:
    promql: bool = False


def plan_grafana_metric_contract(
    query_ir: QueryIR,
    *,
    runtime_capabilities: RuntimeCapabilities,
) -> TargetQueryContract:
    source_language = str(query_ir.source_language or "").strip().lower()
    metric_name = str(query_ir.metric or "").strip()
    panel_type = str(query_ir.panel_type or "").strip().lower()
    range_function = str(query_ir.range_function or "").strip().lower()
    outer_agg = str(query_ir.outer_agg or "").strip().lower()

    if source_language == "promql" and runtime_capabilities.promql and (range_function or metric_name):
        return TargetQueryContract(
            canonical_target="promql",
            exactness_class="exact_if_contract_met",
            target_shape={"required_index_patterns": [query_ir.target_index or "metrics-*"]},
            field_requirements=[
                FieldRequirement(name=metric_name, role="metric")
            ]
            if metric_name
            else [],
            runtime_requirements={"source_command": "PROMQL"},
            degradation_policy={"fallback": "explicit_only"},
        )

    if range_function in {
        "rate",
        "irate",
        "increase",
        "avg_over_time",
        "sum_over_time",
        "max_over_time",
        "min_over_time",
        "count_over_time",
        "delta",
        "deriv",
    }:
        return TargetQueryContract(
            canonical_target="ts",
            exactness_class="exact_if_contract_met",
            target_shape={
                "required_index_patterns": [query_ir.target_index or "metrics-*"],
                "target_mode": "all_tsds",
                "mixed_forbidden": True,
            },
            field_requirements=[
                FieldRequirement(name=metric_name, role="metric", type_family="numeric")
            ]
            if metric_name
            else [],
            runtime_requirements={"source_command": "TS", "functions": [range_function.upper(), "TBUCKET"]},
            degradation_policy={"fallback": "explicit_only"},
            fulfillment_hints={"allow_index_narrowing": True},
        )

    if panel_type in {"table", "table-old"} and outer_agg == "count":
        return TargetQueryContract(
            canonical_target="from",
            exactness_class="exact_if_contract_met",
            target_shape={
                "required_index_patterns": [query_ir.target_index or "metrics-*"],
                "target_mode": "document_index",
            },
            field_requirements=[
                FieldRequirement(name=metric_name, role="metric", type_family="numeric")
            ]
            if metric_name
            else [],
            runtime_requirements={"source_command": "FROM"},
            degradation_policy={"fallback": "explicit_only"},
        )

    return TargetQueryContract(
        canonical_target="ts",
        exactness_class="exact_if_contract_met",
        target_shape={
            "required_index_patterns": [query_ir.target_index or "metrics-*"],
            "target_mode": "all_tsds",
            "mixed_forbidden": True,
        },
        field_requirements=[
            FieldRequirement(
                name=metric_name,
                role="metric",
                type_family="numeric",
                metric_kind="gauge",
            )
        ]
        if metric_name
        else [],
        runtime_requirements={"source_command": "TS", "functions": ["TBUCKET"]},
        degradation_policy={"fallback": "explicit_only"},
        fulfillment_hints={"allow_index_narrowing": True},
    )
