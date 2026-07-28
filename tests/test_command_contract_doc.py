# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_README = ROOT / "README.md"
COMMAND_CONTRACT = ROOT / "docs" / "command-contract.md"
DEV_COMMANDS = ROOT / "docs" / "contributing" / "dev-commands.md"
KIBANA_TARGET_DOC = ROOT / "docs" / "targets" / "kibana.md"
GRAFANA_SOURCE_DOC = ROOT / "docs" / "sources" / "grafana.md"
DATADOG_SOURCE_DOC = ROOT / "docs" / "sources" / "datadog.md"
ASSET_MODEL_DOC = ROOT / "docs" / "architecture" / "asset-model.md"
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
INSTALL_OBS_MIGRATE_SKILL = ROOT / ".cursor" / "skills" / "install-obs-migrate" / "SKILL.md"

COMMAND_NOT_FOUND_HEADING = "### If you see `command not found: obs-migrate`"


def command_not_found_section(text: str) -> str:
    """Return only the `command not found` section of a doc.

    Scoping matters here: the surrounding pages already contain `$PKG` forms
    and launcher prose, so unscoped assertions would pass on Quick Start text
    even if the troubleshooting block itself were wrong.
    """
    _, found, rest = text.partition(COMMAND_NOT_FOUND_HEADING)
    assert found, f"missing heading: {COMMAND_NOT_FOUND_HEADING}"
    # Stop at the next `##`/`###` heading so the scope cannot silently widen to
    # end-of-file, or swallow a sibling subsection added later. Single `#` is
    # excluded on purpose: shell comments inside the code blocks start with it.
    end = re.search(r"(?m)^#{2,3} ", rest)
    return rest[: end.start()] if end else rest


def github_anchor(heading: str) -> str:
    """Approximate GitHub's in-page slug for a Markdown heading."""
    slug = heading.lstrip("#").strip().lower()
    slug = re.sub(r"[^\w\- ]", "", slug)  # drops backticks, colons, parentheses
    return slug.replace(" ", "-")


class CommandContractDocTests(unittest.TestCase):
    def test_command_contract_stays_operator_runnable(self):
        # Issue #329: the operator doc must only contain commands someone can
        # run from an installed wheel. Contributor/CI material moved out.
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        for spelling in re.findall(r"\.venv/bin/[\w.-]*", text):
            self.assertEqual(
                spelling,
                ".venv/bin/activate",
                msg=f"operator doc invokes a repo venv path: {spelling}",
            )
        for banned in (
            "bash scripts/",
            "PYTHONPATH=parity-rig",
            "pytest",
            "python -m verifier.",
        ):
            self.assertNotIn(banned, text, msg=f"contributor-only content: {banned}")
        self.assertIn("contributing/dev-commands.md", text)

    def test_dev_commands_doc_holds_the_moved_contributor_content(self):
        text = DEV_COMMANDS.read_text(encoding="utf-8")
        for fragment in (
            "## Verification And Benchmark Gates",
            "verifier.corpus_manifest",
            "## Validation / Verification CLIs",
            "grafana-validate-uploaded",
            "## Script Commands",
            "bash scripts/start_local_lab.sh",
            "scripts/setup_telemetry_data.py",
            "scripts/audit_pipeline.py --update-docs",
            "## Test Commands",
            "pytest tests/",
        ):
            self.assertIn(fragment, text, msg=f"dev-commands.md missing: {fragment}")

    def test_docs_index_links_the_contributor_command_doc(self):
        text = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
        self.assertIn("contributing/dev-commands.md", text)

    def test_command_contract_mentions_assets_flag(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("--assets {dashboards,alerts,all}", text)

    def test_command_contract_documents_list_samples(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("obs-migrate list-samples", text)
        self.assertIn("bundled sample dashboards", text)

    def test_command_contract_does_not_advertise_dead_unified_flags(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        # Guard the dead bare `--include` unified selector without tripping on
        # real, distinct flags such as the smoke validator's `--include-deleted`.
        self.assertNotRegex(text, r"--include(?![-\w])")
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

    def test_dev_commands_use_split_dashboard_upload_path_for_legacy_flow(self):
        # The legacy repo-checkout alert flow lives in the contributor doc; the
        # operator doc keeps only the one-command flow.
        text = DEV_COMMANDS.read_text(encoding="utf-8")
        self.assertIn(
            "--artifact-dir examples/alerting/generated/grafana/dashboards",
            text,
        )
        self.assertIn(
            "--artifact-format yaml",
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

    def test_root_readme_documents_obs_migrate_invocation_safety_net(self):
        """Issue #254: bare obs-migrate without a launcher looks "broken"."""
        text = ROOT_README.read_text(encoding="utf-8")
        self.assertIn('uvx --from "$PKG" obs-migrate doctor', text)
        self.assertIn(COMMAND_NOT_FOUND_HEADING, text)
        self.assertIn("You do not need to clone this repository", text)
        self.assertIn("Always reuse the same launcher", text)
        # `doctor` still warns on 3.14+ (see `app/cli.py`), so the landing page
        # must keep the tested range rather than a bare "3.11 or newer".
        self.assertIn("tested on 3.11–3.13", text)
        # A launcher the docs name as a fix must also be a launcher the docs
        # teach, with a runnable command.
        self.assertIn("uv tool install 'elastic-observability-migration[all]'", text)

        section = command_not_found_section(text)
        # Readers reach this section from a *new* shell, where `PKG` from the
        # Quick Start block is gone, so every command must run as written.
        self.assertNotIn("$PKG", section)
        self.assertIn(
            "uvx --from 'elastic-observability-migration[all]' obs-migrate doctor",
            section,
        )
        self.assertIn("source .venv/bin/activate && obs-migrate doctor", section)
        # The explicit-path launcher belongs in the remedy list itself: it is
        # the only one that needs neither an activate nor `uv`.
        self.assertIn(".venv/bin/obs-migrate doctor", section)
        # `pipx` / `uv tool` installs also yield a working bare command, so the
        # section must offer that instead of implying a bare command never works.
        self.assertIn("uv tool install", section)

        # Operator-first policy (AGENTS.md): the landing page routes
        # contributors to CONTRIBUTING.md rather than teaching repo-checkout
        # commands. Relax the policy before relaxing these bans.
        self.assertNotIn("make bump-version", text)
        self.assertNotIn("make sync", text)
        self.assertNotIn("Contributor checkout", text)

    def test_command_contract_documents_obs_migrate_invocation_safety_net(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn(COMMAND_NOT_FOUND_HEADING, text)
        self.assertIn("same launcher", text)
        # Body examples stay bare; keep the PATH/launcher assumption so readers
        # who jump past Install do not reintroduce issue #254.
        self.assertIn(
            "Every example below assumes `obs-migrate` is on `PATH`",
            text,
        )
        # Jump-to-section install blocks must define PKG before using it.
        self.assertIn(
            "PKG='elastic-observability-migration[all]'\n"
            "python3 -m venv .venv",
            text,
        )
        self.assertIn(
            "PKG='elastic-observability-migration[all]@git+https://"
            "github.com/elastic/observability-migration-platform.git@v0.4.0rc1'\n"
            'uvx --from "$PKG" obs-migrate doctor',
            text,
        )

        section = command_not_found_section(text)
        self.assertNotIn("$PKG", section)
        self.assertIn("source .venv/bin/activate && obs-migrate doctor", section)
        self.assertIn("console script, not a global binary", section)
        self.assertIn("uv tool install 'elastic-observability-migration[all]'", section)
        # Parity with the README safety-net: the portable uvx remedy must be
        # spelled out in full inside this section (fresh shell, no PKG).
        self.assertIn(
            "uvx --from 'elastic-observability-migration[all]' obs-migrate doctor",
            section,
        )

    def test_operator_docs_in_page_anchors_resolve(self):
        # The `command not found` section is reached through an in-page link, so
        # a heading reword must not silently leave a dangling anchor.
        for path in (ROOT_README, COMMAND_CONTRACT):
            text = path.read_text(encoding="utf-8")
            slugs = {github_anchor(h) for h in re.findall(r"(?m)^#{1,6} .+$", text)}
            for link in re.findall(r"\]\(#([^)]+)\)", text):
                self.assertIn(
                    link,
                    slugs,
                    msg=f"{path.name}: in-page link #{link} has no matching heading",
                )
        contract = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn(f"](#{github_anchor(COMMAND_NOT_FOUND_HEADING)})", contract)

    def test_install_skill_documents_the_invocation_safety_net(self):
        # `check_skill_mirror.py` only proves the three copies match each
        # other, so without a content assertion all of them can drift away
        # from the fix together and still pass.
        text = INSTALL_OBS_MIGRATE_SKILL.read_text(encoding="utf-8")
        self.assertIn("command not found: obs-migrate", text)
        self.assertIn("only resolves when its install location is on `PATH`", text)
        self.assertIn("source .venv/bin/activate", text)
        self.assertIn(".venv/bin/obs-migrate", text)
        # The triage table is what an agent reads first, so it must cover the
        # missing-launcher case and not only a missing `uv`.
        self.assertIn("but `uvx` works", text)

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
        self.assertIn("prometheus_metrics", text)
        self.assertIn("prometheus_native", text)
        self.assertIn("schema_change_report.md", text)
        self.assertIn("telemetry_contract.json", text)
        # Metric names are NOT a no-op: they are rewritten per profile.
        self.assertNotIn("PromQL metric names pass through to ES", text)
        self.assertIn("field_capabilities_discovery", text)

    def test_field_profile_contract_docs_cover_new_layouts_and_summary_keys(self):
        grafana_comparison = GRAFANA_SOURCE_DOC.read_text(encoding="utf-8").split(
            "### Comparison with Datadog Field Profiles",
            1,
        )[1].split("### ", 1)[0]
        asset_model = ASSET_MODEL_DOC.read_text(encoding="utf-8")
        command_contract = COMMAND_CONTRACT.read_text(encoding="utf-8")
        datadog = DATADOG_SOURCE_DOC.read_text(encoding="utf-8")

        self.assertIn("prometheus_metrics", grafana_comparison)
        self.assertIn("prometheus_metrics", asset_model)
        self.assertIn("automatic_profile_selection", command_contract)
        self.assertIn("Prometheus profiles therefore keep ECS / OTel log fields", datadog)

    def test_prepare_target_telemetry_skill_routes_pre_migration_setup(self):
        text = PREPARE_TARGET_TELEMETRY_SKILL.read_text(encoding="utf-8")
        # Covers both sources' target-layout mechanics in one place.
        self.assertIn("prometheus_remote_write", text)
        self.assertIn("prometheus_metrics", text)
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
        self.assertIn("prometheus_metrics", text)
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

    def test_command_contract_documents_field_profile_defaults_and_passthrough(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("### Field Profile Contract", text)
        # Default is otel for every source, including Grafana.
        self.assertIn("defaults to `otel` for every source migration, including", text)
        self.assertIn("Grafana", text)
        self.assertIn("**`otel`** (default)", text)
        self.assertIn("**`passthrough`**", text)
        self.assertIn("automatic mapping is disabled", text)
        self.assertIn("`default` (alias of `otel`)", text)
        self.assertIn("--field-profile passthrough", text)
        self.assertIn("Grafana exits `2`, Datadog exits `1`", text)

    def test_command_contract_documents_esql_index_and_data_view_distinction(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        # --esql-index must be defined in the canonical flag table, not just
        # appear inside copy-paste examples (issue #257).
        self.assertIn("| `--esql-index` |", text)
        # Dedicated side-by-side section disambiguating the two flags.
        self.assertIn("### Target index flags: data-view vs esql-index", text)
        # The default-fallback behavior must be stated explicitly.
        self.assertIn("Defaults to `--data-view` when unset", text)
        # --esql-index is the unified metrics query + discovery target.
        self.assertIn("metrics query + schema-discovery target", text)
        self.assertIn("native `PROMQL index=…`", text)
        # The code-accurate fallback expression is documented.
        self.assertIn("args.esql_index or args.data_view", text)
        # Prometheus remediation callout with a worked example.
        self.assertIn("metrics-alloy.prometheus-default", text)
        self.assertIn("Prometheus users", text)
        # --esql-index is Grafana-only today; Datadog has no such flag.
        self.assertIn("Grafana-only today", text)
        # --logs-index does NOT fall back to --data-view (profile log default).
        self.assertIn("does **not** fall back\nto `--data-view`", text)
        self.assertIn("the source/profile log index", text)
        # Native PROMQL and ES|QL share esql_index or data_view (consistency fix).
        self.assertIn("Metrics query target (native PROMQL and ES|QL)", text)
        self.assertIn("esql_index or data_view", text)
        self.assertIn("retargets **both**", text)
        # Issue #284: both operator timelines + concrete-stream rule of thumb.
        self.assertIn("### Migrate-first vs data-first (data plane before assets)", text)
        self.assertIn("Migrate-first (assets before telemetry)", text)
        self.assertIn("Data-first (telemetry already in Elastic)", text)
        self.assertIn("index readiness", text)
        self.assertIn("ingest path → concrete stream", text)
        # The warning is Grafana-only and its findings are also an artifact.
        self.assertIn("`datadog-migrate` does not print this warning yet", text)
        self.assertIn("`run_summary.json` under\n`metrics_target`", text)
        # Issue #284: the two silent-scoping footguns operators hit next.
        self.assertIn(
            "#### The `data_stream.dataset` filter is scoped to wildcard targets", text
        )
        self.assertIn("#### `--logs-index` is independent of `--data-view`", text)

    def test_command_contract_documents_dedicated_cli_input_mode_parity(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("They accept the same `--input-mode {files,api}`", text)
        self.assertIn("`--source files|api`", text)
        self.assertIn("--no-compile", text)
        # Phrase may wrap across a Markdown line break.
        self.assertRegex(
            text,
            r"Upload deploys through Kibana's\s+typed Dashboards API by default",
        )
        self.assertIn("obs-migrate migrate --source <source> --input-mode files", text)

    def test_command_contract_documents_every_obs_migrate_subcommand(self):
        import argparse

        from observability_migration.app import cli as app_cli

        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        parser = app_cli._build_parser()
        subcommands: list[str] = []
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                subcommands = sorted(action.choices)
                break
        self.assertTrue(subcommands, "failed to discover obs-migrate subcommands")
        for name in subcommands:
            with self.subTest(command=name):
                self.assertRegex(
                    text,
                    rf"obs-migrate\s+{re.escape(name)}\b",
                    msg=f"command-contract.md missing `obs-migrate {name}`",
                )

    def test_command_contract_documents_verify_panels_and_verify_visual(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("### Verify Panels (5-tier panel verifier)", text)
        self.assertIn("### Verify Visual (pixel-diff Grafana vs Kibana)", text)
        self.assertIn("obs-migrate verify-panels", text)
        self.assertIn("obs-migrate verify-visual", text)
        self.assertIn("--grafana-slug", text)
        self.assertIn("--kibana-dash-id", text)

    def test_command_contract_documents_cli_parity_matrix(self):
        text = COMMAND_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("### CLI parity (unified vs dedicated)", text)
        self.assertIn("consistent on the shared migration contract", text)
        self.assertIn("`--space-id` → `--shadow-space`", text)
        self.assertIn("`--fetch-monitors`", text)
        self.assertIn("Grafana-only", text)
        self.assertIn("Datadog-only", text)


if __name__ == "__main__":
    unittest.main()
