# Section audit 13 — Network Traffic

**Dashboard:** Grafana community 1860 *Node Exporter Full* → Kibana `obs-migrate-node-exporter-full`  
**Source JSON:** `/tmp/node-exporter-input.ClIz2R/node-exporter-full.json`  
**Live data:** ES `http://localhost:9201`, index `metrics-node.prometheus-default`, controls `job=node_exporter`, `node=node:9100`, range `now-15m`  
**Evidence dump:** `13-network-traffic.validation.json`

---

## Section verdict

| # | Panel | Live | Fidelity |
|---|-------|------|----------|
| 1 | Network Traffic by Packets | Yes | **Good** (neg-Y transmit) |
| 2 | Network Traffic Errors | Yes | **Good** (neg-Y) |
| 3 | Network Traffic Drop | Yes | **Good** (neg-Y) |
| 4 | Network Traffic Compressed | Yes | **Good** (neg-Y) |
| 5 | Network Traffic Multicast | Yes | **Good** (curated) |
| 6 | Network Traffic Fifo | Yes | **Good** (neg-Y) |
| 7 | Network Traffic Frame | Yes | **Good** (curated) |
| 8 | Network Traffic Carrier | Yes | **Good** (curated) |
| 9 | Network Traffic Colls | Yes | **Good** (curated, neg-Y) |
| 10 | NF Conntrack | Yes | **Good** |
| 11 | ARP Entries | Yes | **Good** (curated) |
| 12 | MTU | Yes | **Good** (curated) |
| 13 | Speed | Yes | **Good** (curated) |
| 14 | Queue Length | Yes | **Good** (curated) |
| 15 | Softnet Packets | Yes | **Good** (neg-Y) |
| 16 | Softnet Out of Quota | Yes | **Good** |
| 17 | Network Operational Status | Yes | **Good** (curated long-form) |

---

## Network Operational Status

Grafana: `node_network_up{operstate="up"}` + `node_network_carrier` (two targets).  
Kibana curated: wide STATS then unpivot to `series_group = "{device} / {metric_label}"` with both **Operational state UP** and **Physical link state**. Live series include both labels across devices (false “2→1” from counting only the `value` column).

---

## Fixes

None required.

---

## Next section

**14 — Network Sockstat**
