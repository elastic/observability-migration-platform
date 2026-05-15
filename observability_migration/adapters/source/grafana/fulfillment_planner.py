# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from observability_migration.core.assets.target_query_contract import FulfillmentAction, FulfillmentPlan


def _unsatisfied_index_patterns(unsatisfied_reasons):
    suffix = " is not all-TSDS"
    patterns = []
    for reason in unsatisfied_reasons:
        if reason.endswith(suffix):
            patterns.append(reason[: -len(suffix)])
    return patterns


def _unsatisfied_metric_kind_requirements(unsatisfied_reasons):
    marker = " is not marked as "
    requirements = []
    for reason in unsatisfied_reasons:
        field_name, separator, metric_kind = reason.partition(marker)
        if separator and field_name and metric_kind:
            requirements.append((field_name, metric_kind))
    return requirements


def plan_contract_fulfillment(contract, evaluation):
    if evaluation.status != "exact_after_fulfillment":
        return FulfillmentPlan(status="not_required")

    actions = []
    unsatisfied_patterns = _unsatisfied_index_patterns(evaluation.unsatisfied)
    if unsatisfied_patterns and contract.fulfillment_hints.get("allow_index_narrowing"):
        actions.append(
            FulfillmentAction(
                kind="narrow_index_pattern",
                description="Narrow the target pattern to an all-TSDS subset before executing TS queries.",
                payload={"required_index_patterns": unsatisfied_patterns},
            )
        )

    for field_name, metric_kind in _unsatisfied_metric_kind_requirements(evaluation.unsatisfied):
        actions.append(
            FulfillmentAction(
                kind="require_metric_kind",
                description=f"Mark {field_name} as {metric_kind} in the target TSDS mapping.",
                payload={"field": field_name, "metric_kind": metric_kind},
            )
        )

    return FulfillmentPlan(status="required", actions=actions)
