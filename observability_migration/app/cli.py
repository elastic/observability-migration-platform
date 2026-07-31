# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Unified source-agnostic CLI entry point.

Orchestrates migrations by calling source adapters and shared
Kibana target runtime directly, without delegating to the
dedicated source CLIs.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

import observability_migration.adapters.source.datadog.adapter
import observability_migration.adapters.source.grafana.adapter
import observability_migration.targets.kibana.adapter  # noqa: F401
from observability_migration.core.cli_contract import ASSET_CHOICES, normalize_requested_assets
from observability_migration.core.http import resolve_tls
from observability_migration.core.interfaces.registries import source_registry, target_registry
from observability_migration.core.progress import null_progress, stderr_progress
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
    clamp_window_to_data,
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

_DOCS_URL = "https://github.com/elastic/observability-migration-platform/blob/main/docs/command-contract.md"

_UPLOAD_SHAPE_HELP = (
    "Accepted input shapes: a directory of .yaml files, a dashboard artifact "
    "dir with a 'yaml/' child (for example "
    "'migration_output/dashboards' or 'migration_output/dashboards/yaml'), "
    "or that artifact dir's sibling 'compiled/' directory (for example "
    "'migration_output/dashboards/compiled')."
)

_UPLOAD_ARTIFACT_DIR_HELP = (
    "Canonical upload input: the dashboard artifact directory written by "
    "'obs-migrate migrate' (for example 'migration_output/dashboards'), or "
    "directly its 'native/' or 'yaml/' child. Combine with --artifact-format "
    "to pick a representation; the default 'auto' prefers reviewed native "
    "Dashboard-as-Code artifacts ('native/*.native.json') when present, else "
    f"falls back to YAML. {_UPLOAD_SHAPE_HELP}"
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
        description=(
            "Migrate Grafana or Datadog observability assets into Kibana. "
            "One CLI for install checks, samples, migrate, upload, verify, and cluster ops."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Quick start:\n"
            "  obs-migrate doctor\n"
            "  obs-migrate list-samples\n"
            "  obs-migrate migrate --source grafana --input-mode files "
            "--input-dir ./dashboards --output-dir ./out\n"
            "  # Review ./out/dashboards/native/*.native.json, then:\n"
            "  obs-migrate upload --artifact-dir ./out/dashboards "
            "--kibana-url <url> --kibana-api-key <key>\n"
            "\n"
            "Prefer this `obs-migrate` entry point. The legacy "
            "`grafana-migrate` / `datadog-migrate` scripts remain as "
            "compatibility aliases.\n"
            f"Full command reference: {_DOCS_URL}"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    migrate = sub.add_parser(
        "migrate",
        help="Migrate dashboards/alerts from Grafana or Datadog into Kibana",
    )
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
            "--fetch-alerts alias). Both fully-automated and draft "
            "(review-required) translations are created disabled and tagged "
            "'obs-migration'; draft rules also get 'obs-migration-review' so "
            "they are easy to find and enable after inspection. Pass "
            "--no-draft-alert-rules to create only fully-automated rules. "
            "Requires alert-capable asset selection, --kibana-url, and "
            "--kibana-api-key. Writes alert_rule_upload_results.json (Grafana) "
            "or monitor_rule_upload_results.json (Datadog) to the output "
            "directory."
        ),
    )
    migrate.add_argument(
        "--no-draft-alert-rules", action="store_true",
        help=(
            "With --create-alert-rules, skip draft (review-required) "
            "translations and create only fully-automated rules. Draft rules "
            "are created by default."
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
            "Grafana supports 'otel', 'prometheus_remote_write' (Fleet "
            "use_types), 'prometheus_metrics' (classic Metricbeat "
            "prometheus.metrics.*), 'prometheus_native', 'passthrough', and "
            "'auto' (requires --es-url); Datadog supports 'otel'/'default', "
            "'elastic_agent', 'prometheus' (Metricbeat remote_write), "
            "'prometheus_native' (ES /_prometheus), 'passthrough', and YAML "
            "profile files (no auto)."
        ),
    )
    migrate.add_argument(
        "--compile", action="store_true",
        help=(
            "Compile generated YAML to legacy NDJSON. Optional local/debug artifact; "
            "not required for the typed Dashboards API upload path. Implied by "
            "--legacy-import when combined with --upload."
        ),
    )
    migrate.add_argument(
        "--validate", action="store_true",
        help=(
            "Validate emitted ES|QL queries against Elasticsearch after translation. "
            "Requires --es-url. Auto-applies safe query fixes and manualizes broken "
            "ones. Works for both Grafana and Datadog."
        ),
    )
    migrate.add_argument("--upload", action="store_true")
    migrate.add_argument(
        "--ensure-data-views", action="store_true",
        help=(
            "Auto-create the data views migrated controls reference before upload. "
            "Both adapter CLIs accept this, but the unified 'migrate' command did "
            "not, so options_list_control filters shipped pointing at a data view "
            "id that did not exist and rendered as 'An error occurred'."
        ),
    )
    migrate.add_argument(
        "--legacy-import",
        dest="legacy_import",
        action="store_true",
        help=(
            "Deploy dashboards via the legacy kb-dashboard-cli saved-objects "
            "import instead of the default typed Kibana Dashboards API "
            "(PUT /api/dashboards/{id}). The native API is used by default; this "
            "flag forces the older compile+import path."
        ),
    )
    migrate.add_argument(
        "--use-dashboards-api",
        dest="use_dashboards_api",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    migrate.add_argument("--es-url", default="")
    migrate.add_argument("--es-api-key", default="")
    migrate.add_argument("--kibana-url", default="")
    migrate.add_argument("--kibana-api-key", default="")
    migrate.add_argument("--space-id", default="")
    migrate.add_argument("--rules-file", action="append", default=[])
    migrate.add_argument(
        "--metric-map-file",
        action="append",
        default=[],
        help=(
            "Source-neutral YAML file with top-level metric_map and/or tag_map "
            "entries (metric_map renames metric names; tag_map renames tags/labels "
            "to ES fields). Works for Grafana and Datadog; may be repeated. Later "
            "files override earlier entries and the active field profile / loaded "
            "rule packs. On Grafana with --translation-mode auto, also selects "
            "ES|QL so the map applies."
        ),
    )
    migrate.add_argument("--plugin", action="append", default=[])
    migrate.add_argument(
        "--no-curated-packs",
        action="store_true",
        default=False,
        dest="no_curated_packs",
        help=(
            "Disable automatic curated-pack loading for known Grafana community dashboards "
            "(gnetId-matched bundles that improve migration fidelity out of the box). "
            "By default curated packs are merged automatically; --rules-file always wins."
        ),
    )
    migrate.add_argument("--polish-metadata", action="store_true")
    migrate.add_argument(
        "--preflight", action="store_true",
        help=(
            "Run readiness checks before migration: probe target field capabilities "
            "and write a readiness contract (Grafana: required_target_contract.json; "
            "Datadog: target_readiness_contract.json). Requires --es-url for live "
            "field discovery."
        ),
    )
    migrate.add_argument("--source-execution", action="store_true",
                         help="Execute each panel's source query against the live source API "
                              "(Datadog) to build source/target comparison packets")
    migrate.add_argument("--dataset-filter", default="",
                         help="Explicit data_stream.dataset filter for metrics")
    migrate.add_argument("--logs-dataset-filter", default="",
                         help="Explicit data_stream.dataset filter for logs")
    migrate.add_argument(
        "--translation-mode",
        dest="translation_mode",
        choices=["auto", "native", "esql"],
        default="auto",
        help="Translation strategy override (Grafana). 'auto' (default) probes the "
             "target and prefers native PROMQL; 'native' forces native PROMQL; "
             "'esql' forces ES|QL translation for every panel. No-op for Datadog.",
    )
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

    sub.add_parser(
        "doctor",
        help="Check that obs-migrate is installed and ready (start here)",
    )

    compile_cmd = sub.add_parser("compile", help="Compile YAML to NDJSON")
    compile_cmd.add_argument("--yaml-dir", required=True, help="Directory with dashboard YAML files")
    compile_cmd.add_argument("--output-dir", required=True, help="Output directory for NDJSON")

    upload_cmd = sub.add_parser(
        "upload",
        help="Deploy a dashboard artifact directory to Kibana via the typed Dashboards API",
        description=(
            "Deploy a dashboard artifact directory to Kibana via the typed Kibana "
            "Dashboards API (PUT /api/dashboards/{id}) by default, with "
            "per-dashboard fallback to the legacy kb-dashboard-cli saved-objects "
            "import. Pass --legacy-import to force the legacy compile+import path. "
            f"{_UPLOAD_SHAPE_HELP}"
        ),
    )
    upload_group = upload_cmd.add_mutually_exclusive_group(required=True)
    upload_group.add_argument(
        "--artifact-dir",
        help=_UPLOAD_ARTIFACT_DIR_HELP,
    )
    upload_group.add_argument(
        "--yaml-dir",
        help="[Compatibility alias for --artifact-dir --artifact-format yaml] "
             "Path to a dashboard YAML directory input for compile+upload. "
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
    upload_cmd.add_argument(
        "--artifact-format",
        choices=["auto", "native", "yaml"],
        default="auto",
        help=(
            "Representation to upload from --artifact-dir. 'auto' (default) "
            "prefers reviewed native Dashboard-as-Code artifacts when present, "
            "else falls back to YAML; 'native' uploads the reviewed typed API "
            "payload exactly, with no YAML re-mapping and no legacy fallback; "
            "'yaml' forces the existing YAML-to-native mapping path. Ignored "
            "(forced to 'yaml') when --yaml-dir/--compiled-dir or "
            "--legacy-import is used."
        ),
    )
    upload_cmd.add_argument("--kibana-url", required=True)
    upload_cmd.add_argument("--kibana-api-key", default="")
    upload_cmd.add_argument("--space-id", default="")
    upload_cmd.add_argument(
        "--legacy-import",
        dest="legacy_import",
        action="store_true",
        help=(
            "Force the legacy kb-dashboard-cli saved-objects import instead of the "
            "default typed Kibana Dashboards API (POST /api/dashboards). Requires "
            "YAML artifacts (--artifact-format is forced to 'yaml')."
        ),
    )
    upload_cmd.add_argument(
        "--use-dashboards-api",
        dest="use_dashboards_api",
        action="store_true",
        help=argparse.SUPPRESS,
    )
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
        help="Per-dashboard obs-migrate output directory (contains migration_report.json, yaml/, compiled/).",
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
    verify_cmd.add_argument(
        "--no-invariants",
        action="store_true",
        help="Skip the deterministic invariant linter.",
    )
    verify_cmd.add_argument(
        "--live-oracle",
        action="store_true",
        help="Resolve invariant columns through the live Elasticsearch _query oracle.",
    )
    verify_cmd.add_argument(
        "--fail-on-invariant",
        action="store_true",
        help="Exit non-zero when invariant linting reports ERROR findings.",
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
    seed_cmd.add_argument("--quiet", action="store_true", help="Suppress progress messages on stderr.")
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
    compare_cmd.add_argument("--quiet", action="store_true", help="Suppress progress messages on stderr.")
    _add_tls_arguments(compare_cmd)

    metric_map_cmd = sub.add_parser(
        "metric-map",
        help="Metric map utilities for migration artifacts.",
        description=(
            "Source-neutral helpers for authoring metric_map YAML from migration "
            "artifact contracts."
        ),
    )
    metric_map_sub = metric_map_cmd.add_subparsers(dest="metric_map_action", required=True)
    scaffold_cmd = metric_map_sub.add_parser(
        "scaffold",
        help="Scaffold metric_map YAML from unmapped source metrics in artifacts.",
        description=(
            "Read required_target_contract.json, target_readiness_contract.json, "
            "and/or migration_manifest.json under --artifact-dir and write a "
            "source-neutral metric_map YAML skeleton for metrics that still need "
            "operator-authored target names."
        ),
    )
    scaffold_cmd.add_argument(
        "--artifact-dir",
        required=True,
        help="Migration artifact directory (for example migration_output/dashboards).",
    )
    scaffold_cmd.add_argument(
        "--output",
        required=True,
        help="Path for the scaffold YAML file to write.",
    )

    verify_unified_cmd = sub.add_parser(
        "verify",
        help="One command + one scorecard for the package-native correctness "
             "gates over a migrated dashboard artifact dir (emitted-query "
             "acceptance + optional numeric parity).",
        description=(
            "Thin orchestrator over the package-native correctness gates. Reads the "
            "emitted ES|QL from a migrated artifact dir's verification_packets.json / "
            "migration_report.json, runs each query against Elasticsearch and "
            "classifies it (ok / real_bug / data_gap / other), optionally runs "
            "'obs-migrate compare' in-process for numeric parity, and prints ONE "
            "consolidated scorecard. It also lists the deeper gates it does NOT run "
            "(Kibana typed-contract dashboards_api, browser render audit) -- these "
            "live in parity-rig/. Read-only on the cluster. Exit code is 2 when the "
            "cluster is unreachable or inputs are invalid, 1 on any real_bug or "
            "compare FAIL/ERROR, and 0 otherwise."
        ),
    )
    verify_unified_cmd.add_argument(
        "--artifact-dir", dest="artifact_dir", required=True,
        help="Migrated dashboard artifact dir (contains verification_packets.json "
             "and/or migration_report.json), e.g. '<output-dir>/dashboards'.",
    )
    verify_unified_cmd.add_argument(
        "--es-url", default=os.getenv("ELASTICSEARCH_ENDPOINT", os.getenv("ES_URL", "")),
        help="Elasticsearch URL (defaults to ELASTICSEARCH_ENDPOINT or ES_URL).",
    )
    verify_unified_cmd.add_argument(
        "--api-key", default=os.getenv("KEY", ""),
        help="Elasticsearch API key (defaults to KEY).",
    )
    verify_unified_cmd.add_argument(
        "--kibana-url", default="",
        help="Kibana base URL (accepted for parity with sibling commands; the "
             "package-native gates run here do not require it -- the deeper "
             "Kibana-contract gates that do live in parity-rig/).",
    )
    verify_unified_cmd.add_argument(
        "--index", default="metrics-*",
        help="ES index pattern used to validate emitted queries and to seed the "
             "compare native-PROMQL oracle (default: metrics-*).",
    )
    verify_unified_cmd.add_argument(
        "--compare", dest="run_compare", action="store_true",
        help="Also run the numeric-parity gate ('obs-migrate compare') in-process "
             "over the same artifact dir.",
    )
    verify_unified_cmd.add_argument(
        "--report-out", default="verify_report.json",
        help="Path for the consolidated JSON report (default: verify_report.json).",
    )
    _add_tls_arguments(verify_unified_cmd)

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
    elif args.command == "metric-map":
        if args.metric_map_action == "scaffold":
            sys.exit(_run_metric_map_scaffold(args))
        sys.exit(2)
    elif args.command == "verify":
        sys.exit(_run_verify(args))
    elif args.command == "doctor":
        sys.exit(_run_doctor())
    else:
        parser.print_help()
        sys.exit(1)


def _run_doctor() -> int:
    """Report first-run readiness: Python, deps, extras, and Kibana tools.

    Returns 0 when the install can compile/migrate (kb tools installed or
    reachable via ``uvx``); returns 1 when a blocking gap remains.
    """
    import importlib.util
    import platform

    from observability_migration import __version__
    from observability_migration._version import read_project_version
    from observability_migration.targets.kibana._kbtool import (
        KB_DASHBOARD_TOOL_VERSION,
        KbToolUnavailableError,
        tool_argv,
    )

    issues: list[str] = []
    notes: list[str] = []
    py = sys.version_info
    package_root = Path(__file__).resolve().parents[1]

    print("obs-migrate doctor")
    print(f"  package version: {__version__}")
    print(f"  package location: {package_root}")
    print(f"  python: {platform.python_version()} ({sys.executable})")
    print(f"  platform: {platform.system()} {platform.machine()} ({platform.platform()})")

    if py < (3, 11):
        issues.append(
            f"Python {platform.python_version()} is below the supported floor "
            "(need 3.11+)."
        )
    elif py >= (3, 14):
        notes.append(
            "Python 3.14+ is allowed by packaging metadata but is not in the "
            "CI pytest matrix yet; prefer 3.11–3.13 for production migrations."
        )

    try:
        project_version = read_project_version()
        if project_version != __version__:
            notes.append(
                f"pyproject/metadata mismatch "
                f"(read_project_version={project_version!r})"
            )
    except RuntimeError:
        pass

    # Core runtime imports (always required by the installed package).
    required = (
        ("yaml", "PyYAML"),
        ("pydantic", "pydantic"),
        ("requests", "requests"),
        ("lark", "lark"),
        ("grafana_client", "grafana-client"),
        ("promql_parser", "promql-parser"),
    )
    print("  required dependencies:")
    for mod, label in required:
        if importlib.util.find_spec(mod) is None:
            print(f"    {label}: MISSING")
            issues.append(
                f"Required dependency {label!r} is not importable. "
                "Reinstall with: uvx --from 'elastic-observability-migration[all]' obs-migrate doctor"
            )
        else:
            print(f"    {label}: ok")

    # Optional extras — helpful for first-time operators choosing [all].
    print("  optional extras:")
    datadog_ok = importlib.util.find_spec("datadog_api_client") is not None
    print(
        f"    datadog (datadog-api-client): "
        f"{'ok' if datadog_ok else 'not installed — Datadog API mode needs elastic-observability-migration[datadog] or [all]'}"
    )
    if not datadog_ok:
        notes.append(
            "Datadog API client not installed; file-mode Datadog migrate still "
            "works. For --input-mode api install elastic-observability-migration[all] (or [datadog])."
        )

    uv_path = shutil.which("uv")
    uvx_path = shutil.which("uvx")
    print(f"  uv on PATH: {'yes (' + uv_path + ')' if uv_path else 'no'}")
    print(f"  uvx on PATH: {'yes (' + uvx_path + ')' if uvx_path else 'no'}")
    if not uvx_path and py < (3, 12):
        issues.append(
            "Python < 3.12 needs uv/uvx on PATH so compile/lint can use the "
            "pinned kb-dashboard-* fallback. Install: https://docs.astral.sh/uv/"
        )
    elif not uvx_path:
        notes.append(
            "uv/uvx not on PATH. Fine if kb-dashboard-* is installed via "
            "elastic-observability-migration[kibana]/[all] on Python 3.12+; otherwise install uv."
        )

    print(f"  pinned kb-dashboard tool version: {KB_DASHBOARD_TOOL_VERSION}")
    kb_ok = True
    for tool in ("kb-dashboard-cli", "kb-dashboard-lint"):
        try:
            argv = tool_argv(tool)
            mode = "installed" if argv[0] != "uvx" else "uvx fallback"
            print(f"  {tool}: available ({mode})")
        except KbToolUnavailableError as exc:
            kb_ok = False
            print(f"  {tool}: UNAVAILABLE - {exc}")
            issues.append(str(exc))
    if not kb_ok and py >= (3, 12):
        notes.append(
            "On Python 3.12+, prefer elastic-observability-migration[all] (or [kibana]) so "
            "kb-dashboard-cli/lint install in-venv without needing uvx."
        )

    if notes:
        print()
        print("Notes:")
        for note in notes:
            print(f"  - {note}")

    if issues:
        print()
        print("Problems (fix these before migrating):")
        for issue in issues:
            print(f"  - {issue}")
        print()
        print("First-time install (macOS/Linux, needs uv):")
        print(
            "  uvx --from 'elastic-observability-migration[all]' obs-migrate doctor"
        )
        return 1

    print()
    print("Ready.")
    print("Next steps (reuse the same launcher you used for doctor):")
    cmd = _doctor_followup_cmd()
    print(f"  {cmd} list-samples")
    print(
        f"  {cmd} migrate --source grafana --input-mode files "
        "--input-dir <dir> --output-dir ./out"
    )
    print("  # Review ./out/dashboards/native/*.native.json, then upload:")
    print(
        f"  {cmd} upload --artifact-dir ./out/dashboards "
        "--kibana-url <url> --kibana-api-key <key>"
    )
    print(f"Full reference: {_DOCS_URL}")
    return 0


def _doctor_followup_cmd() -> str:
    """Return the launcher obs-migrate was invoked with, so Next steps are copy-pasteable."""
    which = shutil.which("obs-migrate")
    if which:
        return "obs-migrate"

    prog = Path(sys.argv[0]).expanduser()
    try:
        prog = prog.resolve()
    except OSError:
        pass
    if prog.name in {"obs-migrate", "grafana-migrate", "datadog-migrate"}:
        # Persistent venv layout: …/<env>/bin/obs-migrate next to pyvenv.cfg.
        # Skip uvx/tool cache paths, which are not a stable operator launcher.
        if (prog.parent.parent / "pyvenv.cfg").is_file():
            try:
                return str(prog.relative_to(Path.cwd()))
            except ValueError:
                return str(prog)

    return "uvx --from 'elastic-observability-migration[all]' obs-migrate"


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
    if args.no_invariants:
        argv += ["--no-invariants"]
    if args.live_oracle:
        argv += ["--live-oracle"]
    if args.fail_on_invariant:
        argv += ["--fail-on-invariant"]
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
    if args.validate:
        legacy_argv.append("--validate")
    if getattr(args, "compile", False):
        legacy_argv.append("--compile")
    if args.upload:
        legacy_argv.append("--upload")
    if getattr(args, "ensure_data_views", False):
        legacy_argv.append("--ensure-data-views")
    if getattr(args, "legacy_import", False):
        legacy_argv.append("--legacy-import")
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
    for mmf in getattr(args, "metric_map_file", []):
        legacy_argv.extend(["--metric-map-file", mmf])
    for pl in args.plugin:
        legacy_argv.extend(["--plugin", pl])
    if getattr(args, "no_curated_packs", False):
        legacy_argv.append("--no-curated-packs")
    if args.polish_metadata:
        legacy_argv.append("--polish-metadata")
    if args.preflight:
        legacy_argv.append("--preflight")
    if args.dataset_filter:
        legacy_argv.extend(["--dataset-filter", args.dataset_filter])
    if args.logs_dataset_filter:
        legacy_argv.extend(["--logs-dataset-filter", args.logs_dataset_filter])
    _translation_mode = getattr(args, "translation_mode", "auto") or "auto"
    if _translation_mode != "auto":
        legacy_argv.extend(["--translation-mode", _translation_mode])
    if args.smoke_report:
        legacy_argv.extend(["--smoke-report", args.smoke_report])
    if getattr(args, "create_alert_rules", False):
        legacy_argv.append("--create-alert-rules")
    if getattr(args, "no_draft_alert_rules", False):
        legacy_argv.append("--no-draft-alert-rules")
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
    for mmf in getattr(args, "metric_map_file", []):
        legacy_argv.extend(["--metric-map-file", mmf])
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
    if getattr(args, "compile", False):
        legacy_argv.append("--compile")
    if args.validate:
        legacy_argv.append("--validate")
    if args.upload:
        legacy_argv.append("--upload")
    if getattr(args, "ensure_data_views", False):
        legacy_argv.append("--ensure-data-views")
    if getattr(args, "legacy_import", False):
        legacy_argv.append("--legacy-import")
    if args.preflight:
        legacy_argv.append("--preflight")
    if getattr(args, "source_execution", False):
        legacy_argv.append("--source-execution")
    if args.dataset_filter:
        legacy_argv.extend(["--dataset-filter", args.dataset_filter])
    if args.logs_dataset_filter:
        legacy_argv.extend(["--logs-dataset-filter", args.logs_dataset_filter])
    _translation_mode = getattr(args, "translation_mode", "auto") or "auto"
    if _translation_mode != "auto":
        legacy_argv.extend(["--translation-mode", _translation_mode])
    if args.kibana_url:
        legacy_argv.extend(["--kibana-url", args.kibana_url])
    if args.kibana_api_key:
        legacy_argv.extend(["--kibana-api-key", args.kibana_api_key])
    if args.space_id:
        legacy_argv.extend(["--space-id", args.space_id])
    if getattr(args, "create_alert_rules", False):
        legacy_argv.append("--create-alert-rules")
    if getattr(args, "no_draft_alert_rules", False):
        legacy_argv.append("--no-draft-alert-rules")
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


def _resolve_upload_input(args: Any) -> tuple[Path, str]:
    """Resolve the effective ``(artifact_dir, artifact_format)`` for upload.

    ``--artifact-dir`` is the canonical input; ``--yaml-dir``/``--compiled-dir``
    are compatibility aliases that pin the format to ``"yaml"`` so existing
    scripts keep their exact prior behavior (see docs/command-contract.md).
    """
    artifact_dir_raw = getattr(args, "artifact_dir", None)
    yaml_dir_raw = getattr(args, "yaml_dir", None)
    compiled_dir_raw = getattr(args, "compiled_dir", None)
    artifact_format = str(getattr(args, "artifact_format", "") or "auto")

    if artifact_dir_raw:
        return Path(artifact_dir_raw), artifact_format
    if compiled_dir_raw and not yaml_dir_raw:
        print(
            "  NOTE: --compiled-dir is a deprecated alias for --yaml-dir. "
            "Upload recompiles YAML internally; prefer --yaml-dir or "
            "--artifact-dir in new scripts.",
            file=sys.stderr,
        )
        return Path(compiled_dir_raw), "yaml"
    if yaml_dir_raw:
        return Path(yaml_dir_raw), "yaml"
    return Path(""), artifact_format


def _run_upload(args: Any) -> None:
    """Deploy a dashboard artifact directory to Kibana via the typed Dashboards API by default."""
    input_dir, artifact_format = _resolve_upload_input(args)
    legacy_import = bool(getattr(args, "legacy_import", False))
    if legacy_import:
        # The legacy importer compiles saved objects from YAML, so it has no
        # native-payload equivalent; forcing yaml here keeps
        # --artifact-format meaningless-but-harmless when combined with
        # --legacy-import instead of silently ignoring an explicit 'native'.
        if artifact_format == "native":
            print(
                "  ERROR: --legacy-import requires YAML artifacts (it compiles "
                "through kb-dashboard-cli) but --artifact-format native was "
                "requested. Pass --artifact-format yaml (or --yaml-dir) instead.",
                file=sys.stderr,
            )
            sys.exit(2)
        artifact_format = "yaml"
    if not input_dir or not input_dir.is_dir():
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
        use_dashboards_api=not legacy_import,
        artifact_format=artifact_format,
    )
    if upload_payload["summary"].get("error") == "no_native_artifacts_found":
        print(
            f"No native Dashboard-as-Code artifacts (native/*.native.json) found "
            f"under {input_dir}. Pass --artifact-format auto or yaml to fall back "
            "to YAML, or point --artifact-dir at a directory containing 'native/'.",
            file=sys.stderr,
        )
        sys.exit(1)
    if upload_payload["summary"].get("error") == "mixed_native_yaml_artifacts":
        missing = upload_payload["summary"].get("missing_native_artifacts") or []
        extra = upload_payload["summary"].get("extra_native_artifacts") or []
        detail = ""
        if missing:
            detail = f" Missing native artifacts for: {', '.join(str(item) for item in missing[:5])}."
        elif extra:
            detail = f" Native artifacts without YAML siblings: {', '.join(str(item) for item in extra[:5])}."
        print(
            "Native Dashboard-as-Code artifacts and YAML artifacts do not match under "
            f"{input_dir}; refusing an auto upload that would skip dashboards.{detail} "
            "Regenerate the migration output, pass --artifact-format yaml, or point "
            "--artifact-dir directly at the native/ directory.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not upload_payload["records"]:
        print(
            f"No dashboard YAML files found under {input_dir}. "
            "Point --yaml-dir (or --artifact-dir) at a directory of .yaml files, "
            "a dashboard artifact dir containing 'yaml/' (e.g. "
            "'migration_output/dashboards' or "
            "'migration_output/dashboards/yaml'), or that dir's sibling "
            "'compiled/' directory (e.g. "
            "'migration_output/dashboards/compiled').",
            file=sys.stderr,
        )
        sys.exit(1)

    for item in upload_payload["records"]:
        status = "OK" if item["success"] else "FAIL"
        suffix = ""
        if item.get("fallback_used"):
            suffix = " (via legacy _import fallback)"
        elif item.get("status"):
            suffix = f" ({item['status']} via dashboards API)"
        print(f"  [{status}] {item['yaml_file']}{suffix}")
        if not item["success"]:
            print(f"         {item['output'][:200]}")
        dropped_filters = item.get("unmapped_reasons", {}).get(
            "dropped_unsupported_dashboard_filter", 0
        )
        if dropped_filters:
            print(
                f"         warning: dropped {dropped_filters} unsupported dashboard "
                "filter(s); affected panels may query a broader dataset than the source",
                file=sys.stderr,
            )
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


def _artifact_control_bindings(
    artifact_dir: Path, packets_doc: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Kibana-faithful ``?param`` bindings, keyed by dashboard title.

    Prefers the native Dashboards API payload (``pinned_panels`` -> esql_control),
    which is what Kibana actually loads; falls back to controls carried on the
    verification packets document (stored under ``""``, the any-dashboard key).

    Bindings MUST stay per-dashboard. A control name like ``job`` or ``instance``
    is only unique within one dashboard: Node Exporter Full defaults ``job`` to
    ``.*`` while the PostgreSQL pack defaults it to ``postgres``. Merging every
    dashboard's controls into one dict makes the last one loaded win, so a panel
    gets filtered by a different dashboard's default, matches nothing, and is
    reported as a translation FAIL when the translation was correct. That is
    exactly what produced the long-standing "returned no series" failures on
    Node Exporter Full and Node.js.
    """
    from observability_migration.core.verification.parity_oracle import build_control_bindings

    by_dashboard: dict[str, dict[str, Any]] = {}
    native_dir = artifact_dir / "native"
    if native_dir.is_dir():
        for native_file in sorted(native_dir.glob("*.native.json")):
            try:
                payload = json.loads(native_file.read_text(encoding="utf-8")).get("payload") or {}
            except (OSError, ValueError):
                continue
            controls = payload.get("pinned_panels") or []
            if not controls:
                continue
            by_dashboard[str(payload.get("title") or "")] = build_control_bindings(controls)
    fallback = packets_doc.get("controls") or []
    if fallback:
        by_dashboard.setdefault("", build_control_bindings(fallback))
    return by_dashboard


def _bindings_for_dashboard(
    by_dashboard: dict[str, dict[str, Any]], dashboard: str
) -> dict[str, Any]:
    """Controls for one dashboard, falling back to the any-dashboard set.

    Never merges across dashboards -- see ``_artifact_control_bindings``.
    """
    if dashboard in by_dashboard:
        return by_dashboard[dashboard]
    return by_dashboard.get("", {})


def _artifact_metric_renames(artifact_dir: Path) -> dict[str, str]:
    """Applied ``metric_map`` renames, source name -> target field path.

    ``derive_field_map_from_translated`` recovers the metric->field mapping from
    the emitted query, but a renamed metric breaks that inference: the source
    says ``redis_memory_fragmentation_ratio`` while the query says
    ``metrics.redis_mem_fragmentation_ratio``, and nothing connects the two. The
    run already records the rename it applied, so read it rather than guess.
    """
    report = artifact_dir / "migration_report.json"
    if not report.exists():
        return {}
    try:
        doc = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    summary = doc.get("metric_map_summary")
    if not isinstance(summary, dict):
        return {}
    out: dict[str, str] = {}
    for entry in summary.get("applied") or []:
        if isinstance(entry, dict) and entry.get("source") and entry.get("target"):
            out[str(entry["source"])] = str(entry["target"])
    return out


def _run_compare(args: Any) -> int:
    """Per-panel side-by-side parity for migrated dashboards (PromQL native oracle)."""
    if not args.es_url or not args.api_key:
        print(json.dumps({"error": "es_url and api_key are required (or set ELASTICSEARCH_ENDPOINT/KEY)"}, indent=2))
        return 2
    packets: list[dict[str, Any]] = []
    control_bindings: dict[str, dict[str, Any]] = {}
    metric_renames: dict[str, str] = {}
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
        # Controls carry the binding CONTRACT (multi-select -> list, single ->
        # scalar). Without them the oracle has to guess a scalar, which both
        # skips most panels and hides list/scalar type errors that only show up
        # in a real Kibana render. Keyed by dashboard: control names collide
        # across dashboards and merging them mis-filters panels.
        control_bindings.update(_artifact_control_bindings(Path(raw), data))
        # An applied metric_map renames the metric, so the source expression's
        # name has no relation to the translated field's bare name and the
        # oracle cannot pair them by itself. Feed it the rename explicitly.
        metric_renames.update(_artifact_metric_renames(Path(raw)))

    from datetime import UTC, datetime, timedelta
    end = datetime.now(UTC)
    start = end - timedelta(minutes=args.window_minutes)
    start_iso = start.isoformat().replace("+00:00", "Z")
    end_iso = end.isoformat().replace("+00:00", "Z")

    verify = _tls_verify(args)
    request = make_es_request(args.es_url, args.api_key, verify=verify)
    # A window that reaches past the data produces "no overlapping time buckets"
    # on every panel, which reads as a mass translation failure rather than as
    # the misconfiguration it is.
    window_note = ""
    try:
        start_iso, end_iso, window_note = clamp_window_to_data(
            request, args.index or "metrics-*", start_iso, end_iso
        )
    except NetworkError as exc:
        print(json.dumps({"error": "es_unreachable", "detail": str(exc)}, indent=2))
        return 2
    if window_note:
        print(f"compare: {window_note}")
    try:
        oracle_ok = native_promql_available(request, args.index or "metrics-*")
    except NetworkError as exc:
        print(json.dumps({"error": "es_unreachable", "detail": str(exc)}, indent=2))
        return 2

    progress = null_progress if getattr(args, "quiet", False) else stderr_progress("compare")
    total_panels = len(packets)
    dashboard_count = len({pkt.get("dashboard", "") for pkt in packets})
    progress(f"comparing {total_panels} panels across {dashboard_count} dashboards")

    rows: list[dict[str, Any]] = []
    for i, pkt in enumerate(packets, start=1):
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
                        control_bindings=_bindings_for_dashboard(
                            control_bindings, str(pkt.get("dashboard", ""))
                        ),
                        metric_renames=metric_renames,
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

        if i % 10 == 0 or i == total_panels:
            progress(f"processed {i}/{total_panels} panels")

    summary = {"panels": len(rows)}
    for r in rows:
        summary[r["verdict"]] = summary.get(r["verdict"], 0) + 1
    report = {
        "summary": summary,
        "oracle_available": oracle_ok,
        "window": {"start": start_iso, "end": end_iso, "note": window_note},
        "panels": rows,
    }
    out = Path(args.report_out)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    out.with_suffix(".md").write_text(_render_compare_md(report), encoding="utf-8")
    progress(f"report written to {out}")
    print(json.dumps(summary, indent=2))
    # DATA_GAP is telemetry that has not landed yet, not a defect, so it must
    # not fail the gate. ERROR is an unexplained execution failure and must.
    return 1 if any(r["verdict"] in ("FAIL", "SOURCE_FAIL", "ERROR") for r in rows) else 0


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


def _verify_compare_runner(args: Any) -> Callable[..., dict[str, Any]]:
    """Build the compare-runner seam for ``obs-migrate verify``.

    Reuses the existing in-process ``_run_compare`` implementation (no shelling
    out, no re-implementation): it writes a structured JSON report to a temp
    file, which we read back to surface the STRICT/FUZZY/SHAPE/FAIL/ERROR
    counts. ``_run_compare`` returns 2 when the cluster is unreachable or inputs
    are invalid -- we map that to ``ran: False`` rather than failing the whole
    verify run.
    """
    import tempfile
    from contextlib import redirect_stdout
    from types import SimpleNamespace

    def runner(*, artifact_dir: str, es_url: str, api_key: str, index: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "verify_compare_report.json"
            compare_args = SimpleNamespace(
                artifact_dir=[artifact_dir],
                es_url=es_url,
                api_key=api_key,
                index=index,
                step_seconds=300,
                window_minutes=60,
                report_out=str(report_path),
                ca_cert=getattr(args, "ca_cert", ""),
                insecure=getattr(args, "insecure", False),
            )
            # _run_compare prints its own JSON summary to stdout; keep the verify
            # scorecard clean by swallowing it (we re-render from the report).
            with redirect_stdout(io.StringIO()):
                code = _run_compare(compare_args)
            if code == 2 or not report_path.exists():
                return {"ran": False, "reason": "compare unavailable (no data / unreachable)"}
            try:
                data = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                return {"ran": False, "reason": f"compare report unreadable: {exc}"}
            return {
                "ran": True,
                "summary": data.get("summary", {}),
                "oracle_available": data.get("oracle_available"),
            }

    return runner


def _run_verify(args: Any) -> int:
    """Run the package-native verify orchestrator (acceptance + optional parity)."""
    from observability_migration.app.verify import run_verify

    compare_runner = _verify_compare_runner(args) if getattr(args, "run_compare", False) else None
    return run_verify(
        artifact_dir=args.artifact_dir,
        es_url=args.es_url,
        api_key=args.api_key,
        index=args.index,
        report_out=args.report_out,
        run_compare=bool(getattr(args, "run_compare", False)),
        compare_runner=compare_runner,
        # Honor --ca-cert / --insecure for the acceptance gate too, not just the
        # optional compare runner (PR #234 review).
        verify=_tls_verify(args),
    )


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
    progress = null_progress if getattr(args, "quiet", False) else stderr_progress("seed")
    try:
        summary = seed_sample_data(
            artifact_dirs, request,
            data_hours=args.data_hours, interval_sec=args.interval_sec,
            batch_docs=args.batch_docs, max_combinations=args.max_combinations,
            no_recreate=args.no_recreate, purge_foreign=args.purge_foreign_streams,
            metric_kind_overrides=overrides,
            on_progress=progress,
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


def _run_metric_map_scaffold(args: Any) -> int:
    """Write a metric_map scaffold YAML from migration artifact contracts."""
    from observability_migration.core.metric_mapping.scaffold import build_scaffold_yaml

    artifact_dir = Path(args.artifact_dir)
    if not artifact_dir.is_dir():
        print(
            json.dumps({"error": "missing_artifact_dir", "path": str(artifact_dir)}),
            file=sys.stderr,
        )
        return 2
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(build_scaffold_yaml(artifact_dir), encoding="utf-8")
    print(f"Wrote metric_map scaffold: {output_path}")
    return 0


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
