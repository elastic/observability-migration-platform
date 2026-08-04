"""Command-line entrypoint for the 5-tier panel verifier.

Usage::

    python -m parity-rig.verifier.cli \
        --migration-out /tmp/obs-migrate-e2e/parity-out-<slug>/dashboards \
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
from typing import Any

from . import collectors, invariants
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
        help="Path to the per-dashboard obs-migrate output directory "
             "(contains migration_report.json, ir/, native/, compiled/).",
    )
    p.add_argument(
        "--kibana-url",
        type=str,
        help="Kibana base URL (e.g. https://<cluster>.kb.us-central1.gcp.staging.elastic.cloud). "
             "Required to collect T3 (GET /api/dashboards/{id} -- the dashboards as Kibana "
             "actually stored them) and T4. Without it T3 is reported unavailable rather than "
             "guessed, unless a legacy compiled/ dir is present.",
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
        "--no-invariants",
        action="store_true",
        help="Skip the Layer-9 deterministic invariant linter (accessor / "
             "merged-series / placeholder honesty).",
    )
    p.add_argument(
        "--live-oracle",
        action="store_true",
        help="Resolve ES|QL output columns via the live cluster (POST /_query) "
             "instead of the offline parser. Requires --es-url and --api-key.",
    )
    p.add_argument(
        "--fail-on-invariant",
        action="store_true",
        help="Exit non-zero if any invariant finding has ERROR severity "
             "(useful as a CI gate).",
    )
    p.add_argument(
        "--allow-empty-t1",
        action="store_true",
        help="Do not fail when T1 (translator ES|QL) is populated for zero "
             "panels. Only for a source that genuinely translated nothing: "
             "an empty T1 makes every verdict SKIP and every drift axis a "
             "vacuous 0 (see vacuous_tier_reason).",
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

    if not report_path.exists():
        print(f"error: migration_report.json not found at {report_path}", file=sys.stderr)
        return 2

    LOG.info("loading migration report: %s", report_path)
    report = collectors.load_migration_report(report_path)

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
                "(common on Elastic Serverless): %s. Falling back to the stored "
                "dashboard as the T4 source. For a true T4/T5 capture, run the "
                "browser walker (parity-rig/verifier/walker.py) which sources "
                "Lens's actual queries from a HAR recording.",
                cluster_unavailable_reason,
            )

    records = _collect_records(
        report,
        migration_dir,
        kibana_url=args.kibana_url or "",
        api_key=args.api_key or "",
        space=args.space,
        es_index=args.es_index,
        es_url=args.es_url or "",
        limit=args.limit,
        cluster_saved_object=cluster_saved_object,
        cluster_panels=cluster_panels,
        dashboard_id=args.dashboard_id or "",
    )

    invariant_findings: list[invariants.Finding] = []
    if not args.no_invariants:
        columns_oracle = None
        if args.live_oracle and args.es_url and args.api_key:
            LOG.info("using live ES column oracle for invariant checks")
            columns_oracle = invariants.make_es_columns_oracle(args.es_url, args.api_key)
        elif args.live_oracle:
            LOG.warning("--live-oracle requested but --es-url/--api-key missing; "
                        "falling back to offline column inference")
        # When --limit scopes the panel loop, scope invariant linting to the
        # same panels so the report stays internally consistent and
        # --fail-on-invariant cannot trip on panels outside the sample.
        lint_target = report
        if args.limit:
            sampled = {(r.dashboard_title, r.title) for r in records}
            lint_target = _scope_report_to_panels(report, sampled)
        invariant_findings = invariants.lint_report(lint_target, columns_oracle=columns_oracle)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "dashboard_id": args.dashboard_id or "",
        "dashboard_title": records[0].dashboard_title if records else "",
        "verdict_counts": aggregate_verdicts(records),
        "drift_axis_counts": aggregate_drift_axes(records),
        "tier_population": _tier_population(records),
        "invariant_summary": invariants.summarize(invariant_findings),
        "invariant_findings": [f.to_jsonable() for f in invariant_findings],
        "panels": [r.to_jsonable() for r in records],
    }
    args.output.write_text(json.dumps(payload, indent=2, default=str))
    LOG.info("wrote %s", args.output)

    md_path = args.output.with_suffix(".md")
    md_path.write_text(_render_markdown(payload, records))
    LOG.info("wrote %s", md_path)

    print(_render_console_summary(payload))

    vacuous = vacuous_tier_reason(payload["tier_population"])
    if vacuous and not args.allow_empty_t1:
        print(f"\nFAIL: {vacuous}", file=sys.stderr)
        return 1

    error_findings = payload["invariant_summary"].get("error_count", 0)
    if args.fail_on_invariant and error_findings:
        print(
            f"\nFAIL: {error_findings} invariant finding(s) with ERROR severity",
            file=sys.stderr,
        )
        return 1
    return 0


def _scope_report_to_panels(
    report: dict, sampled: set[tuple[str, str]]
) -> dict:
    """Return a shallow report copy keeping only the sampled ``(dashboard, panel)``.

    Used so ``--limit`` scopes invariant linting to the same panels the five-tier
    loop processed, keeping ``panels`` and ``invariant_findings`` consistent.
    """
    scoped = dict(report)
    scoped_dashboards = []
    for dashboard in report.get("dashboards", []):
        if not isinstance(dashboard, dict):
            continue
        dtitle = str(dashboard.get("title") or "")
        kept = [
            panel
            for panel in dashboard.get("panels", [])
            if isinstance(panel, dict)
            and (dtitle, str(panel.get("title") or "")) in sampled
        ]
        if kept:
            scoped_dashboards.append({**dashboard, "panels": kept})
    scoped["dashboards"] = scoped_dashboards
    return scoped


def _unmatchable_note(tier: str, artifact_label: str, record: PanelRecord) -> str:
    return (
        f"{tier} unavailable: no {artifact_label} could be matched to "
        f"dashboard (uid={record.dashboard_uid!r}, title={record.dashboard_title!r}); "
        "panel titles are not unique across dashboards, so it was not guessed"
    )


def _scoped_dashboard_index(
    index: dict[str, dict[str, Any]],
    record: PanelRecord,
    tier: str,
    artifact_label: str,
) -> dict[str, Any] | None:
    """Return the per-dashboard sub-index for *record*, or ``None``.

    The index is keyed by dashboard (uid and/or title) rather than by panel
    title alone. Looking a panel up by title across every dashboard of a run
    silently hands one dashboard's panel another dashboard's query whenever
    the titles match -- which on real corpora is common ("CPU Usage",
    "Error Logs", "Uptime") and turns the drift axes into noise in both
    directions.

    An empty index means the artifacts simply are not there; the caller decides
    what that means for its tier. An index that exists but has no entry for this
    record's dashboard is different: the tier is genuinely unresolvable for this
    panel, so say so rather than substituting a neighbour's query.
    """
    if not index:
        return None
    for key in (record.dashboard_uid, record.dashboard_title):
        if key and key in index:
            return index[key]
    if len(index) == 1:
        # One dashboard in the artifact set: there is nothing to confuse it
        # with, and ``--migration-out`` documents exactly this shape. Keeps
        # artifacts that carry no dashboard title joinable.
        return next(iter(index.values()))
    record.notes.append(_unmatchable_note(tier, artifact_label, record))
    return None


def _scoped_panel_query(
    index: dict[str, dict[str, str]],
    record: PanelRecord,
    tier: str,
    artifact_label: str,
) -> str:
    """Return *record*'s query for a dashboard-scoped tier index of strings."""
    scoped = _scoped_dashboard_index(index, record, tier, artifact_label)
    if scoped is None:
        return ""
    return str(scoped.get(record.title, "") or "")


def _scoped_stored_panel(
    index: dict[str, dict[str, collectors.StoredPanel]],
    record: PanelRecord,
) -> collectors.StoredPanel | None:
    """Return *record*'s panel as Kibana stored it, or ``None``.

    ``None`` means the record's dashboard could not be identified in the stored
    set at all (noted on the record); a dashboard that *was* matched but has no
    panel with this title returns a placeholder-free miss via
    ``StoredPanel``-less ``.get`` in the caller, which is a real finding: the
    panel is absent from the uploaded dashboard.
    """
    scoped = _scoped_dashboard_index(index, record, "T3", "stored Kibana dashboard")
    if scoped is None:
        return None
    stored = scoped.get(record.title)
    return stored if isinstance(stored, collectors.StoredPanel) else None


_T3_NO_SOURCE_NOTE = (
    "T3 unavailable: no --kibana-url supplied and no compiled/ artifacts on "
    "disk, so the dashboard as Kibana stored it was never consulted. T3/T4 were "
    "not verified -- pass --kibana-url to check the uploaded dashboard"
)

_T4_NO_SOURCE_NOTE = (
    "T4 unavailable: no cluster saved object was requested (needs --kibana-url, "
    "--api-key and --dashboard-id), so the Lens saved object was never consulted"
)


def _load_stored_panels(
    migration_dir: Path,
    *,
    kibana_url: str,
    api_key: str,
    space: str,
    fetch: Any = None,
) -> tuple[dict[str, dict[str, collectors.StoredPanel]], str]:
    """Return ``({dashboard_key: {panel_title: StoredPanel}}, unavailable_reason)``.

    Sourced from ``GET /api/dashboards/{id}`` for every dashboard the run wrote a
    native artifact for. The ids are deterministic (``obs-migrate-<title-slug>``)
    and recorded in ``native/index.json``, so no id has to be guessed.

    Returns an empty index plus a reason when no Kibana URL was supplied or the
    run wrote no native artifacts. That reason is what keeps the degrade honest:
    an unconsulted tier is reported as unavailable rather than as a mutated one.
    """
    fetch = fetch or collectors.fetch_stored_dashboard
    if not kibana_url:
        return {}, _T3_NO_SOURCE_NOTE
    entries = collectors.load_native_dashboard_index(migration_dir / "native")
    if not entries:
        return {}, (
            "T3 unavailable: no native/ dashboard artifacts in "
            f"{migration_dir}, so no stored dashboard id could be resolved"
        )
    payloads: list[dict[str, Any]] = []
    failures: list[str] = []
    for entry in entries:
        dashboard_id = entry["dashboard_id"]
        try:
            payload = fetch(kibana_url, api_key, dashboard_id, space)
        except Exception as exc:  # network / auth / server error
            failures.append(f"{dashboard_id}: {str(exc)[:120]}")
            continue
        if payload:
            payloads.append(payload)
    if failures:
        LOG.warning(
            "could not fetch %d of %d dashboards from the Dashboards API: %s",
            len(failures), len(entries), "; ".join(failures[:5]),
        )
    if not payloads:
        return {}, (
            "T3 unavailable: none of the "
            f"{len(entries)} migrated dashboard(s) could be read back from "
            f"{kibana_url} via GET /api/dashboards/{{id}}"
        )
    LOG.info("read %d stored dashboard(s) from %s", len(payloads), kibana_url)
    return collectors.stored_panels_by_dashboard(payloads), ""


def _fill_t3(
    record: PanelRecord,
    stored_index: dict[str, dict[str, collectors.StoredPanel]],
    ndjson_panels: dict[str, dict[str, str]],
    stored_unavailable: str,
) -> None:
    """Fill T3 from the stored dashboard, else the legacy compiled NDJSON.

    The API source wins whenever it is available: it is the dashboard Kibana
    actually holds, not a compiler artifact, and it is the only source of the
    real Kibana panel UUIDs. The NDJSON reader stays as a fallback for runs that
    still carry a ``compiled/`` dir.
    """
    if stored_index:
        scoped = _scoped_dashboard_index(
            stored_index, record, "T3", "stored Kibana dashboard"
        )
        if scoped is None:
            record.t3_unavailable_reason = _unmatchable_note(
                "T3", "stored Kibana dashboard", record
            )
            return
        record.t3_source = "dashboards_api"
        stored = scoped.get(record.title)
        if isinstance(stored, collectors.StoredPanel):
            record.t3_ndjson_esql = stored.esql
            record.t3_panel_id = stored.panel_id
            record.t3_dashboard_id = stored.dashboard_id
        return
    if ndjson_panels:
        scoped = _scoped_dashboard_index(ndjson_panels, record, "T3", "compiled NDJSON")
        if scoped is None:
            record.t3_unavailable_reason = _unmatchable_note(
                "T3", "compiled NDJSON", record
            )
            return
        record.t3_source = "compiled_ndjson"
        record.t3_ndjson_esql = str(scoped.get(record.title, "") or "")
        return
    record.t3_unavailable_reason = stored_unavailable or _T3_NO_SOURCE_NOTE
    record.notes.append(record.t3_unavailable_reason)


def _collect_records(
    report: dict,
    migration_dir: Path,
    *,
    kibana_url: str,
    api_key: str,
    space: str,
    es_index: str,
    es_url: str,
    limit: int,
    cluster_saved_object: dict,
    cluster_panels: dict[str, str],
    dashboard_id: str,
    fetch_stored: Any = None,
) -> list[PanelRecord]:
    """Build one :class:`PanelRecord` per report panel with every tier filled."""
    ir_dir = migration_dir / "ir"
    compiled_dir = migration_dir / "compiled"

    LOG.info("scanning ir dir: %s", ir_dir)
    # Scoped per dashboard, not flattened by panel title: one output dir can
    # hold every dashboard of a run, and titles repeat across dashboards.
    ir_panels = (
        collectors.load_ir_panels_by_dashboard(ir_dir) if ir_dir.exists() else {}
    )

    stored_index, stored_unavailable = _load_stored_panels(
        migration_dir,
        kibana_url=kibana_url,
        api_key=api_key,
        space=space,
        fetch=fetch_stored,
    )
    ndjson_panels: dict[str, dict[str, str]] = {}
    if not stored_index:
        LOG.info("scanning compiled dir: %s", compiled_dir)
        ndjson_panels = _load_compiled_panels(compiled_dir)

    records: list[PanelRecord] = []
    for record in collectors.panels_from_migration_report(report):
        if es_index and not record.t1_index:
            record.t1_index = es_index
        record.t2_ir_esql = _scoped_panel_query(ir_panels, record, "T2", "IR export artifact")
        _fill_t3(record, stored_index, ndjson_panels, stored_unavailable)
        if cluster_saved_object:
            record.t4_cluster_esql = cluster_panels.get(record.title, "")
            record.t4_saved_object_id = cluster_saved_object.get("id", "")
            record.t4_saved_object_updated_at = cluster_saved_object.get("updated_at", "")
        elif dashboard_id and record.t3_ndjson_esql:
            record.t4_cluster_esql = record.t3_ndjson_esql
            record.notes.append(
                f"T4 sourced from T3 ({record.t3_source or 'unknown'}); the cluster "
                "saved-objects API was unavailable -- run the browser walker for a "
                "true T4 capture"
            )
        else:
            record.t4_unavailable_reason = _T4_NO_SOURCE_NOTE
        if es_url and api_key and record.t4_cluster_esql:
            status, body = collectors.run_cluster_query(
                es_url, api_key, record.t4_cluster_esql
            )
            collectors.annotate_record_with_live_response(record, status, body)
            record.t5_live_query_body = record.t4_cluster_esql
        compare_panel_record(record)
        records.append(record)
        if limit and len(records) >= limit:
            break
    return records


def vacuous_tier_reason(tier_population: dict[str, Any]) -> str:
    """Why this run's verdicts mean nothing, or ``""`` when they mean something.

    T1 is the translator's own output. ``compare_panel_record`` short-circuits an
    empty T1 to ``SKIP``, so a run whose T1 is populated for **zero** panels
    reports all-SKIP with 0 drift on all five axes no matter what the translator
    emitted -- which is indistinguishable from a perfect run.

    That state had a cause (``07e5829``: the collector read Grafana's ``esql`` and
    never Datadog's ``esql_query``) and the cause was fixed, but nothing stopped
    the *next* cause from producing the same silence. This turns it into a
    non-zero exit. Keyed on the denominator being zero while panels exist: a run
    where some panels translated stays green, and a genuinely empty report is
    already reported as such.
    """
    panels = int(tier_population.get("panels") or 0)
    if panels <= 0:
        return ""
    populated = int((tier_population.get("tiers") or {}).get("t1_translator_esql") or 0)
    if populated:
        return ""
    return (
        f"T1 (translator ES|QL) is populated for 0 of {panels} panel(s), so every "
        "verdict is SKIP and every drift axis is vacuously 0. Either the report "
        "carries no translator output or the collector cannot read the key this "
        "source writes it under. Re-run with --allow-empty-t1 if the source really "
        "translated nothing."
    )


def _tier_population(records: list[PanelRecord]) -> dict[str, Any]:
    """How many panels each tier was actually filled for, plus T3's provenance.

    A verdict distribution alone cannot distinguish "checked and agreed" from
    "never checked": both look quiet. This block makes the denominator of every
    tier explicit so a run that skipped the target side cannot read as a clean
    one.
    """
    tiers = {
        "t0_source_promql": 0,
        "t1_translator_esql": 0,
        "t2_ir_esql": 0,
        "t3_stored_esql": 0,
        "t4_cluster_esql": 0,
        "t5_live_query_body": 0,
    }
    sources: dict[str, int] = {}
    with_panel_uuid = 0
    for record in records:
        tiers["t0_source_promql"] += bool(record.t0_source_promql)
        tiers["t1_translator_esql"] += bool(record.t1_translator_esql)
        tiers["t2_ir_esql"] += bool(record.t2_ir_esql)
        tiers["t3_stored_esql"] += bool(record.t3_ndjson_esql)
        tiers["t4_cluster_esql"] += bool(record.t4_cluster_esql)
        tiers["t5_live_query_body"] += bool(record.t5_live_query_body)
        key = record.t3_source or "unavailable"
        sources[key] = sources.get(key, 0) + 1
        with_panel_uuid += bool(record.t3_panel_id)
    return {
        "panels": len(records),
        "tiers": tiers,
        "t3_source_counts": sources,
        "t3_panels_with_kibana_uuid": with_panel_uuid,
    }


def _load_compiled_panels(compiled_dir: Path) -> dict[str, dict[str, str]]:
    """Return ``{dashboard_title: {panel_title: esql}}`` across every compiled dir.

    This used to ``return`` on the first ``compiled/<slug>/`` subdirectory it
    found, so in a multi-dashboard run every panel's T3 came from whichever
    dashboard sorted first. Read them all and keep them scoped instead.
    """
    merged: dict[str, dict[str, str]] = {}
    if not compiled_dir.exists():
        return merged
    for sub in sorted(compiled_dir.iterdir()):
        if not sub.is_dir():
            continue
        candidate = sub / "compiled_dashboards.ndjson"
        if not candidate.exists():
            continue
        for title, panels in collectors.load_ndjson_panels_by_dashboard(candidate).items():
            merged.setdefault(title, panels)
    return merged


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
    population = payload.get("tier_population") or {}
    if population:
        total = population.get("panels", 0)
        lines.append("")
        lines.append(f"tier population (of {total} panels):")
        for tier, count in (population.get("tiers") or {}).items():
            lines.append(f"  {tier:<20} {count}")
        sources = population.get("t3_source_counts") or {}
        lines.append(
            "  T3 source: "
            + ", ".join(f"{name}={count}" for name, count in sorted(sources.items()))
        )
        lines.append(
            f"  T3 panels with a real Kibana UUID: "
            f"{population.get('t3_panels_with_kibana_uuid', 0)}"
        )
    summary = payload.get("invariant_summary") or {}
    if summary.get("total"):
        lines.append("")
        lines.append("invariant findings:")
        for category, count in summary.get("by_category", {}).items():
            if count:
                lines.append(f"  {category:<26} {count}")
        lines.append(f"  {'(errors)':<26} {summary.get('error_count', 0)}")
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

    population = payload.get("tier_population") or {}
    if population:
        lines += [
            "",
            "## tier population",
            "",
            f"How many of the **{population.get('panels', 0)}** panels each tier was "
            "actually filled for. A tier with a low count was not verified, which a "
            "verdict distribution alone cannot show.",
            "",
            "| tier | panels |",
            "| --- | --- |",
        ]
        for tier, count in (population.get("tiers") or {}).items():
            lines.append(f"| `{tier}` | {count} |")
        sources = population.get("t3_source_counts") or {}
        lines += [
            "",
            "- T3 source: "
            + ", ".join(f"`{name}`={count}" for name, count in sorted(sources.items())),
            f"- T3 panels carrying a real Kibana panel UUID: "
            f"**{population.get('t3_panels_with_kibana_uuid', 0)}**",
        ]

    summary = payload.get("invariant_summary") or {}
    findings = payload.get("invariant_findings") or []
    if summary.get("total"):
        lines += [
            "",
            "## invariant findings (Layer 9)",
            "",
            f"- total: **{summary.get('total', 0)}** "
            f"(errors: **{summary.get('error_count', 0)}**)",
            "",
            "| panel | category | severity | message |",
            "| --- | --- | --- | --- |",
        ]
        for f in findings:
            lines.append(
                f"| {_md_escape(f.get('panel_title', ''))} "
                f"| `{f.get('category', '')}` "
                f"| `{f.get('severity', '')}` "
                f"| {_md_escape(f.get('message', ''))} |"
            )

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
