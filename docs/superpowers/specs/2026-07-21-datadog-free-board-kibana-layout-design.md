# Datadog free-board → Kibana layout (column-band)

**Date:** 2026-07-21  
**Status:** Implemented (hybrid C) — iterate on packing/band heuristics as needed  
**Scope:** Wide Datadog `layout_type: free` dashboards (canvas extent ≫ 12), e.g. HAProxy, Apache, nginx-ingress

## Problem

Datadog free boards use a fine canvas (often 100–200 units wide). Mapping that onto Kibana’s 48-column grid with a single linear scale produces unreadable tiles (metrics/charts ~4 cols). Inflating type min-widths without a column model then overlaps neighbors; horizontal “fixes” that move `x` scramble column alignment (HAProxy looked broken).

## Goal

Optimize for **Kibana readability** while preserving the source board’s **left→right column story**:

- Do **not** merge distinct source columns.
- Do **not** reorder bands L→R.
- Do give each band usable Kibana widths (chart ≥ 8, metric ≥ 6, markdown ≥ 4 when space allows).
- No overlapping panels after layout.

Ordered / ~12-column Datadog boards keep the existing row/heuristic path unchanged.

## Approach (chosen): column-band layout

1. **Detect bands** — Cluster leaf panels by Datadog `_dd_x` starts. Gap threshold ≈ half the median widget width (with a small absolute floor). Sort bands left→right.
2. **Assign band widths** — Weight each band by the max `_dd_w` of panels whose *primary* band is that band (or by band span for multi-band widgets). Normalize weights to 48 columns. Apply readable floors per band based on the heaviest panel family in that band. If floors exceed 48, scale all band widths down proportionally but keep every band ≥ 1 and prefer cutting markdown/note slack before chart/metric floors.
3. **Place panels**
   - Primary band = band containing `_dd_x` (or leftmost overlapping band).
   - Width = sum of widths of bands the panel’s `[x, x+w)` covers (span). Single-band panels fill their band width (or a sub-slot when multiple non-overlapping siblings share a horizontal slice — see below).
   - `x` = start of the leftmost covered band.
   - Height = `max(type min_h, round(_dd_h * scale_y))` with `scale_y` derived from a stable vertical scale (same global scale factor as today, or band-local only for packing anchors). Placeholder markdown may grow from content height.
4. **Sub-slots in a band** — When two+ panels share the same band and overlapping y-range but different `_dd_x` within that band (e.g. HAProxy KPIs at x=25 and x=43 inside the overview band), split the band width proportionally by their `_dd_w` without creating new top-level bands. This densifies KPIs *inside* a column without merging columns.
5. **Vertical pack** — Existing column-aware packer: place by `(anchor_y, x)`; drop only when x-ranges overlap. Do **not** run the pairwise overlap pusher on free boards.
6. **Normalize** — Height mins/max only at the free-board path (no type min-width expansion that escapes the band model). Strip private `_free_*` / `_dd_*` keys as today.

## Non-goals

- Pixel-perfect Datadog fidelity.
- Reflowing KPIs into new visual columns or changing L→R order.
- YAML-only schema work (native IR / existing generate path is enough).
- Fixing Datadog image URL placeholders or `check_status` semantics (separate).

## Success criteria

| Check | Pass rule |
|---|---|
| Column stability | Panels that share a source `_dd_x` keep the same Kibana `x` after layout |
| Readable charts | Every `line`/`area`/`bar`/table free-board panel `w ≥ 8` when board has ≤ 6 bands; if more bands, best-effort with proportional shrink |
| Readable metrics | Every metric/gauge panel `w ≥ 6` under the same band-count caveat |
| No overlaps | Zero AABB overlaps after layout + normalize + pack |
| Regression | Existing ordered-board / interleaved-note tests still pass |
| Live | Remigrate HAProxy + Apache (+ nginx-ingress if available); upload to local Kibana; screenshot top of board shows distinct readable columns |

## Test plan

**Unit (generate.py / TestYAMLGeneration):**

1. Keep `test_wide_free_board_keeps_global_column_scale` (aligned x across rows).
2. Keep/extend `test_haproxy_style_free_board_keeps_dense_columns` — assert metric `w ≥ 6`, chart `w ≥ 8`, log column on the right, KPIs near top.
3. Add Apache-style fixture: multiple mid-board columns; assert no overlaps and chart mins.
4. Add band-detection unit tests: known `_dd_x` set → expected band count/order.
5. Ordered Redis-style interleaved note test unchanged.

**Integration:**

- Migrate `haproxy.json`, `apache.json`, `nginx-ingress-controller.json` with upload to local stack.
- Assert layout validation pass; spot-check YAML positions; full-page screenshots under `popular_ui_controls_20260721/local_ui/`.

## Files

- `observability_migration/adapters/source/datadog/generate.py` — replace `_apply_free_board_layout` (+ helpers for band cluster/assign/sub-slot).
- `tests/test_datadog_migrate.py` — layout assertions above.

## Risks

- Over-clustering (too many bands) → still cramped; mitigate with gap threshold tuning and sub-slots.
- Under-clustering (merge overview+frontend) → wrong story; mitigate by using x-start clusters, not centers, and not merging across large gaps.
- Spanning notes (`width` covering many columns) must sum band widths correctly or they look short.
