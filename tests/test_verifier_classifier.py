# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for parity-rig/verifier/classifier.py.

Covers each rule branch in ``classify`` plus the LLM_HOOK override
contract and the CLI's JSON+Markdown output. The rule fixtures are
constructed via ``_make_record`` (mirroring the helper used by
``tests/test_verifier.py``) so the inputs stay close to real
``PanelRecord`` shapes.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parity-rig"))

from verifier import classifier  # noqa: E402
from verifier.records import PanelRecord, Verdict  # noqa: E402

# --------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------- #


def _make_record(**overrides: Any) -> PanelRecord:
    defaults = dict(
        panel_id="p1",
        title="My Panel",
        dashboard_uid="dash-1",
        dashboard_title="My Dashboard",
        t0_source_promql="rate(http_requests_total[5m])",
        t1_translator_esql="TS metrics-* | STATS x = AVG(RATE(http_requests_total, 5m))",
    )
    defaults.update(overrides)
    return PanelRecord(**defaults)


@pytest.fixture(autouse=True)
def _reset_llm_hook():
    """Make sure stray LLM_HOOK assignment doesn't leak between tests."""
    classifier.LLM_HOOK = None
    yield
    classifier.LLM_HOOK = None


# --------------------------------------------------------------------- #
# Rule: schema_resolution (unknown column)
# --------------------------------------------------------------------- #


class TestSchemaResolutionRule:
    def test_unknown_column_in_t5_error_classifies_as_schema_resolution(self):
        record = _make_record(
            t5_response_status=400,
            t5_response_error="parsing_exception: unknown column [host_name] at line 1",
        )
        cl = classifier.classify(record)
        assert cl.category == classifier.CATEGORY_SCHEMA_RESOLUTION
        assert cl.confidence > 0.5
        assert any("host_name" in e for e in cl.evidence)
        assert "host_name" in cl.suggested_action

    def test_unknown_column_extracts_field_name_into_evidence(self):
        record = _make_record(
            t5_response_error="x: unknown column [k8s.pod.name]",
        )
        cl = classifier.classify(record)
        assert cl.category == classifier.CATEGORY_SCHEMA_RESOLUTION
        assert any("k8s.pod.name" in e for e in cl.evidence)


# --------------------------------------------------------------------- #
# Rule: translator_bug (native PROMQL gate, counter gate)
# --------------------------------------------------------------------- #


class TestTranslatorBugRules:
    def test_cannot_infer_label_set_classifies_as_translator_bug(self):
        record = _make_record(
            t5_response_error="cannot infer label set for binary expression",
        )
        cl = classifier.classify(record)
        assert cl.category == classifier.CATEGORY_TRANSLATOR_BUG
        assert "can_use_native_promql" in cl.suggested_action
        assert cl.evidence  # non-empty

    def test_binary_operator_error_classifies_as_translator_bug(self):
        record = _make_record(
            t5_response_error="binary operator '/' is not supported on this index",
        )
        cl = classifier.classify(record)
        assert cl.category == classifier.CATEGORY_TRANSLATOR_BUG
        assert "can_use_native_promql" in cl.suggested_action

    def test_requires_counter_metric_classifies_as_translator_bug(self):
        record = _make_record(
            t5_response_error="rate() requires a counter metric, got gauge",
        )
        cl = classifier.classify(record)
        assert cl.category == classifier.CATEGORY_TRANSLATOR_BUG
        assert "_COUNTER_TO_GAUGE_FALLBACK" in cl.suggested_action


# --------------------------------------------------------------------- #
# Rule: transient_cluster (NOT a translator bug)
# --------------------------------------------------------------------- #


class TestTransientClusterRule:
    @pytest.mark.parametrize(
        "err",
        [
            "Data too large [parent_breaker]",
            "circuit_breaker_exception: heap usage exceeded",
        ],
    )
    def test_circuit_breaker_classifies_as_transient_cluster(self, err):
        record = _make_record(t5_response_error=err)
        cl = classifier.classify(record)
        assert cl.category == classifier.CATEGORY_TRANSIENT_CLUSTER
        assert cl.confidence >= 0.9
        # MUST be explicit that this is not a translator bug.
        assert "translator" not in cl.suggested_action.lower() or "not" in cl.rationale.lower()

    def test_circuit_breaker_takes_precedence_over_translator_bug(self):
        # If both signals appear, the operator-actionable transient
        # verdict is the more useful one.
        record = _make_record(
            t5_response_error="Data too large; cannot infer label set",
        )
        cl = classifier.classify(record)
        assert cl.category == classifier.CATEGORY_TRANSIENT_CLUSTER


# --------------------------------------------------------------------- #
# Rule: feasibility_gap
# --------------------------------------------------------------------- #


class TestFeasibilityGapRule:
    @pytest.mark.parametrize(
        "token",
        ["histogram_quantile", "topk", "label_replace", "vector", "predict_linear"],
    )
    def test_not_feasible_with_documented_token_classifies_as_feasibility_gap(self, token):
        record = _make_record(
            status="not_feasible",
            t1_translator_esql="",
            t1_notes=[f"PROMQL `{token}` has no ES|QL equivalent"],
        )
        cl = classifier.classify(record)
        assert cl.category == classifier.CATEGORY_FEASIBILITY_GAP
        assert any(token in e for e in cl.evidence)
        assert token in cl.suggested_action

    def test_not_feasible_without_documented_token_falls_through(self):
        record = _make_record(
            status="not_feasible",
            t1_translator_esql="",
            t1_notes=["something completely undocumented"],
        )
        cl = classifier.classify(record)
        assert cl.category == classifier.CATEGORY_UNKNOWN


# --------------------------------------------------------------------- #
# Rule: kibana_cache_stale
# --------------------------------------------------------------------- #


class TestKibanaCacheStaleRule:
    def test_t3_t4_drift_with_old_cluster_object_classifies_as_stale(self):
        cluster_ts = "2026-05-10T08:00:00Z"
        yaml_mtime = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
        record = _make_record(
            drift_axes=["T3=T4"],
            t4_saved_object_updated_at=cluster_ts,
        )
        cl = classifier.classify(record, yaml_mtime=yaml_mtime)
        assert cl.category == classifier.CATEGORY_KIBANA_CACHE_STALE
        assert cl.confidence >= 0.85
        assert "obs-migrate" in cl.suggested_action
        assert any("T3=T4" in e for e in cl.evidence)

    def test_t3_t4_drift_without_yaml_mtime_still_classifies_with_lower_confidence(self):
        record = _make_record(
            drift_axes=["T3=T4"],
            t4_saved_object_updated_at="2026-05-10T08:00:00Z",
        )
        cl = classifier.classify(record)  # no yaml_mtime
        assert cl.category == classifier.CATEGORY_KIBANA_CACHE_STALE
        assert cl.confidence < 0.85

    def test_drift_axis_other_than_t3_t4_does_not_trigger_stale(self):
        record = _make_record(drift_axes=["T1=T2"])
        cl = classifier.classify(record)
        assert cl.category != classifier.CATEGORY_KIBANA_CACHE_STALE


# --------------------------------------------------------------------- #
# Rule: lens_visual_mismatch
# --------------------------------------------------------------------- #


class TestLensVisualMismatchRule:
    def test_visual_diff_above_threshold_with_all_pass_yields_lens_visual_mismatch(self):
        record = _make_record(
            verdict=Verdict.PASS,
            visual_diff_score=0.42,
            visual_diff_threshold=0.15,
            t5_response_status=200,
            t5_response_row_count=10,
        )
        cl = classifier.classify(record)
        assert cl.category == classifier.CATEGORY_LENS_VISUAL_MISMATCH
        assert "Lens" in cl.suggested_action or "lens" in cl.suggested_action
        # Evidence MUST include the score and threshold for the operator
        # to act on.
        evidence_blob = " ".join(cl.evidence)
        assert "0.4200" in evidence_blob or "0.42" in evidence_blob

    def test_visual_diff_below_threshold_does_not_trigger_lens_mismatch(self):
        record = _make_record(
            verdict=Verdict.PASS,
            visual_diff_score=0.05,
            visual_diff_threshold=0.15,
            t5_response_status=200,
            t5_response_row_count=10,
        )
        cl = classifier.classify(record)
        assert cl.category != classifier.CATEGORY_LENS_VISUAL_MISMATCH

    def test_visual_diff_with_drift_does_not_trigger_lens_mismatch(self):
        record = _make_record(
            verdict=Verdict.DRIFT,
            visual_diff_score=0.4,
            visual_diff_threshold=0.15,
            drift_axes=["T1=T2"],
        )
        cl = classifier.classify(record)
        # Should NOT be lens_visual_mismatch because the structural
        # tiers do NOT all pass.
        assert cl.category != classifier.CATEGORY_LENS_VISUAL_MISMATCH


# --------------------------------------------------------------------- #
# Rule: data_gap
# --------------------------------------------------------------------- #


class TestDataGapRule:
    def test_zero_rows_with_working_promql_classifies_as_data_gap(self):
        record = _make_record(
            t0_source_promql="up",
            verdict=Verdict.PASS,
            t5_response_status=200,
            t5_response_row_count=0,
        )
        cl = classifier.classify(record)
        assert cl.category == classifier.CATEGORY_DATA_GAP
        assert "@timestamp" in cl.suggested_action

    def test_zero_rows_but_no_promql_does_not_trigger_data_gap(self):
        record = _make_record(
            t0_source_promql="",
            t5_response_status=200,
            t5_response_row_count=0,
        )
        cl = classifier.classify(record)
        assert cl.category != classifier.CATEGORY_DATA_GAP


# --------------------------------------------------------------------- #
# Rule: unknown
# --------------------------------------------------------------------- #


class TestUnknownFallthrough:
    def test_no_matching_signal_yields_unknown_low_confidence(self):
        record = _make_record(
            verdict=Verdict.SKIP,
            t1_translator_esql="",
        )
        cl = classifier.classify(record)
        assert cl.category == classifier.CATEGORY_UNKNOWN
        assert cl.confidence <= 0.2

    def test_unknown_evidence_includes_drift_axes_and_verdict(self):
        record = _make_record(verdict=Verdict.SKIP, drift_axes=["T1=T2"])
        cl = classifier.classify(record)
        assert any("verdict=" in e for e in cl.evidence)
        assert any("drift_axes" in e for e in cl.evidence)


# --------------------------------------------------------------------- #
# LLM_HOOK contract
# --------------------------------------------------------------------- #


class TestLLMHook:
    def test_llm_hook_overrides_rule_verdict(self):
        record = _make_record(
            t5_response_error="unknown column [foo]",
        )
        baseline = classifier.classify(record)
        assert baseline.category == classifier.CATEGORY_SCHEMA_RESOLUTION

        def hook(rec, rule):
            assert rule.category == classifier.CATEGORY_SCHEMA_RESOLUTION
            return classifier.Classification(
                category=classifier.CATEGORY_TRANSLATOR_BUG,
                confidence=0.99,
                rationale="LLM thinks otherwise",
                suggested_action="LLM action",
                evidence=["LLM said so"],
            )

        classifier.LLM_HOOK = hook
        cl = classifier.classify(record)
        assert cl.category == classifier.CATEGORY_TRANSLATOR_BUG
        assert cl.confidence == 0.99
        assert "LLM" in cl.rationale

    def test_llm_hook_exception_keeps_rule_verdict(self):
        record = _make_record(t5_response_error="unknown column [foo]")

        def boom(rec, rule):
            raise RuntimeError("upstream LLM unreachable")

        classifier.LLM_HOOK = boom
        cl = classifier.classify(record)
        # Falls back to the rule verdict.
        assert cl.category == classifier.CATEGORY_SCHEMA_RESOLUTION

    def test_llm_hook_returning_non_classification_keeps_rule_verdict(self):
        record = _make_record(t5_response_error="unknown column [foo]")

        classifier.LLM_HOOK = lambda rec, rule: {"category": "garbage"}  # type: ignore[assignment,return-value]
        cl = classifier.classify(record)
        assert cl.category == classifier.CATEGORY_SCHEMA_RESOLUTION


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #


def _verifier_payload(panels: list[PanelRecord]) -> dict:
    return {
        "dashboard_id": "dash-1",
        "dashboard_title": "My Dashboard",
        "verdict_counts": {},
        "drift_axis_counts": {},
        "panels": [r.to_jsonable() for r in panels],
    }


class TestClassifierCLI:
    def test_cli_writes_merged_json_and_markdown(self, tmp_path):
        records = [
            _make_record(
                title="bad-schema",
                t5_response_status=400,
                t5_response_error="unknown column [host]",
            ),
            _make_record(
                title="bad-promql-gate",
                t5_response_error="cannot infer label set",
            ),
            _make_record(
                title="cluster-ooom",
                t5_response_error="circuit_breaker_exception",
            ),
        ]
        report_path = tmp_path / "verifier.json"
        report_path.write_text(json.dumps(_verifier_payload(records)))

        out_path = tmp_path / "out" / "classified.json"
        rc = classifier.main(
            [
                "--verifier-report", str(report_path),
                "--output", str(out_path),
            ]
        )
        assert rc == 0
        data = json.loads(out_path.read_text())
        cats = {p["title"]: p["classification"]["category"] for p in data["panels"]}
        assert cats["bad-schema"] == classifier.CATEGORY_SCHEMA_RESOLUTION
        assert cats["bad-promql-gate"] == classifier.CATEGORY_TRANSLATOR_BUG
        assert cats["cluster-ooom"] == classifier.CATEGORY_TRANSIENT_CLUSTER
        assert data["classification_summary"][classifier.CATEGORY_SCHEMA_RESOLUTION] == 1
        assert data["classification_summary"][classifier.CATEGORY_TRANSLATOR_BUG] == 1

        md_path = out_path.with_suffix(out_path.suffix + ".md")
        assert md_path.exists()
        md = md_path.read_text()
        assert "# Triage:" in md
        assert "schema_resolution" in md
        assert "translator_bug" in md
        assert "bad-schema" in md

    def test_cli_uses_yaml_dir_to_promote_kibana_cache_stale_confidence(self, tmp_path):
        cluster_ts = "2026-05-01T00:00:00Z"
        record = _make_record(
            title="stale-panel",
            drift_axes=["T3=T4"],
            t4_saved_object_updated_at=cluster_ts,
        )
        report_path = tmp_path / "verifier.json"
        report_path.write_text(json.dumps(_verifier_payload([record])))

        yaml_dir = tmp_path / "yaml"
        yaml_dir.mkdir()
        yaml_file = yaml_dir / "dash.yaml"
        yaml_file.write_text("dashboards: []")
        # Force the YAML mtime to be much newer than the cluster ts.
        new_mtime = (datetime(2026, 5, 11, 12, 0, tzinfo=UTC)).timestamp()
        import os
        os.utime(yaml_file, (new_mtime, new_mtime))

        out_path = tmp_path / "classified.json"
        rc = classifier.main(
            [
                "--verifier-report", str(report_path),
                "--output", str(out_path),
                "--yaml-dir", str(yaml_dir),
            ]
        )
        assert rc == 0
        data = json.loads(out_path.read_text())
        cl = data["panels"][0]["classification"]
        assert cl["category"] == classifier.CATEGORY_KIBANA_CACHE_STALE
        assert cl["confidence"] >= 0.85  # high-confidence path because we have the mtime
