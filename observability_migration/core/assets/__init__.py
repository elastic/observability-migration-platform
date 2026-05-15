# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Canonical asset contracts shared across all source adapters."""

from .alerting import (
    AlertingIR,
    build_alerting_ir_from_datadog,
    build_alerting_ir_from_grafana,
    build_alerting_ir_from_grafana_unified,
)
from .annotation import AnnotationIR
from .control import ControlIR
from .dashboard import DashboardIR
from .link import LinkIR
from .operational import OperationalIR, build_operational_ir
from .panel import PanelIR
from .query import QueryIR, build_query_ir, infer_output_shape
from .status import AssetStatus
from .target_query_contract import (
    ContractEvaluation,
    FieldRequirement,
    FulfillmentAction,
    FulfillmentPlan,
    TargetEnvironmentSnapshot,
    TargetQueryContract,
)
from .target_query_plan import TargetQueryPlan
from .transform import TransformIR
from .visual import VisualIR, VisualLayout, VisualPresentation, refresh_visual_ir

__all__ = [
    "AlertingIR",
    "AnnotationIR",
    "AssetStatus",
    "ContractEvaluation",
    "ControlIR",
    "DashboardIR",
    "FieldRequirement",
    "FulfillmentAction",
    "FulfillmentPlan",
    "LinkIR",
    "OperationalIR",
    "PanelIR",
    "QueryIR",
    "TargetEnvironmentSnapshot",
    "TargetQueryContract",
    "TargetQueryPlan",
    "TransformIR",
    "VisualIR",
    "VisualLayout",
    "VisualPresentation",
    "build_alerting_ir_from_datadog",
    "build_alerting_ir_from_grafana",
    "build_alerting_ir_from_grafana_unified",
    "build_operational_ir",
    "build_query_ir",
    "infer_output_shape",
    "refresh_visual_ir",
]
