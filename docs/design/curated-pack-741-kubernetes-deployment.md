# Curated Pack — Grafana 741 "Kubernetes Deployment metrics"

> Design + living discoveries for the 741 curated pack. Follows the general
> Curation Playbook in `curated-dashboard-packs-plan.md`. Stacks on 315
> (cAdvisor) and 6417 (kube-state-metrics).

- Source: community **"Kubernetes Deployment metrics"**,
  <https://grafana.com/grafana/dashboards/741-deployment-metrics/>
- gnetId **741**, only revision **1**.
- canonical sha256 (rev 1) = `ee383512d9482a9917c5d5777bde332a91f99972f0168c0a794b20adcaae108c`.
- Datasource: Prometheus — curated-pack eligible.
- Schema: **v12 (`rows[]`)**. 16 leaf panels.
- Metric family: **cAdvisor** (`container_*` + `machine_*`) **and**
  **kube-state-metrics** (`kube_deployment_status_replicas*`).
- Variables: `$Deployment` = `label_values(deployment)` (includeAll `.*`);
  `$Node` = `label_values(kubernetes_io_hostname)` (includeAll `.*`).

## Goal

Ship a curated pack so 741 renders in Kibana against a modern cAdvisor + KSM
scrape, bridging Heapster-era labels, binding Grafana's pod-name prefix
filter, and making `$Node` actually work via `labels.instance`.

## The core problem

| Dashboard convention | Modern reality | Pack handling |
|---|---|---|
| `pod_name` / `io_kubernetes_pod_name` grouping | cAdvisor `pod` | `label_rewrites` + ES\|QL overrides |
| `io_kubernetes_container_name` | `container` | `label_rewrites` |
| `pod_name=~"^$Deployment.*$"` | not a full-value matcher | curated `STARTS_WITH(labels.pod, ?Deployment)` (`.*` All skips the prefix) |
| `label_values(deployment)` | no metric anchor | plugin → `label_values(kube_deployment_status_replicas, deployment)` |
| `label_values(kubernetes_io_hostname)` | label gone | plugin → `label_values(machine_cpu_cores, instance)`; rewrite hostname → `labels.instance` |
| docker / rkt targets | obsolete | dropped (`approximation_note`) |
| three `Total` / two `Used` in one row | same title | `panel_id` overrides + Kibana titles Memory/CPU/Replicas used/total |

## Engine vs pack split

Pipeline already handles rate/gauge, `rows[]` → sections, and `^$Deployment$`
exact matchers (replica tiles). The pack carries the Heapster label bridge,
the prefix bind, k8s-only Containers CPU, Received/Sent network names,
cgroup-id All-processes, and `panel_id` for same-section duplicate titles.

## Fidelity

- **PERFECT**: replica KPIs; per-pod Deployment CPU/memory/network graphs.
- **APPROXIMATE**: KPI ratios (cross-metric); Used/Total tiles; Containers CPU
  (docker/rkt dropped); All-processes (cgroup id).

## Validation

Shared curated rig `k8s_exporter.py` (pod names `{deployment}-{ordinal}` plus
`kube_deployment_status_replicas_available`) ingested as
`metrics-k8s.prometheus-default`.
