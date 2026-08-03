# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Structural cross-checks on the native payload a migration actually ships.

``native/*.native.json`` is the artifact ``obs-migrate upload`` deploys
byte-for-byte, so something has to prove it still describes the dashboard the
translator produced. Two independent checks live here, and both are used by the
Grafana and Datadog CLI artifact tests:

1. :func:`assert_payload_matches_ir` — the payload versus the
   :class:`DashboardIR` it was built from. Every leaf panel in the payload is
   traceable to an IR panel, its ES|QL is the IR panel's ES|QL verbatim, and no
   IR panel carrying a query silently vanished. This does **not** re-run the
   mapping code, so a bug inside ``native_dashboard_from_ir`` shows up as a
   mismatch rather than being reproduced identically on both sides.

2. :func:`assert_payload_matches_dict_shape_bridge` — the payload versus a
   second construction of it through ``DashboardIR.to_yaml_dict()`` and
   ``dashboards_api.build_payload_from_yaml``. That is an in-memory dict shape
   (no file, no YAML text); it survives as the one place two different call
   paths through the mapper are required to agree, which pins the
   dashboard-level derivations (stable id, filters, title) the IR check cannot
   see.

Check 1 is the load-bearing one: it can catch a lost or rewritten query. Check 2
is cheap and pins the dashboard-level fields. Neither reads or writes YAML.
"""

from __future__ import annotations

from typing import Any

from observability_migration.core.assets.dashboard import DashboardIR
from observability_migration.targets.kibana import dashboards_api


def _ir_panel_queries(dashboard_ir: DashboardIR) -> dict[str, list[str]]:
    """``{panel title: [esql query, ...]}`` straight off the IR.

    Read through ``PanelIR.to_yaml_panel_entry()`` -- the internal dict shape
    the panel layer already speaks -- rather than by re-deriving presentation,
    so this stays a *description* of the IR and not a second mapper.
    """
    out: dict[str, list[str]] = {}
    for panel in dashboard_ir.panels:
        entry = panel.to_yaml_panel_entry()
        _index_dict_shape_entry(entry, out)
    return out


def _index_dict_shape_entry(entry: dict[str, Any], out: dict[str, list[str]]) -> None:
    section = entry.get("section")
    if isinstance(section, dict):
        for sub in section.get("panels") or []:
            if isinstance(sub, dict):
                _index_dict_shape_entry(sub, out)
        return
    title = str(entry.get("title") or "")
    esql = entry.get("esql")
    queries: list[str] = []
    if isinstance(esql, dict):
        query = str(esql.get("query") or "")
        if query:
            queries.append(query)
        for layer in esql.get("layers") or []:
            if isinstance(layer, dict):
                layer_query = str(layer.get("query") or "")
                if layer_query:
                    queries.append(layer_query)
    out.setdefault(title, []).extend(queries)


def assert_payload_matches_ir(payload: dict[str, Any], dashboard_ir: DashboardIR, *, label: str = "") -> None:
    """The shipped payload must still describe the IR it was built from.

    Asserts, per leaf panel the payload carries:

    * its title belongs to a panel the IR actually has (nothing invented, and
      no title rewritten on the way out);
    * every ES|QL query on it is one the IR panel of that title declares. An
      ``xy`` panel carries one query per layer under ``config.layers[*]``, which
      :func:`dashboards_api.payload_panel_queries` collects, so a multi-layer
      panel is checked layer by layer rather than only on its first query.

    And across the dashboard: no IR panel that declares an ES|QL query is
    missing from the payload. That is the check the payload-vs-mapper
    comparison cannot make, because a query dropped by the mapper is dropped
    identically no matter which entry point drove it.
    """
    where = f" [{label}]" if label else ""
    payload_queries = dashboards_api.payload_panel_queries(payload)
    ir_queries = _ir_panel_queries(dashboard_ir)

    for (section, title), queries in payload_queries.items():
        assert title in ir_queries, (
            f"payload panel {title!r} (section {section!r}) has no matching IR panel{where}"
        )
        for query in queries:
            assert query in ir_queries[title], (
                f"payload panel {title!r} ships an ES|QL query the IR does not "
                f"declare{where}:\n  payload: {query}\n  IR: {ir_queries[title]}"
            )

    shipped_titles = {title for _section, title in payload_queries}
    shipped_queries = {query for queries in payload_queries.values() for query in queries}
    for title, queries in ir_queries.items():
        if not queries:
            continue
        assert title in shipped_titles, (
            f"IR panel {title!r} declares ES|QL but no panel with that title "
            f"reached the payload{where}"
        )
        for query in queries:
            assert query in shipped_queries, (
                f"IR panel {title!r} declares an ES|QL query that is absent from "
                f"the payload{where}:\n  missing: {query}"
            )


def assert_payload_matches_dict_shape_bridge(
    payload: dict[str, Any],
    dashboard_ir: DashboardIR,
    *,
    allow_divergent_keys: frozenset[str] = frozenset(),
    label: str = "",
) -> dict[str, Any]:
    """Second construction of the payload, through the internal dict shape.

    ``native_dashboard_from_ir`` reads dashboard-level fields straight off the
    IR while ``native_dashboard_from_yaml`` reads them out of
    ``DashboardIR.to_yaml_dict()``. Requiring the two to agree pins the
    dashboard-level derivations -- stable dashboard id, filters, title,
    description -- which the per-panel IR check does not look at.

    ``allow_divergent_keys`` names payload keys the dict shape provably *cannot*
    carry (its schema is ``additionalProperties: false``), so a known gap is
    pinned rather than ignored. Returns the bridged payload for further
    assertions.
    """
    where = f" [{label}]" if label else ""
    bridged, _stats = dashboards_api.build_payload_from_yaml(
        {"dashboards": [dashboard_ir.to_yaml_dict()]}
    )
    divergent = {
        key
        for key in set(payload) | set(bridged)
        if payload.get(key) != bridged.get(key)
    }
    unexpected = divergent - allow_divergent_keys
    assert not unexpected, (
        f"native payload diverged from the dict-shape bridge on "
        f"{sorted(unexpected)}, which the dict shape can represent{where}"
    )
    assert {k: v for k, v in payload.items() if k not in allow_divergent_keys} == {
        k: v for k, v in bridged.items() if k not in allow_divergent_keys
    }, f"payload/bridge mismatch outside the allowed keys{where}"
    return bridged
