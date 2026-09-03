# Curated Pack — Grafana 3831 "Kubernetes Cluster Autoscaler (via Prometheus)"

> Design + living discoveries for the 3831 curated pack. Follows the general
> Curation Playbook in `curated-dashboard-packs-plan.md`. Stacks on the
> Kubernetes 315 / 6417 / 741 / 8171 packs.

- Source: community **"Kubernetes Cluster Autoscaler (via Prometheus)"**,
  <https://grafana.com/grafana/dashboards/3831-autoscaler/>
- gnetId **3831**, only revision **1**.
- canonical sha256 (rev 1) = `fbfc5ff33d138f2a449d4d28b0c0dcd09d351f08a2c78b91e3455d00e2b0b597`.
- Datasource: Prometheus — curated-pack eligible.
- Schema: **v14 (`rows[]`)**. 10 leaf panels. No template variables.
- Metric family: **cluster-autoscaler** `/metrics`
  (`cluster_autoscaler_nodes_count`, `*_last_activity`, `*_total` counters).

## Goal

Ship a curated pack so 3831 renders in Kibana against a cluster-autoscaler
scrape. Force ES|QL for every panel (native PROMQL `LAST(value, step)` is
empty in Lens on prometheus_native ingest) and polish KPI titles for Kibana
tiles.

## Engine vs pack split

Pipeline already handles `rows[]` → sections, flattening Grafana's placeholder
"New row", and singlestat reducers. The pack pins metric kinds, emits ES|QL
for `time()-last_activity` (`DATE_DIFF`), named Activity/Autoscaling series,
and the ready/total + scaled_up−scaled_down KPIs.

Grafana plots `*_total` counters with bare `sum()` (no `rate()`). Kibana
matches that with `LAST_OVER_TIME`, not `RATE`.

Grafana's placeholder row `"New row"` is flattened to top-level panels.
Those must sit at layout `y: 2` (after Info `y=0` and Activity `y=1`) or
they overlap the Info KPIs. Nodes available is `ready/total * 100` so the
Kibana number+% tile shows 100%, not 1.00%.

## Fidelity

- **PERFECT**: Total nodes, Nodes available, last-activity tiles, Pod/Node/
  Autoscaling activity graphs.
- **APPROXIMATE**: Safe to autoscale (0/1, no Yes/No value map), shortened
  Unscheduled pods / Net scaled nodes titles.

## Validation

Shared curated rig `k8s_exporter.py` emits `cluster_autoscaler_*` into
`metrics-k8s.prometheus-default`.
