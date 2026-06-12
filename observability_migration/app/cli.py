# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Unified source-agnostic CLI entry point.

Orchestrates migrations by calling source adapters and shared
Kibana target runtime directly, without delegating to the
dedicated source CLIs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

import observability_migration.adapters.source.datadog.adapter
import observability_migration.adapters.source.grafana.adapter
import observability_migration.targets.kibana.adapter  # noqa: F401
from observability_migration.core.cli_contract import ASSET_CHOICES, normalize_requested_assets
from observability_migration.core.http import resolve_tls
from observability_migration.core.interfaces.registries import source_registry, target_registry
from observability_migration.core.sample_data import (
    NetworkError,
    load_metric_kind_overrides,
    make_es_request,
    remove_sample_data,
    seed_sample_data,
)
from observability_migration.core.selection import (
    add_selection_arguments,
    selection_args_to_argv,
)
from observability_migration.core.telemetry_contract import (
    build_combined_telemetry_contract,
    build_schema_change_report,
    build_telemetry_contract,
    write_telemetry_contract,
)
from observability_migration.core.verification.parity_oracle import (
    compare_panel,
    native_promql_available,
)
from observability_migration.sample_dashboards.catalog import list_samples, resolve_input_dir
from observability_migration.targets.kibana.alerting import (
    audit_migrated_rules,
    cleanup_rules,
    collect_emitted_rule_payloads,
    verify_emitted_rule_uploads,
)

_UPLOAD_SHAPE_HELP = (
    "Accepted input shapes: a directory of .yaml files, a dashboard artifact "
    "dir with a 'yaml/' child (for example "
    "'migration_output/dashboards' or 'migration_output/dashboards/yaml'), "
    "or that artifact dir's sibling 'compiled/' directory (for example "
    "'migration_output/dashboards/compiled')."
)


def _env_truthy_default(name: str) -> bool:
    """Default for a store_true flag backed by an environment variable."""
    return str(os.getenv(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def _tls_verify(args: Any) -> bool | str:
    """Resolve the requests ``verify`` setting from --ca-cert / --insecure args."""
    return resolve_tls(
        ca_cert=getattr(args, "ca_cert", "") or "",
        insecure=bool(getattr(args, "insecure", False)),
    )


def _add_tls_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the shared --ca-cert / --insecure TLS flags to a subparser."""
    parser.add_argument(
        "--ca-cert", default=os.getenv("OBS_MIGRATE_CA_CERT", ""),
        help=(
            "Path to a custom CA certificate (bundle) used to verify TLS for all "
            "outbound connections (Elasticsearch, Kibana, Grafana, Prometheus/Loki, "
            "Datadog). Defaults to OBS_MIGRATE_CA_CERT env var."
        ),
    )
    parser.add_argument(
        "--insecure", action="store_true",
        default=_env_truthy_default("OBS_MIGRATE_INSECURE"),
        help=(
            "Disable TLS certificate verification for all outbound connections. "
            "Insecure — for testing or trusted migration environments only. "
            "Defaults to OBS_MIGRATE_INSECURE env var."
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obs-migrate",
        description="Source-agnostic observability migration platform.",
    )
    sub = parser.add_subparsers(dest="command")

    migrate = sub.add_parser("migrate", help="Run a migration")
    migrate.add_argument(
        "--source", choices=source_registry.names(), required=True,
        help="Source vendor (grafana, datadog, ...)",
    )
    migrate.add_argument("--input-mode", default="files", choices=["files", "api"])
    migrate.add_argument("--input-dir", default=".")
    migrate.add_argument("--output-dir", default="migration_output")
    migrate.add_argument("--target", default="kibana")
    migrate.add_argument(
        "--data-view",
        default="",
        help="Elasticsearch data view or index pattern (source default when omitted)",
    )
    migrate.add_argument(
        "--assets",
        choices=ASSET_CHOICES,
        default="dashboards",
        help="Asset family to migrate: dashboards only, alerts only, or both",
    )
    migrate.add_argument("--esql-index", default="")
    migrate.add_argument("--logs-index", default="")
    native_promql_group = migrate.add_mutually_exclusive_group()
    native_promql_group.add_argument(
        "--native-promql",
        dest="native_promql_flag",
        action="store_const",
        const="force_on",
        help=(
            "Force native PROMQL emission regardless of cluster support detection "
            "(for Elastic clusters with the ES|QL PROMQL command). Forwarded to "
            "the underlying source adapter."
        ),
    )
    native_promql_group.add_argument(
        "--no-native-promql",
        dest="native_promql_flag",
        action="store_const",
        const="force_off",
        help=(
            "Force ES|QL translation even when the cluster supports the PROMQL command "
            "(opt out of the auto-detected default). Forwarded to the underlying "
            "source adapter."
        ),
    )
    migrate.set_defaults(native_promql_flag="auto")
    migrate.add_argument(
        "--fetch-alerts",
        action="store_true",
        help=(
            "Deprecated compatibility alias for alert-capable runs; "
            "prefer --assets alerts or --assets all."
        ),
    )
    migrate.add_argument(
        "--create-alert-rules", action="store_true",
        help=(
            "Create emitted Kibana alerting rules for alert-capable asset "
            "selection (--assets alerts, --assets all, or the deprecated "
            "--fetch-alerts alias). Rules are created disabled by default, "
            "tagged 'obs-migration'. Requires alert-capable asset selection, "
            "--kibana-url, and --kibana-api-key. Writes "
            "alert_rule_upload_results.json (Grafana) or "
            "monitor_rule_upload_results.json (Datadog) to the output "
            "directory."
        ),
    )
    migrate.add_argument("--grafana-token", default="",
                         help="Grafana API bearer token for alert extraction")
    migrate.add_argument("--monitor-ids", default="",
                         help="Comma-separated Datadog monitor IDs to extract (Datadog only)")
    migrate.add_argument("--monitor-query", default="",
                         help="Datadog monitor search query (Datadog only)")
    migrate.add_argument("--dashboard-ids", default="",
                         help="Comma-separated Datadog dashboard IDs to extract (Datadog only)")
    migrate.add_argument("--alert-uids", default="",
                         help="Comma-separated Grafana unified alert rule UIDs to migrate (Grafana only)")
    migrate.add_argument("--alert-folder", default="",
                         help="Comma-separated Grafana folder UIDs; only unified rules from those folders are migrated (Grafana only)")
    migrate.add_argument("--env-file", default="",
                         help="Path to credentials .env file (Datadog)")
    migrate.add_argument(
        "--field-profile",
        default="otel",
        help=(
            "Target field mapping profile. Defaults to 'otel' for all sources; "
            "Grafana currently supports 'otel' only, while Datadog also supports "
            "Datadog-specific built-ins and YAML profile files."
        ),
    )
    migrate.add_argument(
        "--compile", action="store_true",
        help=(
            "Compile generated YAML to NDJSON. Grafana always compiles as part of "
            "'obs-migrate migrate'; Datadog is now also compiled by default for "
            "parity. The flag is kept for compatibility with the dedicated source CLIs."
        ),
    )
    migrate.add_argument("--validate", action="store_true")
    migrate.add_argument("--upload", action="store_true")
    migrate.add_argument("--es-url", default="")
    migrate.add_argument("--es-api-key", default="")
    migrate.add_argument("--kibana-url", default="")
    migrate.add_argument("--kibana-api-key", default="")
    migrate.add_argument("--space-id", default="")
    migrate.add_argument("--rules-file", action="append", default=[])
    migrate.add_argument("--plugin", action="append", default=[])
    migrate.add_argument("--polish-metadata", action="store_true")
    migrate.add_argument("--preflight", action="store_true")
    migrate.add_argument("--source-execution", action="store_true",
                         help="Execute each panel's source query against the live source API "
                              "(Datadog) to build source/target comparison packets")
    migrate.add_argument("--dataset-filter", default="",
                         help="Explicit data_stream.dataset filter for metrics")
    migrate.add_argument("--logs-dataset-filter", default="",
                         help="Explicit data_stream.dataset filter for logs")
    migrate.add_argument("--smoke", action="store_true")
    migrate.add_argument("--browser-audit", action="store_true")
    migrate.add_argument("--capture-screenshots", action="store_true")
    migrate.add_argument("--smoke-output", default="")
    migrate.add_argument("--smoke-timeout", type=int, default=30)
    migrate.add_argument("--chrome-binary", default="")
    migrate.add_argument("--smoke-report", default="")
    migrate.add_argument(
        "--grafana-url", default="",
        help="Grafana base URL for API extraction (Grafana only; defaults to GRAFANA_URL env var)",
    )
    migrate.add_argument(
        "--grafana-user", default="",
        help="Grafana username for HTTP basic auth (Grafana only; defaults to GRAFANA_USER env var)",
    )
    migrate.add_argument(
        "--grafana-pass", default="",
        help="Grafana password for HTTP basic auth (Grafana only; defaults to GRAFANA_PASS env var)",
    )
    _add_tls_arguments(migrate)
    add_selection_arguments(migrate)

    sub.add_parser("doctor", help="Report environment readiness (kb-dashboard tools, uv)")

    compile_cmd = sub.add_parser("compile", help="Compile YAML to NDJSON")
    compile_cmd.add_argument("--yaml-dir", required=True, help="Directory with dashboard YAML files")
    compile_cmd.add_argument("--output-dir", required=True, help="Output directory for NDJSON")

    upload_cmd = sub.add_parser(
        "upload",
        help="Compile dashboard YAML to NDJSON and upload to Kibana",
        description=(
            "Compile dashboard YAML (via kb-dashboard-cli) and upload the resulting "
            f"NDJSON to Kibana. {_UPLOAD_SHAPE_HELP}"
        ),
    )
    upload_group = upload_cmd.add_mutually_exclusive_group(required=True)
    upload_group.add_argument(
        "--yaml-dir",
        help="Path to a dashboard YAML directory input for compile+upload. "
             f"{_UPLOAD_SHAPE_HELP}",
    )
    upload_group.add_argument(
        "--compiled-dir",
        help="[Deprecated alias for --yaml-dir] Kept for backward compatibility. "
             "May point at the dashboard artifact dir's sibling 'compiled/' directory "
             "(for example 'migration_output/dashboards/compiled'). Despite the name, "
             "this upload step recompiles YAML from the matching 'yaml/' directory; "
             "it does not consume pre-compiled NDJSON.",
    )
    upload_cmd.add_argument("--kibana-url", required=True)
    upload_cmd.add_argument("--kibana-api-key", default="")
    upload_cmd.add_argument("--space-id", default="")
    _add_tls_arguments(upload_cmd)

    cluster_cmd = sub.add_parser("cluster", help="Manage target Kibana cluster")
    cluster_cmd.add_argument("action", choices=["list-dashboards", "ensure-data-views", "delete-dashboards", "detect-serverless"],
                             help="Cluster management action")
    cluster_cmd.add_argument("--kibana-url", required=True)
    cluster_cmd.add_argument("--kibana-api-key", default="")
    cluster_cmd.add_argument("--space-id", default="")
    cluster_cmd.add_argument("--dashboard-ids", default="",
                             help="Comma-separated dashboard IDs (for delete-dashboards)")
    cluster_cmd.add_argument("--data-view-patterns", default="metrics-*",
                             help="Comma-separated data view patterns (for ensure-data-views)")
    _add_tls_arguments(cluster_cmd)

    verify_cmd = sub.add_parser(
        "verify-panels",
        help="Run the 5-tier panel verifier against a migrated dashboard "
             "(source PromQL -> translator -> YAML -> NDJSON -> cluster -> live _query).",
    )
    verify_cmd.add_argument(
        "--migration-out",
        required=True,
        help="Per-dashboard mig-to-kbn output directory (contains migration_report.json, yaml/, compiled/).",
    )
    verify_cmd.add_argument("--kibana-url", default="", help="Kibana base URL (required for T4).")
    verify_cmd.add_argument("--es-url", default="", help="Elasticsearch base URL (required for T5).")
    verify_cmd.add_argument("--api-key", default="", help="Elastic API key (used for both Kibana and ES).")
    verify_cmd.add_argument("--dashboard-id", default="", help="Kibana saved-object id (required for T4/T5).")
    verify_cmd.add_argument("--space", default="default", help="Kibana space (default: default).")
    verify_cmd.add_argument(
        "--output",
        required=True,
        help="Path to write the JSON report; a .md triage doc is written alongside.",
    )
    verify_cmd.add_argument("--es-index", default="", help="Default ES index name for the translator output.")
    verify_cmd.add_argument(
        "--limit", type=int, default=0,
        help="Process at most this many panels (0 = no limit).",
    )
    verify_cmd.add_argument("--verbose", action="store_true", help="Verbose logging.")

    visual_cmd = sub.add_parser(
        "verify-visual",
        help="Pixel-diff a migrated Kibana dashboard against its source Grafana "
             "dashboard. Drives agent-browser over both, captures per-panel "
             "screenshots, and aggregates per-panel + median + p95 diff scores. "
             "Requires the parity-rig docker-compose stack to be running for "
             "Grafana access and (optionally) a bootstrapped agent-browser "
             "state file for Kibana SAML auth.",
    )
    visual_cmd.add_argument("--migration-out", required=True,
                            help="Per-dashboard migration output (contains yaml/, compiled/).")
    visual_cmd.add_argument("--grafana-url", default="http://localhost:23000",
                            help="Parity-rig Grafana base URL (default: http://localhost:23000).")
    visual_cmd.add_argument("--grafana-uid", required=True,
                            help="Source Grafana dashboard UID.")
    visual_cmd.add_argument("--grafana-slug", required=True,
                            help="Source Grafana dashboard slug (appears after the UID in the URL).")
    visual_cmd.add_argument("--kibana-url", required=True,
                            help="Kibana base URL (https://...).")
    visual_cmd.add_argument("--kibana-dash-id", required=True,
                            help="Kibana dashboard saved-object id.")
    visual_cmd.add_argument("--output-dir", required=True,
                            help="Directory for screenshots and per-panel diff images.")
    visual_cmd.add_argument("--report", required=True,
                            help="JSON report output path.")
    visual_cmd.add_argument("--from", dest="from_", default="now-1h",
                            help="Time range start (default: now-1h).")
    visual_cmd.add_argument("--to", default="now", help="Time range end (default: now).")
    visual_cmd.add_argument("--threshold", type=float, default=0.15,
                            help="Per-pixel diff threshold 0..1 (default: 0.15).")
    visual_cmd.add_argument("--wait-extra-seconds", type=int, default=4,
                            help="Wait time after navigation before screenshot (default: 4).")
    visual_cmd.add_argument("--state", default="",
                            help="agent-browser persistent state file (for Kibana SAML).")
    visual_cmd.add_argument("--verbose", action="store_true", help="Verbose logging.")

    extensions_cmd = sub.add_parser("extensions", help="Show adapter extension points")
    extensions_cmd.add_argument(
        "--source", choices=source_registry.names(), required=True,
        help="Source vendor to inspect",
    )
    extensions_cmd.add_argument(
        "--format", choices=["json", "yaml"], default="json",
        help="Output format",
    )
    extensions_cmd.add_argument(
        "--template-only",
        action="store_true",
        help="Print only the starter extension template for the source adapter",
    )
    extensions_cmd.add_argument(
        "--template-out",
        default="",
        help="Write the starter extension template to a file",
    )

    schema_report_cmd = sub.add_parser(
        "schema-report",
        help="Emit a per-panel source-to-target schema-change report from migrated "
             "dashboard artifacts (the package-native form of the telemetry contract).",
        description=(
            "Build a human-readable source-to-target schema report (and, optionally, "
            "the telemetry producer contract JSON) from one or more migrated dashboard "
            "artifact directories. Each artifact dir is a per-source 'dashboards/' "
            "output containing yaml/ and verification_packets.json (for example "
            "'migration_output/dashboards'). Repeat --artifact-dir to merge multiple "
            "sources into one report."
        ),
    )
    schema_report_cmd.add_argument(
        "--artifact-dir",
        dest="artifact_dir",
        action="append",
        required=True,
        help="Migrated dashboard artifact directory (contains yaml/ and "
             "verification_packets.json). Repeat to merge multiple sources.",
    )
    schema_report_cmd.add_argument(
        "--output",
        default="schema_change_report.md",
        help="Markdown output path for the schema-change report "
             "(default: schema_change_report.md).",
    )
    schema_report_cmd.add_argument(
        "--contract-out",
        default="",
        help="Optional path to also write the telemetry producer contract JSON.",
    )

    audit_rules_cmd = sub.add_parser(
        "audit-rules",
        help="Audit migrated Kibana alerting rules (tagged 'obs-migration') and "
             "optionally disable any that are currently enabled.",
        description=(
            "List the alerting rules created by a migration (those tagged "
            "'obs-migration' or named '[migrated] ...') and report which are enabled. "
            "Read-only by default; pass --disable-enabled to disable the enabled "
            "subset. Exit code is non-zero while enabled migrated rules remain "
            "(or remediation fails)."
        ),
    )
    audit_rules_cmd.add_argument("--kibana-url", required=True)
    audit_rules_cmd.add_argument("--kibana-api-key", default="")
    audit_rules_cmd.add_argument("--space-id", default="")
    audit_rules_cmd.add_argument("--per-page", type=int, default=100, help="Rules to fetch per page.")
    audit_rules_cmd.add_argument("--max-pages", type=int, default=20, help="Maximum pages to fetch.")
    audit_rules_cmd.add_argument(
        "--disable-enabled",
        action="store_true",
        help="Disable any migrated rules that are currently enabled.",
    )
    _add_tls_arguments(audit_rules_cmd)

    delete_rules_cmd = sub.add_parser(
        "delete-rules",
        help="Delete the alerting rules created by a migration (tagged "
             "'obs-migration' or named '[migrated] ...'). Dry-run by default; "
             "pass --confirm to actually delete.",
        description=(
            "Revert the alert-rule half of a migration by deleting the rules it "
            "created (those tagged 'obs-migration' or named '[migrated] ...'). "
            "Read-only by default: it lists the rules that would be removed. Pass "
            "--confirm to delete them. Exit code is 2 when the cluster is "
            "unreachable, 1 when any delete fails, and 0 otherwise."
        ),
    )
    delete_rules_cmd.add_argument("--kibana-url", required=True)
    delete_rules_cmd.add_argument("--kibana-api-key", default="")
    delete_rules_cmd.add_argument("--space-id", default="")
    delete_rules_cmd.add_argument("--per-page", type=int, default=100, help="Rules to fetch per page.")
    delete_rules_cmd.add_argument("--max-pages", type=int, default=20, help="Maximum pages to fetch.")
    delete_rules_cmd.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete the migrated rules. Without this flag the command "
             "only reports which rules would be deleted (dry run).",
    )
    _add_tls_arguments(delete_rules_cmd)

    verify_alert_rules_cmd = sub.add_parser(
        "verify-alert-rules",
        help="Round-trip verify emitted alert-rule payloads against Kibana: create "
             "them (disabled), confirm they did not land enabled, then delete them.",
        description=(
            "Create the emitted alert-rule payloads from a migration's comparison "
            "report(s) in Kibana, confirm none came back enabled, then clean them up "
            "(unless --keep-rules). This is a self-cleaning write check. The comparison "
            "JSON is written by an alert-capable migration run (for example "
            "'<output-dir>/alerts/alert_comparison_results.json' for Grafana or "
            "'<output-dir>/alerts/monitor_comparison_results.json' for Datadog)."
        ),
    )
    verify_alert_rules_cmd.add_argument(
        "--comparison",
        dest="comparison_paths",
        action="append",
        required=True,
        help="Comparison JSON path written by an alert-capable migration run. "
             "Repeat to verify payloads from multiple reports.",
    )
    verify_alert_rules_cmd.add_argument("--kibana-url", required=True)
    verify_alert_rules_cmd.add_argument("--kibana-api-key", default="")
    verify_alert_rules_cmd.add_argument("--space-id", default="")
    verify_alert_rules_cmd.add_argument(
        "--limit", type=int, default=0,
        help="Optional max number of emitted payloads to verify (0 = no limit).",
    )
    verify_alert_rules_cmd.add_argument(
        "--keep-rules", action="store_true",
        help="Keep the verification rules instead of deleting them.",
    )
    verify_alert_rules_cmd.add_argument(
        "--name-prefix", default="[verification ",
        help="Prefix for temporary verification rule names.",
    )
    _add_tls_arguments(verify_alert_rules_cmd)

    sub.add_parser(
        "list-samples",
        help="List the bundled sample dashboards (offline, no credentials). Use a "
             "sample's input_dir with 'migrate --input-mode files'.",
        description=(
            "Print a JSON catalog of the sample dashboards bundled with the "
            "package. Each entry includes the resolved input_dir to pass to "
            "'obs-migrate migrate --source <source> --input-mode files "
            "--input-dir <input_dir>'. Read-only and fully offline."
        ),
    )

    seed_cmd = sub.add_parser(
        "seed-sample-data",
        help="Seed synthetic Elasticsearch data for migrated dashboard artifacts so "
             "their panels light up. ES-only; pair with remove-sample-data to clean up.",
        description=(
            "Build a telemetry contract from one or more migrated dashboard artifact "
            "directories and ingest synthetic documents into Elasticsearch so migrated "
            "panels render. ES-only (does not touch Kibana). Exit code is 2 when ES is "
            "unreachable or inputs are invalid, 1 on ingest errors, 0 otherwise."
        ),
    )
    seed_cmd.add_argument("--artifact-dir", dest="artifact_dir", action="append", required=True,
                          help="Migrated dashboard artifact dir (contains yaml/). Repeat to combine.")
    seed_cmd.add_argument("--es-url", default=os.getenv("ELASTICSEARCH_ENDPOINT", os.getenv("ES_URL", "")),
                          help="Elasticsearch URL (defaults to ELASTICSEARCH_ENDPOINT or ES_URL).")
    seed_cmd.add_argument("--api-key", default=os.getenv("KEY", ""), help="Elasticsearch API key (defaults to KEY).")
    seed_cmd.add_argument("--data-hours", type=float, default=2.0, help="Hours of synthetic data to generate.")
    seed_cmd.add_argument("--interval-sec", type=int, default=60, help="Seconds between generated samples.")
    seed_cmd.add_argument("--batch-docs", type=int, default=5000, help="Documents per bulk request.")
    seed_cmd.add_argument("--max-combinations", type=int, default=12, help="Max dimension combinations per stream per timestamp.")
    seed_cmd.add_argument("--no-recreate", action="store_true", help="Skip template/data-stream creation; only ingest.")
    seed_cmd.add_argument("--purge-foreign-streams", action="store_true",
                          help="Delete non-seeder streams overlapping the contract wildcards before seeding.")
    seed_cmd.add_argument("--rules-file", action="append", default=[], help="Rule-pack file with metric_kinds overrides. Repeat to layer.")
    seed_cmd.add_argument("--prometheus-url", default="", help="Optional Prometheus base URL for ground-truth metric types.")
    _add_tls_arguments(seed_cmd)

    remove_cmd = sub.add_parser(
        "remove-sample-data",
        help="Remove synthetic Elasticsearch data previously seeded for migrated "
             "dashboards. Dry-run by default; pass --confirm to actually delete.",
        description=(
            "Tear down seeder-owned data streams and templates for the given migrated "
            "dashboard artifact directories. Fail-closed: only streams provably created "
            "by the seeder are deleted; foreign or unverifiable streams are skipped. "
            "Dry-run by default (reports the plan, deletes nothing); pass --confirm to "
            "delete. Exit code is 2 when ES is unreachable or inputs are invalid, 1 when "
            "any delete fails, 0 otherwise."
        ),
    )
    remove_cmd.add_argument("--artifact-dir", dest="artifact_dir", action="append", required=True,
                            help="Migrated dashboard artifact dir (contains yaml/). Repeat to combine.")
    remove_cmd.add_argument("--es-url", default=os.getenv("ELASTICSEARCH_ENDPOINT", os.getenv("ES_URL", "")),
                            help="Elasticsearch URL (defaults to ELASTICSEARCH_ENDPOINT or ES_URL).")
    remove_cmd.add_argument("--api-key", default=os.getenv("KEY", ""), help="Elasticsearch API key (defaults to KEY).")
    remove_cmd.add_argument("--confirm", action="store_true",
                            help="Actually delete. Without this flag the command only prints the plan (dry-run).")
    _add_tls_arguments(remove_cmd)

    compare_cmd = sub.add_parser(
        "compare",
        help="Side-by-side parity: compare each migrated panel's ES|QL against the "
             "source query using Elasticsearch's native PROMQL oracle (PromQL/Grafana); "
             "degrade to the semantic gate otherwise.",
        description=(
            "Read migrated artifact verification_packets.json and, per panel, run the "
            "emitted ES|QL and native PROMQL(source query) on the target cluster to "
            "compute numeric parity. PromQL panels are numerically verified; Datadog / "
            "non-PromQL / clusters without native PROMQL degrade to a structural "
            "(semantic-gate) report, clearly labeled. Exit 2 when ES is unreachable or "
            "inputs are invalid, 1 when any panel parity FAILs, 0 otherwise."
        ),
    )
    compare_cmd.add_argument("--artifact-dir", dest="artifact_dir", action="append", required=True,
                             help="Migrated dashboard artifact dir (contains verification_packets.json). Repeat to combine.")
    compare_cmd.add_argument("--es-url", default=os.getenv("ELASTICSEARCH_ENDPOINT", os.getenv("ES_URL", "")),
                             help="Elasticsearch URL (defaults to ELASTICSEARCH_ENDPOINT or ES_URL).")
    compare_cmd.add_argument("--api-key", default=os.getenv("KEY", ""), help="Elasticsearch API key (defaults to KEY).")
    compare_cmd.add_argument("--index", default="", help="Override the ES index pattern for the native PROMQL oracle (default: infer per panel).")
    compare_cmd.add_argument("--step-seconds", type=int, default=300, help="Oracle bucket step in seconds.")
    compare_cmd.add_argument("--window-minutes", type=int, default=60, help="Look-back window for the comparison.")
    compare_cmd.add_argument("--report-out", default="comparison_report.json", help="Path for the JSON report (a sibling .md is written too).")
    _add_tls_arguments(compare_cmd)

    return parser


def main(argv: list[str] | None = None) -> None:
    """Unified CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "migrate":
        _run_migrate(args)
    elif args.command == "compile":
        _run_compile(args)
    elif args.command == "upload":
        _run_upload(args)
    elif args.command == "extensions":
        _run_extensions(args)
    elif args.command == "cluster":
        _run_cluster(args)
    elif args.command == "verify-panels":
        _run_verify_panels(args)
    elif args.command == "verify-visual":
        _run_verify_visual(args)
    elif args.command == "schema-report":
        sys.exit(_run_schema_report(args))
    elif args.command == "audit-rules":
        sys.exit(_run_audit_rules(args))
    elif args.command == "delete-rules":
        sys.exit(_run_delete_rules(args))
    elif args.command == "verify-alert-rules":
        sys.exit(_run_verify_alert_rules(args))
    elif args.command == "list-samples":
        sys.exit(_run_list_samples(args))
    elif args.command == "seed-sample-data":
        sys.exit(_run_seed_sample_data(args))
    elif args.command == "remove-sample-data":
        sys.exit(_run_remove_sample_data(args))
    elif args.command == "compare":
        sys.exit(_run_compare(args))
    elif args.command == "doctor":
        _run_doctor()
    else:
        parser.print_help()
        sys.exit(1)


def _run_doctor() -> None:
    """Report environment readiness for the Kibana compile/lint path."""
    import shutil

    from observability_migration.targets.kibana._kbtool import (
        KB_DASHBOARD_TOOL_VERSION,
        KbToolUnavailableError,
        tool_argv,
    )

    print("obs-migrate doctor")
    print(f"  pinned kb-dashboard tool version: {KB_DASHBOARD_TOOL_VERSION}")
    print(f"  uv on PATH: {'yes' if shutil.which('uvx') else 'no'}")
    for tool in ("kb-dashboard-cli", "kb-dashboard-lint"):
        try:
            argv = tool_argv(tool)
            mode = "installed" if argv[0] != "uvx" else "uvx fallback"
            print(f"  {tool}: available ({mode})")
        except KbToolUnavailableError as exc:
            print(f"  {tool}: UNAVAILABLE - {exc}")


def _run_verify_panels(args: Any) -> None:
    """Dispatch to the 5-tier panel verifier."""
    # The verifier lives outside the package import root (in parity-rig/
    # so it can be vendored independently); add it to sys.path here.
    repo_root = Path(__file__).resolve().parents[2]
    verifier_parent = repo_root / "parity-rig"
    if str(verifier_parent) not in sys.path:
        sys.path.insert(0, str(verifier_parent))
    try:
        from verifier.cli import main as verifier_main  # type: ignore
    except ImportError as exc:
        print(f"verifier unavailable: {exc}", file=sys.stderr)
        sys.exit(2)

    argv = [
        "--migration-out", args.migration_out,
        "--output", args.output,
        "--space", args.space,
    ]
    if args.kibana_url:
        argv += ["--kibana-url", args.kibana_url]
    if args.es_url:
        argv += ["--es-url", args.es_url]
    if args.api_key:
        argv += ["--api-key", args.api_key]
    if args.dashboard_id:
        argv += ["--dashboard-id", args.dashboard_id]
    if args.es_index:
        argv += ["--es-index", args.es_index]
    if args.limit:
        argv += ["--limit", str(args.limit)]
    if args.verbose:
        argv += ["--verbose"]
    sys.exit(verifier_main(argv))


def _run_verify_visual(args: Any) -> None:
    """Dispatch to the visual-regression harness.

    Mirrors :func:`_run_verify_panels`: adds the ``parity-rig`` parent
    to ``sys.path`` so the ``verifier.visual_regression`` module
    imports cleanly, then forwards CLI args verbatim.
    """
    repo_root = Path(__file__).resolve().parents[2]
    verifier_parent = repo_root / "parity-rig"
    if str(verifier_parent) not in sys.path:
        sys.path.insert(0, str(verifier_parent))
    try:
        from verifier.visual_regression import main as visual_main  # type: ignore
    except ImportError as exc:
        print(f"visual regression module unavailable: {exc}", file=sys.stderr)
        sys.exit(2)

    argv = [
        "--migration-out", args.migration_out,
        "--grafana-url", args.grafana_url,
        "--grafana-uid", args.grafana_uid,
        "--grafana-slug", args.grafana_slug,
        "--kibana-url", args.kibana_url,
        "--kibana-dash-id", args.kibana_dash_id,
        "--output-dir", args.output_dir,
        "--report", args.report,
        "--from", args.from_,
        "--to", args.to,
        "--threshold", str(args.threshold),
        "--wait-extra-seconds", str(args.wait_extra_seconds),
    ]
    if args.state:
        argv += ["--state", args.state]
    if args.verbose:
        argv += ["--verbose"]
    sys.exit(visual_main(argv))


def _run_migrate(args: Any) -> None:
    """Orchestrate a migration through the adapter registry."""
    source = args.source

    if source == "grafana":
        _run_grafana_migration(args)
    elif source == "datadog":
        _run_datadog_migration(args)
    else:
        print(f"Source '{source}' is not yet supported.", file=sys.stderr)
        sys.exit(1)


def _run_grafana_migration(args: Any) -> None:
    """Run the Grafana migration pipeline directly."""
    from observability_migration.adapters.source.grafana.cli import main as grafana_main

    legacy_argv = [
        "--source", args.input_mode,
        "--input-dir", args.input_dir,
        "--output-dir", args.output_dir,
        "--field-profile", getattr(args, "field_profile", "otel"),
    ]
    if args.data_view:
        legacy_argv[6:6] = ["--data-view", args.data_view]
    requested_assets = getattr(args, "assets", None)
    if requested_assets is not None:
        selection = normalize_requested_assets(
            assets=requested_assets,
            fetch_alerts=getattr(args, "fetch_alerts", False),
            fetch_monitors=False,
        )
        legacy_argv.extend(["--assets", selection.label])
    if args.esql_index:
        legacy_argv.extend(["--esql-index", args.esql_index])
    if args.logs_index:
        legacy_argv.extend(["--logs-index", args.logs_index])
    mode = getattr(args, "native_promql_flag", "auto")
    if mode == "force_on":
        legacy_argv.append("--native-promql")
    elif mode == "force_off":
        legacy_argv.append("--no-native-promql")
    if args.validate:
        legacy_argv.append("--validate")
    if args.upload:
        legacy_argv.append("--upload")
    if args.es_url:
        legacy_argv.extend(["--es-url", args.es_url])
    if args.es_api_key:
        legacy_argv.extend(["--es-api-key", args.es_api_key])
    if args.kibana_url:
        legacy_argv.extend(["--kibana-url", args.kibana_url])
    if args.kibana_api_key:
        legacy_argv.extend(["--kibana-api-key", args.kibana_api_key])
    if getattr(args, "space_id", ""):
        legacy_argv.extend(["--shadow-space", args.space_id])
    for rf in args.rules_file:
        legacy_argv.extend(["--rules-file", rf])
    for pl in args.plugin:
        legacy_argv.extend(["--plugin", pl])
    if args.polish_metadata:
        legacy_argv.append("--polish-metadata")
    if args.preflight:
        legacy_argv.append("--preflight")
    if args.dataset_filter:
        legacy_argv.extend(["--dataset-filter", args.dataset_filter])
    if args.logs_dataset_filter:
        legacy_argv.extend(["--logs-dataset-filter", args.logs_dataset_filter])
    if args.smoke_report:
        legacy_argv.extend(["--smoke-report", args.smoke_report])
    if getattr(args, "create_alert_rules", False):
        legacy_argv.append("--create-alert-rules")
    if getattr(args, "grafana_token", ""):
        legacy_argv.extend(["--grafana-token", args.grafana_token])
    if getattr(args, "grafana_url", ""):
        legacy_argv.extend(["--grafana-url", args.grafana_url])
    if getattr(args, "grafana_user", ""):
        legacy_argv.extend(["--grafana-user", args.grafana_user])
    if getattr(args, "grafana_pass", ""):
        legacy_argv.extend(["--grafana-pass", args.grafana_pass])
    if getattr(args, "ca_cert", ""):
        legacy_argv.extend(["--ca-cert", args.ca_cert])
    if getattr(args, "insecure", False):
        legacy_argv.append("--insecure")
    if getattr(args, "alert_uids", ""):
        legacy_argv.extend(["--alert-uids", args.alert_uids])
    if getattr(args, "alert_folder", ""):
        legacy_argv.extend(["--alert-folder", args.alert_folder])
    legacy_argv.extend(selection_args_to_argv(args))
    smoke_requested = (
        args.smoke
        or args.browser_audit
        or args.capture_screenshots
        or bool(args.smoke_output)
        or bool(args.chrome_binary)
    )
    if smoke_requested:
        if args.smoke:
            legacy_argv.append("--smoke")
        if args.browser_audit:
            legacy_argv.append("--browser-audit")
        if args.capture_screenshots:
            legacy_argv.append("--capture-screenshots")
        if args.smoke_output:
            legacy_argv.extend(["--smoke-output", args.smoke_output])
        legacy_argv.extend(["--smoke-timeout", str(args.smoke_timeout)])
        if args.chrome_binary:
            legacy_argv.extend(["--chrome-binary", args.chrome_binary])
    sys.argv = ["obs-migrate"] + legacy_argv
    grafana_main()


def _run_datadog_migration(args: Any) -> None:
    """Run the Datadog migration pipeline directly."""
    from observability_migration.adapters.source.datadog.cli import main as datadog_main

    legacy_argv = [
        "--source", args.input_mode,
        "--input-dir", args.input_dir,
        "--output-dir", args.output_dir,
    ]
    if args.data_view:
        legacy_argv.extend(["--data-view", args.data_view])
    legacy_argv.extend(["--field-profile", args.field_profile])
    requested_assets = getattr(args, "assets", None)
    if requested_assets is not None:
        selection = normalize_requested_assets(
            assets=requested_assets,
            fetch_alerts=getattr(args, "fetch_alerts", False),
            fetch_monitors=False,
        )
        legacy_argv.extend(["--assets", selection.label])
    if args.logs_index:
        legacy_argv.extend(["--logs-index", args.logs_index])
    if args.es_url:
        legacy_argv.extend(["--es-url", args.es_url])
    if args.es_api_key:
        legacy_argv.extend(["--es-api-key", args.es_api_key])
    legacy_argv.append("--compile")
    if args.validate:
        legacy_argv.append("--validate")
    if args.upload:
        legacy_argv.append("--upload")
    if args.preflight:
        legacy_argv.append("--preflight")
    if getattr(args, "source_execution", False):
        legacy_argv.append("--source-execution")
    if args.dataset_filter:
        legacy_argv.extend(["--dataset-filter", args.dataset_filter])
    if args.logs_dataset_filter:
        legacy_argv.extend(["--logs-dataset-filter", args.logs_dataset_filter])
    if args.kibana_url:
        legacy_argv.extend(["--kibana-url", args.kibana_url])
    if args.kibana_api_key:
        legacy_argv.extend(["--kibana-api-key", args.kibana_api_key])
    if args.space_id:
        legacy_argv.extend(["--space-id", args.space_id])
    if getattr(args, "create_alert_rules", False):
        legacy_argv.append("--create-alert-rules")
    if getattr(args, "monitor_ids", ""):
        legacy_argv.extend(["--monitor-ids", args.monitor_ids])
    if getattr(args, "monitor_query", ""):
        legacy_argv.extend(["--monitor-query", args.monitor_query])
    if getattr(args, "dashboard_ids", ""):
        legacy_argv.extend(["--dashboard-ids", args.dashboard_ids])
    if getattr(args, "env_file", ""):
        legacy_argv.extend(["--env-file", args.env_file])
    if getattr(args, "ca_cert", ""):
        legacy_argv.extend(["--ca-cert", args.ca_cert])
    if getattr(args, "insecure", False):
        legacy_argv.append("--insecure")
    legacy_argv.extend(selection_args_to_argv(args))
    smoke_requested = (
        args.smoke
        or args.browser_audit
        or args.capture_screenshots
        or bool(args.smoke_output)
        or bool(args.chrome_binary)
    )
    if smoke_requested:
        if args.smoke:
            legacy_argv.append("--smoke")
        if args.browser_audit:
            legacy_argv.append("--browser-audit")
        if args.capture_screenshots:
            legacy_argv.append("--capture-screenshots")
        if args.smoke_output:
            legacy_argv.extend(["--smoke-output", args.smoke_output])
        legacy_argv.extend(["--smoke-timeout", str(args.smoke_timeout)])
        if args.chrome_binary:
            legacy_argv.extend(["--chrome-binary", args.chrome_binary])
    sys.argv = ["obs-migrate"] + legacy_argv
    datadog_main()


def _run_compile(args: Any) -> None:
    """Compile dashboard YAML to NDJSON using the shared Kibana target."""
    yaml_dir = Path(args.yaml_dir)
    output_dir = Path(args.output_dir)
    if not yaml_dir.is_dir():
        print(f"YAML directory not found: {yaml_dir}", file=sys.stderr)
        sys.exit(1)

    adapter = target_registry.get("kibana")()
    compile_payload = adapter.compile(yaml_dir, output_dir)
    results = compile_payload["compile_results"]
    ok = compile_payload["summary"]["compiled_ok"]
    total = compile_payload["summary"]["total"]
    print(f"\nCompiled {ok}/{total} dashboards to {output_dir}")
    for item in results:
        status = "OK" if item["success"] else "FAIL"
        print(f"  [{status}] {item['name']}")
        if not item["success"]:
            for line in item["output"].strip().splitlines()[:5]:
                print(f"         {line}")
    lint_status = compile_payload["yaml_lint"]["ok"]
    if lint_status is False:
        print("\nYAML lint failed:")
        for line in compile_payload["yaml_lint"]["output"].strip().splitlines()[:10]:
            print(f"  {line}")
    layout_status = compile_payload["layout"]["ok"]
    if layout_status is False:
        print("\nCompiled layout validation failed:")
        for line in compile_payload["layout"]["output"].strip().splitlines()[:10]:
            print(f"  {line}")
    if ok < total or lint_status is False or layout_status is False:
        sys.exit(1)


def _run_upload(args: Any) -> None:
    """Compile YAML dashboards and upload them to Kibana via kb-dashboard-cli."""
    raw_path = getattr(args, "yaml_dir", None) or getattr(args, "compiled_dir", None) or ""
    if getattr(args, "compiled_dir", None) and not getattr(args, "yaml_dir", None):
        print(
            "  NOTE: --compiled-dir is a deprecated alias for --yaml-dir. "
            "Upload recompiles YAML internally; prefer --yaml-dir in new scripts.",
            file=sys.stderr,
        )
    input_dir = Path(raw_path)
    if not input_dir.is_dir():
        print(f"Input directory not found: {input_dir}", file=sys.stderr)
        sys.exit(1)

    verify = _tls_verify(args)
    adapter = target_registry.get("kibana")()
    upload_payload = adapter.upload(
        input_dir,
        kibana_url=args.kibana_url,
        kibana_api_key=args.kibana_api_key,
        space_id=args.space_id,
        verify=verify,
    )
    if not upload_payload["records"]:
        print(
            f"No dashboard YAML files found under {input_dir}. "
            "Point --yaml-dir at a directory of .yaml files, a dashboard "
            "artifact dir containing 'yaml/' (e.g. "
            "'migration_output/dashboards' or "
            "'migration_output/dashboards/yaml'), or that dir's sibling "
            "'compiled/' directory (e.g. "
            "'migration_output/dashboards/compiled').",
            file=sys.stderr,
        )
        sys.exit(1)

    for item in upload_payload["records"]:
        status = "OK" if item["success"] else "FAIL"
        print(f"  [{status}] {item['yaml_file']}")
        if not item["success"]:
            print(f"         {item['output'][:200]}")
    if upload_payload["summary"]["uploaded_ok"] < upload_payload["summary"]["total"]:
        sys.exit(1)


def _run_extensions(args: Any) -> None:
    """Print the shared extension catalog for a source adapter."""
    adapter_cls = source_registry.get(args.source)
    adapter = adapter_cls()
    if args.template_out:
        template = adapter.build_extension_template()
        template_path = Path(args.template_out)
        template_path.parent.mkdir(parents=True, exist_ok=True)
        template_path.write_text(_serialize_data(template, args.format), encoding="utf-8")
        print(template_path)
        return
    if args.template_only:
        print(_serialize_data(adapter.build_extension_template(), args.format), end="")
        return

    catalog = adapter.build_extension_catalog()
    print(_serialize_data(catalog, args.format), end="")


def _run_schema_report(args: Any) -> int:
    """Emit a source-to-target schema-change report from migrated artifacts.

    Package-native equivalent of scripts/generate_telemetry_contract.py: works
    from an installed wheel without a source checkout.
    """
    artifact_dirs = [Path(path) for path in args.artifact_dir]

    report = build_schema_change_report(artifact_dirs)
    output_path = Path(args.output)
    if output_path.parent != Path(""):
        output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    print(f"Schema change report written: {output_path}")

    if args.contract_out:
        contract = (
            build_telemetry_contract(artifact_dirs[0])
            if len(artifact_dirs) == 1
            else build_combined_telemetry_contract(artifact_dirs)
        )
        write_telemetry_contract(contract, args.contract_out)
        print(f"Telemetry contract written: {args.contract_out}")
    return 0


def _run_audit_rules(args: Any) -> int:
    """Audit migrated Kibana alerting rules; optionally disable enabled ones."""
    result = audit_migrated_rules(
        args.kibana_url,
        api_key=args.kibana_api_key,
        space_id=args.space_id,
        per_page=args.per_page,
        max_pages=args.max_pages,
        disable_enabled=args.disable_enabled,
        verify=_tls_verify(args),
    )
    print(json.dumps(result, indent=2))

    if result.get("errors"):
        return 2
    if args.disable_enabled:
        return 0 if not result["remediation"]["failed_rule_ids"] else 1
    return 0 if not result["enabled_migrated_rule_ids"] else 1


def _run_delete_rules(args: Any) -> int:
    """Delete migrated Kibana alerting rules (dry-run unless --confirm)."""
    verify = _tls_verify(args)
    listing = audit_migrated_rules(
        args.kibana_url,
        api_key=args.kibana_api_key,
        space_id=args.space_id,
        per_page=args.per_page,
        max_pages=args.max_pages,
        disable_enabled=False,
        verify=verify,
    )
    if listing.get("errors"):
        print(json.dumps({"errors": listing["errors"]}, indent=2))
        return 2

    rule_ids = [rid for rid in listing.get("migrated_rule_ids", []) if rid]
    if listing.get("listing_truncated"):
        print(
            json.dumps(
                {
                    "error": "rule_listing_truncated",
                    "listing_truncated": True,
                    "listing_warning": listing.get("listing_warning", ""),
                    "would_delete_count": len(rule_ids),
                    "would_delete_rule_ids": rule_ids,
                },
                indent=2,
            )
        )
        return 2

    if not args.confirm:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "would_delete_count": len(rule_ids),
                    "would_delete_rule_ids": rule_ids,
                    "note": "Re-run with --confirm to delete these rules.",
                },
                indent=2,
            )
        )
        return 0

    cleanup = cleanup_rules(
        args.kibana_url,
        rule_ids,
        api_key=args.kibana_api_key,
        space_id=args.space_id,
        verify=verify,
    )
    print(
        json.dumps(
            {
                "dry_run": False,
                "requested_count": len(rule_ids),
                "deleted_count": cleanup["deleted_count"],
                "failed_rule_ids": cleanup["failed_rule_ids"],
            },
            indent=2,
        )
    )
    return 0 if not cleanup["failed_rule_ids"] else 1


def _run_verify_alert_rules(args: Any) -> int:
    """Round-trip verify emitted alert-rule payloads against Kibana."""
    comparison_paths = [Path(path) for path in args.comparison_paths]
    missing = [str(path) for path in comparison_paths if not path.exists()]
    if missing:
        print(json.dumps({"error": "missing_comparison_files", "paths": missing}, indent=2))
        return 2

    reports = [json.loads(path.read_text(encoding="utf-8")) for path in comparison_paths]
    payloads = collect_emitted_rule_payloads(*reports)
    if args.limit > 0:
        payloads = payloads[: args.limit]
    if not payloads:
        print(json.dumps({"error": "no_emitted_rule_payloads"}, indent=2))
        return 2

    summary = verify_emitted_rule_uploads(
        args.kibana_url,
        payloads,
        api_key=args.kibana_api_key,
        space_id=args.space_id,
        keep_rules=bool(args.keep_rules),
        name_prefix=args.name_prefix,
        verify=_tls_verify(args),
    )
    summary = {
        "comparison_paths": [str(path) for path in comparison_paths],
        **summary,
    }
    print(json.dumps(summary, indent=2))

    if summary.get("error") == "preflight_unreachable":
        return 2
    if (
        summary["creation_errors"]
        or summary["enabled_true_in_create_response"]
        or summary["enabled_true_in_rule_listing"]
        or summary["cleanup"]["failed_rule_ids"]
    ):
        return 1
    return 0


def _run_compare(args: Any) -> int:
    """Per-panel side-by-side parity for migrated dashboards (PromQL native oracle)."""
    if not args.es_url or not args.api_key:
        print(json.dumps({"error": "es_url and api_key are required (or set ELASTICSEARCH_ENDPOINT/KEY)"}, indent=2))
        return 2
    packets: list[dict[str, Any]] = []
    for raw in args.artifact_dir:
        path = Path(raw) / "verification_packets.json"
        if not path.exists():
            print(json.dumps({"error": "missing_verification_packets", "path": str(path)}, indent=2))
            return 2
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(json.dumps({"error": "invalid_verification_packets", "path": str(path), "detail": str(exc)}, indent=2))
            return 2
        if not isinstance(data, dict):
            print(json.dumps({"error": "invalid_verification_packets", "path": str(path), "detail": "expected a JSON object"}, indent=2))
            return 2
        packets.extend(data.get("packets") or [])

    from datetime import UTC, datetime, timedelta
    end = datetime.now(UTC)
    start = end - timedelta(minutes=args.window_minutes)
    start_iso = start.isoformat().replace("+00:00", "Z")
    end_iso = end.isoformat().replace("+00:00", "Z")

    verify = _tls_verify(args)
    request = make_es_request(args.es_url, args.api_key, verify=verify)
    try:
        oracle_ok = native_promql_available(request, args.index or "metrics-*")
    except NetworkError as exc:
        print(json.dumps({"error": "es_unreachable", "detail": str(exc)}, indent=2))
        return 2

    rows: list[dict[str, Any]] = []
    for pkt in packets:
        is_promql = (pkt.get("source_language") == "promql") and bool(pkt.get("source_query")) and bool(pkt.get("translated_query"))
        if oracle_ok and is_promql:
            index = args.index or _infer_index(pkt.get("translated_query", "")) or "metrics-*"
            # A merged multi-target panel with per-target provenance is
            # verified one target at a time: each sub-query against its own
            # output column (formula merge) or its own BY-column value
            # (same-metric collapse). Targets whose distinguishing matcher
            # cannot be replayed client-side surface as SKIP rows with the
            # recorded reason. Without provenance the comparator SKIPs the
            # joined query with an explanation.
            provenance = ((pkt.get("query_ir") or {}).get("metadata") or {}).get("collapsed_targets") or []
            column_targets = [
                t for t in provenance
                if t.get("source_expr") and t.get("value_column") and " ||| " not in t["source_expr"]
            ]
            sub_compares: list[dict[str, Any]] = []
            for t in provenance:
                ref = t.get("ref_id", "")
                expr = t.get("source_expr") or ""
                if t.get("unsupported_reason"):
                    sub_compares.append({"target": ref, "skip_reason": str(t["unsupported_reason"]),
                                         "source_query": expr})
                    continue
                if not expr or " ||| " in expr:
                    continue
                # A negated target (drawn below the axis) emits ``-1 * expr``;
                # negate the native reference to match.
                source = f"-({expr})" if t.get("negated") else expr
                if t.get("value_column"):
                    sub_compares.append({"target": ref, "kwargs": {
                        "source_query": source,
                        "translated_value_column": t["value_column"],
                        "translated_ignore_columns": frozenset(
                            other["value_column"] for other in column_targets if other is not t
                        ),
                    }})
                elif t.get("label_column") and t.get("label_value") is not None:
                    sub_compares.append({"target": ref, "kwargs": {
                        "source_query": source,
                        "translated_label_filter": (t["label_column"], t["label_value"]),
                    }})
                elif t.get("whole_translated"):
                    # Fusion kept only this target; the translated query is
                    # its translation in full.
                    sub_compares.append({"target": ref, "kwargs": {"source_query": source}})
            if not sub_compares:
                sub_compares = [{"target": "", "kwargs": {"source_query": pkt["source_query"]}}]
            for job in sub_compares:
                target_ref = job["target"]
                if "skip_reason" in job:
                    rows.append({
                        "dashboard": pkt.get("dashboard", ""), "panel": pkt.get("panel", ""),
                        "target": target_ref,
                        "mode": "native_oracle", "verdict": "SKIP",
                        "max_relative_error": 0.0, "compared_points": 0,
                        "native_series": 0, "translated_series": 0, "common_series": 0,
                        "notes": [], "reason": job["skip_reason"],
                        "source_query": job["source_query"],
                        "translated_query": pkt.get("translated_query", ""),
                    })
                    continue
                extra = job["kwargs"]
                try:
                    cmp_ = compare_panel(
                        request, translated_query=pkt["translated_query"],
                        index=index, step=args.step_seconds, start_iso=start_iso, end_iso=end_iso,
                        **extra,
                    )
                except NetworkError as exc:
                    print(json.dumps({"error": "es_unreachable", "detail": str(exc)}, indent=2))
                    return 2
                row = {
                    "dashboard": pkt.get("dashboard", ""), "panel": pkt.get("panel", ""),
                    "mode": "native_oracle", "verdict": cmp_.verdict(),
                    "max_relative_error": cmp_.max_relative_error, "compared_points": cmp_.compared_points,
                    "native_series": cmp_.native_series, "translated_series": cmp_.translated_series,
                    "common_series": cmp_.common_series, "notes": list(cmp_.notes),
                    "reason": cmp_.skipped_reason or cmp_.fail_reason or cmp_.translated_error or cmp_.native_error or "",
                    "source_query": extra["source_query"], "translated_query": pkt.get("translated_query", ""),
                }
                if target_ref:
                    row["target"] = target_ref
                rows.append(row)
        else:
            live = _live_source_row(pkt)
            if live is not None:
                rows.append(live)
            else:
                rows.append({
                    "dashboard": pkt.get("dashboard", ""), "panel": pkt.get("panel", ""),
                    "mode": "structural", "verdict": "STRUCTURAL", "semantic_gate": pkt.get("semantic_gate", ""),
                    "reason": "not numerically verified (no native PROMQL oracle / non-PromQL panel)",
                    "source_query": pkt.get("source_query", ""), "translated_query": pkt.get("translated_query", ""),
                })

    summary = {"panels": len(rows)}
    for r in rows:
        summary[r["verdict"]] = summary.get(r["verdict"], 0) + 1
    report = {"summary": summary, "oracle_available": oracle_ok, "panels": rows}
    out = Path(args.report_out)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    out.with_suffix(".md").write_text(_render_compare_md(report), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 1 if any(r["verdict"] in ("FAIL", "SOURCE_FAIL") for r in rows) else 0


# Live source-vs-target verdicts recorded by ``migrate --source-execution
# --validate`` (see core/verification/comparators.py), mapped onto the
# compare-report vocabulary. ``material_drift`` fails the run like a numeric
# FAIL; ``target_broken`` is an ERROR (the target query never ran).
_LIVE_COMPARISON_VERDICTS = {
    "within_tolerance": "SOURCE_PASS",
    "drift": "SOURCE_DRIFT",
    "material_drift": "SOURCE_FAIL",
    "target_broken": "ERROR",
}


def _live_source_row(pkt: dict[str, Any]) -> dict[str, Any] | None:
    comparison = pkt.get("comparison") or {}
    verdict = _LIVE_COMPARISON_VERDICTS.get(str(comparison.get("status", "")))
    if verdict is None:
        return None
    counterexamples = [str(c) for c in (comparison.get("counterexamples") or [])]
    reason = str(comparison.get("reason", "") or "")
    if counterexamples:
        reason = f"{reason}: {counterexamples[0]}" if reason else counterexamples[0]
    return {
        "dashboard": pkt.get("dashboard", ""), "panel": pkt.get("panel", ""),
        "mode": "live_source", "verdict": verdict,
        "semantic_gate": pkt.get("semantic_gate", ""),
        "comparator_family": str(comparison.get("comparator_family", "") or ""),
        "reason": reason,
        "notes": [str(comparison.get("diff_summary", "") or "")],
        "source_query": pkt.get("source_query", ""),
        "translated_query": pkt.get("translated_query", ""),
    }


def _infer_index(esql: str) -> str:
    """Best-effort index pattern from a leading FROM/TS source command."""
    for kw in ("FROM ", "TS "):
        if kw in esql:
            tail = esql.split(kw, 1)[1].strip()
            return tail.split()[0].split("|")[0].strip().rstrip(",") if tail else ""
    return ""


def _render_compare_md(report: dict[str, Any]) -> str:
    lines = ["# Side-by-side comparison", "", f"Oracle available: {report['oracle_available']}", "",
             "| Dashboard | Panel | Mode | Verdict | Max rel err | Series (nat/tr/common) | Reason |",
             "|---|---|---|---|---|---|---|"]
    for r in report["panels"]:
        if r.get("mode") == "native_oracle":
            err = f"{r.get('max_relative_error', 0):.4f}"
            series = f"{r.get('native_series', '-')}/{r.get('translated_series', '-')}/{r.get('common_series', '-')}"
        else:
            err = series = "-"
        lines.append(
            f"| {r.get('dashboard','')} | {r.get('panel','')} | {r.get('mode','')} "
            f"| {r.get('verdict','')} | {err} | {series} | {r.get('reason','')} |"
        )
    return "\n".join(lines) + "\n"


def _run_seed_sample_data(args: Any) -> int:
    """Seed synthetic Elasticsearch data for migrated dashboard artifacts (ES-only)."""
    if not args.es_url or not args.api_key:
        print(json.dumps({"error": "es_url and api_key are required (or set ELASTICSEARCH_ENDPOINT/KEY)"}, indent=2))
        return 2
    artifact_dirs = [Path(p) for p in args.artifact_dir]
    missing = [str(p) for p in artifact_dirs if not p.exists()]
    if missing:
        print(json.dumps({"error": "missing_artifact_dirs", "paths": missing}, indent=2))
        return 2
    if args.data_hours <= 0 or args.interval_sec <= 0 or args.max_combinations <= 0:
        print(json.dumps({"error": "--data-hours/--interval-sec/--max-combinations must be > 0"}, indent=2))
        return 2

    verify = _tls_verify(args)
    overrides = load_metric_kind_overrides(args.rules_file, args.prometheus_url, verify=verify)
    request = make_es_request(args.es_url, args.api_key, verify=verify)
    try:
        summary = seed_sample_data(
            artifact_dirs, request,
            data_hours=args.data_hours, interval_sec=args.interval_sec,
            batch_docs=args.batch_docs, max_combinations=args.max_combinations,
            no_recreate=args.no_recreate, purge_foreign=args.purge_foreign_streams,
            metric_kind_overrides=overrides,
        )
    except NetworkError as exc:
        print(json.dumps({"error": "es_unreachable", "detail": str(exc)}, indent=2))
        return 2
    except RuntimeError as exc:
        print(json.dumps({"error": "seed_failed", "detail": str(exc)}, indent=2))
        return 2
    print(json.dumps({"ingested": summary.ok, "errors": summary.errors, "docs_per_stream": summary.docs_per_stream}, indent=2))
    return 0 if not summary.errors else 1


def _run_remove_sample_data(args: Any) -> int:
    """Remove seeder-owned Elasticsearch data for migrated dashboards (dry-run by default)."""
    if not args.es_url or not args.api_key:
        print(json.dumps({"error": "es_url and api_key are required (or set ELASTICSEARCH_ENDPOINT/KEY)"}, indent=2))
        return 2
    artifact_dirs = [Path(p) for p in args.artifact_dir]
    missing = [str(p) for p in artifact_dirs if not p.exists()]
    if missing:
        print(json.dumps({"error": "missing_artifact_dirs", "paths": missing}, indent=2))
        return 2

    verify = _tls_verify(args)
    request = make_es_request(args.es_url, args.api_key, verify=verify)
    try:
        summary = remove_sample_data(artifact_dirs, request, dry_run=not args.confirm)
    except NetworkError as exc:
        print(json.dumps({"error": "es_unreachable", "detail": str(exc)}, indent=2))
        return 2
    except RuntimeError as exc:
        print(json.dumps({"error": "remove_failed", "detail": str(exc)}, indent=2))
        return 2
    print(json.dumps({
        "dry_run": summary.dry_run,
        "deleted_streams": summary.deleted_streams,
        "deleted_templates": summary.deleted_templates,
        "skipped_not_owned": summary.skipped_not_owned,
        "errors": summary.errors,
    }, indent=2))
    return 0 if not summary.errors else 1


def _run_list_samples(args: Any) -> int:
    """Print the bundled sample dashboard catalog as JSON (offline)."""
    catalog = []
    for sample in list_samples():
        input_dir = resolve_input_dir(sample.id)
        catalog.append(
            {
                "id": sample.id,
                "source": sample.source,
                "title": sample.title,
                "description": sample.description,
                "input_dir": str(input_dir),
                "expected_unsupported": list(sample.expected_unsupported),
                "run": (
                    f"obs-migrate migrate --source {sample.source} "
                    f'--input-mode files --input-dir "{input_dir}" --output-dir sample_out'
                ),
            }
        )
    print(json.dumps(catalog, indent=2))
    return 0


def _run_cluster(args: Any) -> None:
    """Manage target Kibana cluster: list dashboards, create data views, etc."""
    from observability_migration.targets.kibana.serverless import (
        delete_dashboards,
        detect_serverless,
        ensure_migration_data_views,
        list_dashboards,
    )

    verify = _tls_verify(args)

    if args.action == "detect-serverless":
        is_sl = detect_serverless(
            args.kibana_url, api_key=args.kibana_api_key, space_id=args.space_id, verify=verify,
        )
        print(f"Serverless: {is_sl}")

    elif args.action == "list-dashboards":
        dashboards = list_dashboards(
            args.kibana_url, api_key=args.kibana_api_key, space_id=args.space_id, verify=verify,
        )
        print(f"\n  {len(dashboards)} dashboard(s):\n")
        for d in dashboards:
            title = d.get("attributes", {}).get("title", "(untitled)")
            print(f"    {d.get('id', '???'):40s}  {title}")

    elif args.action == "ensure-data-views":
        patterns = [p.strip() for p in args.data_view_patterns.split(",") if p.strip()]
        created = ensure_migration_data_views(
            args.kibana_url,
            data_view_patterns=patterns,
            api_key=args.kibana_api_key,
            space_id=args.space_id,
            verify=verify,
        )
        for dv in created:
            print(f"  OK: {dv.get('title', '???')} (id={dv.get('id', '???')})")

    elif args.action == "delete-dashboards":
        ids = [i.strip() for i in args.dashboard_ids.split(",") if i.strip()]
        if not ids:
            print("  ERROR: --dashboard-ids required", file=sys.stderr)
            sys.exit(2)
        result = delete_dashboards(
            args.kibana_url, ids,
            api_key=args.kibana_api_key, space_id=args.space_id, verify=verify,
        )
        print(f"  Cleared: {len(result['cleared'])}")
        for f in result.get("failed", []):
            print(f"  FAILED: {f['id']}: {f['error'][:200]}")
        print(f"\n  {result['note']}")


def _serialize_data(payload: dict[str, Any], output_format: str) -> str:
    if output_format == "yaml":
        return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    return json.dumps(payload, indent=2) + "\n"


if __name__ == "__main__":
    main()
