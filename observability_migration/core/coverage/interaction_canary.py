# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Synthetic native-IR dashboard for Kibana dashboard interaction testing.

Builds a deterministic :class:`DashboardIR` that exercises ES|QL and classic
dashboard controls independently of Grafana/Datadog translator coverage. The
same IR feeds ``native_dashboard_from_ir`` for upload and
``DashboardIR.to_yaml_dict()`` for telemetry-contract extraction.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from observability_migration.core.assets.control import ControlIR
from observability_migration.core.assets.dashboard import DashboardIR
from observability_migration.core.assets.panel import PanelIR
from observability_migration.core.assets.status import AssetStatus
from observability_migration.core.assets.visual import VisualIR, VisualLayout, VisualPresentation

INTERACTION_CANARY_UID = "obs-migrate-interaction-canary"
INTERACTION_CANARY_TITLE = "obs-migrate interaction canary (synthetic controls)"

DATA_VIEW = "metrics-*"
_BASE = "FROM metrics-* | WHERE @timestamp >= NOW() - 3 hours"

# Deterministic host labels used by the synthetic seeder and interaction manifest.
SYNTHETIC_HOST_NAMES: tuple[str, ...] = ("host-1", "host-2", "host-3")
RANGE_SLIDER_DEFAULT_BOUNDS: tuple[str, str] = ("20", "80")
RANGE_INTERACTION_SELECTION = "40..60"

_PANEL_QUERIES: dict[str, str] = {
    "interaction-value": (
        f"{_BASE} | WHERE service.environment == ?environment "
        "| STATS value=AVG(interaction_value)"
    ),
    "interaction-multi": (
        f"{_BASE} | WHERE MV_CONTAINS(?services, service.name) "
        "| STATS value=AVG(interaction_value)"
    ),
    "interaction-field": (
        f"{_BASE} | STATS value=AVG(interaction_value) BY grouping=??grouping"
    ),
    "interaction-function": (
        f"{_BASE} | STATS value=??aggregate(interaction_value)"
    ),
    "interaction-interval": (
        "TS metrics-* | STATS value=AVG(interaction_value) BY bucket=TBUCKET(?interval)"
    ),
    "interaction-options": (
        f"{_BASE} | STATS value=AVG(interaction_value) BY host.name"
    ),
    "interaction-range": (
        f"{_BASE} | WHERE latency_ms >= 0 | STATS value=AVG(latency_ms)"
    ),
}

_ENVIRONMENT_VALUES_QUERY = (
    f"FROM {DATA_VIEW} | WHERE service.environment IS NOT NULL "
    "| STATS count = COUNT(*) BY service.environment "
    "| SORT service.environment ASC | KEEP service.environment | LIMIT 1000"
)

_SERVICES_VALUES_QUERY = (
    f"FROM {DATA_VIEW} | WHERE service.name IS NOT NULL "
    "| STATS count = COUNT(*) BY service.name "
    "| SORT service.name ASC | KEEP service.name | LIMIT 1000"
)


@dataclass(frozen=True)
class InteractionFailureCanary:
    """Isolated dashboard variant that should classify to one failure bucket."""

    canary_id: str
    dashboard: DashboardIR
    expected_failure_classes: tuple[str, ...]
    description: str = ""


def _control_metadata(bound_field: str) -> dict[str, Any]:
    return {"bound_field": bound_field} if bound_field else {}


def _esql_control(
    *,
    control_id: str,
    label: str,
    variable_name: str,
    variable_type: str,
    bound_field: str = "",
    choices: list[str] | None = None,
    defaults: list[str] | None = None,
    query: str = "",
    multiple: bool = False,
) -> ControlIR:
    raw: dict[str, Any] = {
        "type": "esql",
        "label": label,
        "variable_name": variable_name,
        "variable_type": variable_type,
        "data_view_id": DATA_VIEW,
    }
    if query:
        raw["query"] = query
    if choices:
        raw["choices"] = list(choices)
    if defaults:
        raw["defaults"] = list(defaults)
    if multiple:
        raw["multiple"] = True
    metadata = _control_metadata(bound_field)
    if metadata:
        raw["metadata"] = dict(metadata)
    return ControlIR(
        control_id=control_id,
        name=control_id,
        label=label,
        kind="esql",
        variable_name=variable_name,
        variable_type=variable_type,
        data_view=DATA_VIEW,
        query=query,
        selected_options=list(defaults or []),
        available_options=list(choices or []),
        multiple=multiple,
        status=AssetStatus.TRANSLATED,
        metadata=metadata,
        source_extension=raw,
    )


def _classic_control(
    *,
    control_id: str,
    label: str,
    kind: str,
    field_name: str,
    defaults: list[str] | None = None,
    available_options: list[str] | None = None,
) -> ControlIR:
    raw: dict[str, Any] = {
        "type": kind,
        "label": label,
        "data_view_id": DATA_VIEW,
        "field_name": field_name,
    }
    if available_options:
        raw["available_options"] = list(available_options)
    if defaults:
        raw["defaults"] = list(defaults)
    metadata = _control_metadata(field_name)
    if metadata:
        raw["metadata"] = dict(metadata)
    return ControlIR(
        control_id=control_id,
        name=control_id,
        label=label,
        kind=kind,
        field_name=field_name,
        data_view=DATA_VIEW,
        selected_options=list(defaults or []),
        available_options=list(available_options or defaults or []),
        status=AssetStatus.TRANSLATED,
        metadata=metadata,
        source_extension=raw,
    )


def _esql_panel(
    *,
    panel_id: str,
    title: str,
    query: str,
    kibana_type: str,
    x: int,
    y: int,
    config: dict[str, Any] | None = None,
) -> PanelIR:
    esql_config: dict[str, Any] = {
        "type": kibana_type,
        "query": query,
        "data_view": DATA_VIEW,
    }
    if config:
        esql_config.update(config)
    return PanelIR(
        panel_id=panel_id,
        title=title,
        status=AssetStatus.TRANSLATED,
        visual=VisualIR(
            title=title,
            kibana_type=kibana_type,
            layout=VisualLayout(x=x, y=y, w=12, h=8),
            presentation=VisualPresentation(kind="esql", config=esql_config),
        ),
    )


def _markdown_panel(*, panel_id: str, title: str, x: int, y: int) -> PanelIR:
    return PanelIR(
        panel_id=panel_id,
        title=title,
        status=AssetStatus.TRANSLATED,
        visual=VisualIR(
            title=title,
            kibana_type="markdown",
            layout=VisualLayout(x=x, y=y, w=12, h=8),
            presentation=VisualPresentation(
                kind="markdown",
                config={"content": "Unaffected reference panel for interaction isolation."},
            ),
        ),
    )


def _build_controls() -> list[ControlIR]:
    return [
        _esql_control(
            control_id="environment",
            label="environment",
            variable_name="environment",
            variable_type="values",
            bound_field="service.environment",
            query=_ENVIRONMENT_VALUES_QUERY,
            choices=["prod", "staging"],
            defaults=["prod"],
        ),
        _esql_control(
            control_id="services",
            label="services",
            variable_name="services",
            variable_type="values",
            bound_field="service.name",
            query=_SERVICES_VALUES_QUERY,
            choices=["api", "worker", "frontend"],
            defaults=["api", "worker"],
            multiple=True,
        ),
        _esql_control(
            control_id="grouping",
            label="grouping",
            variable_name="grouping",
            variable_type="fields",
            choices=["service.name", "host.name"],
            defaults=["service.name"],
        ),
        _esql_control(
            control_id="aggregate",
            label="aggregate",
            variable_name="aggregate",
            variable_type="functions",
            choices=["AVG", "MAX", "SUM"],
            defaults=["AVG"],
        ),
        _esql_control(
            control_id="interval",
            label="interval",
            variable_name="interval",
            variable_type="time_literal",
            choices=["1 minute", "5 minutes", "15 minutes"],
            defaults=["5 minutes"],
        ),
        _classic_control(
            control_id="host.name",
            label="host.name",
            kind="options_list",
            field_name="host.name",
            available_options=list(SYNTHETIC_HOST_NAMES),
            defaults=[SYNTHETIC_HOST_NAMES[0]],
        ),
        _classic_control(
            control_id="latency_ms",
            label="latency_ms",
            kind="range_slider",
            field_name="latency_ms",
            defaults=list(RANGE_SLIDER_DEFAULT_BOUNDS),
        ),
    ]


def build_interaction_canary() -> DashboardIR:
    """Return the synthetic interaction capability dashboard as native IR."""
    panels = [
        _esql_panel(
            panel_id="interaction-value",
            title="interaction value",
            query=_PANEL_QUERIES["interaction-value"],
            kibana_type="metric",
            x=0,
            y=0,
            config={"primary": {"field": "value"}},
        ),
        _esql_panel(
            panel_id="interaction-multi",
            title="interaction multi",
            query=_PANEL_QUERIES["interaction-multi"],
            kibana_type="metric",
            x=12,
            y=0,
            config={"primary": {"field": "value"}},
        ),
        _esql_panel(
            panel_id="interaction-field",
            title="interaction field",
            query=_PANEL_QUERIES["interaction-field"],
            kibana_type="metric",
            x=24,
            y=0,
            config={"primary": {"field": "value"}, "breakdown": {"field": "grouping"}},
        ),
        _esql_panel(
            panel_id="interaction-function",
            title="interaction function",
            query=_PANEL_QUERIES["interaction-function"],
            kibana_type="metric",
            x=36,
            y=0,
            config={"primary": {"field": "value"}},
        ),
        _esql_panel(
            panel_id="interaction-interval",
            title="interaction interval",
            query=_PANEL_QUERIES["interaction-interval"],
            kibana_type="line",
            x=0,
            y=8,
            config={
                "dimension": {"field": "bucket"},
                "metrics": [{"field": "value"}],
            },
        ),
        _esql_panel(
            panel_id="interaction-options",
            title="interaction options",
            query=_PANEL_QUERIES["interaction-options"],
            kibana_type="bar",
            x=12,
            y=8,
            config={
                "dimension": {"field": "host.name"},
                "metrics": [{"field": "value"}],
                "breakdown": {"field": "host.name"},
            },
        ),
        _esql_panel(
            panel_id="interaction-range",
            title="interaction range",
            query=_PANEL_QUERIES["interaction-range"],
            kibana_type="metric",
            x=24,
            y=8,
            config={"primary": {"field": "value"}},
        ),
        _markdown_panel(
            panel_id="interaction-unaffected",
            title="interaction unaffected",
            x=36,
            y=8,
        ),
    ]
    return DashboardIR(
        uid=INTERACTION_CANARY_UID,
        title=INTERACTION_CANARY_TITLE,
        source_adapter="synthetic",
        description="Synthetic Kibana interaction capability canary.",
        settings={"timeRestore": True},
        panels=panels,
        controls=_build_controls(),
        metadata={"interaction_canary": True},
    )


def build_interaction_failure_canaries() -> tuple[InteractionFailureCanary, ...]:
    """Return isolated dashboards that should classify to one failure bucket each."""
    base_controls = _build_controls()
    return (
        InteractionFailureCanary(
            canary_id="invalid-output-accessor",
            dashboard=DashboardIR(
                uid="obs-migrate-interaction-failure-invalid-accessor",
                title="interaction failure: invalid accessor",
                source_adapter="synthetic",
                panels=[
                    _esql_panel(
                        panel_id="failure-invalid-accessor",
                        title="invalid accessor",
                        query=_PANEL_QUERIES["interaction-value"],
                        kibana_type="metric",
                        x=0,
                        y=0,
                        config={"primary": {"field": "missing_value_column"}},
                    )
                ],
                controls=base_controls[:1],
            ),
            expected_failure_classes=("render_error",),
            description="Lens accessor points at a column the query never emits.",
        ),
        InteractionFailureCanary(
            canary_id="value-instead-of-identifier",
            dashboard=DashboardIR(
                uid="obs-migrate-interaction-failure-value-binding",
                title="interaction failure: value binding",
                source_adapter="synthetic",
                panels=[
                    _esql_panel(
                        panel_id="failure-value-binding",
                        title="value binding",
                        query=(
                            f"{_BASE} | STATS value=AVG(interaction_value) "
                            "BY grouping=?grouping"
                        ),
                        kibana_type="metric",
                        x=0,
                        y=0,
                        config={"primary": {"field": "value"}, "breakdown": {"field": "grouping"}},
                    )
                ],
                controls=base_controls[2:3],
            ),
            expected_failure_classes=("query_contract_error",),
            description="Field control bound with ? instead of ?? in BY clause.",
        ),
        InteractionFailureCanary(
            canary_id="missing-target-field",
            dashboard=DashboardIR(
                uid="obs-migrate-interaction-failure-field-gap",
                title="interaction failure: field gap",
                source_adapter="synthetic",
                panels=[
                    _esql_panel(
                        panel_id="failure-field-gap",
                        title="field gap",
                        query=(
                            f"{_BASE} | STATS value=AVG(interaction_value) "
                            "BY missing_dimension"
                        ),
                        kibana_type="bar",
                        x=0,
                        y=0,
                        config={
                            "dimension": {"field": "missing_dimension"},
                            "metrics": [{"field": "value"}],
                            "breakdown": {"field": "missing_dimension"},
                        },
                    )
                ],
                controls=[],
            ),
            expected_failure_classes=("field_gap",),
            description="Breakdown references a field absent from target telemetry.",
        ),
        InteractionFailureCanary(
            canary_id="missing-required-values",
            dashboard=DashboardIR(
                uid="obs-migrate-interaction-failure-data-gap",
                title="interaction failure: data gap",
                source_adapter="synthetic",
                panels=[
                    _esql_panel(
                        panel_id="failure-data-gap",
                        title="data gap",
                        query=(
                            f"{_BASE} | WHERE service.name == \"missing_service\" "
                            "| STATS value=AVG(interaction_value)"
                        ),
                        kibana_type="metric",
                        x=0,
                        y=0,
                        config={"primary": {"field": "value"}},
                    )
                ],
                controls=[],
            ),
            expected_failure_classes=("data_gap",),
            description="Required dimension value never seeded despite field presence.",
        ),
        InteractionFailureCanary(
            canary_id="unexpected-empty",
            dashboard=DashboardIR(
                uid="obs-migrate-interaction-failure-unexpected-empty",
                title="interaction failure: unexpected empty",
                source_adapter="synthetic",
                panels=[
                    _esql_panel(
                        panel_id="failure-unexpected-empty",
                        title="unexpected empty",
                        query=(
                            f"{_BASE} | WHERE service.environment == \"prod\" "
                            "AND service.environment == \"staging\" "
                            "| STATS value=AVG(interaction_value)"
                        ),
                        kibana_type="metric",
                        x=0,
                        y=0,
                        config={"primary": {"field": "value"}},
                    )
                ],
                controls=base_controls[:1],
            ),
            expected_failure_classes=("unexpected_empty",),
            description="Query succeeds but returns zero rows on a data-required panel.",
        ),
        InteractionFailureCanary(
            canary_id="manifest-control-absent",
            dashboard=DashboardIR(
                uid="obs-migrate-interaction-failure-framework",
                title="interaction failure: framework",
                source_adapter="synthetic",
                panels=[
                    _esql_panel(
                        panel_id="failure-framework",
                        title="framework",
                        query=_PANEL_QUERIES["interaction-value"],
                        kibana_type="metric",
                        x=0,
                        y=0,
                        config={"primary": {"field": "value"}},
                    )
                ],
                controls=[],
            ),
            expected_failure_classes=("control_not_found",),
            description="Manifest declares a control that is absent from the dashboard.",
        ),
    )


def write_interaction_canary_artifact(artifact_dir: str | Path) -> Path:
    """Write the synthetic dashboard YAML used by telemetry-contract tests."""
    artifact_path = Path(artifact_dir)
    yaml_dir = artifact_path / "yaml"
    yaml_dir.mkdir(parents=True, exist_ok=True)
    output_path = yaml_dir / "interaction-canary.yaml"
    payload = {"dashboards": [build_interaction_canary().to_yaml_dict()]}
    output_path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )
    return output_path


__all__ = [
    "INTERACTION_CANARY_TITLE",
    "INTERACTION_CANARY_UID",
    "RANGE_INTERACTION_SELECTION",
    "RANGE_SLIDER_DEFAULT_BOUNDS",
    "SYNTHETIC_HOST_NAMES",
    "InteractionFailureCanary",
    "build_interaction_canary",
    "build_interaction_failure_canaries",
    "write_interaction_canary_artifact",
]
