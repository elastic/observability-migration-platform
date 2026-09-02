# Curated Pack — Grafana 315 "Kubernetes cluster monitoring (via Prometheus)"

> Design + living discoveries for the 315 curated pack. Follows the general
> Curation Playbook in `curated-dashboard-packs-plan.md`. First of two stacked
> Kubernetes packs (315 cAdvisor, then 6417 kube-state-metrics).

- Source: community **"Kubernetes cluster monitoring (via Prometheus)"**,
  <https://grafana.com/grafana/dashboards/315-kubernetes-cluster-monitoring-via-prometheus/>
- gnetId **315**, latest revision **3**.
- canonical sha256 (rev 3) = `6fb5e045bc6d860f0f22ce7e145f4da04d2e25fd4fbf9fda29355cef6d63aeae`.
- Datasource: Prometheus — curated-pack eligible.
- Schema: **v12 (old `rows[]` layout)**. 21 panels across 13 rows.
- Metric family: **cAdvisor** (`container_*`) + **machine_** (`machine_cpu_cores`,
  `machine_memory_bytes`). NOT kube-state-metrics.
- Variables: `$Node` = `label_values(kubernetes_io_hostname)`.

## Goal

Ship a curated pack so 315 renders in Kibana against a modern cAdvisor scrape
ingested in the Elastic `prometheus_native` layout (`metrics.* + labels.*`),
bridging the pre-1.16 cAdvisor label conventions the dashboard was authored
against, and degrading honestly where the source series no longer exist.

## The core problem: a pre-1.16 cAdvisor dashboard

Unlike 12485 (an old dashboard mapped onto a *modern exporter the rig runs*),
315's PromQL is written against label/runtime conventions that modern Kubernetes
and the Elastic cAdvisor integration no longer emit:

| Dashboard convention | Modern reality | Pack handling |
|---|---|---|
| `pod_name` / `container_name` grouping | cAdvisor is `pod` / `container` | `label_rewrites` |
| `kubernetes_io_hostname=~"^$Node$"` | relabel gone; container series carry no node label | `ignored_labels` (drop matcher); `$Node` control is inert → dropped |
| `image!=""`, `name=~"^k8s_.*"` | not emitted | `ignored_labels` (drop matcher) so the good panels are not filtered to empty |
| `name!~"^k8s_.*"` (docker), `rkt_container_name` | obsolete runtimes | **dropped as honest gaps** via curated `query_overrides` with `approximation_note` |
| `systemd_service_name` | pre-labelmap convention | NOT ignored → panel degrades to an honest empty (never a fake single aggregate) |
| `machine_cpu_cores`, `machine_memory_bytes`, `container_fs_*`, `id="/"` | present only on a full cAdvisor + machine scrape | cluster-KPI panels are `APPROXIMATE`; render when that telemetry exists |

## Engine vs pack split

The general pipeline already handles rate() counters, gauge sum, the
unary-minus **butterfly** net-I/O panels, `rows[]` → Kibana sections, singlestat
reducers, and control synthesis. The pack carries only:

- **`metric_kinds`** — cAdvisor counters (`container_cpu_usage_seconds_total`,
  `container_network_{receive,transmit}_bytes_total`) vs gauges
  (`container_memory_working_set_bytes`, `container_fs_*`, `machine_*`).
- **`label_rewrites`** — `pod_name`→`labels.pod`, `container_name`→`labels.container`,
  `id`→`labels.id`.
- **`ignored_labels`** — `kubernetes_io_hostname`, `image`, `name`. These appear
  only as filters on the good container panels; dropping them lets those panels
  resolve instead of filtering to empty. `systemd_service_name` /
  `rkt_container_name` are deliberately NOT ignored so their panels degrade to
  an honest empty rather than collapse into one misleading aggregate line.
- **`query_overrides`** — the three multi-runtime panels (Containers CPU / memory
  / network) keep only the k8s pod/container series and disclose the dropped
  docker/rkt runtimes via `approximation_note` (status capped at
  `migrated_with_warnings`, never a clean `migrated`); Pods CPU/memory keep the
  Lens breakdown on `labels.pod`; Pods/All-processes network name Received/Sent
  (native butterfly otherwise labels transmit `Value B`); All-processes panels
  group by `labels.id` excluding the root cgroup.

No engine changes required — the pack reuses the APIs 12485 added.

## Fidelity

- **PERFECT** (render on any modern cAdvisor scrape after the label bridge):
  Network I/O pressure, Pods CPU / memory / network (Received/Sent series names).
- **APPROXIMATE**: Containers CPU / memory / network (docker/rkt series dropped);
  the cluster-KPI strip and Used/Total stats (require `machine_*` +
  `container_fs_*` + the root-cgroup `id="/"` series); All-processes panels
  (grouped by the cAdvisor cgroup `id`, excluding `id="/"`; the producer must
  emit a per-container cgroup `id` or the legend is a single `(null)`).
- **GAP** (honest empty on modern data): System services CPU / memory
  (`systemd_service_name` is a dead convention).

See `fidelity_manifest.yaml` for the per-panel table.

## Validation gates (UI testing)

Live validation uses the shared curated rig (`parity-rig/curated/…`) extended
with a synthetic cAdvisor exporter (`container_*` + `machine_*` + `container_fs_*`
with modern `pod`/`container` labels, a root-cgroup `id="/"` series, and a
per-container cgroup `id` so All-processes legends are not `(null)`) so the
per-pod panels and the cluster KPIs both have real data.

1. Migrate + upload to Kibana (`prometheus_native`, `--esql-index` = data view).
2. Render audit — 0 `render_error`; document `field_gap`/`data_gap` for the
   `systemd`/all-processes gap panels in `fidelity_manifest.yaml`.
3. Side-by-side vs the provisioned Grafana 315.

## Task checklist

- [x] registry.yaml entry (315, rev 3, sha above)
- [x] pack.yaml + fidelity_manifest.yaml
- [x] offline fixture tests (`tests/test_curated_packs.py`)
- [x] rig: synthetic cAdvisor exporter (`machine_*`, `container_fs_*`, `id="/"`, per-container cgroup `id`)
- [x] live: migrate + upload + render audit + view-mode UI pass
- [x] docs: discoveries here + `docs/sources/grafana.md`

## Discoveries

- Offline translation confirms the label bridge: `Pods CPU usage` resolves to
  `SUM(RATE(container_cpu_usage_seconds_total)) BY time_bucket, labels.pod`
  with the `image`/`name`/`kubernetes_io_hostname` matchers stripped; the
  Containers panels take the curated k8s-only ES|QL; `System services` uses an
  impossible-filter override so Lens does not error on a missing
  `systemd_service_name` column (honest empty).
- The cluster-KPI panels translate to a same-bucket ratio referencing
  `machine_*` and the `id="/"` root cgroup — so live rendering needs those
  series in the scrape (they are absent from a plain container-only ingest).
- Engine butterfly + a pod breakdown names transmit `Value B`; Pods network
  I/O needs an explicit Received/Sent override (same class as Containers
  network).
- All-processes panels group by cgroup `id`. A producer that only sets
  `id="/"` on the root cgroup yields a single `(null)` legend; emit
  `/kubepods/<pod>/<container>` on workload series.
