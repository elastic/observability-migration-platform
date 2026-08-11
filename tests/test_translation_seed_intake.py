# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Mutation self-test for translation seed intake."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from observability_migration.adapters.source.grafana.esql_structural_oracle import (
    StructuralRuleId,
    check_esql_structure,
    structural_errors,
)
from observability_migration.core.verification.disposition import (
    unknown_column_looks_like_alias_bug,
)

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "intake_translation_seeds.py"

_GOOD_ALIAS_QUERY = (
    "TS metrics-*\n"
    "| WHERE node_cpu_scaling_frequency_hertz IS NOT NULL\n"
    "| STATS node_cpu_scaling_frequency_hertz_B = MAX(LAST_OVER_TIME(node_cpu_scaling_frequency_hertz)) "
    "BY time_bucket = TBUCKET(5 minute)\n"
    "| EVAL CPU = node_cpu_scaling_frequency_hertz_B\n"
)

_GOOD_CASE_WRAP_QUERY = (
    "TS metrics-*\n"
    '| STATS a = SUM(CASE((mode == "user"), IRATE(m), NULL)), '
    "b = SUM(CASE(true, IRATE(other), NULL)) BY time_bucket = TBUCKET(5 minute)\n"
)

_DATADOG_GOOD_QUERY = (
    "FROM metrics-*\n"
    "| STATS freq_B = AVG(system.cpu.user) BY host\n"
    "| EVAL CPU = freq_B\n"
)


def corrupt_break_eval_alias(query: str) -> str:
    """Break a renamed STATS alias reference in EVAL (translator bug shape)."""
    return query.replace(
        "EVAL CPU = node_cpu_scaling_frequency_hertz_B",
        "EVAL CPU = node_cpu_scaling_frequency_hertz",
    )


def corrupt_to_inner_case_irate(query: str) -> str:
    """Emit illegal IRATE(CASE(...)) value-arg shape (ClassCast class)."""
    return (
        "TS metrics-*\n"
        '| STATS a = SUM(IRATE(CASE((mode == "user"), m, NULL), 1m)), '
        "b = SUM(IRATE(other)) BY time_bucket = TBUCKET(5 minute)\n"
    )


def corrupt_datadog_break_eval_alias(query: str) -> str:
    return query.replace("EVAL CPU = freq_B", "EVAL CPU = system.cpu.user")


def test_datadog_oracle_flags_alias_corruption():
    from observability_migration.adapters.source.datadog.esql_structural_oracle import (
        check_datadog_esql_structure,
    )

    bad = corrupt_datadog_break_eval_alias(_DATADOG_GOOD_QUERY)
    errs = structural_errors(
        check_datadog_esql_structure(bad, status="ok", backend="esql")
    )
    assert any(e.rule_id == StructuralRuleId.EVAL_UNDEFINED_COLUMN for e in errs)


def test_datadog_intake_proposes_seed_for_structural_mutation(tmp_path):
    bad_query = corrupt_datadog_break_eval_alias(_DATADOG_GOOD_QUERY)
    report = {
        "source": "datadog",
        "panels": [
            {
                "title": "Datadog CPU Alias Bug",
                "status": "fail",
                "disposition": "real_bug",
                "error": "Unknown column [system.cpu.user]",
                "esql_query": bad_query,
                "targets": [{"query": "avg:system.cpu.user{*}"}],
            }
        ],
    }
    report_path = tmp_path / "dd_smoke.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--report",
            str(report_path),
            "--out-dir",
            str(tmp_path / "seeds"),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    proposals = json.loads(proc.stdout)
    assert len(proposals) == 1
    seed = proposals[0]
    assert seed["source"] == "datadog"
    assert seed["panel_title"] == "Datadog CPU Alias Bug"
    assert seed["disposition"] == "real_bug"
    assert seed["esql_query"] == bad_query
    assert seed["rule_hint"] == StructuralRuleId.EVAL_UNDEFINED_COLUMN.value


def test_oracle_clean_query_passes_and_corruptions_error():
    assert structural_errors(check_esql_structure(_GOOD_ALIAS_QUERY)) == []
    alias_errs = structural_errors(check_esql_structure(corrupt_break_eval_alias(_GOOD_ALIAS_QUERY)))
    assert any(e.rule_id == StructuralRuleId.EVAL_UNDEFINED_COLUMN for e in alias_errs)
    assert unknown_column_looks_like_alias_bug(
        "node_cpu_scaling_frequency_hertz",
        corrupt_break_eval_alias(_GOOD_ALIAS_QUERY),
    )

    assert structural_errors(check_esql_structure(_GOOD_CASE_WRAP_QUERY)) == []
    case_errs = structural_errors(check_esql_structure(corrupt_to_inner_case_irate(_GOOD_CASE_WRAP_QUERY)))
    assert any(e.rule_id == StructuralRuleId.STATS_TS_CASE_VALUE_ARG for e in case_errs)


def test_intake_proposes_seed_for_structural_mutation(tmp_path):
    bad_query = corrupt_break_eval_alias(_GOOD_ALIAS_QUERY)
    report = {
        "source": "grafana",
        "panels": [
            {
                "title": "CPU Frequency Scaling",
                "status": "fail",
                "disposition": "real_bug",
                "error": "Unknown column [node_cpu_scaling_frequency_hertz]",
                "esql_query": bad_query,
                "targets": [{"expr": "node_cpu_scaling_frequency_hertz"}],
            }
        ],
    }
    report_path = tmp_path / "smoke.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--report",
            str(report_path),
            "--out-dir",
            str(tmp_path / "seeds"),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    proposals = json.loads(proc.stdout)
    assert len(proposals) == 1
    seed = proposals[0]
    assert seed["panel_title"] == "CPU Frequency Scaling"
    assert seed["disposition"] == "real_bug"
    assert seed["esql_query"] == bad_query
    assert seed["promql_or_targets"] == [{"expr": "node_cpu_scaling_frequency_hertz"}]
    assert seed["rule_hint"] == StructuralRuleId.EVAL_UNDEFINED_COLUMN.value


def test_intake_flips_data_gap_when_alias_bug(tmp_path):
    bad_query = corrupt_break_eval_alias(_GOOD_ALIAS_QUERY)
    report = {
        "panels": [
            {
                "title": "CPU Frequency Scaling",
                "status": "fail",
                "disposition": "data_gap",
                "error": "Unknown column [node_cpu_scaling_frequency_hertz]",
                "esql_query": bad_query,
                "targets": [{"expr": "node_cpu_scaling_frequency_hertz"}],
            }
        ],
    }
    report_path = tmp_path / "live.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--report",
            str(report_path),
            "--out-dir",
            str(tmp_path / "seeds"),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    proposals = json.loads(proc.stdout)
    assert len(proposals) == 1
    assert proposals[0]["disposition"] == "real_bug"


def test_intake_skips_plain_data_gap(tmp_path):
    query = (
        "TS metrics-*\n"
        "| STATS x = AVG(RATE(http_requests_total)) BY time_bucket = TBUCKET(5 minute)\n"
    )
    report = {
        "panels": [
            {
                "title": "HTTP rate",
                "status": "fail",
                "disposition": "data_gap",
                "error": "Unknown column [http_requests_total]",
                "esql_query": query,
                "targets": [{"expr": "rate(http_requests_total[5m])"}],
            }
        ],
    }
    report_path = tmp_path / "gap.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--report",
            str(report_path),
            "--out-dir",
            str(tmp_path / "seeds"),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == []


def test_check_exits_nonzero_when_new_seed_missing(tmp_path):
    bad_query = corrupt_break_eval_alias(_GOOD_ALIAS_QUERY)
    report = {
        "panels": [
            {
                "title": "CPU Frequency Scaling",
                "status": "fail",
                "disposition": "real_bug",
                "error": "Unknown column [node_cpu_scaling_frequency_hertz]",
                "esql_query": bad_query,
                "targets": [{"expr": "node_cpu_scaling_frequency_hertz"}],
            }
        ],
    }
    report_path = tmp_path / "smoke.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    out_dir = tmp_path / "seeds"
    out_dir.mkdir()

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--report",
            str(report_path),
            "--out-dir",
            str(out_dir),
            "--check",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc.returncode == 1, proc.stdout


@pytest.mark.parametrize("flag", ["--dry-run", "--check"])
def test_check_passes_when_seed_already_committed(tmp_path, flag):
    bad_query = corrupt_break_eval_alias(_GOOD_ALIAS_QUERY)
    report = {
        "panels": [
            {
                "title": "CPU Frequency Scaling",
                "status": "fail",
                "disposition": "real_bug",
                "error": "Unknown column [node_cpu_scaling_frequency_hertz]",
                "esql_query": bad_query,
                "targets": [{"expr": "node_cpu_scaling_frequency_hertz"}],
            }
        ],
    }
    report_path = tmp_path / "smoke.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    out_dir = tmp_path / "seeds"
    out_dir.mkdir()
    (out_dir / "cpu_frequency_scaling.json").write_text("{}", encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--report",
            str(report_path),
            "--out-dir",
            str(out_dir),
            flag,
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr
