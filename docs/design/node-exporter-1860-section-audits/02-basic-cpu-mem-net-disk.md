# Section audit 02 — Basic CPU / Mem / Net / Disk

**Dashboard:** Grafana community 1860 *Node Exporter Full* → Kibana `obs-migrate-node-exporter-full`  
**Source JSON:** `/tmp/node-exporter-input.ClIz2R/node-exporter-full.json`  
**Uploaded SO:** `http://localhost:5602`  
**Live data:** ES `http://localhost:9201`, index `metrics-node.prometheus-default`, controls `job=node_exporter`, `node=node:9100`, range `now-15m`  
**Curated pack:** all four panels have `panel.query_overrides` in `grafana_1860_node_exporter_full/pack.yaml`  
**Evidence dump:** `02-basic-cpu-mem-net-disk.validation.json`

Claims below were checked against Grafana source, pack/IR/SO, live ES|QL (with retries after circuit-breaker pressure), and Kibana UI legends.

---

## Section verdict

| # | Panel | Grafana → Kibana | Live series | UI renders | Fidelity vs Grafana |
|---|-------|------------------|-------------|------------|---------------------|
| 1 | CPU Basic | timeseries → `lnsXY` `area_percentage_stacked` | 6 series (names match) | Yes — all 6 legends | **Good** — formula matches; `TBUCKET(100)` is aggressive for 15m IRATE |
| 2 | Memory Basic | timeseries → `lnsXY` `area_stacked` + Total line | 5 metrics | Yes | **Good** after fix — RAM Total restored as unstacked overlay |
| 3 | Network Traffic Basic | timeseries → `lnsXY` `area` | 22 series (11 devices × 2) | Yes | **Good** — `*8` to bits + negative transmit mirrors Grafana `negative-Y` |
| 4 | Disk Space Used Basic | timeseries → `lnsXY` `area` | 22 mountpoints | Yes | **Good** — `% used` by mountpoint; axis locked 0–100 |

---

## Why these panels are curated

Grafana emits **multi-target** timeseries (several PromQL expressions / legend labels). Lens ES|QL XY expects a long shape: `time_bucket` + metric `value` + `series_group`. The pack therefore:

1. Computes each Grafana target as columns (or by-label stats)
2. **Unpivots** via `MV_APPEND` / `MV_ZIP` / `MV_EXPAND` into `series_group` + `value`
3. Lets the emitter pick stacked/percentage series types from Grafana `fieldConfig.custom.stacking`

That is why queries look large compared to single-gauge curated panels in section 1.

---

## Shared bucket note

All four use `TBUCKET(100, ?_tstart, ?_tend)` (100 buckets across the dashboard range ≈ **9s/bucket** on 15m).

- **Gauge / `LAST_OVER_TIME` panels** (Memory, Disk): fine.
- **IRATE panels** (CPU Basic, Network): same short-window risk documented in section 1 for CPU Busy. Charts can show gaps/nulls on newest buckets rather than going fully empty. A lighter equivalent of CPU Basic with `TBUCKET(20)` on this host summed to ~`1.0` across modes (percent-unit identity check). Pack still ships `TBUCKET(100)` for these overview charts.

---

## Panel-by-panel

### 1. CPU Basic

**Grafana**
- Type: `timeseries`, unit `percentunit` (0–1)
- Stacking: `mode: percent`
- Six `irate` targets, each `sum(irate(...mode...)) / scalar(count(count(... ) by (cpu)))`
- Legends: Busy System / User / Iowait / IRQs / Other / Idle

**Kibana (curated)**
- `area_percentage_stacked`, y-title `%`, value format `percent`
- Same six modes via `SUM(CASE(mode, IRATE(...))) / COUNT_DISTINCT(cpu)` then unpivot to those legend strings
- Busy IRQs: `labels.mode RLIKE ".*irq"` (covers irq + softirq), matching Grafana `mode=~".*irq"`
- Busy Other: excludes idle/user/system/iowait/irq/softirq (same intent as Grafana)

**Validated**
- Live summary: exactly the six series names; UI legend lists all six
- Light rewrite (`TBUCKET(20)`, without Busy Other): recent buckets sum ≈ `1.00–1.005` → percent-unit modes are coherent
- Full SO query is heavy (hit ES parent circuit breaker / 429 under parallel load); UI still draws the chart

**Correctness:** Translation decision is sound. Residual risk is `TBUCKET(100)` + IRATE on short ranges (gaps), not wrong formulas.

---

### 2. Memory Basic

**Grafana**
- Unit `bytes`, stacking `mode: normal`
- Five targets:
  - **RAM Total** — with override: fillOpacity 0, **stacking off** (outline / reference)
  - RAM Used — Total − Free − (Cached+Buffers+SReclaimable)
  - RAM Cache + Buffer — Cached+Buffers+SReclaimable
  - RAM Free
  - SWAP Used — SwapTotal − SwapFree

**Kibana (curated)**
- `area_stacked`, y-title `Bytes`, format `bytes`
- Computes `RAM_Total` internally for the Used formula, then unpivots **only**:
  - RAM Used, RAM Cache + Buffer, RAM Free, SWAP Used
- **Does not emit RAM Total** as a series

**Validated**
- Live: four series only; UI legend matches those four (no “RAM Total”)
- `RAM_Total` appears in `STATS` / `EVAL RAM_Used = ...` but not in `MV_APPEND` labels — intentional omission in the pack text, not an accidental drop of the metric field
- Used / Cache+Buffer / Free / SWAP formulas match Grafana B–E

**Why it differs:** Lens stacked area + a non-stacked reference line is awkward in the single `series_group` unpivot model. Pack keeps the stacked memory breakdown and drops the Total overlay.

**Correctness:** Stacked memory composition is right; **missing RAM Total is a real fidelity gap** vs Grafana (call out in review, not “silent success”).

---

### Fix applied (2026-08-07)

Memory Basic curated query is now **wide** (`RAM Total` / Used / Cache+Buffer / Free / SWAP Used columns). Field override marks `RAM Total` as `stack: false`; Kibana emits a stacked area layer plus an unstacked **line** overlay. Verified on re-upload: `preferredSeriesType=area_stacked`, `layers=2`.

---

### 3. Network Traffic Basic

**Grafana**
- Unit `bps`
- `irate(receive_bytes)*8` legend `recv {{device}}`
- `irate(transmit_bytes)*8` legend `trans {{device}}`
- Override: `/.*trans.*/` → `custom.transform: negative-Y` (mirror chart)
- Stacking: none

**Kibana (curated)**
- `area` (unstacked)
- `AVG(IRATE(...))` by `TBUCKET(100)` + `labels.device`
- `recv = recv * 8`, `trans = -1 * (trans * 8)` — **negation in-query** instead of a Lens negative-Y transform
- Series names: `CONCAT(device, " / ", direction)` with `Receive` / `Transmit`
- Format: `bits` + suffix `/s` (bits-per-second)

**Validated**
- Live: 22 series; `eth0 / Receive` positive (~2k–14k), `eth0 / Transmit` negative (~−38k to −266k)
- UI legend shows `eth0 / Receive`, `eth0 / Transmit`, plus many virtual interfaces (same device cardinality Grafana would list)

**Why negation in ES|QL:** Lens text-based layers don’t carry Grafana’s field override transform; multiplying transmit by `-1` preserves the classic Node Exporter “up = recv / down = trans” look.

**Correctness:** Good semantic match; legend wording differs (`device / Transmit` vs `trans device`) but meaning is clear.

---

### 4. Disk Space Used Basic

**Grafana**
- Unit `percent`, min 0 max 100
- `100 - (avail*100/size)` with `device!~'rootfs'`, legend `{{mountpoint}}`
- Stacking: none

**Kibana (curated)**
- `area`, `yLeftExtent` custom **0–100** (matches Grafana axis)
- Filter: `NOT (labels.device RLIKE "rootfs")`
- `AVG(LAST_OVER_TIME(avail/size))` by `TBUCKET(100)` + `labels.mountpoint`
- Format: `number` + suffix `%` (values already 0–100; not Lens `percent` format)

**Validated**
- Live: 22 mountpoints including `/oldroot` (100%), virtiofs/host_mark paths (~66%), `/var/lib` (~35%), etc.
- UI legend lists the same mountpoint crowd
- No `/` mount in this host’s series (same environment note as section 1 Root FS)

**Correctness:** Formula and device filter match Grafana. Busy legend is an artifact of this Docker/mac node exporter filesystem inventory, not a translator inventing mounts.

---

## UI cross-check (Playwright)

Section expanded; all four panels present with chart chrome + legends:

| Panel | Legend evidence in DOM |
|-------|-------------------------|
| CPU Basic | Busy Other, Iowait, Idle, System, IRQs, User |
| Memory Basic | RAM Cache + Buffer, SWAP Used, RAM Used, RAM Free (**no RAM Total**) |
| Network Traffic Basic | `… / Receive` and `… / Transmit` including `eth0` |
| Disk Space Used Basic | Many `mountpoint` paths including `/oldroot`, `/var/lib`, … |

---

## Follow-ups (validated gaps only)

1. **Memory Basic — restore RAM Total** as a non-stacked series (or second layer) if Lens allows; otherwise document the gap in pack comments.  
2. **CPU Basic / Network — consider `TBUCKET(20)`** (or adaptive buckets) for short dashboard ranges so IRATE nulls don’t gap the newest points (same lesson as section 1 CPU Busy).  
3. **CPU Basic pack query** is expensive on this ES (circuit breaker under load); worth profiling if operators hit timeouts on dense metrics.  
4. Network legend naming is fine; optional align to `recv/trans` wording for screenshot parity.

---

## Sources checked

| Artifact | Role |
|----------|------|
| Grafana row `Basic CPU / Mem / Net / Disk` | Types, exprs, stacking, negative-Y, RAM Total override |
| Pack `query_overrides` for all four titles | Curated ES\|QL / unpivot |
| Kibana SO `panelsJSON` | Lens series type, formats, axis extent, queries |
| ES `_query` summaries + light CPU rewrite | Series cardinality and formula sanity |
| Kibana UI | Legends / render presence |

---

## Next section

**03 — CPU / Memory / Net / Disk** (deeper row: CPU, Memory Stack, Network Traffic, Disk Space Used, Disk IOps, …).  
Not started until you say to continue.
