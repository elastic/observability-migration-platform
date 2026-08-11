# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Offline gate over examples/alerting Grafana rules + Datadog monitors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from observability_migration.adapters.source.datadog.field_map import load_profile
from observability_migration.adapters.source.grafana.alerts import (
    build_alert_migration_tasks,
    extract_alerts_from_dashboard,
)
from observability_migration.adapters.source.grafana.extract import (
    extract_all_alerting_resources_from_files,
    extract_dashboards_from_files,
)
from observability_migration.core.assets.alerting import (
    build_alerting_ir_from_datadog,
    build_alerting_ir_from_grafana,
    build_alerting_ir_from_grafana_unified,
)
from observability_migration.core.mapping import map_alerts_batch
from observability_migration.core.verification.alert_offline_gate import (
    check_alert_batch,
    gate_bugs,
)

ROOT = Path(__file__).resolve().parents[1]
GRAFANA_EXAMPLES = ROOT / "examples" / "alerting" / "grafana"
DATADOG_MONITORS = ROOT / "examples" / "alerting" / "monitors" / "datadog_monitors.json"
DATADOG_PROFILE = ROOT / "examples" / "datadog-field-profile.example.yaml"


def _grafana_batch():
    dashboards = extract_dashboards_from_files(str(GRAFANA_EXAMPLES))
    legacy_tasks = []
    for dashboard in dashboards:
        legacy_tasks.extend(build_alert_migration_tasks(extract_alerts_from_dashboard(dashboard)))
    unified = extract_all_alerting_resources_from_files(str(GRAFANA_EXAMPLES))
    datasource_map = unified.get("datasources", {}) or {}
    irs = [build_alerting_ir_from_grafana(task) for task in legacy_tasks]
    irs.extend(
        build_alerting_ir_from_grafana_unified(rule, datasource_map=datasource_map)
        for rule in (unified.get("alert_rules", []) or [])
    )
    return map_alerts_batch(irs), irs


def _datadog_batch():
    monitors = json.loads(DATADOG_MONITORS.read_text(encoding="utf-8"))
    field_map = load_profile(str(DATADOG_PROFILE))
    irs = [build_alerting_ir_from_datadog(monitor, field_map=field_map) for monitor in monitors]
    degraded = {
        ir.alert_id: bool((ir.metadata or {}).get("parse_degraded"))
        for ir in irs
    }
    return map_alerts_batch(irs), degraded


def test_grafana_alerting_examples_have_no_offline_gate_bugs():
    batch, _irs = _grafana_batch()
    bugs = gate_bugs(check_alert_batch(batch, source_name="grafana"))
    assert bugs == [], [f"{b.rule_id.value}: {b.message} ({b.evidence.get('alert_name')})" for b in bugs]


def test_datadog_monitor_examples_have_no_offline_gate_bugs():
    batch, degraded = _datadog_batch()
    bugs = gate_bugs(
        check_alert_batch(batch, source_name="datadog", parse_degraded_by_id=degraded)
    )
    assert bugs == [], [f"{b.rule_id.value}: {b.message} ({b.evidence.get('alert_name')})" for b in bugs]


def test_corrupting_enabled_flag_makes_grafana_gate_fail():
    batch, _irs = _grafana_batch()
    emitted = [
        item for item in batch["results"] if item["mapping"].get("payload_status") == "emitted"
    ]
    assert emitted, "expected at least one emitted Grafana alert in examples"
    victim = emitted[0]["mapping"]["rule_payload"]
    victim["enabled"] = True
    bugs = gate_bugs(check_alert_batch(batch, source_name="grafana"))
    assert any(b.rule_id.value == "ENABLED_TRUE" for b in bugs)


# ---------------------------------------------------------------------------
# --create-alert-rules requested but not performed must fail the run.
#
# End-to-end over the same example fixtures, through each source CLI's own
# main(), because the exit code is the part an operator and CI actually see.
# ---------------------------------------------------------------------------


def _grafana_alerts_argv(output_dir: Path, *extra: str) -> list[str]:
    return [
        "--source", "files",
        "--assets", "alerts",
        "--input-dir", str(GRAFANA_EXAMPLES),
        "--output-dir", str(output_dir),
        *extra,
    ]


def _datadog_alerts_argv(output_dir: Path, *extra: str) -> list[str]:
    return [
        "--source", "files",
        "--assets", "alerts",
        # load_raw_monitors() reads <input-dir>/monitors, so point one level up.
        "--input-dir", str(DATADOG_MONITORS.parent.parent),
        "--field-profile", str(DATADOG_PROFILE),
        "--output-dir", str(output_dir),
        *extra,
    ]


def test_grafana_create_alert_rules_without_api_key_fails_the_run(tmp_path, capsys):
    from observability_migration.adapters.source.grafana.cli import main

    out = tmp_path / "grafana_out"
    with pytest.raises(SystemExit) as excinfo:
        main(
            _grafana_alerts_argv(
                out, "--create-alert-rules", "--kibana-url", "http://localhost:5602"
            )
        )

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "--kibana-api-key" in captured.err
    assert "no rules were created" in captured.err
    # The translated alerts are still on disk -- the run failed, it did not
    # throw away the work the operator can still use.
    summary = json.loads((out / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["alerts"]["total"] == 27
    assert summary["alerts"]["rule_creation"]["reason"] == "missing_kibana_api_key"


def test_grafana_alerts_run_without_create_alert_rules_stays_green(tmp_path, capsys):
    # The regression that would get the check disabled: no --create-alert-rules,
    # no failure, no new noise.
    from observability_migration.adapters.source.grafana.cli import main

    out = tmp_path / "grafana_out"
    main(_grafana_alerts_argv(out, "--kibana-url", "http://localhost:5602"))

    captured = capsys.readouterr()
    assert "create-alert-rules" not in captured.out
    assert "create-alert-rules" not in captured.err
    summary = json.loads((out / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["alerts"]["total"] == 27
    assert "rule_creation" not in summary["alerts"]


def test_datadog_create_alert_rules_without_api_key_fails_the_run(tmp_path, capsys):
    from observability_migration.adapters.source.datadog.cli import main

    out = tmp_path / "datadog_out"
    with pytest.raises(SystemExit) as excinfo:
        main(
            _datadog_alerts_argv(
                out, "--create-alert-rules", "--kibana-url", "http://localhost:5602"
            )
        )

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "--kibana-api-key" in captured.err
    assert "no rules were created" in captured.err
    summary = json.loads((out / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["alerts"]["total"] == 35
    assert summary["alerts"]["rule_creation"]["reason"] == "missing_kibana_api_key"


def test_datadog_alerts_run_without_create_alert_rules_stays_green(tmp_path, capsys):
    from observability_migration.adapters.source.datadog.cli import main

    out = tmp_path / "datadog_out"
    main(_datadog_alerts_argv(out, "--kibana-url", "http://localhost:5602"))

    captured = capsys.readouterr()
    assert "create-alert-rules" not in captured.out
    assert "create-alert-rules" not in captured.err
    summary = json.loads((out / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["alerts"]["total"] == 35
    assert "rule_creation" not in summary["alerts"]
