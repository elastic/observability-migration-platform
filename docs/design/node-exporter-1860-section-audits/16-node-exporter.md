# Section audit 16 — Node Exporter

**Dashboard:** Grafana community 1860 *Node Exporter Full* → Kibana `obs-migrate-node-exporter-full`  
**Evidence dump:** `16-node-exporter.validation.json`

---

## Section verdict

| # | Panel | Live | Fidelity |
|---|-------|------|----------|
| 1 | Node Exporter Scrape Time | Yes | **Good** (`area_stacked`) |
| 2 | Node Exporter Scrape | Yes | **Good** (neg-Y on one series) |

Final Grafana row. Target counts match. Live high row counts (per-collector scrape metrics). No curated overrides required.

---

## Fixes

None required.

---

## Audit complete

All Grafana rows for dashboard 1860 have section reports under `docs/design/node-exporter-1860-section-audits/`.
