# Section audit 15 — Network Netstat

**Dashboard:** Grafana community 1860 *Node Exporter Full* → Kibana `obs-migrate-node-exporter-full`  
**Source JSON:** `/tmp/node-exporter-input.ClIz2R/node-exporter-full.json`  
**Live data:** ES `http://localhost:9201`, index `metrics-node.prometheus-default`, controls `job=node_exporter`, `node=node:9100`, range `now-15m`  
**Evidence dump:** `15-network-netstat.validation.json`

---

## Section verdict

| # | Panel | Live | Fidelity |
|---|-------|------|----------|
| 1 | Netstat IP In / Out Octets | Yes | **Good** (neg-Y out) |
| 2 | Netstat IP Forwarding | Yes | **Good** |
| 3 | ICMP In / Out | Yes | **Good** (neg-Y) |
| 4 | ICMP Errors | Yes | **Good** |
| 5 | UDP In / Out | Yes | **Good** (neg-Y) |
| 6 | UDP Errors | Yes | **Good** (5 series) |
| 7 | TCP In / Out | Yes | **Good** (neg-Y) |
| 8 | TCP Errors | Yes | **Good** (8/8 after lab seed + pack) |
| 9 | TCP Connections | Yes | **Good** |
| 10 | TCP SynCookie | Yes | **Good** (neg-Y) |
| 11 | TCP Direct Transition | Yes | **Good** |
| 12 | TCP Stat | Yes | **Good** (4 series) |

---

## TCP Errors (curated / optional metric)

Grafana has **8** irate targets including `node_netstat_TcpExt_TCPRcvQDrop`.

**Update 2026-08-07:** Lab host seeded the counter + pack override restored the
eighth series. Remigrate is **117 Green / 0 Yellow**. On hosts where the field
is still absent, curated TCP Errors **omits** `TCPRcvQDrop` via
`live_optional_metrics` + field-caps-aware override materialization, so the
other seven series still migrate without requiring lab seeding.

---

## Fixes

- Pack: include `TCPRcvQDrop` in curated TCP Errors when present; omit when
  field-caps prove it absent (`live_optional_metrics`).
- Lab seed: `metrics.node_netstat_TcpExt_TCPRcvQDrop` (+ HardwareCorrupted / MaxConn for other sections).

---

## Next section

**16 — Node Exporter**
