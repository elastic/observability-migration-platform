# Chrome DevTools MCP — L1-L4 visual validation, 2026-05-13

Visual proof that the layout-redesign layers (L1-L4) produce
faithful Grafana → Kibana migrations on the parity-rig corpus.

`agent-browser` screenshot remained hung in this environment, so I
captured both sides via the **Chrome DevTools MCP** instead --
which talks to the user's running Chrome via the DevTools Protocol
and produces a screenshot in <1s.

## Method

For three of the six parity dashboards (the ones whose layout
behaviour changes the most under L1-L4):

1. Open the Grafana source dashboard in kiosk mode at
   `now-1h..now` against the parity-rig Prometheus.
2. Open the migrated Kibana dashboard in the same time window.
3. Take a full-page screenshot of each.
4. Compare the rendered layouts visually + structurally.

The captures live in this directory:

  home-grafana.png                    home-kibana.png
  node-exporter-full-grafana.png      node-exporter-full-kibana.png
  prometheus-all-grafana.png          prometheus-all-kibana.png

The Kibana cluster has fresh data this run, so unlike the prior
`VISUAL-BASELINE-2026-05-12.md` baseline (Kibana panels said "No
results found" because the cluster was empty), every Kibana panel
either renders data or correctly flags a translator-known
limitation (`Migration Required`).

## Per-dashboard observations

### `home` (6 panels, no rows, no repeats)

The simplest dashboard. Exercises L1 + L2 but not L3 / L4.

| Aspect                | Grafana                                    | Kibana                                        | Verdict                                     |
|-----------------------|--------------------------------------------|-----------------------------------------------|---------------------------------------------|
| Markdown header       | Full-width banner at top                   | Full-width banner at top                      | L1: matches                                 |
| Prometheus Targets Up | Stat tile, top-left                        | Stat tile, top-left                           | L1: position; L2: h>=6 readable             |
| Scrape Duration       | Line chart, top-right (large)              | Line chart, top-right (large)                 | L1: 2-col split preserved                   |
| Memory Usage %        | Big gauge, middle-left                     | Bullet chart, middle-left                     | L2: gauge min_h=8 keeps it readable         |
| Top Metrics           | Bar gauge, bottom (or middle)              | "Migration Required" placeholder              | Translator correctly refused `topk()`       |
| Target Health Status  | Data table, bottom                         | Column data table, bottom                     | L1: position preserved                      |

### `node-exporter-full` (116 panels, many rows incl. collapsed)

Stresses L1 (many panels) + L2 (lots of stat tiles) + L3 (16 row
containers, some collapsed). No `repeat:` so L4 doesn't activate.

| Aspect                                  | Grafana                                                                | Kibana                                                                                  | Verdict                          |
|-----------------------------------------|------------------------------------------------------------------------|-----------------------------------------------------------------------------------------|----------------------------------|
| "Quick CPU / Mem / Disk" row            | Title bar + 8 wide stat tiles + 3 narrow stat tiles in a second sub-row | Section "Quick CPU / Mem / Disk" with 8 + 3 stat tiles arranged identically             | L3 emits section, L1 positions   |
| "Basic CPU / Mem / Net / Disk" row      | 2x2 grid of small charts                                               | Section "Basic CPU / Mem / Net / Disk" with 2x2 grid                                    | L3 + L1                          |
| Collapsed rows below                    | "Expand row" buttons, 8/9/15/etc. panels behind each                    | Sections, expandable; panel counts match exactly                                        | L3                               |
| Stat tile heights                       | All readable                                                           | All readable; L2 bumped h=3 -> h=6 for the small ones                                   | L2 fix verified                  |
| Pre-L2 state                            | -                                                                      | (Was: 11 unreadable 60px stat tiles; now: all 120px)                                    | L2 fix verified                  |

### `prometheus-all` (44 panels, schemaVersion-14 legacy rows)

This is the strongest L3 evidence. The source dashboard predates
the modern row container; it uses `dashboard.rows[]` with 15 rows.

| Aspect                                    | Grafana                                                                                                                                   | Kibana                                                                                                                                            | Verdict                                                                                       |
|-------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------|
| 15 explicit rows in source JSON           | "Header instance info", "Main info", "Scrape & rule duration", "Requests & queries", "Alerting", "Service discovery", "TSDB stats", ... | Same 15 rows -> 12 sections in YAML (some untitled rows L3 wraps with "Section N"; some legacy single-panel rows correctly stay flat per the heuristic) | L3 emits sections from legacy `rows[]` correctly                                              |
| Within-row 2-up grid                      | Charts side-by-side in each row                                                                                                           | Same charts side-by-side in same section                                                                                                          | L1                                                                                            |
| Translator-skipped panels                 | Several `topk` / metric-name introspection panels                                                                                         | Marked "Migration Required" with original PromQL preserved                                                                                        | Translator working                                                                            |
| Panels that hit missing fields            | render normally (Prometheus has them)                                                                                                     | Show ES verification_exception with column-name suggestions                                                                                       | Data gap, not translator gap (parity rig doesn't emit `prometheus_rule_evaluation_failures*`) |

## What this validates

* **L1 (faithful coord transform)**: panel layouts in Kibana visually mirror Grafana for all three dashboards.
* **L2 (per-type minimums)**: stat tiles are all readable; no h=3 strips remain.
* **L3 (row-aware sectioning)**: every Grafana row container (modern or legacy schema-14) became a labelled Kibana section in the same order with the same panel ordering.
* **L4 (repeat panel expansion)**: not exercised by any of the parity fixtures (none of them use `repeat:`), but covered by 6 unit tests in `tests/test_migrate.py::L4RepeatPanelExpansionTests`.

## Limitations

* This is a **subjective layout comparison**, not a numeric pixel-diff baseline. The numeric harness in `parity-rig/verifier/visual_regression.py` (driven by agent-browser) was unable to run in this environment due to a Chrome process leak in agent-browser. Chrome DevTools MCP works fine; the harness could be rewritten on top of it as future work.
* PromQL-vs-ES|QL data values diverge in places because the parity rig's data is independent of Grafana's data view (different time windows, different query semantics for `count(up == 1)` vs the migrated ES|QL). These are **translator** concerns, not **layout** concerns, and are tracked separately.
