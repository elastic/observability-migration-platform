# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Recording-rule gap notes on the ES|QL panel path and preflight contract."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from observability_migration.adapters.source.grafana import preflight
from observability_migration.adapters.source.grafana.panels import translate_panel
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver


class RecordingRuleSurfacingTests(unittest.TestCase):
    def test_esql_path_warns_on_unmapped_recording_rule(self) -> None:
        rule_pack = RulePackConfig()
        resolver = SchemaResolver(rule_pack, field_profile="otel")
        panel = {
            "id": 1,
            "type": "timeseries",
            "title": "Recording Rule",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {
                    "expr": "sum(job:http_requests:rate5m)",
                    "refId": "A",
                }
            ],
        }

        _yaml_panel, result = translate_panel(
            panel,
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=rule_pack,
            resolver=resolver,
        )

        self.assertEqual(result.status, "migrated_with_warnings", result.reasons)
        self.assertTrue(
            any(
                "Recording-rule metric 'job:http_requests:rate5m'" in str(reason)
                for reason in (result.reasons or [])
            ),
            result.reasons,
        )

    def test_preflight_marks_recording_rule_derived_and_actions(self) -> None:
        rule_pack = RulePackConfig()
        resolver = SchemaResolver(rule_pack, field_profile="otel")
        resolver._rule_pack = rule_pack
        panel_result = SimpleNamespace(
            title="RR",
            query_ir={
                "target_index": "metrics-*",
                "source_type": "FROM",
                "metric": "job:http_requests:rate5m",
                "source_metric": "job:http_requests:rate5m",
            },
            reasons=[],
            inventory={},
            contract_evaluation={},
            fulfillment_plan={},
            verification_packet={"semantic_gate": "Yellow", "source_execution": {"status": "not_configured"}},
            readiness="",
        )
        dash_result = SimpleNamespace(
            total_panels=1,
            panel_results=[panel_result],
            inventory={"variables": 0},
            dashboard_title="RR dash",
        )
        contract = preflight.build_target_schema_contract([dash_result], resolver=resolver)
        field_info = contract["required_fields"].get("job:http_requests:rate5m") or {}
        self.assertTrue(field_info.get("recording_rule_derived"), field_info)
        self.assertTrue(
            any(
                "Recording-rule metric" in str(gap)
                for gap in (contract.get("metric_map") or {}).get("gaps", [])
            ),
            contract.get("metric_map"),
        )

        report = preflight.build_preflight_report(
            [dash_result],
            validation_summary={},
            validation_records=[],
            verification_payload={},
            schema_contract=contract,
            source_urls_configured=False,
            target_url_configured=False,
        )
        self.assertTrue(
            any("recording-rule" in str(action).lower() for action in report.get("actions") or []),
            report.get("actions"),
        )


if __name__ == "__main__":
    unittest.main()
