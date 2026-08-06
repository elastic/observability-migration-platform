# Section audit 07 — System Processes

**Dashboard:** Grafana community 1860 *Node Exporter Full* → Kibana `obs-migrate-node-exporter-full`  
**Source JSON:** `/tmp/node-exporter-input.ClIz2R/node-exporter-full.json`  
**Live data:** ES `http://localhost:9201`, index `metrics-node.prometheus-default`, controls `job=node_exporter`, `node=node:9100`, range `now-15m`  
**Evidence dump:** `07-system-processes.validation.json`

---

## Section verdict

| # | Panel | Grafana → Kibana | Live | Fidelity |
|---|-------|------------------|------|----------|
| 1 | Processes Status | 2 gauges → area | Yes | **Good** |
| 2 | Processes State | 1 by `state` → `area_stacked` | Yes | **Good** |
| 3 | Processes Forks | irate forks → area | Yes | **Good** |
| 4 | Processes Memory | 4 buggy targets → 3 gauges (curated) | Yes | **Good** (intentional repair) |
| 5 | PIDs Number and Limit | 2 → area | Yes | **Good** |
| 6 | Process schedule stats Running / Waiting | 2 irate + neg-Y wait → area | Yes | **Good** |
| 7 | Threads Number and Limit | 2 → area | Yes | **Good** |

Kibana section `collapsed: true` (matches Grafana collapsed row).

---

## Panel notes

### Processes Memory (curated)

Grafana ships **four** targets with duplicated legends and mixed semantics:

| Ref | Expr | Issue |
|-----|------|-------|
| A/C | `irate(process_virtual_memory_bytes[…])` | Duplicate; virtual size is a gauge, not a rate |
| B | `process_resident_memory_max_bytes` | Legend says “Maximum … virtual”; metric name is resident max |
| D | `irate(process_virtual_memory_max_bytes[…])` | irate on a gauge |

Pack override plots three **LAST_OVER_TIME** gauges: virtual, resident (`process_resident_memory_bytes`), and virtual max. Live rows show sensible virtual/resident byte ranges; virtual max ≈ `2^64−1` (unlimited) on this host.

### Process schedule stats Running / Waiting

Waiting series uses Grafana `negative-Y`; Kibana query includes `* -1`. Live OK.

---

## Fixes

None required beyond the existing Processes Memory pack override (already correct).

---

## Next section

**08 — System Misc**
