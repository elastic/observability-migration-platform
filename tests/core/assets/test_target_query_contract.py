# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import json
import tempfile
import unittest
from pathlib import Path

from observability_migration.core.assets.target_query_contract import (
    ContractEvaluation,
    FieldRequirement,
    FulfillmentPlan,
    TargetQueryContract,
)
from observability_migration.core.reporting import report
from observability_migration.core.reporting.report import MigrationResult, PanelResult


class TestTargetQueryContract(unittest.TestCase):
    def test_contract_to_dict_includes_field_requirements(self):
        contract = TargetQueryContract(
            canonical_target="ts",
            exactness_class="exact_if_contract_met",
            target_shape={
                "required_index_patterns": ["metrics-*"],
                "target_mode": "all_tsds",
                "allow_index_narrowing": True,
            },
            field_requirements=[
                FieldRequirement(
                    name="http_requests_total",
                    role="metric",
                    type_family="numeric",
                    metric_kind="counter",
                )
            ],
            data_invariants={"raw_counter_samples": ["http_requests_total"]},
            runtime_requirements={"source_command": "TS", "functions": ["RATE", "TBUCKET"]},
            degradation_policy={"fallback": "explicit_only"},
            fulfillment_hints={"allow_index_narrowing": True},
        )

        data = contract.to_dict()

        self.assertEqual(data["canonical_target"], "ts")
        self.assertEqual(data["field_requirements"][0]["metric_kind"], "counter")
        self.assertEqual(data["runtime_requirements"]["source_command"], "TS")

    def test_panel_result_can_store_contract_artifacts(self):
        panel = PanelResult(
            title="CPU",
            grafana_type="timeseries",
            kibana_type="line",
            status="migrated",
            confidence=0.9,
            target_query_contract={"canonical_target": "ts"},
            contract_evaluation={"status": "exact_now"},
            fulfillment_plan={"actions": []},
        )

        self.assertEqual(panel.target_query_contract["canonical_target"], "ts")
        self.assertEqual(panel.contract_evaluation["status"], "exact_now")
        self.assertEqual(panel.fulfillment_plan["actions"], [])

    def test_save_detailed_report_preserves_contract_artifacts(self):
        panel = PanelResult(
            title="CPU",
            grafana_type="timeseries",
            kibana_type="line",
            status="migrated",
            confidence=0.9,
            target_query_contract={"canonical_target": "ts"},
            contract_evaluation={"status": "exact_now"},
            fulfillment_plan={"actions": []},
        )
        result = MigrationResult(
            dashboard_title="Dash",
            dashboard_uid="dash-1",
            total_panels=1,
            panel_results=[panel],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "report.json"
            report.save_detailed_report([result], [], output_path)
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        saved_panel = payload["dashboards"][0]["panels"][0]
        self.assertEqual(saved_panel["target_query_contract"]["canonical_target"], "ts")
        self.assertEqual(saved_panel["contract_evaluation"]["status"], "exact_now")
        self.assertEqual(saved_panel["fulfillment_plan"]["actions"], [])

    def test_save_detailed_report_normalizes_typed_contract_artifacts(self):
        panel = PanelResult(
            title="CPU",
            grafana_type="timeseries",
            kibana_type="line",
            status="migrated",
            confidence=0.9,
            target_query_contract=TargetQueryContract(
                canonical_target="ts",
                exactness_class="exact_if_contract_met",
            ),
            contract_evaluation=ContractEvaluation(status="exact_now"),
            fulfillment_plan=FulfillmentPlan(status="not_required"),
        )
        result = MigrationResult(
            dashboard_title="Dash",
            dashboard_uid="dash-2",
            total_panels=1,
            panel_results=[panel],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "report.json"
            report.save_detailed_report([result], [], output_path)
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        saved_panel = payload["dashboards"][0]["panels"][0]
        self.assertEqual(saved_panel["target_query_contract"]["canonical_target"], "ts")
        self.assertEqual(saved_panel["contract_evaluation"]["status"], "exact_now")
        self.assertEqual(saved_panel["fulfillment_plan"]["status"], "not_required")
