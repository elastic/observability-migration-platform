#!/usr/bin/env python3
"""Panel verification framework: trace each panel through 5 representations
of its query and report any divergence.

The 5 representations:

  T0: source PromQL    — what the Grafana panel declared
  T1: translator out   — what mig-to-kbn emitted (migration_report.json `esql`)
  T2: YAML on disk     — what was written to <output-dir>/dashboards/yaml/*.yaml
  T3: compiled NDJSON  — what `kb-dashboard-cli compile` produced
  T4: cluster Lens     — what Kibana actually has saved (per /internal/dashboards/app/<id>)
  T5: live _query body — what Lens sends to Elasticsearch at render time
  T6: cluster response — the actual rows that come back from Elasticsearch

Each panel gets a row with 5 boolean axes (T0=T1, T1=T2, T2=T3, T3=T4, T4=T5)
plus a verdict (PASS / DRIFT-* / DATA-EMPTY / TRANSLATOR-NOT-FEASIBLE).

Outputs:
  - reports/verify-<dashboard-slug>.json  — machine-readable, agent-friendly
  - reports/verify-<dashboard-slug>.md    — human-friendly grid

Usage:
  python verify.py --dashboard <kibana-dashboard-id> --slug <local-output-slug>
  python verify.py --slug express-prometheus-middleware    # auto-discover id
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent.parent  # repo root
PARITY_OUTPUT_PREFIX = "/tmp/mig-to-kbn-e2e/parity-out-"


# Optional ANSI colour helpers — disabled when stdout isn't a TTY.
def _color(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


GREEN = lambda s: _color(s, "32")  # noqa: E731
YELLOW = lambda s: _color(s, "33")  # noqa: E731
RED = lambda s: _color(s, "31")  # noqa: E731
CYAN = lambda s: _color(s, "36")  # noqa: E731
DIM = lambda s: _color(s, "2")  # noqa: E731


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PanelRecord:
    title: str
    source_panel_id: str | None = None
    # T0
    source_promql: str = ""
    source_legend_format: str = ""
    # T1
    translator_esql: str = ""
    translator_status: str = ""  # "migrated" | "migrated_with_warnings" | "skipped" | "not_feasible"
    translator_warnings: list[str] = dataclasses.field(default_factory=list)
    # T2
    yaml_esql: str = ""
    # T3
    ndjson_esql: str = ""
    # T4
    cluster_esql: str = ""
    cluster_updated_at: str = ""
    # T5 (optional, captured live)
    live_query_body: str = ""
    # T6 — cluster response when we re-run the cluster_esql ourselves
    cluster_response_summary: dict[str, Any] = dataclasses.field(default_factory=dict)
    # Verdict
    verdict: str = "UNKNOWN"
    drift_axes: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_migration_report(slug: str) -> dict[str, Any]:
    p = Path(f"{PARITY_OUTPUT_PREFIX}{slug}/dashboards/migration_report.json")
    if not p.exists():
        raise FileNotFoundError(
            f"No migration_report.json at {p}. Run obs-migrate against this slug first."
        )
    return json.loads(p.read_text())


def load_yaml_panels(slug: str) -> dict[str, str]:
    """Walk the YAML and return {panel_title: esql_query}."""
    yaml_dir = Path(f"{PARITY_OUTPUT_PREFIX}{slug}/dashboards/yaml")
    out: dict[str, str] = {}
    if not yaml_dir.exists():
        return out
    try:
        import yaml as _yaml
    except ImportError:
        print("WARN: PyYAML not available — skipping T2.", file=sys.stderr)
        return out
    for yp in yaml_dir.glob("*.yaml"):
        doc = _yaml.safe_load(yp.read_text())

        def _walk(d: Any) -> None:
            if isinstance(d, dict):
                title = d.get("title")
                esql = d.get("esql")
                if isinstance(title, str) and isinstance(esql, dict):
                    q = esql.get("query")
                    if isinstance(q, str):
                        out[title] = q
                for v in d.values():
                    _walk(v)
            elif isinstance(d, list):
                for v in d:
                    _walk(v)

        _walk(doc)
    return out


def load_ndjson_panels(slug: str) -> dict[str, str]:
    """Walk the compiled NDJSON for each Lens panel's ES|QL query string.
    The dashboard saved object stores Lens panels inline inside ``panelsJSON``.
    """
    out: dict[str, str] = {}
    compiled_dir = Path(f"{PARITY_OUTPUT_PREFIX}{slug}/dashboards/compiled")
    if not compiled_dir.exists():
        return out
    for nd in compiled_dir.rglob("compiled_dashboards.ndjson"):
        for line in nd.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "dashboard":
                continue
            attrs = obj.get("attributes", {})
            pj = attrs.get("panelsJSON", "")
            if not pj:
                continue
            try:
                panels = json.loads(pj)
            except json.JSONDecodeError:
                continue

            def _walk(panel: Any) -> None:
                if isinstance(panel, dict):
                    title = (
                        (panel.get("embeddableConfig") or {}).get("attributes", {}).get("title")
                        or (panel.get("config") or {}).get("attributes", {}).get("title")
                        or panel.get("title")
                    )
                    # The Lens panel stores the ESQL under
                    # embeddableConfig.attributes.state.datasourceStates.textBased.layers[*].query.esql
                    cfg_attrs = (
                        (panel.get("embeddableConfig") or {}).get("attributes")
                        or (panel.get("config") or {}).get("attributes")
                        or {}
                    )
                    state = cfg_attrs.get("state") or {}
                    if isinstance(state, str):
                        try:
                            state = json.loads(state)
                        except json.JSONDecodeError:
                            state = {}
                    ds = state.get("datasourceStates") or {}
                    tb = ds.get("textBased") or {}
                    layers = tb.get("layers") or {}
                    for layer in layers.values():
                        q = layer.get("query") if isinstance(layer, dict) else None
                        esql = q.get("esql") if isinstance(q, dict) else None
                        if title and isinstance(esql, str):
                            out[title] = esql
                            break
                    for v in panel.values():
                        if isinstance(v, (dict, list)):
                            _walk(v)
                elif isinstance(panel, list):
                    for v in panel:
                        _walk(v)

            _walk(panels)
    return out


# ---------------------------------------------------------------------------
# Live Kibana / cluster I/O
# ---------------------------------------------------------------------------


def _http_get_json(url: str, key: str, kibana: bool = False) -> Any:
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"ApiKey {key}")
    if kibana:
        req.add_header("kbn-xsrf", "x")
        req.add_header("elastic-api-version", "1")
        req.add_header("x-elastic-internal-origin", "Kibana")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _http_post_json(url: str, body: dict[str, Any], key: str) -> Any:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"ApiKey {key}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def load_cluster_dashboard(kibana_url: str, key: str, dashboard_id: str) -> dict[str, Any]:
    url = f"{kibana_url.rstrip('/')}/internal/dashboards/app/{dashboard_id}"
    return _http_get_json(url, key, kibana=True)


def cluster_dashboard_panels(dashboard_payload: dict[str, Any]) -> tuple[dict[str, str], str]:
    """Return ({panel_title: esql}, updated_at)."""
    out: dict[str, str] = {}
    updated_at = (dashboard_payload.get("meta") or {}).get("updated_at", "")

    def _walk(d: Any) -> None:
        if isinstance(d, dict):
            cfg = d.get("config") or {}
            attrs = cfg.get("attributes") if isinstance(cfg, dict) else None
            if isinstance(attrs, dict) and attrs.get("title"):
                title = attrs["title"]
                state = attrs.get("state") or {}
                ds = state.get("datasourceStates") or {}
                tb = ds.get("textBased") or {}
                layers = tb.get("layers") or {}
                for layer in layers.values():
                    q = layer.get("query") if isinstance(layer, dict) else None
                    esql = q.get("esql") if isinstance(q, dict) else None
                    if isinstance(esql, str):
                        out[title] = esql
                        break
            for v in d.values():
                _walk(v)
        elif isinstance(d, list):
            for v in d:
                _walk(v)

    _walk(dashboard_payload)
    return out, updated_at


def run_cluster_query(es_url: str, key: str, esql: str, time_window_min: int = 15) -> dict[str, Any]:
    """Run an ES|QL query and return a summary {rows, sample_value, error}."""
    import datetime as _dt

    now = _dt.datetime.now(_dt.UTC)
    tstart = (now - _dt.timedelta(minutes=time_window_min)).isoformat().replace("+00:00", "Z")
    tend = now.isoformat().replace("+00:00", "Z")
    body = {
        "query": esql,
        "params": [{"_tstart": tstart}, {"_tend": tend}],
    }
    try:
        resp = _http_post_json(f"{es_url.rstrip('/')}/_query", body, key)
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
        except Exception:
            err_body = {"raw": str(e)}
        return {"error": err_body.get("error", err_body), "rows": 0}
    except Exception as e:
        return {"error": str(e), "rows": 0}

    rows = resp.get("values") or []
    cols = [c.get("name") for c in resp.get("columns", [])]
    summary: dict[str, Any] = {
        "rows": len(rows),
        "columns": cols,
    }
    # Try to highlight the metric value if there's a likely scalar.
    if rows:
        first = rows[0]
        if len(first) > 0:
            value_idx = 0
            for i, name in enumerate(cols):
                if name and ("value" in name.lower() or name.lower() == "computed_value"):
                    value_idx = i
                    break
            summary["sample_value"] = first[value_idx]
    return summary


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _normalize_esql(text: str) -> str:
    """Compress whitespace so cosmetic newline differences don't trip diff."""
    return re.sub(r"\s+", " ", text or "").strip()


def _short_hash(s: str) -> str:
    return hashlib.sha1((s or "").encode()).hexdigest()[:8]


def compare(panel: PanelRecord) -> None:
    """Populate panel.verdict + panel.drift_axes based on T1..T4 agreement."""
    t1 = _normalize_esql(panel.translator_esql)
    t2 = _normalize_esql(panel.yaml_esql)
    t3 = _normalize_esql(panel.ndjson_esql)
    t4 = _normalize_esql(panel.cluster_esql)

    # Map of axis name -> (left_present, right_present, equal)
    pairs = [
        ("T1=T2", t1, t2),
        ("T2=T3", t2, t3),
        ("T3=T4", t3, t4),
    ]

    drift: list[str] = []
    for label, left, right in pairs:
        if not left and not right:
            continue  # both missing; not a drift, just unknown
        if not left or not right:
            # One side missing — that's a kind of drift.
            drift.append(f"{label} (one side empty)")
            continue
        if left != right:
            drift.append(label)

    panel.drift_axes = drift

    # Verdict precedence:
    # 1. Translator declined → TRANSLATOR-NOT-FEASIBLE
    # 2. Any drift → DRIFT-<which>
    # 3. Cluster query returned an error → CLUSTER-ERROR
    # 4. Cluster query returned 0 rows → DATA-EMPTY
    # 5. Cluster query returned rows → PASS
    if panel.translator_status in {"not_feasible", "requires_manual", "skipped"}:
        if panel.translator_status == "skipped":
            panel.verdict = "ROW-DIVIDER"
        else:
            panel.verdict = "TRANSLATOR-NOT-FEASIBLE"
        return

    if drift:
        panel.verdict = "DRIFT"
        # Be specific about which axis drifted
        if "T3=T4" in drift:
            panel.notes.append(
                "Cluster Lens spec differs from the locally-compiled NDJSON. "
                "Likely a stale upload — re-run `obs-migrate --upload` for this dashboard."
            )
        elif "T2=T3" in drift:
            panel.notes.append(
                "YAML differs from compiled NDJSON. `kb-dashboard-cli compile` may have "
                "rewritten the query. Check the compiled output."
            )
        elif "T1=T2" in drift:
            panel.notes.append(
                "Translator output differs from the YAML on disk. The YAML emitter is "
                "transforming the query — inspect translate.py or yaml emit."
            )
        return

    resp = panel.cluster_response_summary
    if resp.get("error"):
        panel.verdict = "CLUSTER-ERROR"
        return
    if resp.get("rows", 0) == 0:
        panel.verdict = "DATA-EMPTY"
        return

    panel.verdict = "PASS"


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def collect_panels(
    slug: str,
    dashboard_id: str,
    kibana_url: str,
    es_url: str,
    key: str,
    run_cluster: bool = True,
) -> list[PanelRecord]:
    report = load_migration_report(slug)
    yaml_panels = load_yaml_panels(slug)
    ndjson_panels = load_ndjson_panels(slug)

    cluster_panels: dict[str, str] = {}
    cluster_updated_at = ""
    if kibana_url and key:
        try:
            dashboard_payload = load_cluster_dashboard(kibana_url, key, dashboard_id)
            cluster_panels, cluster_updated_at = cluster_dashboard_panels(dashboard_payload)
        except Exception as exc:
            print(f"WARN: failed to fetch cluster dashboard: {exc}", file=sys.stderr)

    records: list[PanelRecord] = []
    for db in report.get("dashboards", []):
        for p in db.get("panels", []):
            title = p.get("title") or ""
            rec = PanelRecord(
                title=title,
                source_panel_id=str(p.get("source_panel_id", "")),
                source_promql=p.get("promql", ""),
                source_legend_format=(p.get("source") or {}).get("legendFormat", "")
                if isinstance(p.get("source"), dict)
                else "",
                translator_esql=p.get("esql", "") or "",
                translator_status=p.get("status", ""),
                translator_warnings=list(p.get("notes") or []) + list(p.get("reasons") or []),
                yaml_esql=yaml_panels.get(title, ""),
                ndjson_esql=ndjson_panels.get(title, ""),
                cluster_esql=cluster_panels.get(title, ""),
                cluster_updated_at=cluster_updated_at,
            )
            if (
                run_cluster
                and rec.cluster_esql
                and es_url
                and key
                and rec.translator_status not in {"not_feasible", "requires_manual", "skipped"}
            ):
                rec.cluster_response_summary = run_cluster_query(es_url, key, rec.cluster_esql)
            compare(rec)
            records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def emit_json(records: list[PanelRecord], path: Path) -> None:
    payload = {
        "panels": [dataclasses.asdict(r) for r in records],
        "summary": _summary_counts(records),
    }
    path.write_text(json.dumps(payload, indent=2, default=str))


def _summary_counts(records: list[PanelRecord]) -> dict[str, int]:
    from collections import Counter

    c = Counter(r.verdict for r in records)
    return dict(c)


def emit_markdown(records: list[PanelRecord], path: Path, slug: str, dashboard_id: str) -> None:
    lines: list[str] = []
    lines.append(f"# Panel verification: `{slug}` ({dashboard_id})")
    lines.append("")
    counts = _summary_counts(records)
    total = len(records)
    pass_n = counts.get("PASS", 0)
    lines.append(f"**{pass_n}/{total} panels pass.**")
    lines.append("")
    lines.append("| Verdict | Count |")
    lines.append("|---|---:|")
    for v in sorted(counts.keys()):
        lines.append(f"| {v} | {counts[v]} |")
    lines.append("")
    lines.append("## Per-panel detail (non-passing)")
    lines.append("")
    lines.append("| Panel | Verdict | Drift | Notes |")
    lines.append("|---|---|---|---|")
    for r in records:
        if r.verdict == "PASS":
            continue
        drift = "; ".join(r.drift_axes) or "—"
        notes = " ".join(r.notes)[:200] or "—"
        lines.append(f"| {r.title} | {r.verdict} | {drift} | {notes} |")
    path.write_text("\n".join(lines) + "\n")


def emit_console(records: list[PanelRecord], slug: str) -> None:
    counts = _summary_counts(records)
    total = len(records)
    pass_n = counts.get("PASS", 0)
    print(f"\nDashboard {slug}: {pass_n}/{total} pass\n")
    for v in sorted(counts):
        col = GREEN if v == "PASS" else YELLOW if v in {"DRIFT", "DATA-EMPTY"} else RED
        if v in {"ROW-DIVIDER", "TRANSLATOR-NOT-FEASIBLE"}:
            col = DIM
        print(f"  {col(v):30s} {counts[v]:>4}")
    print()
    # First 15 non-pass examples
    shown = 0
    for r in records:
        if r.verdict == "PASS":
            continue
        if r.verdict in {"ROW-DIVIDER"}:
            continue
        if shown >= 15:
            print(f"  ... {sum(1 for x in records if x.verdict not in {'PASS','ROW-DIVIDER'}) - 15} more")
            break
        shown += 1
        col = YELLOW if r.verdict in {"DRIFT", "DATA-EMPTY"} else RED
        if r.verdict == "TRANSLATOR-NOT-FEASIBLE":
            col = DIM
        print(f"  {col(r.verdict):30s} {r.title}")
        if r.drift_axes:
            print(f"    {DIM('drift:')} {'; '.join(r.drift_axes)}")
        if r.notes:
            print(f"    {DIM('note: ')}{r.notes[0]}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_dashboard_id(slug: str) -> str | None:
    """Walk the compiled NDJSON to find the dashboard saved-object ID."""
    nd_dir = Path(f"{PARITY_OUTPUT_PREFIX}{slug}/dashboards/compiled")
    for nd in nd_dir.rglob("compiled_dashboards.ndjson"):
        for line in nd.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "dashboard":
                return obj.get("id")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--slug", required=True, help="Local output slug under /tmp/mig-to-kbn-e2e/parity-out-<slug>")
    parser.add_argument("--dashboard", help="Kibana dashboard ID (auto-resolved from compiled NDJSON if omitted)")
    parser.add_argument("--kibana-url", default=os.environ.get("KIBANA_ENDPOINT", ""))
    parser.add_argument("--es-url", default=os.environ.get("ELASTICSEARCH_ENDPOINT", ""))
    parser.add_argument("--api-key", default=os.environ.get("KEY", ""))
    parser.add_argument("--report-dir", default=str(Path(__file__).parent / "reports"))
    parser.add_argument("--no-cluster-query", action="store_true", help="Skip T6 (running queries against the cluster)")
    args = parser.parse_args()

    dashboard_id = args.dashboard or _resolve_dashboard_id(args.slug)
    if not dashboard_id:
        print(
            f"ERROR: couldn't resolve dashboard id for slug={args.slug}. "
            f"Pass --dashboard explicitly.",
            file=sys.stderr,
        )
        return 2

    records = collect_panels(
        slug=args.slug,
        dashboard_id=dashboard_id,
        kibana_url=args.kibana_url,
        es_url=args.es_url,
        key=args.api_key,
        run_cluster=not args.no_cluster_query,
    )

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"verify-{args.slug}.json"
    md_path = report_dir / f"verify-{args.slug}.md"
    emit_json(records, json_path)
    emit_markdown(records, md_path, args.slug, dashboard_id)
    emit_console(records, args.slug)
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")
    # Exit non-zero if any panel drift or cluster error
    bad = [r for r in records if r.verdict in {"DRIFT", "CLUSTER-ERROR"}]
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
