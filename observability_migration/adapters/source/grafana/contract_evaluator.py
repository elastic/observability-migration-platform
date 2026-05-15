# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from observability_migration.core.assets.target_query_contract import ContractEvaluation


def _is_fulfillable_unsatisfied(reason, contract):
    if reason.endswith(" is not all-TSDS"):
        return bool(contract.fulfillment_hints.get("allow_index_narrowing"))
    if " is not marked as " in reason:
        return True
    return False


def evaluate_target_query_contract(contract, snapshot):
    satisfied = []
    unsatisfied = []
    blocking = []

    target_mode = contract.target_shape.get("target_mode", "")
    for pattern in contract.target_shape.get("required_index_patterns", []):
        pattern_info = (snapshot.target_patterns or {}).get(pattern, {})
        if target_mode == "all_tsds":
            if pattern_info.get("all_tsds", False):
                satisfied.append(f"{pattern} is all-TSDS")
            else:
                unsatisfied.append(f"{pattern} is not all-TSDS")

    for requirement in contract.field_requirements:
        capability = (snapshot.field_capabilities or {}).get(requirement.name)
        if capability is None:
            unsatisfied.append(f"missing field {requirement.name}")
            continue
        if requirement.type_family:
            if capability.type_family == requirement.type_family:
                satisfied.append(f"{requirement.name} has type_family {capability.type_family}")
            else:
                unsatisfied.append(f"{requirement.name} has type_family {capability.type_family}")
        if requirement.metric_kind:
            if capability.time_series_metric_kind == requirement.metric_kind:
                satisfied.append(
                    f"{requirement.name} is marked as {capability.time_series_metric_kind or requirement.metric_kind}"
                )
            else:
                unsatisfied.append(f"{requirement.name} is not marked as {requirement.metric_kind}")

    source_command = str(contract.runtime_requirements.get("source_command", "") or "")
    if source_command:
        if (snapshot.runtime_capabilities or {}).get(source_command, False):
            satisfied.append(f"{source_command} runtime is available")
        else:
            blocking.append(f"{source_command} runtime is unavailable")

    for fn in contract.runtime_requirements.get("functions", []):
        if (snapshot.runtime_capabilities or {}).get(fn, False):
            satisfied.append(f"{fn} runtime is available")
        else:
            blocking.append(f"{fn} runtime is unavailable")

    if blocking:
        return ContractEvaluation(status="blocked", satisfied=satisfied, unsatisfied=unsatisfied, blocking=blocking)
    if not unsatisfied:
        return ContractEvaluation(status="exact_now", satisfied=satisfied, unsatisfied=unsatisfied, blocking=blocking)
    if all(_is_fulfillable_unsatisfied(reason, contract) for reason in unsatisfied):
        return ContractEvaluation(status="exact_after_fulfillment", satisfied=satisfied, unsatisfied=unsatisfied, blocking=blocking)
    if contract.degradation_policy.get("fallback") == "explicit_only":
        return ContractEvaluation(status="degraded_if_forced", satisfied=satisfied, unsatisfied=unsatisfied, blocking=blocking)
    return ContractEvaluation(status="blocked", satisfied=satisfied, unsatisfied=unsatisfied, blocking=blocking)
