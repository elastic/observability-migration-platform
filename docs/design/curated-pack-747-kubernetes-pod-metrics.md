# Curated Pack — Grafana 747 "Kubernetes Pod Metrics"

> Design + living discoveries for the 747 curated pack. Follows the general
> Curation Playbook in `curated-dashboard-packs-plan.md`. Stacks on 315/1621
> (cAdvisor) and 6417 (kube-state-metrics).

- Source: community **"Kubernetes Pod Metrics"**,
  <https://grafana.com/grafana/dashboards/747-pod-metrics/>
- gnetId **747**, latest revision **2**.
- canonical sha256 (rev 2) = `47837bfea31e6156decd5c152379addfd9c3f9a9991a0cc96d764f91388b2d55`.
- Datasource: Prometheus — curated-pack eligible.
- Schema: **v12 (`rows[]`)**. 18 leaf panels.
- Metric family: **cAdvisor** (`container_*` + `machine_*`) **and**
  **kube-state-metrics** (`kube_pod_info`, `kube_pod_status_phase`,
  `kube_pod_container_info`, `kube_pod_container_status_restarts`).
- Variables: `$Node` (hostname), `$Pod` (`label_values(kube_pod_info, pod)`),
  hidden `$Pod_ip` / `$phase` / `$container`.

## Goal

Ship a curated pack so 747 is a pod-scoped Kibana dashboard: working Pod/Node
controls, live IP/phase/container tiles instead of markdown `$var`
interpolation, and cAdvisor graphs that group by canonical `pod`.

## The core problem

| Dashboard convention | Modern reality | Pack handling |
|---|---|---|
| `pod_name` / `io_kubernetes_pod_name` | cAdvisor `pod` | `label_rewrites` + ES\|QL overrides |
| `kubernetes_io_hostname` | scrape `instance` | rewrite → `instance`; plugin populate |
| markdown `# $Pod_ip` (hidden var) | Kibana markdown does not interpolate controls | datatable of pod → IP; engine allows query_overrides on `text` panels |
| `kube_pod_container_status_restarts` | `*_total` counter | `metric_map` |
| `pod=~"$Pod.*$"` prefix | Grafana regex interpolation | exact `MV_CONTAINS` plus `pod IS NOT NULL` (root cgroup must not leak in) |
| CPU Total `format=bytes` + node-wide container rate | not capacity, not bytes | Kibana title **Node CPU** |
| `$Pod` single-select + All | hydrates to first pod | multi-select so first paint is All |

## Engine vs pack split

Pipeline already handles rate/gauge, `rows[]` → sections, hidden-variable
skip, and `^$Pod$` exact matchers. The pack carries the Heapster label
bridge, text→ES|QL overrides (skip the markdown early-return
when a curated `esql_query` is present), Node/Pod multi-select, Received/Sent
names, named Used/Total tiles, and `pod IS NOT NULL` so MV_CONTAINS cannot
pull in the root cgroup.

## Fidelity

- **PERFECT**: IP/status/container tiles, restarts, network pressure, pod
  graphs, Memory/CPU used, Node CPU, working-set tile.
- **APPROXIMATE**: pod/node % ratios (cross-metric); All-processes (cgroup id).

## Validation

Shared curated rig `k8s_exporter.py` emits `pod_ip` on `kube_pod_info` and
`kube_pod_container_info` into `metrics-k8s.prometheus-default`.
