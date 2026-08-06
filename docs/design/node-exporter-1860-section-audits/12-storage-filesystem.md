# Section audit 12 — Storage Filesystem

**Dashboard:** Grafana community 1860 *Node Exporter Full* → Kibana `obs-migrate-node-exporter-full`  
**Source JSON:** `/tmp/node-exporter-input.ClIz2R/node-exporter-full.json`  
**Live data:** ES `http://localhost:9201`, index `metrics-node.prometheus-default`, controls `job=node_exporter`, `node=node:9100`, range `now-15m`  
**Evidence dump:** `12-storage-filesystem.validation.json`

---

## Section verdict

| # | Panel | Grafana → Kibana | Live | Fidelity |
|---|-------|------------------|------|----------|
| 1 | Filesystem space available | avail by mount → area | Yes | **Good** |
| 2 | File Nodes Free | inodes free → area | Yes | **Good** |
| 3 | File Descriptor | process max/open fds → area | Yes | **Good** |
| 4 | File Nodes Size | inodes total → area | Yes | **Good** |
| 5 | Filesystem in ReadOnly / Error | readonly + error flags → `area_stacked` | Yes | **Good** |

Target counts match. No neg-Y. No curated overrides in this section. Live row counts high (per-mount timeseries).

---

## Fixes

None required.

---

## Next section

**13 — Network Traffic** (17 panels)
