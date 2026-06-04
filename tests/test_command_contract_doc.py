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
REPORT_COVERAGE_SKILL = ROOT / ".cursor" / "skills" / "report-migration-coverage" / "SKILL.md"
EXPLAIN_GAPS_SKILL = ROOT / ".cursor" / "skills" / "explain-migration-gaps" / "SKILL.md"
VALIDATE_SXS_SKILL = ROOT / ".cursor" / "skills" / "validate-side-by-side" / "SKILL.md"
PREPARE_CUTOVER_SKILL = ROOT / ".cursor" / "skills" / "prepare-production-cutover" / "SKILL.md"
REMEDIATE_FIELD_GAPS_SKILL = ROOT / ".cursor" / "skills" / "remediate-field-mapping-gaps" / "SKILL.md"
REVIEW_ALERTS_SKILL = ROOT / ".cursor" / "skills" / "review-and-enable-migrated-alerts" / "SKILL.md"
UNDERSTAND_SCHEMA_SKILL = ROOT / ".cursor" / "skills" / "understand-source-schema" / "SKILL.md"
PREPARE_TARGET_TELEMETRY_SKILL = ROOT / ".cursor" / "skills" / "prepare-target-telemetry" / "SKILL.md"


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

    def test_command_contract_documents_compare(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("obs-migrate compare", text)
        self.assertIn("native PROMQL", text)
        self.assertIn("comparison_report", text)

    def test_migrate_all_supported_skill_uses_datadog_widget_type(self):
        text = MIGRATE_ALL_SUPPORTED_SKILL.read_text(encoding="utf-8")
        self.assertIn("panels[].datadog_widget_type", text)
        self.assertNotIn("`panels[].grafana_type` (Datadog: widget type)", text)

    def test_revert_skill_does_not_claim_dashboard_delete_dry_run(self):
        text = REVERT_MIGRATION_SKILL.read_text(encoding="utf-8")
        self.assertIn("Dashboard deletion has no dry-run or `--confirm`", text)
        self.assertNotIn("Both revert paths have a **read-only / dry-run first**", text)

    def test_report_coverage_skill_reads_real_artifacts(self):
        text = REPORT_COVERAGE_SKILL.read_text(encoding="utf-8")
        self.assertIn("migration_summary.md", text)
        self.assertIn("migration_manifest.json", text)
        self.assertIn("run_summary.json", text)
        # Honest about partial success
        self.assertIn("exit 0", text)

    def test_explain_gaps_skill_uses_real_status_vocab_and_is_honest(self):
        text = EXPLAIN_GAPS_SKILL.read_text(encoding="utf-8")
        self.assertIn("not_feasible", text)
        self.assertIn("requires_manual", text)
        self.assertIn("transformation_redesign_tasks", text)
        self.assertIn("blocked", text)  # Datadog-only status surfaced
        # Honest about the grafana-only richer explanations
        self.assertIn("--review-explanations", text)
        self.assertIn("comparison_report", text)  # parity-FAIL handoff from validate-side-by-side

    def test_validate_sxs_skill_wraps_compare_and_is_honest(self):
        text = VALIDATE_SXS_SKILL.read_text(encoding="utf-8")
        self.assertIn("obs-migrate compare", text)
        self.assertIn("comparison_report", text)
        self.assertIn("not numerically verified", text)  # honest about structural fallback

    def test_prepare_cutover_skill_stitches_existing_skills_and_artifacts(self):
        text = PREPARE_CUTOVER_SKILL.read_text(encoding="utf-8")
        self.assertIn("report-migration-coverage", text)
        self.assertIn("validate-side-by-side", text)
        self.assertIn("explain-migration-gaps", text)
        self.assertIn("revert-migration", text)
        self.assertIn("run_summary.json", text)
        self.assertIn("go/no-go", text)

    def test_remediate_field_mapping_gaps_skill_uses_package_native_artifacts(self):
        text = REMEDIATE_FIELD_GAPS_SKILL.read_text(encoding="utf-8")
        self.assertIn("<output-dir>/dashboards/schema_change_report.md", text)
        self.assertIn("obs-migrate schema-report", text)
        self.assertIn("required_target_contract.json", text)
        self.assertIn("target_readiness_contract.json", text)
        self.assertIn("--rules-file", text)
        self.assertIn("--field-profile", text)
        self.assertIn("--suggest-rule-pack-out", text)
        self.assertIn("debug-uploaded-kibana-dashboard", text)

    def test_review_enable_alerts_skill_keeps_alert_rules_safe(self):
        text = REVIEW_ALERTS_SKILL.read_text(encoding="utf-8")
        self.assertIn("obs-migrate verify-alert-rules", text)
        self.assertIn("obs-migrate audit-rules", text)
        self.assertIn("alert_rule_upload_results.json", text)
        self.assertIn("monitor_rule_upload_results.json", text)
        self.assertIn("disabled", text)
        self.assertIn("Do NOT enable", text)

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

    def test_grafana_source_doc_documents_schema_profiles(self):
        text = GRAFANA_SOURCE_DOC.read_text(encoding="utf-8")
        # The verified model is profile-aware: the resolver auto-detects how the
        # Prometheus data landed in Elastic before resolving labels/metrics.
        self.assertIn("prometheus_remote_write", text)
        self.assertIn("prometheus_native", text)
        self.assertIn("schema_change_report.md", text)
        self.assertIn("telemetry_contract.json", text)
        # Metric names are NOT a no-op: they are rewritten per profile.
        self.assertNotIn("PromQL metric names pass through to ES", text)
        self.assertIn("field_capabilities_discovery", text)

    def test_prepare_target_telemetry_skill_routes_pre_migration_setup(self):
        text = PREPARE_TARGET_TELEMETRY_SKILL.read_text(encoding="utf-8")
        # Covers both sources' target-layout mechanics in one place.
        self.assertIn("prometheus_remote_write", text)
        self.assertIn("prometheus_native", text)
        self.assertIn("--field-profile", text)
        # Datadog has NO auto-detection (the key honesty contrast vs Prometheus).
        self.assertIn("auto-detect", text)
        # Ingest-first dependency + the package-native verify surface.
        self.assertIn("--es-url", text)
        self.assertIn("seed-sample-data", text)
        self.assertIn("required_target_contract.json", text)
        self.assertIn("target_readiness_contract.json", text)
        self.assertIn("<out>/dashboards/schema_change_report.md", text)
        self.assertIn("obs-migrate schema-report", text)
        # Routes to existing skills instead of duplicating their setup docs.
        self.assertIn("understand-source-schema", text)
        self.assertIn("remediate-field-mapping-gaps", text)

    def test_understand_source_schema_skill_documents_three_profile_model(self):
        text = UNDERSTAND_SCHEMA_SKILL.read_text(encoding="utf-8")
        self.assertIn("prometheus_remote_write", text)
        self.assertIn("prometheus_native", text)
        self.assertIn("_field_caps", text)
        # Honest about the hard dependency: detection needs data already in ES.
        self.assertIn("ingest first", text)
        # The old flat "4-level priority chain" framing is superseded.
        self.assertNotIn("4-level priority chain", text)

    def test_datadog_source_doc_defers_command_examples_to_canonical_contract(self):
        text = DATADOG_SOURCE_DOC.read_text(encoding="utf-8")
        self.assertIn("docs/command-contract.md", text)
        self.assertIn("## Command Coverage", text)
        self.assertIn("--assets {dashboards,alerts,all}", text)
        self.assertNotIn("Inventory (representative)", text)

    def test_datadog_source_doc_documents_target_readiness_contract(self):
        text = DATADOG_SOURCE_DOC.read_text(encoding="utf-8")
        self.assertIn("schema_change_report.md", text)
        self.assertIn("telemetry_contract.json", text)
        self.assertIn("target_readiness_contract.json", text)
        self.assertIn("field_profile", text)
        self.assertIn("confirmed", text)
        self.assertIn("missing", text)
        self.assertIn("unknown", text)
        self.assertIn("explicit override", text)

    def test_command_contract_documents_source_specific_readiness_artifacts(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("Dashboard migrations also write `schema_change_report.md`", text)
        self.assertIn("`telemetry_contract.json`", text)
        self.assertIn("required_target_contract.json", text)
        self.assertIn("target_readiness_contract.json", text)
        self.assertIn("field_capabilities_discovery", text)
        self.assertIn("Datadog `--data-view` is an explicit override", text)

    def test_command_contract_documents_dedicated_cli_input_mode_parity(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("They accept the same `--input-mode {files,api}`", text)
        self.assertIn("`--source files|api`", text)
        self.assertIn("--no-compile", text)
        self.assertIn("Upload still compiles", text)
        self.assertIn("obs-migrate migrate --source <source> --input-mode files", text)


if __name__ == "__main__":
    unittest.main()
