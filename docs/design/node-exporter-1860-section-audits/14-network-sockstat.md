# Section audit 14 — Network Sockstat

**Dashboard:** Grafana community 1860 *Node Exporter Full* → Kibana `obs-migrate-node-exporter-full`  
**Evidence dump:** `14-network-sockstat.validation.json`

---

## Section verdict

| # | Panel | Live | Fidelity |
|---|-------|------|----------|
| 1 | Sockstat TCP | Yes | **Good** (4 series) |
| 2 | Sockstat UDP | Yes | **Good** (3 series) |
| 3 | Sockstat FRAG / RAW | Yes | **Good** (2 series) |
| 4 | Sockstat Memory Size | Yes | **Good** (3 series) |
| 5 | Sockstat Used | Yes | **Good** |

Collapsed in Kibana. Target counts match. No neg-Y. No curated overrides. Live OK for all five.

---

## Fixes

None required.

---

## Next section

**15 — Network Netstat**
