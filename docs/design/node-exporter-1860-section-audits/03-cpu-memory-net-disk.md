# Section audit 03 — CPU / Memory / Net / Disk

**Dashboard:** Grafana community 1860 *Node Exporter Full* → Kibana `obs-migrate-node-exporter-full`  
**Source:** nested panels under collapsed Grafana row `CPU / Memory / Net / Disk` (8 children)  
**Kibana:** section forced open via pack `layout_overrides` (`collapsed: false`)  
**Evidence:** `03-cpu-memory-net-disk.validation.json`  
**Controls used for live checks:** `job=node_exporter`, `node=node:9100`, `diskdevices=[a-z]+|nvme…|mmcblk…`, range `now-15m`

---

## Section verdict

| # | Panel | Grafana → Kibana | Live data | UI | Fidelity |
|---|-------|------------------|-----------|----|----------|
| 1 | CPU | timeseries % stacked → `area_percentage_stacked` (curated) | 8 series | Yes — all mode legends | **Good** — formulas match; `TBUCKET(20)` applied |
| 2 | Memory Stack | stacked bytes → `area_stacked` + line overlay | Yes | Yes | **Good** — Hardware Corrupted present after 2026-08-07 lab seed |
| 3 | Network Traffic | bps area, neg-Y Transmit → `area` + `Transmit *= -1` | Yes (`eth0` etc.) | Yes — `device - Receive/Transmit` | **Good** |
| 4 | Disk Space Used | bytes used by mount → curated `used_bytes` | Yes | Yes | **Good** — size−avail, not percent (unlike Basic) |
| 5 | Disk IOps | iops, neg-Y Reads → Reads negated | Yes (`vda`/`vdb`) | Yes | **Good** |
| 6 | I/O Usage Read / Write | Bps, neg-Y read → read negated | Yes | Yes | **Good** |
| 7 | I/O Utilization | percentunit irate → curated `utilization` | Yes | Yes | **Good** — `TBUCKET(20)` applied |
| 8 | CPU spent seconds in guests (VMs) | timeseries **bars** → `bar` | Yes (0 on this host) | Yes | **Good** — bar matches Grafana `drawStyle: bars`; `group_left` approximated in ES\|QL |

---

## Fixes applied during this audit

1. **Curated IRATE buckets:** `CPU`, `CPU Basic`, `Network Traffic Basic`, `I/O Utilization` pack queries moved from `TBUCKET(100)` → **`TBUCKET(20)`** so short windows keep enough samples per bucket (same lesson as CPU Busy / Pressure). Re-uploaded; SO confirms `TBUCKET(20, …)`.

---

## Panel-by-panel

### 1. CPU (curated)

**Grafana:** 8 `irate` modes / core count, unit `percentunit`, stacking `percent`.  
**Kibana:** Curated unpivot to `series_group` + `value`, `area_percentage_stacked`, percent format.  
**Validated:** Live distinct series = System / User / Nice / Iowait / Irq / Softirq / Steal / Idle (full Grafana legend strings). UI lists the same.  
**Why curated:** Multi-target percent stack needs long-form unpivot for Lens.

### 2. Memory Stack (generic)

**Grafana:** 9 byte series, normal stack; **Hardware Corrupted** override `stack: false`.  
**Kibana:** Wide metrics, `area_stacked` + `line` overlay (`stack: false` on Hardware).  
**Validated:** Apps/PageTables/SwapCache/Slab/Cache/Buffers/Unused/Swap render; legends prefixed with `node:9100` because query groups `BY labels.instance` (cosmetic with single-node filter).  
**Gap (resolved 2026-08-07 on lab host):** `metrics.node_memory_HardwareCorrupted_bytes` was missing from field caps. After seeding the gauge (+ remigrate), the series is present as `Hardware_Corrupted_…` in the live query columns. On hosts that never export this meminfo line, expect the optional-metric / field-gap behavior again.

### 3. Network Traffic (generic)

**Grafana:** `irate*8` recv/trans, override `/.*Trans.*/` → `negative-Y`.  
**Kibana:** `Receive = ×8`, `Transmit = -1 × ×8`, breakdown `labels.device`.  
**Validated:** Live eth0 receive positive; UI legends `eth0 - Receive` / `Transmit`. Matches intent of Grafana `{{device}} - Receive`.

### 4. Disk Space Used (curated)

**Grafana:** `size - avail` bytes, `device!~rootfs`, legend `{{mountpoint}}`.  
**Kibana:** Curated `used_bytes = size_bytes - avail_bytes` by mountpoint (not the percent panel from section 2).  
**Validated:** Live mountpoints with large used_bytes on virtiofs paths; UI shows mount list. Correct unit/semantics vs “Disk Space Used **Basic**” (percent).

### 5. Disk IOps (generic)

**Grafana:** irate reads/writes with `device=~$diskdevices`; `/.*Read.*/` → negative-Y.  
**Kibana:** `Reads_completed = -1 * …`, writes positive; `?diskdevices` control.  
**Validated:** With diskdevices regex bound, `vda`/`vdb` rows; UI legends Reads/Writes completed. Direct `_query` without `?diskdevices` fails (expected — needs control).

### 6. I/O Usage Read / Write (generic)

**Grafana:** irate read/written bytes; `/.*read*./` → negative-Y.  
**Kibana:** `Successfully_read_bytes` negated; writes positive.  
**Validated:** Live + UI legends for vda/vdb.

### 7. I/O Utilization (curated)

**Grafana:** `irate(node_disk_io_time_seconds_total)` percentunit by device.  
**Kibana:** Curated `utilization = AVG(IRATE(...))` + diskdevices filter.  
**Validated:** Live vda/vdb utilization in 0–1 range; UI shows devices. Pack now `TBUCKET(20)`.

### 8. CPU spent seconds in guests (VMs) (generic)

**Grafana:** `drawStyle: bars`, percentunit max 1; guest_seconds / cpu_seconds with `group_left` (unsupported in ES PROMQL bridge).  
**Kibana:** `bar` chart; ES\|QL ratio via `SUM(CASE(mode))/SUM(IRATE(total))` by instance.  
**Validated:** UI bar chart with Guest / GuestNice legends; live values 0 (no guest CPU on this VM — plausible).  
**Why bar not area:** Grafana explicitly sets bars — correct mapping.

---

## Notes (not bugs)

- **`?diskdevices`:** Disk IOps / I/O Usage / I/O Utilization require the dashboard control; omitting it in raw `_query` is a validation footgun, not a migration defect.  
- **Instance prefix on Memory/Guest legends:** `BY labels.instance` + single selected node → `node:9100 - …` labels. Acceptable; optional later cleanup.  
- **Heavy CPU query:** Previously tripped ES circuit breakers under parallel load; `TBUCKET(20)` reduces bucket count.

---

## Sources checked

| Artifact | Role |
|----------|------|
| Grafana row children (collapsed JSON) | Exprs, stack, neg-Y, drawStyle |
| Pack overrides for CPU / Disk Space Used / I/O Utilization | Curated ES\|QL |
| Kibana SO section panels | Lens types, negation EVAL, layers |
| ES `_query` with controls | Series presence / signs |
| Playwright exact title match | On-screen legends |

---

## Next section

**04 — Memory Meminfo** (15 nested panels under collapsed row).
