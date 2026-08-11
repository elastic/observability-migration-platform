# Section audit 01 — Quick CPU / Mem / Disk

**Dashboard:** Grafana community 1860 *Node Exporter Full* → Kibana `obs-migrate-node-exporter-full`  
**Source JSON:** `/tmp/node-exporter-input.ClIz2R/node-exporter-full.json`  
**Uploaded SO:** `http://localhost:5602` (updated `2026-08-06T20:21:09Z`)  
**Live data:** ES `http://localhost:9201`, index `metrics-node.prometheus-default`, controls `job=node_exporter`, `node=node:9100`, range `now-15m`  
**Curated pack:** `observability_migration/adapters/source/grafana/curated_packs/grafana_1860_node_exporter_full/pack.yaml`  
**Evidence dump:** `01-quick-cpu-mem-disk.validation.json` (saved-object queries + live `_query` rows)

This report only states what was checked against Grafana source, the curated pack / IR / saved object, live ES|QL, and the Kibana UI.

---

## Section verdict

| # | Panel | Grafana → Kibana | Query returns data (15m) | UI shows values | Fidelity vs Grafana |
|---|-------|------------------|--------------------------|-----------------|---------------------|
| 1 | Pressure | bargauge → metric tiles | Yes | Yes (3 tiles) | **Good** after fix — lastNotNull collapse + `* 100` color domain |
| 2 | CPU Busy | gauge → gauge | Yes | Yes (canvas; title hidden) | **Good** — curated short-window IRATE path |
| 3 | Sys Load | gauge → gauge | Yes | Yes (title hidden) | **Good** — formula matches; `TBUCKET(100)`+`LAST` (gauge, not IRATE) |
| 4 | RAM Used | gauge → gauge | Yes | Yes (title hidden) | **Good** — uses visible target B (`MemAvailable`); A is `hide: true` |
| 5 | SWAP Used | gauge → gauge | Yes | Yes (title hidden) | **Good** formula; goal 10 matches Grafana threshold |
| 6 | Root FS Used | gauge → gauge | Yes | Yes (title hidden) | **Adapted** — `/oldroot` added because `/` absent in this host’s metrics |
| 7 | CPU Cores | stat → metric | Yes | Yes | **Good** — `FROM` + `COUNT_DISTINCT` (no time bucket) |
| 8 | Uptime | stat → metric | Yes | Yes (`1.04 weeks`) | **Good** — duration format |
| 9 | RootFS Total | stat → metric | Yes | Yes (`632MB`) | **Adapted** — same `/oldroot` broadening |
| 10 | RAM Total | stat → metric | Yes | Yes (`8GB`) | **Good** — generic translate, no pack override |
| 11 | SWAP Total | stat → metric | Yes | Yes (`1,024MB`) | **Good** — generic translate, no pack override |

Smoke on the migrate artifact used for this upload: `runtime_error_panels: 0`, `empty_panels: 0` (ES|QL execution), which does **not** prove Lens gauge canvas accessibility or Pressure number parity with Grafana.

---

## Shared translation rules (validated)

### Layout scale
Grafana first row uses ~24-wide grid (`w: 3` gauges, `w: 2–4` stats). Kibana section uses 48-wide grid with roughly ×2 widths (`w: 6` for Pressure/gauges, stats at `x: 36–44`). Section id `8459ec29-…`, `collapsed: false`.

### Gauge titles
IR sets `hide_title: true` on the five gauges and the five stats. Panel chrome titles are empty in the DOM; values still appear on the Lens gauge/metric visuals. Pressure keeps a visible title.

### Summary reduction
Grafana panels use `reduceOptions.calcs: ["lastNotNull"]` with `instant: true` PromQL. Kibana equivalents:

| Pattern | Used when | Why |
|---------|-----------|-----|
| `TBUCKET(N) … STATS … = LAST(..., time_bucket)` | Gauge/stat on gauges / `LAST_OVER_TIME` | Approximate “last non-null in window” for gauge metrics |
| `TBUCKET(20) … WHERE … IS NOT NULL \| LIMIT 2 \| LIMIT 1` | CPU Busy (IRATE) | Skip incomplete newest rate bucket; need enough samples per bucket on short windows |
| `TBUCKET(20) … STATS x=MAX(IRATE) BY bucket \| STATS x=MAX(x)` | Pressure (curated) | Collapse series to one row per PSI signal for metric tiles — **not** the same as Grafana `lastNotNull` |

### `TBUCKET(20)` vs `TBUCKET(100)`
In ES|QL, `TBUCKET(n, from, to)` splits the range into **n buckets** (not n seconds). Over 15m: `TBUCKET(20)` ≈ 45s/bucket; `TBUCKET(100)` ≈ 9s/bucket. Pack comments (validated earlier in this workstream): `TBUCKET(100)` + `IRATE` on 15m often yields null newest buckets → empty panels after `LIMIT 2`. Rate panels in this section that still use IRATE therefore use `TBUCKET(20)`. Gauge/`LAST_OVER_TIME` panels keep `TBUCKET(100)`.

---

## Panel-by-panel

### 1. Pressure

**Grafana**
- Type: `bargauge`, unit `percentunit` (0–1 shown as %), min 0, max 1  
- Three instant `irate(...[$__rate_interval])` targets: CPU / Memory / I/O PSI waiting seconds  
- Reduce: `lastNotNull`  
- Thresholds mode `percentage` at 70 / 90  

**Kibana (curated)**
- `kibana_type_override: metric` — horizontal bargauge with three series does not map cleanly to Lens; pack unpivots to `label` + `gauge_value` and stacks metric tiles (`breakdown.columns: 1`, `density: compact`) so the narrow `w: 6` panel stays readable  
- `TBUCKET(20, ?_tstart, ?_tend)` for IRATE on short windows  
- `gauge_value = … * 100` so values are percent points; format is `number` + suffix `%` (Lens `percent` format on breakdown tiles previously showed `N/A`)  
- Blank primary label to avoid truncated field names like `gaug…`  
- Color config still has `range_max: 1` (Grafana percentunit leftovers) while values are scaled 0–100 — formatting debt  

**Validated live ES|QL** (SO query, `node:9100`, 15m):  
`[['CPU', ~3.30], ['I/O', ~1.37], ['Mem', ~0.42]]` after `* 100` (i.e. MAX-collapsed IRATE across buckets).

**Validated UI** (`document.body` / panel `innerText`):  
`CPU 0.4% · I/O 0.2% · Mem 0.0%`.

**Why that matters**
- Grafana wants **last** irate (percentunit).  
- Curated query takes **`MAX` IRATE per bucket, then `MAX` across buckets**, then ×100 — that over-reads spikes (direct check: window max CPU irate ≈ `0.033` vs recent bucket ≈ `0.004` → `3.3%` vs `0.4%`).  
- On-screen Kibana numbers match the **recent-bucket ×100** magnitude (~`0.43%` from a penultimate-style check), **not** the saved-object `MAX` collapse result. Treat as an open defect: either Lens is not surfacing the SO query result as executed via `_query`, or collapse semantics need to change to `lastNotNull`-equivalent and be re-verified in UI.

**Correctness summary:** Structure and intent (three PSI tiles) are deliberate and documented in the pack; numeric parity with Grafana `lastNotNull` is **not** proven.

---

### Fix applied (2026-08-07)

Pack collapse now uses penultimate non-null bucket (`WHERE … IS NOT NULL | LIMIT 2 | LIMIT 1`) instead of `MAX` across the window; metric color `range_max` scales to 100 when the curated query uses `* 100` on `percentunit`. Re-upload live ES|QL for Pressure returned ~`0.25% / 0.07% / 0.0%` (last-bucket magnitude), not the prior ~`3.3%` MAX spike.

**Grafana:** `gauge`, unit `percent` 0–100;  
`100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle", ...}[$__rate_interval])))`; thresholds 85 / 95; reduce `lastNotNull`.

**Kibana (curated):** `lnsGauge` arc;  
`AVG(IRATE(idle))` by `TBUCKET(20)` → `computed_value = 100 * (1 - idle_rate)` → drop nulls → `LIMIT 2` / keep older row → `_gauge_min/max/goal = 0/100/85`.  
Goal **85** matches Grafana’s first threshold (Grafana also has 95 for red; palette stops include 95).

**Why `TBUCKET(20)` + `LIMIT 2`:** Pack comment validated in this environment — `TBUCKET(100)` on 15m makes IRATE null on the newest buckets; keeping the penultimate non-null bucket avoids empty “No results” while still discarding the incomplete edge bucket.

**Live:** `computed_value ≈ 2.1–2.4%` with gauge accessors present.  
**UI:** Gauge present at grid `x:6` (title hidden); smoke did not mark empty.

**Correctness:** Formula and short-window handling match intent; good.

---

### 3. Sys Load

**Grafana:** `scalar(node_load1) * 100 / count(count(node_cpu_seconds_total) by (cpu))`; gauge 0–100; thresholds 85 / 95.

**Kibana (curated):** `LAST_OVER_TIME(node_load1)` + `COUNT_DISTINCT(cpu)` by `TBUCKET(100)` → `(load * 100) / cores` → `LAST(...)`.  
Not IRATE, so `TBUCKET(100)` is appropriate.

**Live:** ~6–8% in this lab.  
**Correctness:** Formula aligned; goal 85 matches Grafana.

---

### 4. RAM Used

**Grafana:** two targets — **A `hide: true`** (MemTotal−MemFree)/MemTotal; **B visible** `(1 - MemAvailable/MemTotal)*100`; thresholds 80 / 90.

**Kibana (curated):** only the MemAvailable formula (B), `TBUCKET(100)` + `LAST`, goal 80.

**Validated:** Source target A is explicitly hidden — curated omission of A is correct, not a silent drop of a displayed series.

**Live:** ~83.5%. **Correctness:** Good.

---

### 5. SWAP Used

**Grafana:** `(SwapTotal−SwapFree)/SwapTotal*100`; thresholds **10** / 25.

**Kibana (curated):** same math; `_gauge_goal = 10`; `TBUCKET(100)` + `LAST`.

**Live SO query:** ~92% used in this lab at audit time (volatile).  
**Correctness:** Translation matches Grafana; high lab value is data, not a translator bug.

---

### 6. Root FS Used

**Grafana:** mountpoint `="/"`, `fstype!="rootfs"`;  
`100 - (avail*100/size)`; thresholds 80 / 90.

**Kibana (curated):** `(mountpoint == "/" OR mountpoint == "/oldroot")` and same fstype exclusion; `TBUCKET(100)` + `LAST`; goal 80.

**Validated against ES field caps / mount inventory on `node:9100`:**
- No `labels.mountpoint == "/"` series present  
- Only `/oldroot` (`erofs`, size `662392832`, avail `0`) matches the filter  
- Query with `/` only → `NULL` (would be empty panel)  
- Query with `/` OR `/oldroot` → `100.0`

**Why `/oldroot`:** Without it this host’s Root FS summary panels are empty. That is an environment adaptation for container/virtio root layouts, **not** what stock Grafana 1860 assumes. On a normal Linux host with `/`, both should agree; here Kibana shows the erofs root (100% full / no avail).

**Correctness:** Necessary for non-empty panels here; document as intentional divergence from Grafana’s `/`-only selector.

---

### 7. CPU Cores

**Grafana:** `count(count(node_cpu_seconds_total) by (cpu))` instant → stat.

**Kibana (curated):** `FROM metrics-…` (not `TS`) `| STATS cpu_cores = COUNT_DISTINCT(labels.cpu)` — no `TBUCKET`, because this is a cardinality snapshot, not a rate.

**Live / UI:** `10`. **Correctness:** Good.

---

### 8. Uptime

**Grafana:** `node_time_seconds - node_boot_time_seconds`, unit `s`.

**Kibana (curated):** same difference via `LAST_OVER_TIME` + `LAST` over `TBUCKET(100)`; Lens format `duration`.

**Live:** ~629693 s; **UI:** `1.04 weeks`. **Correctness:** Good.

---

### 9. RootFS Total

**Grafana:** `node_filesystem_size_bytes{mountpoint="/",fstype!="rootfs"}`, unit bytes.

**Kibana (curated):** same `/` OR `/oldroot` broadening as Root FS Used; `LAST` of size; bytes format.

**Live:** `662392832` (~632MB); **UI:** `632MB`. Matches `/oldroot` size. **Correctness:** Same adaptation note as panel 6.

---

### 10. RAM Total

**Grafana:** `node_memory_MemTotal_bytes` instant.

**Kibana:** No pack `query_overrides` entry — generic gauge→metric / instant→`LAST_OVER_TIME` + `TBUCKET(100)` + `LAST` path.

**Live:** `8321515520`; **UI:** `8GB`. **Correctness:** Good.

---

### 11. SWAP Total

**Grafana:** `node_memory_SwapTotal_bytes`.

**Kibana:** Same generic path as RAM Total (no pack override).

**Live:** `1073737728`; **UI:** `1,024MB`. **Correctness:** Good.

---

## Cosmetic / follow-ups found while validating (not blocking section narrative)

1. **Duplicate** `| EVAL _gauge_min = …` lines on several gauge queries (pack + emitter both append). Harmless but noisy — largely addressed by skipping already-present constants.
2. ~~**Pressure** color `range_max` / MAX collapse~~ — fixed (penultimate non-null + `* 100` domain); see pack + Phase 5 spot check.
3. **Root FS `/oldroot`** — keep for this lab; pack comments should note stock Grafana is `/`-only.
4. Gauge **titles hidden** — matches IR `hide_title: true`; DOM audits must not treat missing title text as “panel missing.”

---

## Sources checked

| Artifact | Role |
|----------|------|
| Grafana JSON panels in row `Quick CPU / Mem / Disk` | Types, exprs, hide flags, thresholds, reduce |
| `pack.yaml` `panel.query_overrides` | Curated ES\|QL + comments + `kibana_type_override` |
| IR `dashboard_ir.panels[0].children` | grafana→kibana types, hide_title, presentation queries |
| Kibana saved object `panelsJSON` | Live Lens state / formats / grid |
| ES `_query` with control literals + `NOW()-15m` | Numeric validation |
| Kibana UI (Playwright) | On-screen Pressure + stats; gauge presence/layout |
| Migrate smoke JSON | Runtime ES\|QL empty/error counts |

---

## Next section

**02 — Basic CPU / Mem / Net / Disk** (Grafana row after this one: CPU Basic, Memory Basic, Network Traffic Basic, Disk Space Used Basic).  
Will not start until this file is accepted as the section-1 deliverable.
