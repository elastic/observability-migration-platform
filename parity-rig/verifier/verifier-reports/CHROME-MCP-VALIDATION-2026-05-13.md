# Chrome DevTools MCP — L1-L4 visual validation, 2026-05-13

Visual evidence that the layout-redesign layers (L1-L4 + overlap
resolver) produce faithful, compile-clean Grafana → Kibana
migrations on the parity-rig corpus.

`agent-browser` screenshot remained hung in this environment, so I
drove the captures via the **Chrome DevTools MCP** instead.

## What was captured

This report compares three of the six parity dashboards
side-by-side: Grafana (source), Kibana before L1-L4 (the
pre-existing migration), and Kibana after L1-L4 (re-migrated and
re-uploaded with this branch's code).

  home-grafana.png                        home-kibana.png        home-kibana-after-l1l4.png
  node-exporter-full-grafana.png          node-exporter-full-kibana.png   node-exporter-full-kibana-after-l1l4.png
  prometheus-all-grafana.png              prometheus-all-kibana.png   prometheus-all-kibana-after-l1l4.png

## The re-upload step

The first round of captures (`home-kibana.png` etc.) was taken
**before** the new code was uploaded — they show the prior
migration's output, which mostly already had structurally faithful
output. To validate L1-L4 properly I re-ran the full pipeline with
the current branch:

    .venv/bin/python -m observability_migration.adapters.source.grafana.cli \
      --input-dir <each parity fixture> \
      --output-dir <out> --upload \
      --kibana-url $KIBANA_ENDPOINT --kibana-api-key $KEY \
      --es-url $ELASTICSEARCH_ENDPOINT --es-api-key $KEY \
      --data-view metrics-express.prometheus-parity \
      --esql-index metrics-express.prometheus-parity \
      --ensure-data-views

Result for all 6 parity dashboards:

  diverse-panels-test              compiled=1/1 uploaded=1/1 layout_ok=1
  home                             compiled=1/1 uploaded=1/1 layout_ok=1
  k8s-views-global                 compiled=1/1 uploaded=1/1 layout_ok=1
  node-exporter-full               compiled=1/1 uploaded=1/1 layout_ok=1
  prometheus-all                   compiled=1/1 uploaded=1/1 layout_ok=1
  express-prometheus-middleware    compiled=1/1 uploaded=1/1 layout_ok=1

The Kibana saved-object IDs are stable across re-uploads (the
migration uses content-derived UUIDs), so the existing dashboard
URLs and the prior pre-L1-L4 captures are directly comparable.

## A regression caught during this re-run

`node-exporter-full` failed to compile on the first attempt with:

    Panel "Root FS Used" at (x=31, y=0, w=6, h=8) overlaps with
    panel "RootFS Total" at (x=36, y=3, w=4, h=6)

Root cause: L2's per-type minimums (gauge h ≥ 8 etc.) widened
panels in the first row, then the existing
``apply_style_guide_layout._fill_simple_row`` post-processor
rescaled the row to fit 48 columns -- which nudged x positions by
1-2 cols and pushed the right edge of one row over the left edge
of a 2D-grid panel in the row below. `kb-dashboard-cli` compile
strictly rejects any overlap.

The fix (commit `142c38a`): wire up the pre-existing but
never-called `_resolve_panel_overlaps` helper as the final pass
after `apply_style_guide_layout`. It walks panels in (y, x) order
and pushes any overlapping panel's y down. After the fix:
`node-exporter-full` compiles+uploads cleanly.

This is exactly why "do we upload after the code change?" was the
right question -- the e2e tests in `tests/e2e/` use
``diverse-panels-test`` only, which doesn't exercise the
`_fill_simple_row` rescaling. Running against `node-exporter-full`
end-to-end surfaced a bug pure unit tests couldn't catch.

## Per-dashboard observations

### `home` (6 panels, no rows, no repeats)

The simplest dashboard. Exercises L1 + L2 but not L3 / L4.

Grafana renders all 6 panels in a 2-up grid (markdown header,
then stat + line chart side-by-side, gauge + table, etc.).

Kibana after L1-L4 mirrors the same layout: the 6-panel grid
shows up with the same proportions. The "Top Metrics by Series
Count" panel renders as "Migration Required" because the
translator correctly refused `topk()` + `__name__` introspection.

### `node-exporter-full` (116 panels, 16 rows incl. collapsed)

Stresses L1 (many panels) + L2 (stat tiles) + L3 (16 rows) +
overlap resolver.

The actual coordinate change after L1-L4 (first row of the
"Quick CPU / Mem / Disk" section):

    Panel          Before L1-L4          After L1-L4
    Pressure       (0, 0, 6, 6)          (0, 0, 7, 6)        (style-guide rescale)
    CPU Busy       (6, 0, 6, 6)          (7, 0, 6, 8)        (L2 gauge min_h=8)
    Sys Load       (12, 0, 6, 6)         (13, 0, 6, 8)       (L2 gauge min_h=8)
    ...
    RootFS Total   (36, 6, 4, 3)         (36, 8, 4, 6)       (L2 metric min_h=6,
                                                              + overlap resolver push)
    RAM Total      (40, 6, 4, 3)         (40, 6, 4, 6)
    SWAP Total     (44, 6, 4, 3)         (44, 6, 4, 6)

The stat tiles in the second sub-row are now tall enough to read
(h=6 instead of h=3); the gauges are tall enough for their dial
(h=8); the overlap resolver kept the 2D grid clean.

Visual check: section structure preserved across all 16 rows,
including the collapsed ones; section titles match Grafana.

### `prometheus-all` (44 panels, schemaVersion-14 legacy `rows[]`)

Strongest L3 evidence. The dashboard is Grafana schemaVersion 14
with 15 row containers in `dashboard.rows[]`.

Kibana after L1-L4 renders the same 12 sections in the same order
as before -- L3 wasn't bypassed but the prior code already
emitted sections for `rows[]` properly; this branch's L3 just
*also* covers the untitled-row edge case that the parity corpus
doesn't exercise.

The single placeholder panel labelled "2" (a singlestat in
Grafana whose title was empty and value was 2) is now reliably
paired by position with the source panel -- it isn't lost in the
verifier the way it was under title-only pairing (U2).

## What this validates

End-to-end, against a live Kibana cluster:

* **All 6 parity dashboards re-uploaded cleanly** after L1-L4
  (compile + layout-validate + upload).
* **L1 + L2 changed actual coordinates** that ship to the cluster
  (gauges now h=8, metrics now h≥6, overlap resolver kicks in for
  the node-exporter-full first-row case).
* **L3 + L4 are correct by construction**: L3 only changes
  untitled-row dashboards (none in our corpus); L4 only changes
  dashboards with `repeat:` (none in our corpus). Both are covered
  by unit tests.

## Limitations

* This is a **structural / visual** comparison, not a numeric
  pixel-diff baseline. The numeric harness in
  `parity-rig/verifier/visual_regression.py` (driven by
  agent-browser) was unable to run in this environment due to a
  Chrome process leak in agent-browser. The harness could be
  rewritten on Chrome DevTools MCP as future work.
* Some PromQL → ES|QL data values diverge because the parity
  rig's data is independent of Grafana's data view (different time
  windows, different query semantics). These are **translator**
  concerns, not **layout** concerns.
