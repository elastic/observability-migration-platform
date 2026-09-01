# Curated Pack — Grafana 6417 "Kubernetes Cluster (Prometheus)"

> Design + living discoveries for the 6417 curated pack. Follows the general
> Curation Playbook in `curated-dashboard-packs-plan.md`. Second of two stacked
> Kubernetes packs; stacks on the 315 (cAdvisor) pack.

- Source: community **"Kubernetes Cluster (Prometheus)"**,
  <https://grafana.com/grafana/dashboards/6417-kubernetes-cluster-prometheus/>
- gnetId **6417**, only revision **1**.
- canonical sha256 (rev 1) = `6694907c373d7bb0a24143a171f2751cc939f4e8da3fefb04b25b8a59aaca0ab`.
- Datasource: Prometheus — curated-pack eligible.
- Schema: v16. 34 items (6 rows + ~27 leaf panels): mostly singlestats plus 4
  capacity graphs and 1 deployment table.
- Metric family: **kube-state-metrics** (`kube_*`) + **node_exporter**
  (`node_filesystem_*`).
- Variables: `$datasource` (picker, dropped), `$node`/`$namespace`
  (`type=constant`, value `.*`).

## Goal

Ship a curated pack so 6417 renders in Kibana against a modern kube-state-metrics
+ node_exporter scrape in the Elastic `prometheus_native` layout, bridging the
old KSM/node_exporter names and the per-resource → `resource=`-label reshape.

## The core problem: old KSM + node_exporter lineage

| Dashboard shape | Modern reality | Pack handling |
|---|---|---|
| `node_filesystem_size` / `node_filesystem_free` | `*_bytes` (node_exporter ≥ 0.16) | `metric_map` rename |
| `kube_pod_container_status_restarts` (gauge under `delta`) | `*_total` (counter) | `metric_map` + `metric_kinds: counter` |
| `kube_node_status_allocatable_pods` / `_cpu_cores` / `_memory_bytes` | one metric `kube_node_status_allocatable{resource="pods\|cpu\|memory"}` | **`query_overrides`** (name+label reshape — not a rename) |
| `kube_pod_container_resource_requests_cpu_cores` / `_memory_bytes` | `kube_pod_container_resource_requests{resource=...}` | `query_overrides` |
| `kube_node_status_condition{condition="OutOfDisk"}` | removed in k8s 1.12 | honest **GAP** (empty) |
| `node_filesystem_*{nodename=...}` | `nodename` not a standard fs label | `ignored_labels` (drop matcher) |
| `{{ deployment }}` legend with no `by()` on the table | collapses to one MAX line | `query_overrides` group by `labels.deployment` |
| `$node` / `$namespace` = const `.*` | — | engine synthesizes optional node/namespace controls |

## Engine vs pack split

The pipeline already handles gauge sum, counter `delta()`, the Disk `size-free`
binary ratio, `rows` → sections, singlestat reducers, and turning the constant
`$node`/`$namespace` into bound controls. The pack carries:

- **`metric_map`** — the three pure renames (`node_filesystem_size/free` →
  `*_bytes`, `restarts` → `restarts_total`).
- **`metric_kinds`** — `restarts_total` counter; all the KSM status/count series
  and node_filesystem sizes as gauges (pre-map names classified too, so the
  offline path does not warn about counter typing).
- **`query_overrides`** — the four cluster-health ratios and three capacity
  graphs reshaped to the `resource=`-label KSM metric; the deployment table
  grouped by `labels.deployment`.
- **`ignored_labels`** — `nodename`.

No engine changes required.

## Fidelity

- **PERFECT**: the count/status singlestats — pods by phase, containers
  running/waiting/terminated, restarts (30m delta), deployment replicas,
  jobs, nodes, requested cpu/memory.
- **APPROXIMATE**: the four cluster-health ratios + four capacity graphs
  (resource-split reshape; PERFECT under native PROMQL) and the Disk panels
  (nodename dropped, same-bucket ratio) and the deployment table.
- **GAP**: Nodes Out of Disk (`OutOfDisk` condition removed in k8s 1.12).

Source quirk preserved: the dashboard ships two tiles titled "Jobs Succeeded";
the second actually queries `kube_job_status_active`. Left as-is (a source bug),
noted in `fidelity_manifest.yaml`.

## Validation gates (UI testing)

Live validation uses the shared curated rig extended with a synthetic
kube-state-metrics + node_exporter source emitting the modern shape
(`kube_node_status_allocatable{resource=...}`, `kube_pod_container_resource_requests{resource=...}`,
`kube_deployment_*`, `kube_job_*`, `kube_pod_status_phase`,
`kube_node_spec_unschedulable`, `node_filesystem_*_bytes`).

1. Migrate + upload to Kibana (`prometheus_native`).
2. Render audit — 0 `render_error`; document the OutOfDisk gap.
3. Interaction audit — `node` / `namespace` controls rewrite panel queries.
4. Side-by-side vs the provisioned Grafana 6417.

## Task checklist

- [x] registry.yaml entry (6417, rev 1, sha above)
- [x] pack.yaml + fidelity_manifest.yaml
- [x] offline fixture tests
- [ ] rig: synthetic KSM + node_exporter source (resource-split shape)
- [ ] live: migrate + upload + render/interaction audit + side-by-side
- [ ] docs: discoveries here + `docs/sources/grafana.md`

## Discoveries

- Offline translation: 27 panels, 0 not_feasible; the resource-split
  `query_overrides` land cleanly (`SUM(CASE(labels.resource == "cpu", …))`),
  the constant `$node`/`$namespace` became bound `?node`/`?namespace` controls,
  `Containers Restarts` became `SUM(DELTA(TO_DOUBLE(metrics.kube_pod_container_status_restarts_total), 30m))`,
  and `Nodes Out of Disk` keeps a `labels.condition == "OutOfDisk"` filter that
  resolves to empty (honest gap).
- The deployment table needed an explicit `BY labels.deployment` override; the
  source legend `{{ deployment }}` alone did not carry a grouping key.
