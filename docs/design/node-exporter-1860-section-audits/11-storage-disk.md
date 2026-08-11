# Section audit 11 — Storage Disk

**Dashboard:** Grafana community 1860 *Node Exporter Full* → Kibana `obs-migrate-node-exporter-full`  
**Source JSON:** `/tmp/node-exporter-input.ClIz2R/node-exporter-full.json`  
**Live data:** ES `http://localhost:9201`, index `metrics-node.prometheus-default`, controls `job=node_exporter`, `node=node:9100`, `diskdevices` regex, range `now-15m`  
**Evidence dump:** `11-storage-disk.validation.json`

---

## Section verdict

| # | Panel | Grafana → Kibana | Live | Fidelity |
|---|-------|------------------|------|----------|
| 1 | Disk IOps Completed | read/write irate + neg-Y write → area | Yes | **Good** |
| 2 | Disk R/W Data | bytes + neg-Y write → area | Yes | **Good** |
| 3 | Disk Average Wait Time | await + neg-Y write → area | Yes | **Good** |
| 4 | Average Queue Size | curated → area | Yes | **Good** |
| 5 | Disk R/W Merged | merged + neg-Y write → area | Yes | **Good** |
| 6 | Time Spent Doing I/Os | io time + weighted → area | Yes | **Good** |
| 7 | Instantaneous Queue Size | curated → area | Yes | **Good** |
| 8 | Disk IOps Discards completed / merged | discards → area | Yes | **Good** |

Kibana section `collapsed: false` (pack may force open). All eight panels live-OK with `?diskdevices` bound.

---

## Shared notes

- Read vs write panels use Grafana `negative-Y` on write/read-paired series; Kibana mirrors with `* -1`.
- Disk panels require the **diskdevices** control; raw `_query` without it fails or over-matches — validation binds the pack/default regex.
- **Average Queue Size** / **Instantaneous Queue Size** are curated pack overrides (queue depth formulas); live rows present.

---

## Fixes

None required.

---

## Next section

**12 — Storage Filesystem**
