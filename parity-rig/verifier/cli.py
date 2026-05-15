"""Command-line entrypoint for the 5-tier panel verifier.

Usage::

    python -m parity-rig.verifier.cli \
        --migration-out /tmp/mig-to-kbn-e2e/parity-out-<slug>/dashboards \
        --kibana-url $KIBANA_ENDPOINT \
        --es-url $ELASTICSEARCH_ENDPOINT \
        --api-key $KEY \
        --dashboard-id <kibana-dash-id> \
        --output /tmp/verifier-<slug>.json

Outputs both ``<slug>.json`` (machine readable) and ``<slug>.md``
(human readable triage) next to ``--output``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import collectors
from .compare import (
    aggregate_drift_axes,
    aggregate_verdicts,
    compare_panel_record,
)
from .records import PanelRecord, Verdict

LOG = logging.getLogger("verifier")


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="parity-rig.verifier",
        description="5-tier verification for a migrated dashboard.",
    )
    p.add_argument(
        "--migration-out",
        type=Path,
        required=True,
        help="Path to the per-dashboard mig-to-kbn output directory "
             "(contains migration_report.json, yaml/, compiled/).",
    )
    p.add_argument(
        "--kibana-url",
        type=str,
        help="Kibana base URL (e.g. https://<cluster>.kb.us-central1.gcp.staging.elastic.cloud). "
             "Required to collect T4.",
    )
    p.add_argument(
        "--es-url",
        type=str,
        help="Elasticsearch base URL. Required to collect T5.",
    )
    p.add_argument(
        "--api-key",
        type=str,
        help="Elastic API key (used for both Kibana and ES). Required for T4/T5.",
    )
    p.add_argument(
        "--dashboard-id",
        type=str,
        help="Kibana saved-object ID of the uploaded dashboard. Required for T4/T5. "
             "If omitted, only T0..T3 are collected.",
    )
    p.add_argument(
        "--space",
        type=str,
        default="default",
        help="Kibana space (default: default).",
    )
    p.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path to write the JSON report (a .md file is written alongside).",
    )
    p.add_argument(
        "--es-index",
        type=str,
        default="",
        help="If provided, used to fill in the t1.index field when the translator "
             "output is a bare PROMQL/TS query.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most this many panels (0 = no limit).",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose logging.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    migration_dir: Path = args.migration_out
    report_path = migration_dir / "migration_report.json"
    yaml_dir = migration_dir / "yaml"
    compiled_dir = migration_dir / "compiled"

    if not report_path.exists():
        print(f"error: migration_report.json not found at {report_path}", file=sys.stderr)
        return 2

    LOG.info("loading migration report: %s", report_path)
    report = collectors.load_migration_report(report_path)

    LOG.info("scanning yaml dir: %s", yaml_dir)
    yaml_panels = collectors.load_yaml_panels(yaml_dir) if yaml_dir.exists() else {}

    LOG.info("scanning compiled dir: %s", compiled_dir)
    ndjson_panels = _load_compiled_panels(compiled_dir)

    cluster_panels: dict[str, str] = {}
    cluster_saved_object: dict = {}
    cluster_unavailable_reason = ""
    if args.kibana_url and args.api_key and args.dashboard_id:
        LOG.info("fetching cluster saved object %s from %s", args.dashboard_id, args.kibana_url)
        try:
            cluster_saved_object = collectors.fetch_cluster_dashboard(
                args.kibana_url, args.api_key, args.dashboard_id, args.space
            )
            cluster_panels = collectors.cluster_dashboard_panels(cluster_saved_object)
        except Exception as exc:
            cluster_unavailable_reason = str(exc)[:200]
            LOG.warning(
                "could not fetch cluster dashboard via saved-objects API "
                "(common on Elastic Serverless): %s. Falling back to NDJSON "
                "as the T4 source. For a true T4/T5 capture, run the browser "
                "walker (parity-rig/verifier/walker.py) which sources Lens's "
                "actual queries from a HAR recording.",
                cluster_unavailable_reason,
            )

    records: list[PanelRecord] = []
    for record in collectors.panels_from_migration_report(report):
        if args.es_index and not record.t1_index:
            record.t1_index = args.es_index
        record.t2_yaml_esql = yaml_panels.get(record.title, "")
        record.t3_ndjson_esql = ndjson_panels.get(record.title, "")
        if cluster_saved_object:
            record.t4_cluster_esql = cluster_panels.get(record.title, "")
            record.t4_saved_object_id = cluster_saved_object.get("id", "")
            record.t4_saved_object_updated_at = cluster_saved_object.get("updated_at", "")
        elif args.dashboard_id:
            record.t4_cluster_esql = record.t3_ndjson_esql
            record.notes.append(
                "T4 sourced from NDJSON (cluster saved-objects API unavailable); "
                "run the browser walker for a true T4 capture"
            )
        if args.es_url and args.api_key and record.t4_cluster_esql:
            status, body = collectors.run_cluster_query(
                args.es_url, args.api_key, record.t4_cluster_esql
            )
            collectors.annotate_record_with_live_response(record, status, body)
            record.t5_live_query_body = record.t4_cluster_esql
        compare_panel_record(record)
        records.append(record)
        if args.limit and len(records) >= args.limit:
            break

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "dashboard_id": args.dashboard_id or "",
        "dashboard_title": records[0].dashboard_title if records else "",
        "verdict_counts": aggregate_verdicts(records),
        "drift_axis_counts": aggregate_drift_axes(records),
        "panels": [r.to_jsonable() for r in records],
    }
    args.output.write_text(json.dumps(payload, indent=2, default=str))
    LOG.info("wrote %s", args.output)

    md_path = args.output.with_suffix(".md")
    md_path.write_text(_render_markdown(payload, records))
    LOG.info("wrote %s", md_path)

    print(_render_console_summary(payload))
    return 0


def _load_compiled_panels(compiled_dir: Path) -> dict[str, str]:
    if not compiled_dir.exists():
        return {}
    for sub in sorted(compiled_dir.iterdir()):
        if not sub.is_dir():
            continue
        candidate = sub / "compiled_dashboards.ndjson"
        if candidate.exists():
            return collectors.load_ndjson_panels(candidate)
        candidate = sub / "yaml.ndjson"
        if candidate.exists():
            return collectors.load_ndjson_panels(candidate)
    return {}


def _render_console_summary(payload: dict) -> str:
    lines = [
        f"\nverifier summary  ({payload['dashboard_title'] or '(unknown)'})",
        "-" * 60,
    ]
    for verdict, count in sorted(payload["verdict_counts"].items()):
        if count:
            lines.append(f"  {verdict:<14} {count}")
    lines.append("")
    lines.append("drift axes:")
    for axis, count in payload["drift_axis_counts"].items():
        lines.append(f"  {axis:<8} {count}")
    return "\n".join(lines)


def _render_markdown(payload: dict, records: list[PanelRecord]) -> str:
    lines = [
        f"# verifier report: {payload['dashboard_title'] or '(unknown)'}",
        "",
        f"- dashboard id: `{payload['dashboard_id'] or '(local-only)'}`",
        f"- panels analysed: **{len(records)}**",
        "",
        "## verdict counts",
        "",
        "| verdict | count |",
        "| --- | --- |",
    ]
    for verdict, count in sorted(payload["verdict_counts"].items()):
        if count:
            lines.append(f"| `{verdict}` | {count} |")
    lines += [
        "",
        "## drift axes",
        "",
        "| axis | count |",
        "| --- | --- |",
    ]
    for axis, count in payload["drift_axis_counts"].items():
        lines.append(f"| `{axis}` | {count} |")
    lines += [
        "",
        "## per-panel triage",
        "",
        "| panel | verdict | drift | notes |",
        "| --- | --- | --- | --- |",
    ]
    for record in records:
        drift = ", ".join(record.drift_axes) or "—"
        notes = "; ".join(record.notes) or ""
        lines.append(
            f"| {_md_escape(record.title)} "
            f"| `{record.verdict.value}` "
            f"| {drift} "
            f"| {_md_escape(notes)} |"
        )
    lines.append("")
    if any(r.verdict == Verdict.DRIFT for r in records):
        lines.append("## drift details")
        lines.append("")
        for record in records:
            if record.verdict != Verdict.DRIFT:
                continue
            lines.append(f"### {_md_escape(record.title)}")
            for axis in record.drift_axes:
                detail = record.drift_details.get(axis, "")
                lines.append(f"- **{axis}** — {_md_escape(detail)}")
            lines.append("")
    return "\n".join(lines)


def _md_escape(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
