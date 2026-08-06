# Node Exporter 1860 — section audits

Per-row Grafana → Kibana fidelity audits for community dashboard **1860**
(*Node Exporter Full*), migrated to Kibana saved object
`obs-migrate-node-exporter-full`.

**Lab target (validation host):** Kibana `localhost:5602`, ES
`localhost:9201`, index `metrics-node.prometheus-default`, profile
`prometheus_native`, controls `job=node_exporter`, `node=node:9100`.

**Source JSON:** `/tmp/node-exporter-input.ClIz2R/node-exporter-full.json`  
**Curated pack:**
`observability_migration/adapters/source/grafana/curated_packs/grafana_1860_node_exporter_full/pack.yaml`  
**Workplan:** [`../node-exporter-1860-curation-workplan.md`](../node-exporter-1860-curation-workplan.md)

Each section has a markdown report plus a `.validation.json` evidence dump
(Grafana exprs, Kibana ES|QL, live `_query` outcomes).

---

## Index

| # | Grafana row | Report | Verdict |
|---|-------------|--------|---------|
| 01 | Quick CPU / Mem / Disk | [01-quick-cpu-mem-disk.md](01-quick-cpu-mem-disk.md) | Good after Pressure / IRATE fixes |
| 02 | Basic CPU / Mem / Net / Disk | [02-basic-cpu-mem-net-disk.md](02-basic-cpu-mem-net-disk.md) | Good after Memory Basic overlay fix |
| 03 | CPU / Memory / Net / Disk | [03-cpu-memory-net-disk.md](03-cpu-memory-net-disk.md) | Good; Hardware Corrupted = field gap |
| 04 | Memory Meminfo | [04-memory-meminfo.md](04-memory-meminfo.md) | Clean |
| 05 | Memory Vmstat | [05-memory-vmstat.md](05-memory-vmstat.md) | Clean |
| 06 | System Timesync | [06-system-timesync.md](06-system-timesync.md) | Clean |
| 07 | System Processes | [07-system-processes.md](07-system-processes.md) | Good; Processes Memory curated repair |
| 08 | System Misc | [08-system-misc.md](08-system-misc.md) | Good after per-CPU Frequency fix |
| 09 | Hardware Misc | [09-hardware-misc.md](09-hardware-misc.md) | Clean |
| 10 | Systemd | [10-systemd.md](10-systemd.md) | Clean |
| 11 | Storage Disk | [11-storage-disk.md](11-storage-disk.md) | Clean (`diskdevices` control) |
| 12 | Storage Filesystem | [12-storage-filesystem.md](12-storage-filesystem.md) | Clean |
| 13 | Network Traffic | [13-network-traffic.md](13-network-traffic.md) | Clean |
| 14 | Network Sockstat | [14-network-sockstat.md](14-network-sockstat.md) | Clean |
| 15 | Network Netstat | [15-network-netstat.md](15-network-netstat.md) | Good; TCPRcvQDrop optional/field gap |
| 16 | Node Exporter | [16-node-exporter.md](16-node-exporter.md) | Clean |

---

## Fixes landed during audits

| Panel / area | Change |
|--------------|--------|
| Pressure | Penultimate non-null IRATE collapse; color domain when `* 100` |
| Memory Basic | RAM Total unstacked overlay; series-override axes on curated path |
| Duplicate `_gauge_*` EVAL | Skip constants already present in query |
| IRATE short windows | Pack `TBUCKET(100)` → `TBUCKET(20)` for selected panels |
| CPU Frequency Scaling | Per-`labels.cpu` series + single Max/Min |

---

## Remaining work (see workplan)

1. ~~**Phase 4** — seed the three live-optional metrics~~ **Done on lab host**
   (2026-08-07): HardwareCorrupted / TCPRcvQDrop / MaxConn → **117 Green / 0 Yellow**.
2. ~~**Phase 3** — native multi-target `PROMQL`~~ **Blocked** — decision:
   [`../node-exporter-1860-phase3-native-promql.md`](../node-exporter-1860-phase3-native-promql.md).
3. ~~**Phase 5** — canonical migrate+upload+smoke~~ **Done** —
   [`../node-exporter-1860-phase5-verification.md`](../node-exporter-1860-phase5-verification.md)
   (`/tmp/node-exporter-phase5-20260807-030538`).

Curation workplan lab DoD is met aside from deferred Phase 3 / control chaining.
