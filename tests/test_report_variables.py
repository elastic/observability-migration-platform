# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Phase B: migration report carries variable bindings and parameterization counts.

See `docs/roadmap/2026-04-27-kibana-variable-controls-design.md` §10.3.
"""
from __future__ import annotations

import json

from observability_migration.core import variable_classifier as vc
from observability_migration.core.reporting.report import (
    MigrationResult,
    save_detailed_report,
)


def test_report_includes_variables_block(tmp_path):
    result = MigrationResult(dashboard_title="Dashboard X", dashboard_uid="uid-x")
    result.variable_bindings = {
        "Dashboard X": {
            "instance": vc.AcceptedBinding(
                field="service.instance.id",
                multi=False,
                options_query="FROM x",
            ),
            "namespace": vc.RejectedBinding(reason="include_all_unsupported"),
            "verifier_var": vc.RejectedBinding(
                reason="verifier_failed_field_consistency"
            ),
        }
    }
    result.panel_parameterizations = {
        "Dashboard X": {"?instance": 12, "?cluster": 0}
    }

    out = tmp_path / "report.json"
    save_detailed_report([result], compile_results=[], output_path=str(out))

    body = json.loads(out.read_text())
    dash = body["dashboards"][0]

    assert dash["variables"]["accepted"] == [
        {"name": "instance", "field": "service.instance.id", "multi": False}
    ]
    assert {r["name"] for r in dash["variables"]["rejected"]} == {"namespace"}
    assert dash["variables"]["rejected"][0]["reason"] == "include_all_unsupported"

    assert {r["name"] for r in dash["variables"]["verifier_downgraded"]} == {
        "verifier_var"
    }
    assert (
        dash["variables"]["verifier_downgraded"][0]["reason"]
        == "verifier_failed_field_consistency"
    )

    assert dash["variables"]["accepted_fields"] == []
    assert dash["variables"]["accepted_functions"] == []
    assert dash["variables"]["accepted_intervals"] == []

    assert dash["panel_parameterizations"]["?instance"] == 12
    assert dash["panel_parameterizations"]["?cluster"] == 0


def test_report_variables_block_defaults_empty_when_unset(tmp_path):
    result = MigrationResult(dashboard_title="Dashboard Y", dashboard_uid="uid-y")

    out = tmp_path / "report.json"
    save_detailed_report([result], compile_results=[], output_path=str(out))

    body = json.loads(out.read_text())
    dash = body["dashboards"][0]

    assert dash["variables"] == {
        "accepted": [],
        "accepted_fields": [],
        "accepted_functions": [],
        "accepted_intervals": [],
        "rejected": [],
        "verifier_downgraded": [],
    }
    assert dash["panel_parameterizations"] == {}
