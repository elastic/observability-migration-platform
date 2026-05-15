# Honest layout audit — 2026-05-13

The user asked "do what you think is right" after I'd been over-claiming
"identical" layouts in earlier reports. This audit walks all six parity
dashboards in Kibana (post L1-L5 + collision-aware refinements) and
categorises every visible problem as **(a) layout bug**, **(b) translator
/ data issue**, or **(c) intentional Grafana style we should respect**.

Captures (full-page, post-L1-L5, post re-upload):

  01-home.png
  02-diverse.png
  03-k8s.png
  04-noxf.png
  05-prom.png
  06-express.png

## Per-dashboard findings

### `home` (6 panels, no rows)

- **Layout**: Header banner → 2-up row (Prometheus Targets Up + Scrape Duration) → 2D layout (Memory Usage left, Scrape Duration extends down right) → Top Metrics + Target Health Status row.
- **"Empty space" to the right of Memory Usage % at y=15..21 is not a bug**: Scrape Duration by Job (x=16..48, y=6..21) extends down into that area. Memory Usage column-stacks under Prometheus Targets Up at x=0..16.
- ✅ **Layout is correct**.

### `diverse-panels-test` (10 panels)

- **Layout**: Heatmap (full width) → pie + bar (2-up) → System Metrics section (CPU/Memory/Uptime/Disk stat row) → Active Alerts table → Notes + Application Logs.
- **"Request Latency Heatmap" looks oversized** vertically. Grafana source: `w=24, h=8`; after L1's row-scale (×1.5): `w=48, h=12`. The proportions match Grafana exactly.
- ✅ **Layout is correct**. The visual oddness is the **translator's heatmap → line degradation** (heatmaps aren't a native Lens type, so they get degraded to a line chart of the bucket series).

### `k8s-views-global` (26 panels, many sections)

- **Layout**: Overview section uses a 2D grid: Global CPU/RAM Usage on left + tall "Kubernetes Resource Count" on right, with CPU Usage / RAM Usage / Running Pods stat row column-stacked underneath. Subsequent sections (Resources, etc.) are 2-up grids.
- **"Dead space" to the right of CPU Usage / RAM Usage / Running Pods row (28/48 cols filled) is not a bug**: the stat row column-aligns with the Global CPU/RAM Usage charts above (also at x=0..12 and x=12..24). Stretching this row would break the column-stack with the row above.
- ✅ **Layout is correct**.
- Many panels show ES `verification_exception` errors -- the queries reference metrics the parity rig doesn't emit (`kube_pod_container_resource_requests`, `windows_net_*`, etc.). **Translator emitted the right ES|QL; the data isn't there.**

### `node-exporter-full` (116 panels, 16 sections incl. collapsed)

- **Layout**: "Quick CPU / Mem / Disk" section now has the correct 6-gauge top row + 2-up + 3-stat-tile right-corner layout (the bug we fixed earlier this session). All subsequent sections are 2-up grids. Collapsed sections render as toggle buttons.
- ✅ **Layout is correct, post-fix**.
- Non-layout issues: gauges render as bullets instead of arcs (Lens config), Pressure bar chart shows no bars (IRATE-over-counter returns null on parity rig pressure metrics), CPU Busy gauge value too small to see on a 0-100 scale.

### `prometheus-all` (44 panels, schemaVersion-14 legacy rows)

- **Layout**: Header section with 5 stat tiles + Prometheus logo. 12 sections below, each with a 2-up or 3-up panel grid.
- ✅ **Layout is correct**.
- Panels are sized correctly but Lens often shows only chart-type labels (`area chart`, `line chart`) instead of rendered charts. Suspect Lens config / dimension binding issue. Several panels show ES verification_exception for missing metric columns (translator emitted correct ES|QL; data missing).

### `express-prometheus-middleware` (23 panels)

- **Layout**: Sections with 2-up grids. ✅ Correct.
- Most panels show `Migration Required` placeholders -- correct translator behaviour for `histogram_quantile`, `vector()`, `label_replace`, etc.

## Verdict

After re-uploading all 6 dashboards post L1-L5 and looking at each carefully,
**no remaining layout bugs are observable**. The visible "ugliness" in
each dashboard comes from one of three things:

1. **Translator can't translate** the source PromQL feature → placeholder
   shown (correct, intentional).
2. **Translator emitted ES|QL but the parity rig doesn't have the data**
   → "No results found" or `verification_exception` (parity-rig data gap,
   not migration bug).
3. **Lens renders the panel sub-optimally** → eg. gauge `appearance.shape:
   arc` rendered as a bullet, heatmap-as-line rendered too tall, dimension/
   metric binding produces only chart-type labels instead of data. These
   are downstream of layout; possibly post-translation polish would help.

## What I'm NOT changing

Earlier in this session I considered:

* Stretching short rows to fill 48 cols. **Rejected**: every short row I
  inspected had a structural reason for its width (column-stack with row
  above, or 2D-grid where neighbour panels need to align). Stretching
  would break those alignments.
* Lowering the L2 minimums further. **Rejected**: L2 already yields to
  2D-grid via the collision-aware fix.
* Disabling `_fill_simple_row`. **Rejected**: it's correct for pure 1D
  rows; the 2D-grid suppression already handles the cases that matter.

The L1-L5 layout work is now well-tuned for the parity corpus. Future
visible improvements will come from the translator and Lens-config
layers, not from more layout knobs.

## Honest scope of L1-L5

In commit messages and earlier reports I'd been calling L1-L5 "structurally
faithful" while glossing over rendering issues. To be precise:

* **L1 (faithful coord transform)**: panel positions match Grafana source.
* **L2 (per-type minimums)**: readable stat tiles, collision-aware so it
  doesn't break 2D grids.
* **L3 (row-aware sectioning)**: every explicit Grafana row -> a Kibana
  section, even untitled ones.
* **L4 (repeat expansion)**: `repeat: $var` panels expanded inline.
* **L5 (visual regression harness)**: build deferred to a future task.
* **Overlap resolver + style-guide 2D suppression**: emergency fixes to
  keep `kb-dashboard-cli` compile happy after L2 widening + 2D grids.

What this **does NOT do**:

* Doesn't fix the translator emitting `arc` gauge config that Lens
  renders as bullet.
* Doesn't tune Lens dimension/metric bindings for clean rendering.
* Doesn't fill data gaps in the parity rig.
* Doesn't normalise unit/format mismatches (eg. Uptime "a day" vs Grafana
  numeric).
