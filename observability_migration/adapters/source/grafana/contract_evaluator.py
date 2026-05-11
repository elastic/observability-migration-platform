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

    for pattern in contract.target_shape.get("required_index_patterns", []):
        requirement = contract.target_shape.get("target_mode", "")
        pattern_info = (snapshot.target_patterns or {}).get(pattern, {})
        if requirement == "all_tsds" and not pattern_info.get("all_tsds", False):
            unsatisfied.append(f"{pattern} is not all-TSDS")

    for requirement in contract.field_requirements:
        capability = (snapshot.field_capabilities or {}).get(requirement.name)
        if capability is None:
            unsatisfied.append(f"missing field {requirement.name}")
            continue
        if requirement.type_family and capability.type_family != requirement.type_family:
            unsatisfied.append(f"{requirement.name} has type_family {capability.type_family}")
        if requirement.metric_kind and capability.time_series_metric_kind != requirement.metric_kind:
            unsatisfied.append(f"{requirement.name} is not marked as {requirement.metric_kind}")

    source_command = str(contract.runtime_requirements.get("source_command", "") or "")
    if source_command and not (snapshot.runtime_capabilities or {}).get(source_command, False):
        blocking.append(f"{source_command} runtime is unavailable")

    for fn in contract.runtime_requirements.get("functions", []):
        if not (snapshot.runtime_capabilities or {}).get(fn, False):
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
