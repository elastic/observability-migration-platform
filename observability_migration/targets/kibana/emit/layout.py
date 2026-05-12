# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Conservative layout post-processor for generated dashboard YAML.

Preserves the source dashboard's spatial relationships (2D grid positions,
proportional widths, visual groupings).  Only fixes genuinely broken panels:

  - Panels narrower than HARD_MIN_W (4 columns) — unreadable
  - Panels overflowing past the 48-column grid boundary
  - Simple contiguous rows that don't fill exactly 48 columns (rounding gaps)

Heights and y-positions are NEVER changed — source adapters (Grafana 2x scaler,
Datadog proportional layout) are responsible for correct vertical layout.
Changing heights here would require cascading y-position updates that break
2D grid arrangements (e.g. Node Exporter's scoreboard with stacked stats).

Reference: https://strawgate.com/kb-yaml-to-lens/guides/dashboard-style-guide/
"""

from __future__ import annotations

from typing import Any

GRID_COLUMNS = 48
HARD_MIN_W = 4


def apply_style_guide_layout(yaml_doc: dict[str, Any]) -> dict[str, Any]:
    """Post-process dashboard YAML: fix overflow, fill simple rows."""
    for dashboard in yaml_doc.get("dashboards", []):
        _fix_dashboard(dashboard)
    return yaml_doc


def _fix_dashboard(dashboard: dict[str, Any]) -> None:
    panels = dashboard.get("panels", [])
    if not panels:
        return

    for panel in panels:
        section = panel.get("section")
        if isinstance(section, dict):
            inner = section.get("panels")
            if isinstance(inner, list) and inner:
                _fix_panel_group(inner)

    non_section = [p for p in panels if "section" not in p]
    if non_section:
        _fix_panel_group(non_section)


def _fix_panel_group(panels: list[dict[str, Any]]) -> None:
    if not panels:
        return

    for p in panels:
        _clamp_single_panel(p)

    rows = _collect_rows(panels)
    for row in rows:
        if (
            len(row) > 1
            and _is_simple_contiguous_row(row)
            and not _row_has_overlapping_x_neighbours(row, panels)
        ):
            _fill_simple_row(row)


def _row_has_overlapping_x_neighbours(
    row: list[dict[str, Any]],
    all_panels: list[dict[str, Any]],
) -> bool:
    """Detect when a row is the top stripe of a 2D grid.

    ``_fill_simple_row`` stretches a row's panels to span the full 48
    columns. That's correct for true 1D rows (one strip of panels
    above and below empty space) but **wrong** when the panels in
    the row sit beside taller neighbours, or when other panels
    below share x-ranges with the row -- stretching the row pushes
    those neighbours to the wrong x positions, often introducing
    overlaps that ``_resolve_panel_overlaps`` then has to fix by
    cascading panels down.

    A row counts as having overlapping x-neighbours when any panel
    OUTSIDE the row (above OR below) has an x-range that overlaps
    the rightmost panel in the row. Top-stripes of 2D grids are
    exactly that pattern: the right-edge stat tiles in
    ``node-exporter-full``'s "Quick CPU" section sit at x=36..48 at
    both y=0 and y=3, so the y=0 row has below-neighbours at
    overlapping x and should NOT be stretched.
    """
    if not row:
        return False
    row_ids = {id(p) for p in row}
    row_min_x = min(int(p["position"].get("x", 0) or 0) for p in row)
    row_max_x = max(
        int(p["position"].get("x", 0) or 0)
        + int(p["size"].get("w", 0) or 0)
        for p in row
    )
    for other in all_panels:
        if id(other) in row_ids:
            continue
        ox = int(other.get("position", {}).get("x", 0) or 0)
        ow = int(other.get("size", {}).get("w", 0) or 0)
        # Overlap in x-range (touching is fine; strict overlap is not)
        if ox < row_max_x and ox + ow > row_min_x:
            return True
    return False


def _clamp_single_panel(panel: dict[str, Any]) -> None:
    """Enforce hard minimum width and clamp grid overflow."""
    size = panel.setdefault("size", {})
    pos = panel.setdefault("position", {})

    w = int(size.get("w", 12) or 12)
    x = int(pos.get("x", 0) or 0)

    w = max(w, HARD_MIN_W)

    if x + w > GRID_COLUMNS:
        w = max(HARD_MIN_W, GRID_COLUMNS - x)
        if x + w > GRID_COLUMNS:
            x = max(0, GRID_COLUMNS - w)

    size["w"] = w
    pos["x"] = x


def _collect_rows(
    panels: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    by_y: dict[int, list[dict[str, Any]]] = {}
    for panel in panels:
        y = int(panel.get("position", {}).get("y", 0) or 0)
        by_y.setdefault(y, []).append(panel)
    return [
        sorted(
            by_y[y],
            key=lambda p: int(p.get("position", {}).get("x", 0) or 0),
        )
        for y in sorted(by_y)
    ]


def _is_simple_contiguous_row(row: list[dict[str, Any]]) -> bool:
    """True if the row forms a contiguous strip starting near x=0.

    A "simple row" is a 1D horizontal arrangement (not part of a 2D grid).
    Rows that start far from x=0 are likely the right-side portion of a 2D
    grid and should NOT be rearranged.
    """
    sorted_row = sorted(row, key=lambda p: p["position"]["x"])
    if sorted_row[0]["position"]["x"] > 2:
        return False
    for i in range(1, len(sorted_row)):
        prev_end = sorted_row[i - 1]["position"]["x"] + sorted_row[i - 1]["size"]["w"]
        curr_start = sorted_row[i]["position"]["x"]
        if curr_start - prev_end > 2:
            return False
    return True


def _fill_simple_row(row: list[dict[str, Any]]) -> None:
    """Scale a simple contiguous row to fill exactly 48 columns.

    Proportionally adjusts widths (floor at HARD_MIN_W) and reassigns
    contiguous x positions.  Only acts when the row totals between 50% and
    150% of GRID_COLUMNS — outside that range the row is likely part of a
    2D grid or genuinely broken in a way this function cannot fix.
    """
    sorted_row = sorted(row, key=lambda p: p["position"]["x"])
    widths = [p["size"]["w"] for p in sorted_row]
    total = sum(widths)
    n = len(widths)

    if total == GRID_COLUMNS:
        x = 0
        for p, w in zip(sorted_row, widths):
            p["position"]["x"] = x
            x += w
        return

    if total < GRID_COLUMNS * 0.5 or total > GRID_COLUMNS * 1.5:
        return

    scale = GRID_COLUMNS / total
    new_widths = [max(HARD_MIN_W, round(w * scale)) for w in widths]

    indices = sorted(range(n), key=lambda i: -new_widths[i])
    for _pass in range(GRID_COLUMNS):
        diff = GRID_COLUMNS - sum(new_widths)
        if diff == 0:
            break
        changed = False
        for i in indices:
            if diff == 0:
                break
            if diff > 0:
                new_widths[i] += 1
                diff -= 1
                changed = True
            elif new_widths[i] > HARD_MIN_W:
                new_widths[i] -= 1
                diff += 1
                changed = True
        if not changed:
            break

    x = 0
    for p, w in zip(sorted_row, new_widths):
        p["size"]["w"] = w
        p["position"]["x"] = x
        x += w


def _panel_type(panel: dict[str, Any]) -> str:
    """Detect panel visualization type."""
    esql = panel.get("esql")
    if isinstance(esql, dict):
        return esql.get("type", "line")
    lens = panel.get("lens")
    if isinstance(lens, dict):
        return lens.get("type", "line")
    if "markdown" in panel:
        return "markdown"
    if "vega" in panel:
        return "line"
    if "search" in panel:
        return "datatable"
    return "metric"
