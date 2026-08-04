# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Structural cross-checks on the native payload a migration actually ships.

``native/*.native.json`` is the artifact ``obs-migrate upload`` deploys
byte-for-byte, so something has to prove it still describes the dashboard the
translator produced. Three independent checks live here, and the first two are
used by the Grafana and Datadog CLI artifact tests:

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

3. :func:`assert_payload_has_no_kibana_rejections` — the payload versus the
   shapes a live Kibana is *known* to refuse. Kibana can accept a dashboard
   with HTTP 200 and silently drop the panels it cannot transform, so a green
   upload proves nothing about them; this is the offline stand-in for that
   authority. Every rule cites the live error message that motivated it.

Check 1 is the load-bearing one: it can catch a lost or rewritten query. Check 2
is cheap and pins the dashboard-level fields. Check 3 is the only one that can
catch a payload Kibana will drop panels over. Neither reads or writes YAML.

All three are registered in ``tests/vacuity/registry.py`` with mutations that
must make them fail, so a future edit cannot quietly turn one of them into a
guard that can no longer go red.
"""

from __future__ import annotations

from typing import Any

from observability_migration.core.assets.dashboard import DashboardIR
from observability_migration.core.assets.native_dashboard import (
    MAX_DASHBOARD_ITEMS,
    MAX_SECTION_PANELS,
)
from observability_migration.targets.kibana import dashboards_api


def ir_leaf_entries(dashboard_ir: DashboardIR) -> list[dict[str, Any]]:
    """Every leaf panel of the IR, as its internal dict-shape entry.

    Read through ``PanelIR.to_yaml_panel_entry()`` -- the internal dict shape
    the panel layer already speaks -- rather than by re-deriving presentation,
    so this stays a *description* of the IR and not a second mapper. Section
    containers are walked through, never counted: they carry no query and no
    payload leaf of their own.
    """
    out: list[dict[str, Any]] = []
    for panel in dashboard_ir.panels:
        _collect_leaf_entries(panel.to_yaml_panel_entry(), out)
    return out


def _collect_leaf_entries(entry: dict[str, Any], out: list[dict[str, Any]]) -> None:
    section = entry.get("section")
    if isinstance(section, dict):
        for sub in section.get("panels") or []:
            if isinstance(sub, dict):
                _collect_leaf_entries(sub, out)
        return
    out.append(entry)


def entry_queries(entry: dict[str, Any]) -> list[str]:
    """Every ES|QL query one dict-shape leaf entry declares, in document order."""
    esql = entry.get("esql")
    if not isinstance(esql, dict):
        return []
    queries: list[str] = []
    query = str(esql.get("query") or "")
    if query:
        queries.append(query)
    for layer in esql.get("layers") or []:
        if isinstance(layer, dict):
            layer_query = str(layer.get("query") or "")
            if layer_query:
                queries.append(layer_query)
    return queries


def _ir_panel_queries(dashboard_ir: DashboardIR) -> dict[str, list[str]]:
    """``{panel title: [esql query, ...]}`` straight off the IR."""
    out: dict[str, list[str]] = {}
    for entry in ir_leaf_entries(dashboard_ir):
        out.setdefault(str(entry.get("title") or ""), []).extend(entry_queries(entry))
    return out


def assert_payload_matches_ir(
    payload: dict[str, Any],
    dashboard_ir: DashboardIR,
    *,
    label: str = "",
    allowed_unmapped: int = 0,
) -> None:
    """The shipped payload must still describe the IR it was built from.

    Asserts, per leaf panel the payload carries:

    * its title belongs to a panel the IR actually has (nothing invented, and
      no title rewritten on the way out);
    * every ES|QL query on it is one the IR panel of that title declares. An
      ``xy`` panel carries one query per layer under ``config.layers[*]``, which
      :func:`dashboards_api.payload_panel_queries` collects, so a multi-layer
      panel is checked layer by layer rather than only on its first query.

    And across the dashboard:

    * no IR panel that declares an ES|QL query is missing from the payload. That
      is the check the payload-vs-mapper comparison cannot make, because a query
      dropped by the mapper is dropped identically no matter which entry point
      drove it;
    * the payload ships one leaf panel per IR leaf panel. Query-based checks are
      blind to a dropped panel that has no query -- markdown, links, images, a
      section's notes -- and blind to one whose title and query both duplicate a
      surviving panel's. The count sees both. ``allowed_unmapped`` is the escape
      hatch for a panel the mapper deliberately cannot map: it must be *declared*
      per call site, so a translation gap is stated rather than absorbed.
    """
    where = f" [{label}]" if label else ""
    payload_queries = dashboards_api.payload_panel_queries(payload)
    ir_queries = _ir_panel_queries(dashboard_ir)
    ir_leaves = ir_leaf_entries(dashboard_ir)
    payload_leaves = dashboards_api.iter_payload_leaf_panels(payload)
    assert len(payload_leaves) == len(ir_leaves) - allowed_unmapped, (
        f"the payload ships {len(payload_leaves)} leaf panel(s) for an IR that "
        f"declares {len(ir_leaves)}"
        + (f" with {allowed_unmapped} declared unmappable" if allowed_unmapped else "")
        + f"{where}. A panel that vanishes on the way to the payload is silent data "
        f"loss; if the mapper cannot map it, say so with allowed_unmapped."
    )

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


# --------------------------------------------------------------------------- #
# Check 3 — shapes a live Kibana refuses
# --------------------------------------------------------------------------- #

# Every rule below was witnessed against a live cluster; the docstring of each
# helper carries the error Kibana returned. This is deliberately a *short* list
# of empirically-confirmed refusals rather than a re-implementation of the
# Dashboards API schema: the full OpenAPI bundle is externally hosted (see
# ``scripts/fetch_dashboards_api_schema.py`` and ``make check-native-schema``),
# so offline we can only encode what a real upload has already taught us.


def _iter_color_objects(node: Any) -> list[dict[str, Any]]:
    """Every ``color`` object anywhere under *node*, at any depth.

    Walks rather than reading fixed paths. A fixed-path reader is how a payload
    check goes vacuous: ``xy`` panels keep their colours per layer under
    ``config.layers[*]`` while ``metric`` panels keep them on the metric, so a
    reader that knows one shape silently examines nothing on the other and
    passes.
    """
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        color = node.get("color")
        if isinstance(color, dict):
            found.append(color)
        for key, child in node.items():
            if key == "color":
                continue
            found.extend(_iter_color_objects(child))
    elif isinstance(node, list):
        for child in node:
            found.extend(_iter_color_objects(child))
    return found


def payload_kibana_rejections(payload: dict[str, Any]) -> tuple[list[str], dict[str, int]]:
    """Return ``(findings, examined)`` for one native API payload.

    ``findings`` names every shape a live Kibana is known to refuse.
    ``examined`` counts what was actually inspected per rule family, so a caller
    can tell "nothing wrong" apart from "nothing looked at" — the difference
    between a guard and a vacuous one.

    Rules:

    * **single-step dynamic palette.** Kibana validates a step list pairwise and
      answers ``[metrics.0.color.0.steps.1]: At least one of "gte", "lt", or
      "lte" must be provided``, then **drops the whole panel** from the saved
      dashboard. Measured cost before ``_dynamic_palette`` collapsed the case to
      a static colour: 6 panels across 4 of 13 Datadog dashboards, with a 2xx
      response and no warning on the upload path.
    * **step with no boundary.** The same refusal, stated at its root: a step
      that carries none of ``gte``/``lt``/``lte`` has no threshold to switch at.
    * **item caps.** ``NativeDashboard.enforce_item_cap`` exists because the API
      refuses more than :data:`MAX_DASHBOARD_ITEMS` top-level items or
      :data:`MAX_SECTION_PANELS` panels inside one section; a payload that
      exceeds them was built past its own cap.
    """
    findings: list[str] = []
    examined = {"panels": 0, "colors": 0, "step_lists": 0, "steps": 0, "containers": 0}

    top_level = payload.get("panels")
    top_level = top_level if isinstance(top_level, list) else []
    examined["containers"] += 1
    if len(top_level) > MAX_DASHBOARD_ITEMS:
        findings.append(
            f"dashboard carries {len(top_level)} top-level items, above the "
            f"API cap of {MAX_DASHBOARD_ITEMS}"
        )

    for section, panel in dashboards_api.iter_payload_leaf_panels(payload):
        examined["panels"] += 1
        title = str((panel.get("config") or {}).get("title") or "")
        where = f"panel {title!r}" + (f" in section {section!r}" if section else "")
        for color in _iter_color_objects(panel):
            examined["colors"] += 1
            steps = color.get("steps")
            if not isinstance(steps, list):
                continue
            examined["step_lists"] += 1
            examined["steps"] += len(steps)
            if len(steps) == 1:
                findings.append(
                    f"{where} carries a single-step {color.get('type')!r} palette "
                    f"({color}); Kibana answers '[steps.1]: At least one of \"gte\", "
                    '"lt", or "lte" must be provided\' and drops the panel'
                )
                continue
            for index, step in enumerate(steps):
                if not isinstance(step, dict):
                    continue
                if not {"gte", "lt", "lte"} & set(step):
                    findings.append(
                        f"{where} palette step {index} has no gte/lt/lte boundary "
                        f"({step}); Kibana refuses the panel config"
                    )

    for index, item in enumerate(top_level):
        if not isinstance(item, dict) or "type" in item:
            continue
        nested = item.get("panels")
        if not isinstance(nested, list):
            continue
        examined["containers"] += 1
        if len(nested) > MAX_SECTION_PANELS:
            name = str(item.get("title") or f"#{index}")
            findings.append(
                f"section {name!r} carries {len(nested)} panels, above the API cap "
                f"of {MAX_SECTION_PANELS}"
            )

    return findings, examined


def assert_payload_has_no_kibana_rejections(
    payload: dict[str, Any],
    *,
    label: str = "",
    min_examined_panels: int = 1,
) -> dict[str, int]:
    """Assert *payload* carries no shape a live Kibana is known to refuse.

    ``min_examined_panels`` is the denominator assertion: a payload walk that
    inspected nothing must not read as clean. Pass ``0`` only for a payload that
    genuinely has no leaf panels. Returns the ``examined`` counters so a
    corpus-level caller can accumulate them and assert its own denominator.
    """
    where = f" [{label}]" if label else ""
    findings, examined = payload_kibana_rejections(payload)
    assert not findings, (
        f"payload carries {len(findings)} shape(s) Kibana is known to refuse, which "
        f"costs the panel silently on a 2xx upload{where}:\n  "
        + "\n  ".join(findings)
    )
    assert examined["panels"] >= min_examined_panels, (
        f"the Kibana-rejection oracle examined {examined['panels']} leaf panel(s), "
        f"below the required {min_examined_panels}{where}: it passed because it "
        f"looked at nothing, not because the payload is clean"
    )
    return examined
