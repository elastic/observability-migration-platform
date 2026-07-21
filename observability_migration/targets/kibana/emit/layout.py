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

# Per-type (min_w, min_h, max_h) constraints.
# Shared by the Grafana and Datadog layout paths so both sources produce
# panels that are readable in Kibana.  None means "no cap".
PANEL_SIZE_CONSTRAINTS: dict[str, tuple[int, int, int | None]] = {
    "metric":    (4,  6,  12),
    "gauge":     (6,  8,  16),
    "bargauge":  (6,  6,  16),
    "bar":       (8,  6,  24),
    "line":      (8,  6,  24),
    "area":      (8,  6,  24),
    "xy":        (8,  6,  24),
    "datatable": (12, 8,  24),
    "pie":       (8,  8,  24),
    "treemap":   (8,  8,  24),
    "heatmap":   (8,  8,  24),
    "markdown":  (4,  2,  None),
}


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
    """Detect when a row is part of a 2D grid (do not stretch-fill).

    ``_fill_simple_row`` stretches a row's panels to span the full 48
    columns. That's correct for true 1D rows but **wrong** when:

    - Panels **below** share x-ranges (node-exporter Quick CPU): stretching
      pushes the row's right-edge panels further right and breaks column
      alignment with the tiles underneath.
    - Taller panels **above** still occupy this row's y-band (Istio GC
      toplist at y=26 h=8 over a y=28 metric stripe): stretching slides
      lower-stripe tiles sideways into that span and creates overlaps.

    Full-width headers that end exactly at ``row_y`` do **not** suppress
    fill — they share no vertical band with the row.
    """
    if not row:
        return False
    row_ids = {id(p) for p in row}
    row_y = int((row[0].get("position") or {}).get("y", 0) or 0)
    row_bottom = max(
        row_y + int((p.get("size") or {}).get("h", 0) or 0) for p in row
    )
    row_min_x = min(int((p.get("position") or {}).get("x", 0) or 0) for p in row)
    row_max_x = max(
        int((p.get("position") or {}).get("x", 0) or 0)
        + int((p.get("size") or {}).get("w", 0) or 0)
        for p in row
    )
    for other in all_panels:
        if id(other) in row_ids:
            continue
        oy = int((other.get("position") or {}).get("y", 0) or 0)
        oh = int((other.get("size") or {}).get("h", 0) or 0)
        ox = int((other.get("position") or {}).get("x", 0) or 0)
        ow = int((other.get("size") or {}).get("w", 0) or 0)
        shares_x = ox < row_max_x and ox + ow > row_min_x
        if not shares_x:
            continue
        if oy > row_y:
            # Any tile below (even edge-adjacent) signals a 2D column stack.
            return True
        # Above: only when the taller tile still covers this row's y-band.
        if oy + oh > row_y and oy < row_bottom:
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
    sorted_row = sorted(row, key=lambda p: int((p.get("position") or {}).get("x", 0) or 0))
    if int((sorted_row[0].get("position") or {}).get("x", 0) or 0) > 2:
        return False
    for i in range(1, len(sorted_row)):
        prev_x = int((sorted_row[i - 1].get("position") or {}).get("x", 0) or 0)
        prev_w = int((sorted_row[i - 1].get("size") or {}).get("w", 0) or 0)
        curr_x = int((sorted_row[i].get("position") or {}).get("x", 0) or 0)
        if curr_x - (prev_x + prev_w) > 2:
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

    # If all panels are pinned at HARD_MIN_W and the sum still exceeds
    # GRID_COLUMNS, the loop exits early without converging.  In that case
    # bail out and leave the panels unchanged rather than writing
    # coordinates that overflow the grid.
    if sum(new_widths) != GRID_COLUMNS:
        return

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
