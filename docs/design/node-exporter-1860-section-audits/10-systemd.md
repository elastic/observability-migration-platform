# Section audit 10 — Systemd

**Dashboard:** Grafana community 1860 *Node Exporter Full* → Kibana `obs-migrate-node-exporter-full`  
**Source JSON:** `/tmp/node-exporter-input.ClIz2R/node-exporter-full.json`  
**Live data:** ES `http://localhost:9201`, index `metrics-node.prometheus-default`, controls `job=node_exporter`, `node=node:9100`, range `now-15m`  
**Evidence dump:** `10-systemd.validation.json`

---

## Section verdict

| # | Panel | Grafana → Kibana | Live | Fidelity |
|---|-------|------------------|------|----------|
| 1 | Systemd Sockets | irate accepted connections by name → area | Yes | **Good** |
| 2 | Systemd Units State | 5 state gauges → `area_stacked` | Yes | **Good** |

---

## Details

### Systemd Units State

Grafana five targets (`activating` / `active` / `deactivating` / `failed` / `inactive`) with stacking `normal` → Kibana `area_stacked` with matching named series. Live on this host: Active≈92, Inactive≈14, Activating≈1, Failed/Deactivating≈0.

### Systemd Sockets

Generic `IRATE` on `node_systemd_socket_accepted_connections_total` by socket name. Live returns rows.

No curated pack overrides. No neg-Y.

---

## Fixes

None required.

---

## Next section

**11 — Storage Disk** (or next remaining Grafana row after Systemd)
