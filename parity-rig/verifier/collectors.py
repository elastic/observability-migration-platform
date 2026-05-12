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
import yaml

from .records import PanelRecord

LOG = logging.getLogger(__name__)


_REQUEST_TIMEOUT = 30


# --------------------------------------------------------------------- #
# T0 + T1  — migration_report.json (source PromQL + translator output)
# --------------------------------------------------------------------- #


def load_migration_report(report_path: Path) -> dict[str, Any]:
    return json.loads(report_path.read_text())


def panels_from_migration_report(report: dict[str, Any]) -> Iterable[PanelRecord]:
    """Yield one :class:`PanelRecord` per panel in the report.

    ``migration_report.json`` is laid out as::

        { "dashboards": [ { "uid": ..., "title": ..., "panels": [...] }, ... ] }

    Panels live at ``dashboards[*].panels[*]``; both the source PromQL
    (``query_ir.source_expression``) and the translator output
    (``esql``) are direct fields on each panel object.
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
    esql = (panel.get("esql") or "").strip()
    is_native = esql.lstrip().upper().startswith("PROMQL")
    return PanelRecord(
        panel_id=str(panel.get("source_panel_id") or f"panel-{idx}"),
        title=panel.get("title", "") or f"(untitled-{idx})",
        dashboard_uid=dash_uid,
        dashboard_title=dash_title,
        grafana_type=panel.get("grafana_type", ""),
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
# T2  — yaml on disk (kb-dashboard-cli input)
# --------------------------------------------------------------------- #


def load_yaml_panels(yaml_dir: Path) -> dict[str, str]:
    """Return a ``{panel_title: esql_query}`` mapping for every YAML
    dashboard in ``yaml_dir``.

    YAML schema (the kb-dashboard-cli contract)::

        dashboards:
        - panels:
          - title: <section title>
            section:
              panels:
              - title: <panel title>
                esql: { query: <ES|QL> }
                # or
                markdown: { content: <markdown> }
    """
    out: dict[str, str] = {}
    for yaml_path in sorted(yaml_dir.glob("*.yaml")):
        try:
            doc = yaml.safe_load(yaml_path.read_text())
        except Exception as exc:  # pragma: no cover - defensive
            LOG.warning("failed to parse %s: %s", yaml_path, exc)
            continue
        for dash in (doc or {}).get("dashboards", []):
            for panel in _iter_yaml_panels(dash.get("panels", [])):
                out[panel["title"]] = panel.get("esql_query", "")
    return out


def _iter_yaml_panels(panels: list[dict[str, Any]]) -> Iterable[dict[str, str]]:
    for panel in panels or []:
        section = panel.get("section")
        if isinstance(section, dict):
            yield from _iter_yaml_panels(section.get("panels", []))
            continue
        title = panel.get("title") or "(untitled)"
        esql_block = panel.get("esql") or {}
        query = ""
        if isinstance(esql_block, dict):
            query = (esql_block.get("query") or "").strip()
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
    """
    if not ndjson_path.exists():
        return {}
    out: dict[str, str] = {}
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
        panels_blob = (obj.get("attributes") or {}).get("panelsJSON")
        if not panels_blob:
            continue
        try:
            panels = json.loads(panels_blob)
        except json.JSONDecodeError:
            continue
        for panel in panels:
            title, esql = _extract_panel_title_and_esql(panel)
            if title:
                out[title] = esql
    return out


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
    headers = {"Authorization": f"ApiKey {api_key}", "kbn-xsrf": "verifier"}
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
    headers = {
        "Authorization": f"ApiKey {api_key}",
        "Content-Type": "application/json",
    }
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


_NAMED_PARAM_PATTERN = re.compile(r"\?([a-zA-Z_][a-zA-Z0-9_]*)")


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
        if name in ("_tstart", "_t_start", "tstart"):
            params.append({name: start.isoformat().replace("+00:00", "Z")})
        elif name in ("_tend", "_t_end", "tend"):
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
    "load_migration_report",
    "load_ndjson_panels",
    "load_yaml_panels",
    "panels_from_migration_report",
    "run_cluster_query",
]
