# Curated Pack — Grafana 1621 "Kubernetes cluster monitoring (via Prometheus)"

> Design + living discoveries for the 1621 curated pack. Follows the general
> Curation Playbook in `curated-dashboard-packs-plan.md`. Stacks on 315
> (same dashboard family; 1621 is the all-partitions filesystem fork).

- Source: community **"Kubernetes cluster monitoring (via Prometheus)"**,
  <https://grafana.com/grafana/dashboards/1621>
- gnetId **1621**, only revision **1**.
- canonical sha256 (rev 1) = `d714551536ca794e3088ac535e59b3d2f93dd705e3b60f84d6f3c42da40fe9d4`.
- Datasource: Prometheus — curated-pack eligible.
- Schema: **v12 (`rows[]`)**. 21 panels across 13 rows (same layout as 315).
- Metric family: **cAdvisor** (`container_*` + `machine_*`).
- Variables: `$Node` = `label_values(kubernetes_io_hostname)` (includeAll `.*`).

## Goal

Ship a curated pack so 1621 renders in Kibana against a modern cAdvisor scrape
with **every `/dev/*` filesystem partition aggregated** (the reason this
dashboard exists vs 315) and a working `$Node` control via `instance`.

## 1621 vs 315

| | 315 | 1621 |
|---|---|---|
| Cluster filesystem device matcher | `^/dev/[sv]d[a-z][1-9]$` | `^/dev/.*$` |
| `$Node` | dropped (ignored hostname) | rewritten to `instance`; multi-select populate from `machine_cpu_cores` |
| Title | identical | identical — 1621 is **gnetId-only** in the registry |

## Engine vs pack split

Same split as 315 for the pre-1.16 label bridge, k8s-only container series,
Received/Sent names, cgroup-id All-processes, and systemd honest empty. Pack
adds filesystem per-device `LAST_OVER_TIME` then `SUM`, Node bind on every
live panel, and a 48-col named Memory/CPU/Filesystem KPI strip.

## Fidelity

- **PERFECT**: Network I/O pressure; filesystem used/total/% (all `/dev/*`);
  per-pod CPU/memory/network.
- **APPROXIMATE**: cluster memory/CPU ratios (need `machine_*` + `id="/"`);
  Containers (docker/rkt dropped); All-processes (cgroup id).
- **GAP**: System services (`systemd_service_name`).

## Validation

Shared curated rig `k8s_exporter.py` now emits two root-cgroup devices
(`/dev/sda1`, `/dev/nvme0n1p1`) into
`metrics-k8s.prometheus-default`. 315's regex matches only sda1; 1621 sums
both.
