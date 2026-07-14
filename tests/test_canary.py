# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Kitchen-sink canary tests.

The canary is a single generated Grafana dashboard covering one panel per
distinct chart-bearing Kibana target. These tests guarantee it stays a faithful
"maximum variety" fixture: it covers every supported type's Kibana target,
migrates cleanly, and validates against the vendored Kibana schema. The same
canary is the fixture the live render-audit gate will upload.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import jsonschema
import yaml

from observability_migration.adapters.source.grafana import panels, rules, schema
from observability_migration.adapters.source.grafana.runtime_features import (
    ESQL_NAMED_PARAM_BINDING,
    set_runtime_feature,
)
from observability_migration.core.coverage import supported_types as st
from observability_migration.core.coverage.canary import (
    CANARY_KIBANA_TARGETS,
    build_grafana_canary,
    build_late_bound_grouping_canary,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "docs" / "dashboards" / "schema.json"

_TRANSLATED = {"migrated", "migrated_with_warnings"}


def _migrate_canary():
    rp = rules.RulePackConfig()
    resolver = schema.SchemaResolver(rp)
    canary = build_grafana_canary()
    with tempfile.TemporaryDirectory() as td:
        result, yaml_path = panels.translate_dashboard(
            canary, Path(td),
            datasource_index="metrics-*", esql_index="metrics-*",
            rule_pack=rp, resolver=resolver,
        )
        payload = yaml.safe_load(yaml_path.read_text())
    return result, payload


def _migrate_late_bound_grouping_canary(default_grouping="transport"):
    # Mirror the live render-audit CLI, which probes ``--es-url`` and enables
    # ES|QL named-parameter binding, so the pure ``by ($grouping)`` panel takes
    # the late-bound field-control path (issue #282).
    rp = rules.RulePackConfig()
    set_runtime_feature(rp, ESQL_NAMED_PARAM_BINDING, supported=True, source="probe")
    resolver = schema.SchemaResolver(rp)
    canary = build_late_bound_grouping_canary(default_grouping=default_grouping)
    with tempfile.TemporaryDirectory() as td:
        result, yaml_path = panels.translate_dashboard(
            canary, Path(td),
            datasource_index="metrics-*", esql_index="metrics-*",
            rule_pack=rp, resolver=resolver,
        )
        payload = yaml.safe_load(yaml_path.read_text())
    return result, payload


def test_canary_covers_every_supported_kibana_target():
    # Every supported Grafana panel type's Kibana target must be exercised by the
    # canary (so "maximum variety" is enforced, not aspirational).
    needed = {
        st.GRAFANA_SUPPORTED_PANEL_TYPES[t]
        for t in st.GRAFANA_SUPPORTED_PANEL_TYPES
    }
    missing = needed - set(CANARY_KIBANA_TARGETS)
    assert not missing, (
        f"canary is missing Kibana targets for supported types: {sorted(missing)}. "
        "Add a representative panel to canary._CANARY_PANELS."
    )


def test_canary_migrates_clean():
    result, _payload = _migrate_canary()
    assert result.total_panels == 8
    bad = [
        (pr.grafana_type, pr.status)
        for pr in result.panel_results
        if pr.status not in _TRANSLATED
    ]
    assert not bad, f"canary panels did not migrate cleanly: {bad}"


def test_canary_produces_all_expected_targets():
    result, _payload = _migrate_canary()
    produced = {pr.kibana_type for pr in result.panel_results}
    assert produced == set(CANARY_KIBANA_TARGETS), (
        f"canary produced {sorted(produced)}, expected {sorted(CANARY_KIBANA_TARGETS)}"
    )


def test_canary_yaml_validates_against_kibana_schema():
    _result, payload = _migrate_canary()
    schema_doc = json.loads(SCHEMA_PATH.read_text())
    validator = jsonschema.Draft202012Validator(schema_doc)
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    detail = "\n  ".join(
        f"@{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors[:10]
    )
    assert not errors, f"canary YAML has {len(errors)} Kibana-schema error(s):\n  {detail}"


def test_late_bound_grouping_canary_emits_field_control_and_degrades_collision():
    # Issue #282 render-audit fixture: the pure ``by ($grouping)`` panel migrates
    # to an ES|QL fields control bound to the stable ``grouping`` alias, and the
    # concrete+variable panel degrades gracefully (keeps ``exporter``, drops the
    # colliding selector) — both must be shippable so the live gate can prove
    # they render.
    result, payload = _migrate_late_bound_grouping_canary()
    dash = payload["dashboards"][0]

    field_controls = [c for c in (dash.get("controls") or []) if c.get("variable_type") == "fields"]
    assert len(field_controls) == 1
    assert field_controls[0]["variable_name"] == "grouping"
    assert field_controls[0]["choices"] == ["exporter", "transport", "receiver"]

    panels_by_title = {p.get("title"): p for p in dash["panels"]}
    pure = panels_by_title["spans by grouping"]
    assert "grouping = ??grouping" in pure["esql"]["query"]
    assert pure["esql"]["breakdown"]["field"] == "grouping"

    collision = panels_by_title["spans by exporter and grouping"]
    assert "??grouping" not in collision["esql"]["query"]
    assert "exporter" in collision["esql"]["query"]

    assert [pr.status for pr in result.panel_results if pr.status not in _TRANSLATED] == []


def test_late_bound_grouping_canary_validates_against_kibana_schema():
    _result, payload = _migrate_late_bound_grouping_canary()
    schema_doc = json.loads(SCHEMA_PATH.read_text())
    validator = jsonschema.Draft202012Validator(schema_doc)
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    detail = "\n  ".join(
        f"@{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors[:10]
    )
    assert not errors, f"late-bound canary YAML has {len(errors)} Kibana-schema error(s):\n  {detail}"


def test_late_bound_grouping_canary_variants_cover_each_field_choice():
    dashboards = [
        build_late_bound_grouping_canary(default_grouping=choice)
        for choice in ("exporter", "transport", "receiver")
    ]

    assert len({dashboard["uid"] for dashboard in dashboards}) == 3
    assert len({dashboard["title"] for dashboard in dashboards}) == 3
    for choice, dashboard in zip(("exporter", "transport", "receiver"), dashboards):
        variable = dashboard["templating"]["list"][0]
        assert variable["current"]["value"] == choice
        selected = [
            option["value"]
            for option in variable["options"]
            if option.get("selected")
        ]
        assert selected == [choice]
        _result, payload = _migrate_late_bound_grouping_canary(choice)
        field_control = next(
            control
            for control in payload["dashboards"][0]["controls"]
            if control.get("variable_type") == "fields"
        )
        assert field_control["default"] == choice
