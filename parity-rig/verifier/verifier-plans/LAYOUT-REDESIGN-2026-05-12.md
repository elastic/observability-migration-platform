# Layout redesign — measurement-driven plan

Status: design accepted, harness-first parallel execution
Owner: this chat
Last updated: 2026-05-12

## Why now

The current Grafana → Kibana layout step is a single coordinate transform plus
a clamp pass; there is no real layout algorithm. Audit of every migrated
parity dashboard (see `parity-rig/verifier/verifier-reports/ALL-DASHBOARDS-2026-05-12.md`
and the layout audit run on `/tmp/mig-to-kbn-e2e/parity-out-*/`) shows six
distinct visual-fidelity defects we can fix incrementally.

We now have `agent-browser` wired in, which means **layout decisions can be
measured pixel-for-pixel against Grafana** instead of debated subjectively.
That is the prerequisite for any meaningful redesign.

## Defects observed in compiled NDJSONs

| Defect | Most extreme case | Root cause in `panels.py` |
|---|---|---|
| 38 panels stacked in a single `y=0` band | `node-exporter-full` | `_apply_kibana_native_layout` keys by Grafana `gridPos.y`; collapsed-row children with shared `y` all land on one Kibana band (`panels.py` ~2719–2760) |
| Jagged heights inside a band (heights {3,6,11,15,18} on band 0) | `node-exporter-full` | No per-type height normalization beyond datatable/metric (`panels.py` ~2282–2299) |
| Right-side gap (band ends at col 16/48) | `home` | `_fill_simple_row` only kicks in for 50–150% rows (`emit/layout.py` ~119–140) |
| Stat tiles too short (h=3 → 60px) | `node-exporter-full` | Generic `30/20` row scale; no per-type minimum (`panels.py` 94–97) |
| Heatmap → bar approximation stretches vertically | translator warning + raw Grafana h | Bar chart inherits heatmap h; no clamp |
| Row containers sometimes flatten, sometimes section | mixed | `_build_section_groups` heuristic (`panels.py` 2446–2497) |
| Repeat panels silently collapsed | k8s-views-global, prometheus-all | Var collected but no fan-out (`panels.py` 2506–2522, 2359–2363) |

## Six layers (each shippable independently)

### L1 — Faithful coordinate transform

**Goal:** stop banding by `gridPos.y`. Transform every Grafana panel to its
exact Kibana coordinates and trust Kibana's own layout to render them.

**Change site:** `_apply_kibana_native_layout` in `panels.py` (~2719–2785).

**Algorithm:**

```
for each panel p:
    p.kibana_x = round(p.grafana_x * 48 / 24)
    p.kibana_y = round(p.grafana_y * 30 / 20)   # absolute, not cumulative
    p.kibana_w = max(1, round(p.grafana_w * 48 / 24))
    p.kibana_h = max(1, round(p.grafana_h * 30 / 20))
```

**Validation:** new test in `tests/test_migrate.py` that asserts
`node-exporter-full`'s 116 panels span at least 30 distinct y-bands (not 14).

### L2 — Per-panel-type minimums

**Goal:** stop emitting visually unusable tiles (h=3 stats, tiny w datatables).

**Change site:** `_normalize_tile_size` in `panels.py` (~2282–2299).

**New table:**

| Panel type | min w | min h | max h | Note |
|---|---:|---:|---:|---|
| `metric` (stat / single-value) | 4 | 6 | 12 | value + title must fit |
| `bar` / `xy` (with composite legend) | 8 | 8 | 24 | legend ≥ 2 rows |
| `xy` / `area` (no legend) | 8 | 6 | 24 | |
| `gauge` | 6 | 8 | 16 | |
| `datatable` | 12 | 8 | 24 | |
| `markdown` / `text` | 4 | 2 | — | |
| `bar` (from degraded heatmap) | original | 6 | 12 | clamp anemic stretches |

**Validation:** unit tests per panel type + harness diff score before/after.

### L3 — Row-aware sectioning

**Goal:** every Grafana `type: row` panel becomes a Kibana `section` with its
title preserved and its own coordinate origin.

**Change site:** `_build_section_groups` in `panels.py` (~2446–2497) — promote
the "sometimes section" path to always-section when an explicit row exists.

**Knock-on benefits:** y-values inside a section reset to 0, which makes the
NDJSON readable and side-steps the y-cursor accumulation in
`translate_dashboard` (~2657–2664).

### L4 — Repeat panel expansion

**Goal:** when Grafana has `repeat: "$var"` and `$var` has N values, emit N
Kibana panels instead of collapsing to a single-select.

**Change site:** new pass in `panels.py` between `_build_section_groups` and
`_translate_panel_group`. Uses `SchemaResolver` to query the var's values
(already in field cache for label vars; `_field_caps` lookup for metric vars).

**Layout rule:** if `repeatDirection == "h"` → fan out left-to-right wrapping
at col 48; else fan out top-to-bottom. Each fan-out panel inherits the
template's w and h.

**Risk:** very high-cardinality vars (e.g. `instance` with 50 nodes) would
balloon the dashboard. Cap at 8 fan-out panels and append a single-select
control for the rest. Emit a warning in `translator.warnings`.

### L5 — Browser-driven visual regression harness (BUILD FIRST)

**Goal:** every layout change is gated by a numeric diff score across all
parity dashboards.

**New module:** `parity-rig/verifier/visual_regression.py`.

**Per-panel flow:**

1. Resolve `panel_id` → Grafana URL (`grafana/d-solo/<uid>/<slug>?panelId=N`)
   and Kibana URL (`/app/dashboards#/view/<uid>?_a=(panels:!((panelId:N,...)))`).
   Both panel-solo views render a single panel at viewport size.
2. `agent-browser screenshot --selector "<panel-content-selector>"` on each.
3. `agent-browser diff screenshot --annotate` between the two PNGs.
4. Parse diff score (0.0 = identical, 1.0 = totally different).
5. Stamp `record.visual.diff_score` on the `PanelRecord`.

**Aggregation:** new CLI `obs-migrate verify-panels --visual` runs the harness
across every panel in a dashboard. Median + p95 + per-panel scores are written
to a JSON report alongside the existing tier-comparison JSON.

**Acceptance contract for L1–L4:** for any layout change, the median diff
score across all 6 parity dashboards must **not regress** by more than 5%,
and the p95 must improve on at least one dashboard.

### L6 — Lens-aware aspect-ratio tuning (data-driven defaults)

**Goal:** the L2 minimums table should not be hand-picked. Use the harness to
sweep `(w, h)` ranges per panel type and pick the bucket with the lowest
median diff score.

**Approach:**

1. Build a small parameter sweeper that re-translates the same source PromQL
   with overridden w/h.
2. Upload each variant to a throwaway Kibana space.
3. Run the visual harness.
4. Pick the optimum per panel type.

This is optional — only worthwhile if L1–L4 + harness reveal that the
hand-picked L2 minimums are leaving fidelity on the table.

## Execution order (per accepted plan)

1. Commit current state (rig fixes, translator fixes, verifier, skills) under
   a chore commit so we have a clean baseline.
2. **L5 harness** + tests + baseline run committed first.
3. **L1 + L2 in parallel.** Run harness against both; commit each only after
   it proves a median-score improvement.
4. **L3.** Same gate.
5. **L4.** Same gate; emits cardinality-capped fan-out with a warning.
6. **L6** only if the harness shows L2 leaving > 5% diff on any panel type.

## Out of scope (deliberate)

- Re-implementing Kibana's own auto-layout in our compiler. We respect what
  Kibana renders.
- Cross-section overlap repair. Today's per-section overlap validator is
  enough.
- 100% pixel parity with Grafana. Lens panel chrome (legend position,
  axis density) is intentionally different.

## Sign-off

User accepted **all six layers**, **parallel** execution (harness + L1+L2
first), on 2026-05-12. This document is the source of truth for the redesign;
deviations get appended as ADR-style notes below.
