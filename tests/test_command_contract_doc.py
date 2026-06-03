# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_README = ROOT / "README.md"
COMMAND_CONTRACT = ROOT / "docs" / "command-contract.md"
KIBANA_TARGET_DOC = ROOT / "docs" / "targets" / "kibana.md"
GRAFANA_SOURCE_DOC = ROOT / "docs" / "sources" / "grafana.md"
DATADOG_SOURCE_DOC = ROOT / "docs" / "sources" / "datadog.md"
ALERTING_EXAMPLES_README = ROOT / "examples" / "alerting" / "README.md"
MIGRATE_ALL_SUPPORTED_SKILL = ROOT / ".cursor" / "skills" / "migrate-all-supported-assets" / "SKILL.md"
REVERT_MIGRATION_SKILL = ROOT / ".cursor" / "skills" / "revert-migration" / "SKILL.md"


class CommandContractDocTests(unittest.TestCase):
    def test_command_contract_mentions_assets_flag(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("--assets {dashboards,alerts,all}", text)

    def test_command_contract_documents_list_samples(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("obs-migrate list-samples", text)
        self.assertIn("bundled sample dashboards", text)

    def test_command_contract_does_not_advertise_dead_unified_flags(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertNotIn("--include", text)
        self.assertNotIn("--alert-dry-run", text)
        self.assertNotIn("obs-migrate migrate --list-dashboards", text)

    def test_command_contract_describes_legacy_alias_warning_and_dashboard_upgrade(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("always emits a deprecation warning", text)
        self.assertIn("including explicit `--assets dashboards`", text)
        self.assertNotIn("when no explicit asset selector is supplied", text)

    def test_kibana_target_doc_uses_assets_contract_for_alert_rule_creation(self):
        text = KIBANA_TARGET_DOC.read_text(encoding="utf-8")
        self.assertNotIn("Primary, production path.", text)
        self.assertIn("--assets alerts", text)

    def test_command_contract_uses_split_dashboard_upload_path_for_legacy_flow(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn(
            "--yaml-dir examples/alerting/generated/grafana/dashboards/yaml",
            text,
        )
        self.assertNotIn(
            "--yaml-dir examples/alerting/generated/grafana/yaml",
            text,
        )

    def test_command_contract_scopes_offline_output_claims_by_asset_selection(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("`--assets dashboards` or `--assets all`", text)
        self.assertIn("`--assets alerts`", text)
        self.assertIn("alert artifacts", text)

    def test_command_contract_describes_run_summary_as_shared_root_artifact(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("Grafana and Datadog both write a root", text)
        self.assertIn("`run_summary.json`", text)
        self.assertNotIn("Datadog also writes a root", text)

    def test_command_contract_documents_source_specific_validation_streams(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("metrics-prometheus-default", text)
        self.assertIn("metrics-datadog-default", text)
        self.assertIn("logs-generic-default", text)
        self.assertIn("avoid mapping conflicts", text)

    def test_root_readme_does_not_drift_to_legacy_dashboard_paths(self):
        # The root README is intentionally short and routes readers to
        # `docs/command-contract.md` for command snippets (see AGENTS.md).
        # We only guard against drift to legacy/pre-split output paths in
        # case examples are reintroduced.
        text = ROOT_README.read_text(encoding="utf-8")
        self.assertNotIn("--yaml-dir migration_output/yaml", text)
        self.assertNotIn("--output-dir migration_output/compiled", text)

    def test_alerting_examples_readme_uses_split_alert_artifact_paths(self):
        text = ALERTING_EXAMPLES_README.read_text(encoding="utf-8")
        self.assertIn(
            "examples/alerting/generated/grafana/alerts/alert_comparison_results.json",
            text,
        )
        self.assertIn(
            "examples/alerting/generated/datadog/alerts/monitor_migration_results.json",
            text,
        )
        self.assertIn(
            "examples/alerting/generated/datadog/alerts/monitor_comparison_results.json",
            text,
        )
        self.assertNotIn(
            "examples/alerting/generated/grafana/alert_comparison_results.json",
            text,
        )
        self.assertNotIn(
            "examples/alerting/generated/datadog/monitor_migration_results.json",
            text,
        )
        self.assertNotIn(
            "because the current CLI loads dashboards before monitor extraction",
            text,
        )

    def test_command_contract_uses_split_datadog_alert_comparison_path(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("<output-dir>/alerts/monitor_comparison_results.json", text)
        self.assertNotIn("or\n`monitor_comparison_results.json` for Datadog", text)

    def test_command_contract_documents_delete_rules_guardrails(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("obs-migrate delete-rules", text)
        self.assertIn("--confirm", text)
        self.assertIn("--max-pages", text)
        self.assertIn("rule_listing_truncated", text)

    def test_command_contract_documents_seed_sample_data(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("obs-migrate seed-sample-data", text)
        self.assertIn("ES-only", text)

    def test_command_contract_documents_remove_sample_data_failclosed(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("obs-migrate remove-sample-data", text)
        self.assertIn("fail-closed", text)
        self.assertIn("telemetry-data-", text)

    def test_migrate_all_supported_skill_uses_datadog_widget_type(self):
        text = MIGRATE_ALL_SUPPORTED_SKILL.read_text(encoding="utf-8")
        self.assertIn("panels[].datadog_widget_type", text)
        self.assertNotIn("`panels[].grafana_type` (Datadog: widget type)", text)

    def test_revert_skill_does_not_claim_dashboard_delete_dry_run(self):
        text = REVERT_MIGRATION_SKILL.read_text(encoding="utf-8")
        self.assertIn("Dashboard deletion has no dry-run or `--confirm`", text)
        self.assertNotIn("Both revert paths have a **read-only / dry-run first**", text)

    def test_dashboard_delete_docs_match_clear_placeholder_behavior(self):
        contract_text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        revert_text = REVERT_MIGRATION_SKILL.read_text(encoding="utf-8")
        self.assertIn("clears saved objects into `[DELETED]` placeholders", contract_text)
        self.assertNotIn("On Serverless, `delete-dashboards`", contract_text)
        self.assertIn("Dashboards become `[DELETED]` placeholders", revert_text)
        self.assertNotIn("Serverless dashboards become `[DELETED]` placeholders", revert_text)

    def test_grafana_source_doc_defers_command_examples_to_canonical_contract(self):
        text = GRAFANA_SOURCE_DOC.read_text(encoding="utf-8")
        self.assertIn("docs/command-contract.md", text)
        self.assertIn("## Command Coverage", text)
        self.assertIn("--assets {dashboards,alerts,all}", text)
        self.assertNotIn("Inventory (representative)", text)

    def test_datadog_source_doc_defers_command_examples_to_canonical_contract(self):
        text = DATADOG_SOURCE_DOC.read_text(encoding="utf-8")
        self.assertIn("docs/command-contract.md", text)
        self.assertIn("## Command Coverage", text)
        self.assertIn("--assets {dashboards,alerts,all}", text)
        self.assertNotIn("Inventory (representative)", text)


if __name__ == "__main__":
    unittest.main()
