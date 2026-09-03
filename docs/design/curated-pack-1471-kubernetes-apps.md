# Curated Pack — Grafana 1471 "Kubernetes App Metrics"

> Design + living discoveries for the 1471 curated pack. Follows the general
> Curation Playbook in `curated-dashboard-packs-plan.md`. Stacks on 315
> (cAdvisor) and 741 (Deployment metrics).

- Source: community **"Kubernetes App Metrics"**,
  <https://grafana.com/grafana/dashboards/1471-kubernetes-apps/>
- gnetId **1471**, only revision **1**.
- canonical sha256 (rev 1) = `27552f4c9ba5ce3e43ae7c962c98c6979e87e1e4c2f4d463f26df62652cabc52`.
- Datasource: Prometheus — curated-pack eligible.
- Schema: **v14 (`rows[]`)**. 13 leaf panels.
- Metric family: **cAdvisor** (`container_*`, `container_spec_*`) **and**
  app HTTP (`http_requests_total`, `nginx_http_*`, `haproxy_backend_http_*`).
- Variables: `$namespace` =
  `label_values(container_memory_usage_bytes{…container_name!="POD"}, namespace)`;
  `$container` =
  `label_values(…{namespace=~"$namespace",container_name!="POD"}, container_name)`.
  HTTP panels also filter `app="$container"` (app name == container name).

## Goal

Ship a curated pack so 1471 renders in Kibana against a modern cAdvisor scrape
plus optional native/nginx/haproxy HTTP metrics, with working namespace and
container controls.

## The core problem

| Dashboard convention | Modern reality | Pack handling |
|---|---|---|
| `container_name` / `pod_name` | cAdvisor `container` / `pod` | `label_rewrites` |
| `kubernetes_io_hostname` | scrape `instance` | rewrite → `instance` |
| HTTP `kubernetes_namespace` | `namespace` | rewrite → `namespace` |
| `app="$container"` | app label equals container name | HTTP ES\|QL binds `?container` to `app` |
| nginx grouped by `status`, native/haproxy by `code` | engine drops nginx | curated ES\|QL; per-doc `series` like `native \| 200` then `STATS rate BY time_bucket, series` so Lens matches Grafana legendFormat |
| `histogram_quantile(*_bucket)` | no classic-histogram field | `PERCENTILE` of duration gauge |
| per-pod `by (id, pod_name)` + limit/request lines | Lens XY one breakdown | group by `pod`; drop reference lines |

## Engine vs pack split

Pipeline already handles irate/rate, `rows[]` → sections, flattening
single-panel legacy rows, and control synthesis. The pack carries the Heapster
label bridge, populate rewrites, HTTP named series, ratio ES|QL, and
`LAST_OVER_TIME` gauges (raw `SUM` of gauge docs over-counts).

## Fidelity

- **PERFECT**: request/error rates, pod/host counts, usage-vs-request/limit,
  avg-per-pod and total CPU/memory.
- **APPROXIMATE**: response-time percentiles (PERCENTILE vs histogram_quantile);
  per-pod CPU/memory (reference lines dropped).

## Validation

Shared curated rig `k8s_exporter.py` emits cAdvisor spec/user/system + HTTP
series (`app` = container name) into `metrics-k8s.prometheus-default`.
