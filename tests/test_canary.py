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
from observability_migration.core.coverage import supported_types as st
from observability_migration.core.coverage.canary import (
    CANARY_KIBANA_TARGETS,
    build_grafana_canary,
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
