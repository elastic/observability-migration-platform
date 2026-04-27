# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Executable registries and extension catalog for the Datadog adapter."""

from __future__ import annotations

from observability_migration.core.extensions import (
    ExtensionCatalog,
    ExtensionRuleCard,
    ExtensionSurface,
    RuleRegistry,
)

from .extension_schema import FieldMapProfileModel

PLANNER_PRECHECKS = RuleRegistry("planner_prechecks", "plan")
METRIC_PLANNERS = RuleRegistry("metric_planners", "plan")
LOG_PLANNERS = RuleRegistry("log_planners", "plan")
METRIC_TRANSLATORS = RuleRegistry("metric_translators", "translate_metric")
LOG_TRANSLATORS = RuleRegistry("log_translators", "translate_log")
LENS_TRANSLATORS = RuleRegistry("lens_translators", "translate_lens")


def build_rule_catalog() -> dict:
    registries = [
        PLANNER_PRECHECKS,
        METRIC_PLANNERS,
        LOG_PLANNERS,
        METRIC_TRANSLATORS,
        LOG_TRANSLATORS,
        LENS_TRANSLATORS,
    ]
    rule_cards = [
        ExtensionRuleCard(
            id=rule["id"],
            stage=rule["stage"],
            summary=rule["summary"],
            registry=rule["registry"],
            priority=rule["priority"],
            extenders=["field_profile"],
        )
        for registry in registries
        for rule in registry.describe()
    ]
    rule_cards.append(
        ExtensionRuleCard(
            id="datadog.preflight.field_capabilities",
            stage="preflight",
            summary="Validate mapped metric and log fields against target capability data before translation.",
            extenders=["field_profile", "live_field_caps"],
            notes=["Preflight is executable today but not yet routed through a public registry."],
        )
    )
    catalog = ExtensionCatalog(
        adapter="datadog",
        summary=(
            "Datadog exposes executable planner and translator registries backed by "
            "field profiles and live target capability data."
        ),
        stages=[
            "plan",
            "preflight",
            "translate_metric",
            "translate_log",
            "translate_lens",
            "emit",
        ],
        current_surfaces=[
            ExtensionSurface(
                id="datadog.field_profile",
                kind="declarative",
                summary=(
                    "YAML field profiles remap Datadog metric and tag names to target "
                    "Elasticsearch fields."
                ),
                entrypoint="--field-profile",
                format="yaml",
                example_path="examples/datadog-field-profile.example.yaml",
            ),
            ExtensionSurface(
                id="datadog.live_field_caps",
                kind="runtime_context",
                summary=(
                    "Live target _field_caps data can be supplied through Elasticsearch "
                    "connection flags to make planning and preflight capability-aware."
                ),
                entrypoint="--es-url",
                format="elasticsearch_field_caps",
            ),
        ],
        planned_surfaces=[
            ExtensionSurface(
                id="datadog.plugin",
                kind="python_plugin",
                summary=(
                    "A future Python plugin contract can target these registries once "
                    "the Datadog extension API is stabilized."
                ),
                entrypoint="register(api)",
                format="python",
            )
        ],
        rules=rule_cards,
        template=build_extension_template(),
        metadata={
            "registries": {registry.name: registry.describe() for registry in registries},
            "current_contract": ["field_profile", "live_field_caps"],
            "future_contract": ["python_plugin"],
        },
    )
    return catalog.to_dict()


def build_extension_template() -> dict:
    return FieldMapProfileModel().model_dump()


__all__ = [
    "LENS_TRANSLATORS",
    "LOG_PLANNERS",
    "LOG_TRANSLATORS",
    "METRIC_PLANNERS",
    "METRIC_TRANSLATORS",
    "PLANNER_PRECHECKS",
    "build_extension_template",
    "build_rule_catalog",
]
