"""Collect each tier's ES|QL into a ``PanelRecord``.

Each function is responsible for filling exactly one tier; they are
designed to be runnable in isolation so a failure to e.g. reach the
cluster doesn't poison the local-only tiers (T0..T3).

Where Kibana / Lens stores the ES|QL is non-obvious, so the path used
to extract each tier is documented inline.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import UTC
from pathlib import Path
from typing import Any

import requests

from .records import PanelRecord

LOG = logging.getLogger(__name__)


_REQUEST_TIMEOUT = 30


def _auth_headers(api_key: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = dict(extra or {})
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
    return headers


# --------------------------------------------------------------------- #
# T0 + T1  — migration_report.json (source PromQL + translator output)
# --------------------------------------------------------------------- #


def load_migration_report(report_path: Path) -> dict[str, Any]:
    return json.loads(report_path.read_text())


def panels_from_migration_report(report: dict[str, Any]) -> Iterable[PanelRecord]:
    """Yield one :class:`PanelRecord` per panel in the report.

    ``migration_report.json`` is laid out as::

        { "dashboards": [ { "uid": ..., "title": ..., "panels": [...] }, ... ] }

    Panels live at ``dashboards[*].panels[*]``; both the source expression
    (``query_ir.source_expression``) and the translator output are direct
    fields on each panel object. The two source adapters spell the
    translator-output key differently -- Grafana writes ``esql``, Datadog
    writes ``esql_query`` -- so :func:`_record_from_report_panel` reads
    both.
    """
    for dashboard in report.get("dashboards", []):
        dash_uid = dashboard.get("uid", "")
        dash_title = dashboard.get("title", "")
        for idx, panel in enumerate(dashboard.get("panels", [])):
            yield _record_from_report_panel(idx, panel, dash_uid, dash_title)


def _record_from_report_panel(
    idx: int,
    panel: dict[str, Any],
    dash_uid: str,
    dash_title: str,
) -> PanelRecord:
    qir = panel.get("query_ir") or {}
    promql = (
        panel.get("promql")
        or qir.get("source_expression")
        or qir.get("clean_expression")
        or ""
    )
    # Grafana's report key is ``esql``; Datadog's is ``esql_query``. Reading
    # only ``esql`` left T1 empty for every Datadog panel, and
    # ``compare_panel_record`` short-circuits an empty T1 to SKIP -- so a
    # Datadog run verified as "all SKIP, 0 drift on every axis" no matter
    # what the translator had emitted. Vacuously green is worse than red.
    esql = (panel.get("esql") or panel.get("esql_query") or "").strip()
    is_native = esql.lstrip().upper().startswith("PROMQL")
    return PanelRecord(
        panel_id=str(panel.get("source_panel_id") or panel.get("widget_id") or f"panel-{idx}"),
        title=panel.get("title", "") or f"(untitled-{idx})",
        dashboard_uid=dash_uid,
        dashboard_title=dash_title,
        # ``grafana_type`` is the record's source-panel-type slot; Datadog
        # reports the same thing as ``dd_widget_type``.
        grafana_type=panel.get("grafana_type") or panel.get("dd_widget_type") or "",
        kibana_type=panel.get("kibana_type", ""),
        status=panel.get("status", ""),
        feasibility=(panel.get("readiness") or "").lower() or panel.get("status", ""),
        t0_source_promql=promql,
        t1_translator_esql=esql,
        t1_native_promql=is_native,
        t1_index=_extract_index_from_esql(esql),
        t1_warnings=list(panel.get("reasons") or []),
        t1_notes=list(panel.get("notes") or []),
    )


_INDEX_PATTERN = re.compile(
    r"^\s*(?:TS|FROM|PROMQL\s+index\s*=)\s*([\S]+)", re.IGNORECASE | re.MULTILINE
)


def _extract_index_from_esql(esql: str) -> str:
    if not esql:
        return ""
    m = _INDEX_PATTERN.search(esql)
    if not m:
        return ""
    return m.group(1).strip().rstrip(",")


# --------------------------------------------------------------------- #
# T2  — the migration's IR export (ir/*.ir.json)
# --------------------------------------------------------------------- #


def load_ir_panels(ir_dir: Path) -> dict[str, str]:
    """Return a ``{panel_title: esql_query}`` mapping for every IR
    dashboard artifact in ``ir_dir``.

    ``ir/<stem>.ir.json`` is the migration's semantic export, written next
    to the native Dashboards API payload::

        {
          "kind": "dashboard_ir",
          "dashboard_ir": {
            "title": ...,
            "panels": [
              {"kind": "section", "children": [...]},
              {"kind": "panel", "title": ..., "visual": {
                  "presentation": {"kind": "esql", "config": {"query": ...}}}}
            ]
          }
        }

    This replaces the previous ``yaml/*.yaml`` read. The IR's
    ``visual.presentation.config.query`` is the exact string the YAML
    export carried in ``esql.query`` -- the YAML was derived from this IR
    (``DashboardIR.to_yaml_dict``), never the other way around -- so T2
    means the same thing it always did while sourcing it from the artifact
    that survives.

    This is the *flat* view: every dashboard in ``ir_dir`` is folded into
    one title-keyed dict, so panels sharing a title across dashboards
    collide. Safe only for a single-dashboard directory. The verifier uses
    :func:`load_ir_panels_by_dashboard` instead -- see its docstring for
    what the collision costs.
    """
    out: dict[str, str] = {}
    for _key, panels in _iter_ir_dashboards(ir_dir):
        out.update(panels)
    return out


def load_ir_panels_by_dashboard(ir_dir: Path) -> dict[str, dict[str, str]]:
    """Return ``{dashboard_key: {panel_title: esql_query}}`` for ``ir_dir``.

    :func:`load_ir_panels` flattens every dashboard into one title-keyed
    dict. That is only correct for a single-dashboard directory, which is
    what ``--migration-out`` documents -- but ``grafana-migrate
    --input-dir`` / ``datadog-migrate --input-dir`` write every dashboard
    of a run into one ``ir/``, and pointing the verifier at that is the
    obvious thing to do. Panels sharing a title across dashboards then
    collide: last writer wins (``sorted()`` order), and every earlier
    dashboard's panel silently inherits a *different dashboard's* query.

    That does not merely lose data, it fabricates findings. On the in-repo
    15-dashboard Datadog corpus both of the T1=T2 "drift" findings were
    artifacts of this collision -- the Kafka dashboard's ``Error Logs``
    panel was compared against the Redis dashboard's ``Error Logs`` query.
    And on a 300-dashboard output it collapses 7135 IR panels down to 497
    keys, which is where the bogus "T2 = 497" figure came from.

    Each dashboard is registered under both its ``uid`` and its ``title``:
    Grafana's ``migration_report.json`` carries a dashboard uid, Datadog's
    carries only a title, so the caller tries uid first and falls back to
    title. A key claimed by two *different* dashboards is dropped rather
    than resolved arbitrarily, so an artifact set with no usable dashboard
    identity yields an empty T2 -- visible as drift plus a note -- instead
    of a confidently wrong one.
    """
    scoped: dict[str, dict[str, str]] = {}
    owner: dict[str, int] = {}
    ambiguous: set[str] = set()
    for idx, (keys, panels) in enumerate(_iter_ir_dashboards(ir_dir, with_keys=True)):
        for key in keys:
            if owner.get(key, idx) != idx:
                ambiguous.add(key)
                continue
            owner[key] = idx
            scoped[key] = panels
    for key in ambiguous:
        scoped.pop(key, None)
        LOG.warning(
            "IR dashboard key %r is claimed by more than one artifact in %s; "
            "T2 will be reported as unavailable for it rather than guessed",
            key, ir_dir,
        )
    return scoped


def _iter_ir_dashboards(ir_dir: Path, *, with_keys: bool = False):
    """Yield ``(key_or_keys, {panel_title: esql})`` per IR artifact.

    Shared by the flat and dashboard-scoped readers so both agree on how an
    artifact is parsed and which panels count as leaves.
    """
    for ir_path in sorted(ir_dir.glob("*.ir.json")):
        try:
            artifact = json.loads(ir_path.read_text())
        except Exception as exc:  # pragma: no cover - defensive
            LOG.warning("failed to parse %s: %s", ir_path, exc)
            continue
        dashboard_ir = (artifact or {}).get("dashboard_ir")
        if not isinstance(dashboard_ir, dict):
            continue
        panels = {
            panel["title"]: panel.get("esql_query", "")
            for panel in _iter_ir_panels(dashboard_ir.get("panels") or [])
        }
        if not with_keys:
            yield ir_path.name, panels
            continue
        keys = [
            key
            for key in (
                str(dashboard_ir.get("uid") or ""),
                str(dashboard_ir.get("title") or ""),
            )
            if key
        ]
        # An artifact with neither uid nor title is registered under the empty
        # key: unjoinable by name, but still reachable by the caller's
        # single-dashboard fallback (and still collision-detected if a second
        # nameless artifact shows up).
        yield keys or [""], panels


def _iter_ir_panels(panels: list[dict[str, Any]]) -> Iterable[dict[str, str]]:
    """Flatten an IR panel tree to ``{title, esql_query}`` leaf records.

    Section containers (``kind == "section"``) carry their panels in
    ``children`` and have no query of their own, mirroring the YAML
    export's ``section.panels`` nesting.
    """
    for panel in panels or []:
        if not isinstance(panel, dict):
            continue
        children = panel.get("children")
        if isinstance(children, list) and children:
            yield from _iter_ir_panels(children)
            continue
        if str(panel.get("kind") or "panel").strip().lower() != "panel":
            continue
        visual = panel.get("visual") or {}
        title = (
            (visual.get("title") if isinstance(visual, dict) else "")
            or panel.get("title")
            or "(untitled)"
        )
        query = ""
        presentation = visual.get("presentation") if isinstance(visual, dict) else None
        if isinstance(presentation, dict) and presentation.get("kind") == "esql":
            config = presentation.get("config") or {}
            if isinstance(config, dict):
                query = (config.get("query") or "").strip()
        yield {"title": title, "esql_query": query}


# --------------------------------------------------------------------- #
# T3  — compiled NDJSON (kb-dashboard-cli output)
# --------------------------------------------------------------------- #


def load_ndjson_panels(ndjson_path: Path) -> dict[str, str]:
    """Return a ``{panel_title: esql_query}`` mapping extracted from
    the compiled NDJSON.

    Saved object schema (Kibana 9.x dashboard)::

        { "attributes": {
            "panelsJSON": "<stringified JSON>",
            ...
        }}

    ``panelsJSON`` decodes to ``[{embeddableConfig: {attributes:
    {state: {query: {esql: "..."}}}}, ...}, ...]``.

    This is the *flat* view. One NDJSON can hold several dashboard saved
    objects, and folding them together collides panels that share a title
    -- see :func:`load_ir_panels_by_dashboard`. Use
    :func:`load_ndjson_panels_by_dashboard` when more than one dashboard
    may be present.
    """
    out: dict[str, str] = {}
    for _title, panels in _iter_ndjson_dashboards(ndjson_path):
        out.update(panels)
    return out


def load_ndjson_panels_by_dashboard(ndjson_path: Path) -> dict[str, dict[str, str]]:
    """Return ``{dashboard_title: {panel_title: esql_query}}`` for one NDJSON.

    The compiled saved object carries the dashboard's own
    ``attributes.title``, which is the key the panel records can be joined
    on. Two saved objects sharing a title are dropped rather than merged,
    for the reason given in :func:`load_ir_panels_by_dashboard`. A saved
    object with no title lands under the empty key -- unjoinable by name,
    but still reachable by the caller's single-dashboard fallback.
    """
    scoped: dict[str, dict[str, str]] = {}
    ambiguous: set[str] = set()
    for title, panels in _iter_ndjson_dashboards(ndjson_path):
        if title in scoped:
            ambiguous.add(title)
            continue
        scoped[title] = panels
    for title in ambiguous:
        scoped.pop(title, None)
    return scoped


def _iter_ndjson_dashboards(ndjson_path: Path):
    """Yield ``(dashboard_title, {panel_title: esql})`` per saved object."""
    if not ndjson_path.exists():
        return
    for line in ndjson_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "dashboard":
            continue
        attributes = obj.get("attributes") or {}
        panels_blob = attributes.get("panelsJSON")
        if not panels_blob:
            continue
        try:
            panels = json.loads(panels_blob)
        except json.JSONDecodeError:
            continue
        out: dict[str, str] = {}
        for panel in panels:
            title, esql = _extract_panel_title_and_esql(panel)
            if title:
                out[title] = esql
        yield str(attributes.get("title") or ""), out


def _extract_panel_title_and_esql(panel: dict[str, Any]) -> tuple[str, str]:
    embeddable = panel.get("embeddableConfig") or {}
    attrs = embeddable.get("attributes") or {}
    title = attrs.get("title") or embeddable.get("title") or panel.get("title") or ""
    state = attrs.get("state") or {}
    query = state.get("query") or {}
    esql = (query.get("esql") or "").strip() if isinstance(query, dict) else ""
    return title, esql


# --------------------------------------------------------------------- #
# T4  — cluster Lens (live saved object)
# --------------------------------------------------------------------- #


def fetch_cluster_dashboard(
    kibana_url: str,
    api_key: str,
    dashboard_id: str,
    space: str = "default",
) -> dict[str, Any]:
    """Pull a single dashboard saved object via the Kibana saved-objects API.

    Note: the saved-object schema across Kibana 8.x and 9.x has the same
    ``attributes.panelsJSON`` envelope as the compiled NDJSON, so the
    parser is the same.
    """
    url = (
        f"{kibana_url.rstrip('/')}/s/{space}/api/saved_objects/dashboard/"
        f"{dashboard_id}"
    )
    headers = _auth_headers(api_key, {"kbn-xsrf": "verifier"})
    r = requests.get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    return r.json()


def cluster_dashboard_panels(saved_object: dict[str, Any]) -> dict[str, str]:
    """Wrap a saved object in the same shape ``load_ndjson_panels``
    consumes, so the parser is shared."""
    if not saved_object:
        return {}
    panels_blob = (saved_object.get("attributes") or {}).get("panelsJSON")
    if not panels_blob:
        return {}
    try:
        panels = json.loads(panels_blob)
    except json.JSONDecodeError:
        return {}
    out: dict[str, str] = {}
    for panel in panels:
        title, esql = _extract_panel_title_and_esql(panel)
        if title:
            out[title] = esql
    return out


# --------------------------------------------------------------------- #
# T5  — live _query body (what the cluster actually executed)
# --------------------------------------------------------------------- #


def run_cluster_query(
    es_url: str,
    api_key: str,
    esql: str,
    params: list[dict[str, Any]] | None = None,
    timeout: int = _REQUEST_TIMEOUT,
) -> tuple[int, dict[str, Any] | str]:
    """Execute an ES|QL query against the cluster, returning
    ``(status_code, parsed_body_or_error_text)``.

    Used as the T5 collector: we re-run the T4 (cluster Lens) ES|QL
    directly against ``/_query`` so we can record exactly what the
    cluster does with the query Lens dispatches.

    If the query references named ``?_tstart`` / ``?_tend`` parameters
    (Lens injects them at runtime) and ``params`` is ``None``, we
    auto-supply a 1-hour window ending now.
    """
    headers = _auth_headers(api_key, {"Content-Type": "application/json"})
    body: dict[str, Any] = {"query": esql}
    if params is None:
        params = _autoparams_for_esql(esql)
    if params:
        body["params"] = params
    try:
        r = requests.post(
            f"{es_url.rstrip('/')}/_query",
            headers=headers,
            json=body,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return 0, f"transport error: {exc}"
    if r.status_code >= 400:
        return r.status_code, r.text[:2000]
    try:
        return r.status_code, r.json()
    except json.JSONDecodeError:
        return r.status_code, r.text[:2000]


_NAMED_PARAM_PATTERN = re.compile(
    r"(?<!\?)\?(?!\?)([a-zA-Z_][a-zA-Z0-9_]*)"
)

# Lens injects the chart time range under any of these alias spellings; each
# must bind to a concrete date, not a string/number wildcard. Exposed so other
# binders (e.g. ``live_validate._merge_validation_params``) can recognise the
# same set and avoid overwriting these date binds with control-param wildcards.
_TSTART_ALIASES = ("_tstart", "_t_start", "tstart")
_TEND_ALIASES = ("_tend", "_t_end", "tend")
_TIME_PARAM_ALIASES = frozenset(_TSTART_ALIASES + _TEND_ALIASES)


def _autoparams_for_esql(esql: str) -> list[dict[str, Any]]:
    """Build a minimal ``params`` list for any ``?name`` references in
    the query.

    Lens conventionally references ``?_tstart`` / ``?_tend`` for the
    chart time range, and named parameters elsewhere; if our T4 capture
    didn't preserve them we have to synthesise a reasonable default to
    avoid 400s.
    """
    from datetime import datetime, timedelta

    names = set(_NAMED_PARAM_PATTERN.findall(esql))
    if not names:
        return []
    end = datetime.now(UTC)
    start = end - timedelta(hours=1)
    params: list[dict[str, Any]] = []
    for name in sorted(names):
        if name in _TSTART_ALIASES:
            params.append({name: start.isoformat().replace("+00:00", "Z")})
        elif name in _TEND_ALIASES:
            params.append({name: end.isoformat().replace("+00:00", "Z")})
        else:
            params.append({name: ""})
    return params


def annotate_record_with_live_response(
    record: PanelRecord,
    status: int,
    body: dict[str, Any] | str,
) -> None:
    """Populate the T5 fields on a :class:`PanelRecord` from a
    :func:`run_cluster_query` result."""
    record.t5_response_status = status
    if status >= 400 or isinstance(body, str):
        record.t5_response_error = body if isinstance(body, str) else json.dumps(body)
        return
    columns = body.get("columns") or []
    record.t5_response_columns = [c.get("name", "") for c in columns]
    record.t5_response_row_count = len(body.get("values") or [])


__all__ = [
    "annotate_record_with_live_response",
    "cluster_dashboard_panels",
    "fetch_cluster_dashboard",
    "load_ir_panels",
    "load_ir_panels_by_dashboard",
    "load_migration_report",
    "load_ndjson_panels",
    "load_ndjson_panels_by_dashboard",
    "panels_from_migration_report",
    "run_cluster_query",
]
