# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from dataclasses import asdict, dataclass, field


@dataclass
class FieldRequirement:
    name: str
    role: str
    type_family: str = ""
    metric_kind: str = ""
    context: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TargetQueryContract:
    canonical_target: str
    exactness_class: str
    target_shape: dict = field(default_factory=dict)
    field_requirements: list[FieldRequirement] = field(default_factory=list)
    data_invariants: dict = field(default_factory=dict)
    runtime_requirements: dict = field(default_factory=dict)
    degradation_policy: dict = field(default_factory=dict)
    fulfillment_hints: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["field_requirements"] = [item.to_dict() for item in self.field_requirements]
        return payload


@dataclass
class TargetEnvironmentSnapshot:
    target_patterns: dict = field(default_factory=dict)
    field_capabilities: dict = field(default_factory=dict)
    runtime_capabilities: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ContractEvaluation:
    status: str
    satisfied: list[str] = field(default_factory=list)
    unsatisfied: list[str] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FulfillmentAction:
    kind: str
    description: str
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FulfillmentPlan:
    status: str
    actions: list[FulfillmentAction] = field(default_factory=list)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["actions"] = [item.to_dict() for item in self.actions]
        return payload
