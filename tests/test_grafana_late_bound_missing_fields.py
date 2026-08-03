# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Late-bound grouping choices must survive live schema remaps to missing fields."""

from __future__ import annotations

from observability_migration.adapters.source.grafana import panels, rules, schema
from observability_migration.adapters.source.grafana.runtime_features import (
    ESQL_NAMED_PARAM_BINDING,
    set_runtime_feature,
)
from observability_migration.core.coverage.canary import build_late_bound_grouping_canary


class _RemappingMissingResolver:
    """Live-schema stand-in: remaps bare dims to ``labels.*`` that do not exist."""

    def resolve_control_field(self, name, metric_field=None):
        return f"labels.{name}"

    def field_exists(self, field_name):
        # Index has prometheus-style fields, not the remapped labels.* paths.
        return False

    def is_aggregatable_field(self, field_name):
        return False


def test_late_bound_custom_choices_kept_when_remapped_fields_absent():
    rp = rules.RulePackConfig()
    set_runtime_feature(rp, ESQL_NAMED_PARAM_BINDING, supported=True, source="probe")
    variable = {
        "name": "grouping",
        "type": "custom",
        "label": "Group by",
        "query": "exporter,transport,receiver",
        "current": {"text": "transport", "value": "transport"},
        "options": [
            {"text": "exporter", "value": "exporter"},
            {"text": "transport", "value": "transport"},
            {"text": "receiver", "value": "receiver"},
        ],
    }
    choices = panels._build_late_bound_group_var_choices(
        [variable], _RemappingMissingResolver(), rp
    )
    assert "grouping" in choices
    assert choices["grouping"]["choices"] == ["exporter", "transport", "receiver"]
    assert choices["grouping"]["default"] == "transport"


def test_late_bound_canary_migrates_with_live_style_missing_dims():
    """translate_dashboard must not not_feasible pure by ($grouping) solely because
    remapped dimension fields are absent from live discovery (data readiness)."""
    rp = rules.RulePackConfig()
    set_runtime_feature(rp, ESQL_NAMED_PARAM_BINDING, supported=True, source="probe")
    canary = build_late_bound_grouping_canary()
    resolver = _RemappingMissingResolver()
    # SchemaResolver is normally used; monkeypatch choice resolution via wrapper
    # by installing the remapping behavior on a real resolver.
    real = schema.SchemaResolver(rp)
    real.resolve_control_field = resolver.resolve_control_field  # type: ignore[method-assign]
    real.field_exists = resolver.field_exists  # type: ignore[method-assign]
    real.is_aggregatable_field = resolver.is_aggregatable_field  # type: ignore[method-assign]
    real._discovery_attempted = True

    result = panels.translate_dashboard(
        canary,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rp,
        resolver=real,
    )
    pure = next(pr for pr in result.panel_results if pr.title == "spans by grouping")
    assert pure.status in {"migrated", "migrated_with_warnings"}, (pure.status, pure.reasons)
    assert "??grouping" in (pure.esql_query or "")
    assert getattr(rp, "_late_bound_group_var_choices", {}).get("grouping")
