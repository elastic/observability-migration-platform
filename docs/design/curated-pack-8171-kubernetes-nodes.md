# Curated Pack — Grafana 8171 "Kubernetes Nodes"

> Design + living discoveries for the 8171 curated pack. Follows the general
> Curation Playbook in `curated-dashboard-packs-plan.md`. Stacks on the
> Kubernetes 315 / 6417 / 741 packs. Live-validated against the rig's real
> `node_exporter` scrape (`metrics-node.prometheus-default`).

- Source: community **"Kubernetes Nodes"**,
  <https://grafana.com/grafana/dashboards/8171-kubernetes-nodes/>
- gnetId **8171**, only revision **1**.
- canonical sha256 (rev 1) = `5d580022bf35bc2cbe42056affd0a1670aa2adb0afd6942f2e642c550a6b29b2`.
- Datasource: Prometheus — curated-pack eligible.
- Schema: v16. 8 leaf panels. `$server` =
  `label_values(node_boot_time_seconds, instance)`.
- Metric family: **node_exporter 0.16+** (`node_cpu_seconds_total`,
  `node_memory_*_bytes`, `node_load*`, `node_filesystem_*_bytes`,
  `node_network_*`, `node_disk_*`).

## Goal

Ship a curated pack so 8171 is as good as Grafana on a modern node_exporter
scrape, and strictly better on two source bugs (Idle CPU title, nfsd Disk I/O).

## Source bugs repaired

| Grafana | Kibana (pack) |
|---|---|
| Panel titled "Idle CPU" computes `100 - idle` = **busy** % by cpu | Same formula, legend `Busy`, layout title **CPU Busy** |
| Disk I/O `rate(node_nfsd_disk_bytes_{read,written}_total)` (NFS *server*) | `metric_map` → `node_disk_read_bytes_total` / `node_disk_written_bytes_total`; series named Read / Written / IO time |

Two panels share the title "Memory Usage" (stacked graph + percent
singlestat). Pack does **not** override by title — the engine already
translates each from panel type.

## Engine vs pack split

Pipeline already handles irate/rate, memory used subtraction, stacked graph,
`instance="$server"` parameterization, `device!~"lo"` / `device!="rootfs"`.
The pack pins metric_kinds + instance/cpu/device labels, maps nfsd → disk,
and emits the CPU Busy / Disk I/O ES|QL overrides.

## Fidelity

- **PERFECT**: System Load, Memory Usage (both), Disk Space Usage, Network
  Received/Transmitted.
- **APPROXIMATE**: CPU Busy (title repair), Disk I/O (real disk vs nfsd).

## Validation

Rig `node_exporter` already scraped into `metrics-node.prometheus-default`.
No producer change required for 8171.
