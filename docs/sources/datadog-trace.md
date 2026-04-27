# Datadog Pipeline Trace

> **Auto-generated.** Regenerate with:
>
> ```bash
> python scripts/audit_pipeline.py --update-docs
> python scripts/audit_pipeline.py --update-docs --source datadog   # Datadog only
> ```
>
> Static narrative lives in `docs/sources/datadog-trace.tpl.md`.
> See also: [Datadog Adapter](datadog.md) | [Shared Pipeline Overview](../pipeline-trace.md) | [Grafana Trace](grafana-trace.md)

This document traces every Datadog dashboard in `infra/datadog/dashboards/`
through the migration pipeline, showing source metric/log/formula queries,
each translation step, the emitted Kibana ES|QL, and a semantic verdict.

---

## Translation Paths

The Datadog adapter translates per query type within each widget:

- **Metric queries** — `avg:system.cpu.user{host:web-*}` is parsed into
  metric name, aggregation, scope filters, and group-by tags. Each component
  is mapped through a field profile (`otel`, `prometheus`, `elastic_agent`, or
  custom) and rendered as an ES|QL `STATS … BY` query with `WHERE` filters.
- **Log queries** — Datadog log search DSL is parsed by a Lark grammar into
  an AST, then rendered as ES|QL `WHERE` clauses (or KQL bridge filters for
  complex boolean composition).
- **Formula queries** — arithmetic expressions over lettered query references
  (`a + b`, `a / b * 100`) are inlined as ES|QL `EVAL` expressions.
- **Change queries** — `change()` / `diff()` are approximated with delta
  calculations over the observed time bucket.

### Field Mapping

Datadog tags are mapped to Elasticsearch fields through profiles:

| Profile | Example mapping |
|---------|----------------|
| `otel` | `host` → `host.name`, `env` → `deployment.environment`, `service` → `service.name` |
| `prometheus` | `host` → `instance`, `env` → `deployment.environment`, metrics prefixed with `prometheus.metrics.` |
| `elastic_agent` | Tags map to Elastic Agent integration fields |
| `passthrough` | Keep Datadog tag names as-is |

### Template Variables → Controls

Datadog `template_variables` are translated into Kibana dashboard controls.
Each variable's `tag` is resolved through the active field profile to an
Elasticsearch field. The controls apply dashboard-level filtering, replacing
the `$var` LIKE-broadening in individual panel queries.

---

## Dashboard Summary

<!-- GENERATED:DASHBOARD_SUMMARY -->
| Source | Dashboard | Panels | Migrated | Warnings | Manual | Not Feasible | Skipped |
|--------|-----------|--------|----------|----------|--------|--------------|---------|
| datadog | Docker - Overview | 28 | 6 | 19 | 1 | 2 | 0 |
| datadog | Kubernetes - Overview | 57 | 2 | 39 | 2 | 4 | 10 |
| datadog | NGINX - Overview | 27 | 12 | 4 | 0 | 5 | 6 |
| datadog | Postgres - Metrics | 9 | 0 | 9 | 0 | 0 | 0 |
| datadog | Redis - Overview | 43 | 7 | 27 | 0 | 2 | 7 |
| datadog | System Overview - Sample | 11 | 8 | 2 | 0 | 1 | 0 |

**6 dashboards, 175 panels** audited from `infra/datadog/dashboards/`.
<!-- /GENERATED:DASHBOARD_SUMMARY -->

<!-- GENERATED:VERDICT_SUMMARY -->
## Verdict Summary

| Verdict | Count | Meaning |
|---------|-------|---------|
| **CORRECT** | 61 | Translation is semantically accurate |
| **MINOR_ISSUE** | 2 | Translated with approximations — review recommended |
| **EXPECTED_LIMITATION** | 112 | Known unsupported feature — placeholder or skip |
<!-- /GENERATED:VERDICT_SUMMARY -->

<!-- GENERATED:WARNING_PATTERNS -->
## Top Warning Patterns

| Count | Warning |
|------:|---------|
| 103 | Template variable filters applied via Kibana dashboard controls |
| 1 | query syntax not recognized; manual review needed |
| 1 | Data source 'events' has no direct Kibana equivalent; panel will be a placeholder |
| 1 | Data source 'event_stream' has no direct Kibana equivalent; panel will be a placeholder |
| 1 | as_count semantics are approximated for non-count metrics |
| 1 | change calculation is approximated |
| 1 | translation error: metric widget with grouped query needs a reducing formula |
| 1 | translation error: formula syntax not recognized: top(query1 / 1000, 10, 'mean', 'desc') |
| 1 | translation error: unsupported formula function: diff |
| 1 | rollup interval is approximated in ES\|QL |
<!-- /GENERATED:WARNING_PATTERNS -->

---

## Per-Dashboard Traces

<!-- GENERATED:PER_DASHBOARD_TRACES -->
### Datadog: Docker - Overview

**File:** `docker.json` — **Panels:** 28

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Running containers by image | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | sum:docker.containers.running{$scope} by {docker_image}.fill(0) | — |
| Most RAM-intensive containers | `toplist` → `table` | warning | **EXPECTED_LIMITATION** | top(avg:docker.mem.rss{$scope} by {container_name}, 5, 'max', 'desc') | — |
| Most CPU-intensive containers | `toplist` → `table` | warning | **EXPECTED_LIMITATION** | top(avg:docker.cpu.user{$scope} by {container_name}, 5, 'max', 'desc') | — |
| Memory by container | `heatmap` → `heatmap` | warning | **CORRECT** | avg:docker.mem.rss{$scope} by {container_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Running containers | `query_value` → `metric` | warning | **EXPECTED_LIMITATION** | sum:docker.containers.running{$scope} | — |
| Stopped containers | `query_value` → `metric` | warning | **EXPECTED_LIMITATION** | sum:docker.containers.stopped{$scope} | — |
| CPU by container | `heatmap` → `heatmap` | warning | **CORRECT** | avg:docker.cpu.user{$scope} by {container_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| CPU user by image | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | avg:docker.cpu.user{$scope} by {docker_image}.fill(0) | — |
| RSS memory by image | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | avg:docker.mem.rss{$scope} by {docker_image}.fill(0) | — |
| 9 | `event_stream` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | — | — |
| 10 | `event_timeline` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | — | — |
| Running container change | `query_value` → `metric` | requires_manual | **EXPECTED_LIMITATION** | 100*(sum:docker.containers.running{$scope}/timeshift(sum:docker.containers.runni... | — |
| CPU system by image | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | avg:docker.cpu.system{$scope} by {docker_image}.fill(0) | — |
| 13 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Cache memory by image | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | sum:docker.mem.cache{$scope} by {docker_image} | — |
| 15 | `image` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 16 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 17 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Swap by image | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | avg:docker.mem.swap{$scope} by {docker_image} | — |
| 19 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Avg. I/O bytes read by image | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | avg:docker.io.read_bytes{$scope} by {docker_image} | — |
| Avg. I/O bytes written by image | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | avg:docker.io.write_bytes{$scope} by {docker_image} | — |
| 22 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Avg. rx bytes by image | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | avg:docker.net.bytes_rcvd{$scope} by {docker_image} | — |
| Avg. tx bytes by image | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | avg:docker.net.bytes_sent{$scope} by {docker_image} | — |
| Most tx-intensive containers | `toplist` → `table` | warning | **EXPECTED_LIMITATION** | top(avg:docker.net.bytes_sent{$scope} by {container_name}, 5, 'max', 'desc') | — |
| tx by container | `heatmap` → `heatmap` | warning | **CORRECT** | avg:docker.net.bytes_sent{$scope} by {container_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Running containers by image | `toplist` → `table` | warning | **EXPECTED_LIMITATION** | top(timeshift(sum:docker.containers.running{$scope} by {docker_image}.fill(60), ... | — |

<details>
<summary>Detailed traces (28 panels)</summary>

#### Running containers by image

**Translation path:** `lens` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:docker.containers.running{$scope} by {docker_image}.fill(0)
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected lens XY panel
- `translate_lens` / `datadog.translate.lens_single_query` → translated Lens metric widget

**Plan:**

- Backend: `lens`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → lens XY panel

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** EXPECTED_LIMITATION

#### Most RAM-intensive containers

**Translation path:** `lens` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (toplist):**

```
top(avg:docker.mem.rss{$scope} by {container_name}, 5, 'max', 'desc')
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_toplist` → selected lens toplist table
- `translate_lens` / `datadog.translate.lens_single_query` → translated Lens metric widget

**Plan:**

- Backend: `lens`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: top list → lens table with ORDER BY + LIMIT

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** EXPECTED_LIMITATION

#### Most CPU-intensive containers

**Translation path:** `lens` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (toplist):**

```
top(avg:docker.cpu.user{$scope} by {container_name}, 5, 'max', 'desc')
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_toplist` → selected lens toplist table
- `translate_lens` / `datadog.translate.lens_single_query` → translated Lens metric widget

**Plan:**

- Backend: `lens`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: top list → lens table with ORDER BY + LIMIT

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** EXPECTED_LIMITATION

#### Memory by container

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (heatmap):**

```
avg:docker.mem.rss{$scope} by {container_name}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_heatmap_distribution` → selected ES|QL for heatmap
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (heatmap):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS value = AVG(docker_mem_rss) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), container.name
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `heatmap`
- Data source: `metrics`
- Reasons: heatmap → ES|QL

**Query IR:**

- Output metric: `value`
- Output groups: `time_bucket, container.name`

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** CORRECT

#### Running containers

**Translation path:** `lens` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:docker.containers.running{$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected lens metric panel
- `translate_lens` / `datadog.translate.lens_single_query` → translated Lens metric widget

**Plan:**

- Backend: `lens`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → lens metric panel

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** EXPECTED_LIMITATION

#### Stopped containers

**Translation path:** `lens` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:docker.containers.stopped{$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected lens metric panel
- `translate_lens` / `datadog.translate.lens_single_query` → translated Lens metric widget

**Plan:**

- Backend: `lens`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → lens metric panel

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** EXPECTED_LIMITATION

#### CPU by container

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (heatmap):**

```
avg:docker.cpu.user{$scope} by {container_name}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_heatmap_distribution` → selected ES|QL for heatmap
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (heatmap):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS value = AVG(docker_cpu_user) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), container.name
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `heatmap`
- Data source: `metrics`
- Reasons: heatmap → ES|QL

**Query IR:**

- Output metric: `value`
- Output groups: `time_bucket, container.name`

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** CORRECT

#### CPU user by image

**Translation path:** `lens` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
avg:docker.cpu.user{$scope} by {docker_image}.fill(0)
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected lens XY panel
- `translate_lens` / `datadog.translate.lens_single_query` → translated Lens metric widget

**Plan:**

- Backend: `lens`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → lens XY panel

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** EXPECTED_LIMITATION

#### RSS memory by image

**Translation path:** `lens` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
avg:docker.mem.rss{$scope} by {docker_image}.fill(0)
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected lens XY panel
- `translate_lens` / `datadog.translate.lens_single_query` → translated Lens metric widget

**Plan:**

- Backend: `lens`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → lens XY panel

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** EXPECTED_LIMITATION

#### 9

**Translation path:** `blocked` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (event_stream):**

**Pipeline trace:**

- `plan` / `datadog.plan.unsupported_widget` → blocked unsupported widget type event_stream

**Plan:**

- Backend: `blocked`
- Kibana type: `markdown`
- Data source: ``
- Reasons: unsupported widget type: event_stream

**Verdict:** EXPECTED_LIMITATION

#### 10

**Translation path:** `blocked` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (event_timeline):**

**Pipeline trace:**

- `plan` / `datadog.plan.unsupported_widget` → blocked unsupported widget type event_timeline

**Plan:**

- Backend: `blocked`
- Kibana type: `markdown`
- Data source: ``
- Reasons: unsupported widget type: event_timeline

**Verdict:** EXPECTED_LIMITATION

#### Running container change

**Translation path:** `markdown` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
100*(sum:docker.containers.running{$scope}/timeshift(sum:docker.containers.running{$scope}, -300))
```

**Pipeline trace:**

- `plan` / `datadog.plan.unparsed_query` → selected markdown because a metric query could not be parsed

**Plan:**

- Backend: `markdown`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: metric query could not be parsed

**Warnings:** query syntax not recognized; manual review needed; Template variable filters applied via Kibana dashboard controls

**Verdict:** EXPECTED_LIMITATION

#### CPU system by image

**Translation path:** `lens` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
avg:docker.cpu.system{$scope} by {docker_image}.fill(0)
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected lens XY panel
- `translate_lens` / `datadog.translate.lens_single_query` → translated Lens metric widget

**Plan:**

- Backend: `lens`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → lens XY panel

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** EXPECTED_LIMITATION

#### 13

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (note):**

**Pipeline trace:**

- `plan` / `datadog.plan.text_widget` → selected markdown for text widget note

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: text widget (note)

**Verdict:** EXPECTED_LIMITATION

#### Cache memory by image

**Translation path:** `lens` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:docker.mem.cache{$scope} by {docker_image}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected lens XY panel
- `translate_lens` / `datadog.translate.lens_single_query` → translated Lens metric widget

**Plan:**

- Backend: `lens`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → lens XY panel

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** EXPECTED_LIMITATION

</details>

<details>
<summary>Template Variables (1)</summary>

- `$scope` → tag: `None`, default: `*`

</details>

---

### Datadog: Kubernetes - Overview

**File:** `kubernetes.json` — **Panels:** 57

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Banner | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 24 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 6152894268304392 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Overview | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Clusters | `query_value` → `metric` | warning | **CORRECT** | avg:kubernetes.pods.running{$scope,$label,$node,$service,$deployment,$statefulse... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Nodes | `query_value` → `metric` | warning | **EXPECTED_LIMITATION** | sum:kubernetes_state.node.count{$scope,$label,$node,$service,$namespace,$cluster... | — |
| Namespaces | `query_value` → `metric` | warning | **CORRECT** | avg:kubernetes.pods.running{$scope,$label,$node,$service,$deployment,$statefulse... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| DaemonSets | `query_value` → `metric` | warning | **CORRECT** | avg:kubernetes_state.daemonset.desired{$scope,$label,$node,$service,$daemonset,$... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Services | `query_value` → `metric` | warning | **EXPECTED_LIMITATION** | sum:kubernetes_state.service.count{$scope,$label,$node,$service,$namespace,$clus... | — |
| Deployments | `query_value` → `metric` | warning | **CORRECT** | avg:kubernetes_state.deployment.replicas{$scope,$label,$node,$service,$deploymen... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Pods | `query_value` → `metric` | warning | **EXPECTED_LIMITATION** | sum:kubernetes.pods.running{$scope,$label,$node,$service,$deployment,$statefulse... | — |
| Containers | `query_value` → `metric` | warning | **EXPECTED_LIMITATION** | sum:kubernetes.containers.running{$scope,$label,$node,$service,$deployment,$stat... | — |
| Kubelets up | `check_status` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | — | — |
| Kubelet Ping | `check_status` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | — | — |
| Events | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Events per node | `timeseries` → `xy` | requires_manual | **EXPECTED_LIMITATION** |  | — |
| Event logs per node | `list_stream` → `table` | requires_manual | **EXPECTED_LIMITATION** | source:kubernetes $node $cluster | — |
| Pods | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Ready state by node | `toplist` → `table` | warning | **CORRECT** | sum:kubernetes_state.pod.ready{$scope,$cluster,$namespace,$deployment,$statefuls... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend AND condi... |
| Running pods per node | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | sum:kubernetes.pods.running{$scope,$deployment,$statefulset,$replicaset,$daemons... | — |
| Running by namespace | `toplist` → `table` | warning | **CORRECT** | sum:kubernetes.pods.running{$scope,$namespace,$cluster,$deployment,$statefulset,... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Running pods per namespace | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | sum:kubernetes.pods.running{$scope,$cluster,$namespace,$deployment,$statefulset,... | — |
| Failure by namespaces | `toplist` → `table` | warning | **CORRECT** | sum:kubernetes_state.pod.status_phase{$scope,$cluster,$namespace,$deployment,$st... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend AND pod_p... |
| CrashloopBackOff by Pod | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | sum:kubernetes_state.container.status_report.count.waiting{$cluster,$namespace,$... | — |
| DaemonSets | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Ready | `query_value` → `metric` | warning | **EXPECTED_LIMITATION** | sum:kubernetes_state.daemonset.ready{$scope,$daemonset,$cluster,$label,$namespac... | — |
| Pods ready | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | sum:kubernetes_state.daemonset.ready{$scope,$daemonset,$service,$namespace,$labe... | — |
| Desired | `query_value` → `metric` | warning | **EXPECTED_LIMITATION** | sum:kubernetes_state.daemonset.desired{$scope,$daemonset,$cluster,$label,$namesp... | — |
| Pods desired | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | sum:kubernetes_state.daemonset.desired{$scope,$daemonset,$service,$namespace,$la... | — |
| Deployments | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Pods desired | `query_value` → `metric` | warning | **EXPECTED_LIMITATION** | sum:kubernetes_state.deployment.replicas_desired{$scope,$deployment,$cluster,$la... | — |
| Pods desired | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | sum:kubernetes_state.deployment.replicas_desired{$scope,$deployment,$cluster,$la... | — |
| Pods available | `query_value` → `metric` | warning | **EXPECTED_LIMITATION** | sum:kubernetes_state.deployment.replicas_available{$scope,$deployment,$cluster,$... | — |
| Pods available | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | sum:kubernetes_state.deployment.replicas_available{$scope,$deployment,$service,$... | — |
| Pods unavailable | `query_value` → `metric` | warning | **EXPECTED_LIMITATION** | sum:kubernetes_state.deployment.replicas_unavailable{$scope,$deployment,$cluster... | — |
| Pods unavailable | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | sum:kubernetes_state.deployment.replicas_unavailable{$scope,$deployment,$service... | — |
| ReplicaSets | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Ready | `query_value` → `metric` | warning | **EXPECTED_LIMITATION** | sum:kubernetes_state.replicaset.replicas_ready{$scope,$deployment,$replicaset,$c... | — |
| Ready | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | sum:kubernetes_state.replicaset.replicas_ready{$scope,$service,$namespace,$deplo... | — |
| Not ready | `query_value` → `metric` | warning | **CORRECT** | sum:kubernetes_state.replicaset.replicas_desired{$scope,$deployment,$replicaset,... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Not ready | `timeseries` → `xy` | warning | **CORRECT** | sum:kubernetes_state.replicaset.replicas_desired{$scope,$service,$namespace,$dep... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Containers | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Container states | `timeseries` → `xy` | warning | **CORRECT** | sum:kubernetes_state.container.running{$scope,$deployment,$statefulset,$replicas... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Resource Utilization | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| CPU utilization per node | `hostmap` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | — | — |
| Sum Kubernetes CPU requests per node | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | sum:kubernetes.cpu.requests{$scope,$deployment,$statefulset,$replicaset,$daemons... | — |
| Most CPU-intensive pods | `toplist` → `table` | warning | **CORRECT** | sum:kubernetes.cpu.usage.total{$scope,$deployment,$statefulset,$replicaset,$daem... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend AND kuber... |
| Memory usage per node | `hostmap` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | — | — |
| Sum Kubernetes memory requests per node | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | sum:kubernetes.memory.requests{$scope,$deployment,$statefulset,$replicaset,$daem... | — |
| Most memory-intensive pods | `toplist` → `table` | warning | **CORRECT** | sum:kubernetes.memory.usage{$scope,$deployment,$statefulset,$replicaset,$daemons... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend AND kuber... |
| Disk I/O & Network | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Network in per node | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | sum:kubernetes.network.rx_bytes{$scope,$deployment,$statefulset,$replicaset,$dae... | — |
| Network out per node | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | sum:kubernetes.network.tx_bytes{$scope,$deployment,$statefulset,$replicaset,$dae... | — |
| Network errors per node | `timeseries` → `xy` | warning | **CORRECT** | sum:kubernetes.network.rx_errors{$scope,$deployment,$statefulset,$replicaset,$da... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Network errors per pod | `timeseries` → `xy` | warning | **CORRECT** | sum:kubernetes.network.rx_errors{$scope,$deployment,$statefulset,$replicaset,$da... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Disk writes per node | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | sum:kubernetes.io.write_bytes{$scope,$service,$namespace,$deployment,$statefulse... | — |
| Disk reads per node | `timeseries` → `xy` | warning | **EXPECTED_LIMITATION** | sum:kubernetes.io.read_bytes{$scope,$service,$namespace,$deployment,$statefulset... | — |

<details>
<summary>Detailed traces (57 panels)</summary>

#### Banner

**Translation path:** `group` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (group):**

**Pipeline trace:**

- `plan` / `datadog.plan.group_widget` → selected group backend

**Plan:**

- Backend: `group`
- Kibana type: `group`
- Data source: ``
- Reasons: group/container widget

**Verdict:** EXPECTED_LIMITATION

#### 24

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (note):**

**Pipeline trace:**

- `plan` / `datadog.plan.text_widget` → selected markdown for text widget note

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: text widget (note)

**Verdict:** EXPECTED_LIMITATION

#### 6152894268304392

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (note):**

**Pipeline trace:**

- `plan` / `datadog.plan.text_widget` → selected markdown for text widget note

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: text widget (note)

**Verdict:** EXPECTED_LIMITATION

#### Overview

**Translation path:** `group` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (group):**

**Pipeline trace:**

- `plan` / `datadog.plan.group_widget` → selected group backend

**Plan:**

- Backend: `group`
- Kibana type: `group`
- Data source: ``
- Reasons: group/container widget

**Verdict:** EXPECTED_LIMITATION

#### Clusters

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
avg:kubernetes.pods.running{$scope,$label,$node,$service,$deployment,$statefulset,$replicaset,$daemonset,$namespace,$cluster} by {kube_cluster_name}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = AVG(kubernetes_pods_running) BY kubernetes.cluster.name
| WHERE query1 > 0
| STATS value = COUNT(*)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `query1`
- Output groups: `kubernetes.cluster.name`

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** CORRECT

#### Nodes

**Translation path:** `lens` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:kubernetes_state.node.count{$scope,$label,$node,$service,$namespace,$cluster}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected lens metric panel
- `translate_lens` / `datadog.translate.lens_single_query` → translated Lens metric widget

**Plan:**

- Backend: `lens`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → lens metric panel

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** EXPECTED_LIMITATION

#### Namespaces

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
avg:kubernetes.pods.running{$scope,$label,$node,$service,$deployment,$statefulset,$replicaset,$daemonset,$namespace,$cluster} by {kube_cluster_name,kube_namespace}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = AVG(kubernetes_pods_running) BY kubernetes.cluster.name, kubernetes.namespace
| WHERE query1 > 0
| STATS value = COUNT(*)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `query1`
- Output groups: `kubernetes.cluster.name, kubernetes.namespace`

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** CORRECT

#### DaemonSets

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
avg:kubernetes_state.daemonset.desired{$scope,$label,$node,$service,$daemonset,$namespace,$cluster} by {kube_cluster_name,kube_namespace,kube_daemon_set}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = AVG(kubernetes_state_daemonset_desired) BY kubernetes.cluster.name, kubernetes.namespace, kube_daemon_set
| WHERE query1 > 0
| STATS value = COUNT(*)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `query1`
- Output groups: `kubernetes.cluster.name, kubernetes.namespace, kube_daemon_set`

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** CORRECT

#### Services

**Translation path:** `lens` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:kubernetes_state.service.count{$scope,$label,$node,$service,$namespace,$cluster}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected lens metric panel
- `translate_lens` / `datadog.translate.lens_single_query` → translated Lens metric widget

**Plan:**

- Backend: `lens`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → lens metric panel

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** EXPECTED_LIMITATION

#### Deployments

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
avg:kubernetes_state.deployment.replicas{$scope,$label,$node,$service,$deployment,$replicaset,$namespace,$cluster} by {kube_cluster_name,kube_namespace,kube_deployment}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = AVG(kubernetes_state_deployment_replicas) BY kubernetes.cluster.name, kubernetes.namespace, kubernetes.deployment.name
| WHERE query1 > 0
| STATS value = COUNT(*)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `query1`
- Output groups: `kubernetes.cluster.name, kubernetes.namespace, kubernetes.deployment.name`

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** CORRECT

#### Pods

**Translation path:** `lens` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:kubernetes.pods.running{$scope,$label,$node,$service,$deployment,$statefulset,$replicaset,$daemonset,$namespace,$cluster}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected lens metric panel
- `translate_lens` / `datadog.translate.lens_single_query` → translated Lens metric widget

**Plan:**

- Backend: `lens`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → lens metric panel

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** EXPECTED_LIMITATION

#### Containers

**Translation path:** `lens` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:kubernetes.containers.running{$scope,$label,$node,$service,$deployment,$statefulset,$replicaset,$daemonset,$namespace,$cluster}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected lens metric panel
- `translate_lens` / `datadog.translate.lens_single_query` → translated Lens metric widget

**Plan:**

- Backend: `lens`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → lens metric panel

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** EXPECTED_LIMITATION

#### Kubelets up

**Translation path:** `blocked` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (check_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.unsupported_widget` → blocked unsupported widget type check_status

**Plan:**

- Backend: `blocked`
- Kibana type: `markdown`
- Data source: ``
- Reasons: unsupported widget type: check_status

**Verdict:** EXPECTED_LIMITATION

#### Kubelet Ping

**Translation path:** `blocked` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (check_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.unsupported_widget` → blocked unsupported widget type check_status

**Plan:**

- Backend: `blocked`
- Kibana type: `markdown`
- Data source: ``
- Reasons: unsupported widget type: check_status

**Verdict:** EXPECTED_LIMITATION

#### Events

**Translation path:** `group` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (group):**

**Pipeline trace:**

- `plan` / `datadog.plan.group_widget` → selected group backend

**Plan:**

- Backend: `group`
- Kibana type: `group`
- Data source: ``
- Reasons: group/container widget

**Verdict:** EXPECTED_LIMITATION

</details>

<details>
<summary>Template Variables (10)</summary>

- `$scope` → tag: ``, default: `*`
- `$cluster` → tag: `kube_cluster_name`, default: `*`
- `$namespace` → tag: `kube_namespace`, default: `*`
- `$deployment` → tag: `kube_deployment`, default: `*`
- `$daemonset` → tag: `kube_daemon_set`, default: `*`
- `$statefulset` → tag: `kube_stateful_set`, default: `*`
- `$replicaset` → tag: `kube_replica_set`, default: `*`
- `$service` → tag: `kube_service`, default: `*`
- `$node` → tag: `node`, default: `*`
- `$label` → tag: `label`, default: `*`

</details>

---

### Datadog: NGINX - Overview

**File:** `nginx_overview.json` — **Panels:** 27

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| New group | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 7370311124819436 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 5476438101081174 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Activity Summary | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Dropped connections, last 15m | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:nginx.net.conn_dropped_per_s{*}.as_count() | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Agent connection to NGINX | `check_status` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | — | — |
| 4071809555103542 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 6613327356980228 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Nginx metric monitors | `manage_status` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | — | — |
| Anomaly Detection | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Nginx Watchdog alerts | `event_stream` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | — | — |
| 2165182479929144 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Logs | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 518123602946722 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| NGINX Error logs | `log_stream` → `table` | ok | **CORRECT** | source:nginx @http.status_code:(404 OR 500) | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend AND service.... |
| Requests | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Requests per second | `query_value` → `metric` | ok | **CORRECT** | avg:nginx.net.request_per_s{*} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Requests per second by host | `hostmap` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | — | — |
| 8663159993822306 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 183855449379928 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Requests: reading, writing, waiting | `timeseries` → `xy` | warning | **CORRECT** | sum:nginx.net.reading{$Host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Change in overall requests per second | `change` → `metric` | not_feasible | **EXPECTED_LIMITATION** | sum:nginx.net.request_per_s{*} by {service} | — |
| 4851971395880802 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Connections  | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Dropped connections per second | `timeseries` → `xy` | warning | **CORRECT** | sum:nginx.net.conn_dropped_per_s{$Host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Active connections per second | `timeseries` → `xy` | warning | **CORRECT** | sum:nginx.net.connections{$Host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| 5157405700596810 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |

<details>
<summary>Detailed traces (27 panels)</summary>

#### New group

**Translation path:** `group` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (group):**

**Pipeline trace:**

- `plan` / `datadog.plan.group_widget` → selected group backend

**Plan:**

- Backend: `group`
- Kibana type: `group`
- Data source: ``
- Reasons: group/container widget

**Verdict:** EXPECTED_LIMITATION

#### 7370311124819436

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (note):**

**Pipeline trace:**

- `plan` / `datadog.plan.text_widget` → selected markdown for text widget note

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: text widget (note)

**Verdict:** EXPECTED_LIMITATION

#### 5476438101081174

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (note):**

**Pipeline trace:**

- `plan` / `datadog.plan.text_widget` → selected markdown for text widget note

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: text widget (note)

**Verdict:** EXPECTED_LIMITATION

#### Activity Summary

**Translation path:** `group` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (group):**

**Pipeline trace:**

- `plan` / `datadog.plan.group_widget` → selected group backend

**Plan:**

- Backend: `group`
- Kibana type: `group`
- Data source: ``
- Reasons: group/container widget

**Verdict:** EXPECTED_LIMITATION

#### Dropped connections, last 15m

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:nginx.net.conn_dropped_per_s{*}.as_count()
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS _bucket_value = SUM(nginx_net_conn_dropped_per_s) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| STATS value = MAX(_bucket_value)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `_bucket_value`
- Output groups: `time_bucket`

**Warnings:** as_count semantics are approximated for non-count metrics

**Verdict:** MINOR_ISSUE

#### Agent connection to NGINX

**Translation path:** `blocked` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (check_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.unsupported_widget` → blocked unsupported widget type check_status

**Plan:**

- Backend: `blocked`
- Kibana type: `markdown`
- Data source: ``
- Reasons: unsupported widget type: check_status

**Verdict:** EXPECTED_LIMITATION

#### 4071809555103542

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (note):**

**Pipeline trace:**

- `plan` / `datadog.plan.text_widget` → selected markdown for text widget note

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: text widget (note)

**Verdict:** EXPECTED_LIMITATION

#### 6613327356980228

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (note):**

**Pipeline trace:**

- `plan` / `datadog.plan.text_widget` → selected markdown for text widget note

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: text widget (note)

**Verdict:** EXPECTED_LIMITATION

#### Nginx metric monitors

**Translation path:** `blocked` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (manage_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.unsupported_widget` → blocked unsupported widget type manage_status

**Plan:**

- Backend: `blocked`
- Kibana type: `markdown`
- Data source: ``
- Reasons: unsupported widget type: manage_status

**Verdict:** EXPECTED_LIMITATION

#### Anomaly Detection

**Translation path:** `group` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (group):**

**Pipeline trace:**

- `plan` / `datadog.plan.group_widget` → selected group backend

**Plan:**

- Backend: `group`
- Kibana type: `group`
- Data source: ``
- Reasons: group/container widget

**Verdict:** EXPECTED_LIMITATION

#### Nginx Watchdog alerts

**Translation path:** `blocked` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (event_stream):**

**Pipeline trace:**

- `plan` / `datadog.plan.unsupported_widget` → blocked unsupported widget type event_stream

**Plan:**

- Backend: `blocked`
- Kibana type: `markdown`
- Data source: ``
- Reasons: unsupported widget type: event_stream

**Verdict:** EXPECTED_LIMITATION

#### 2165182479929144

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (note):**

**Pipeline trace:**

- `plan` / `datadog.plan.text_widget` → selected markdown for text widget note

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: text widget (note)

**Verdict:** EXPECTED_LIMITATION

#### Logs

**Translation path:** `group` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (group):**

**Pipeline trace:**

- `plan` / `datadog.plan.group_widget` → selected group backend

**Plan:**

- Backend: `group`
- Kibana type: `group`
- Data source: ``
- Reasons: group/container widget

**Verdict:** EXPECTED_LIMITATION

#### 518123602946722

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (note):**

**Pipeline trace:**

- `plan` / `datadog.plan.text_widget` → selected markdown for text widget note

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: text widget (note)

**Verdict:** EXPECTED_LIMITATION

#### NGINX Error logs

**Translation path:** `esql_log` · **Query language:** `datadog_log` · **Readiness:** `—`

**Source (log_stream):**

```
source:nginx @http.status_code:(404 OR 500)
```

**Pipeline trace:**

- `plan` / `datadog.plan.log_stream` → selected esql log stream table
- `translate_log` / `datadog.translate.log_direct_esql` → translated log widget with direct ES|QL filters

**Translated (table):**

```
FROM logs-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend AND service.name == "nginx" AND ((http.status_code == 404 OR http.status_code == 500))
| SORT @timestamp DESC
| KEEP @timestamp, message, log.level, service.name, host.name
| LIMIT 100
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `logs`
- Reasons: log stream → esql table

**Query IR:**

- Output metric: `@timestamp`
- Output groups: `@timestamp`

**Verdict:** CORRECT

</details>

<details>
<summary>Template Variables (1)</summary>

- `$Host` → tag: `host`, default: `*`

</details>

---

### Datadog: Postgres - Metrics

**File:** `postgres.json` — **Panels:** 9

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Rows fetched / returned / inserted / updated (per sec) | `timeseries` → `xy` | warning | **CORRECT** | avg:postgresql.rows_fetched{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Connections | `timeseries` → `xy` | warning | **CORRECT** | avg:postgresql.connections{$scope} by {db} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Inserts / updates / deletes (per sec) | `timeseries` → `xy` | warning | **CORRECT** | postgresql.rows_inserted{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Disk utilization (%) | `timeseries` → `xy` | warning | **CORRECT** | avg:system.io.util{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| System load | `timeseries` → `xy` | warning | **CORRECT** | system.load.1{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| CPU usage (%) | `timeseries` → `xy` | warning | **CORRECT** | system.cpu.idle{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| I/O wait (%) | `timeseries` → `xy` | warning | **CORRECT** | max:system.cpu.iowait{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| System memory | `timeseries` → `xy` | warning | **CORRECT** | sum:system.mem.usable{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Network traffic (per sec) | `timeseries` → `xy` | warning | **CORRECT** | sum:system.net.bytes_rcvd{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |

<details>
<summary>Detailed traces (9 panels)</summary>

#### Rows fetched / returned / inserted / updated (per sec)

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
avg:postgresql.rows_fetched{$scope}
```

```
avg:postgresql.rows_returned{$scope}
```

```
avg:postgresql.rows_inserted{$scope}
```

```
avg:postgresql.rows_updated{$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = AVG(postgresql_rows_fetched), query1_2 = AVG(postgresql_rows_returned), query1_3 = AVG(postgresql_rows_inserted), query1_4 = AVG(postgresql_rows_updated) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| KEEP time_bucket, query1, query1_2, query1_3, query1_4
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket`

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** CORRECT

#### Connections

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
avg:postgresql.connections{$scope} by {db}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = AVG(postgresql_connections) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), db
| KEEP time_bucket, db, query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket, db`

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** CORRECT

#### Inserts / updates / deletes (per sec)

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
postgresql.rows_inserted{$scope}
```

```
postgresql.rows_deleted{$scope}
```

```
postgresql.rows_updated{$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = AVG(postgresql_rows_inserted), query2 = AVG(postgresql_rows_deleted), query3 = AVG(postgresql_rows_updated) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| KEEP time_bucket, query1, query2, query3
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket`

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** CORRECT

#### Disk utilization (%)

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
avg:system.io.util{$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = AVG(system_io_util) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| KEEP time_bucket, query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket`

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** CORRECT

#### System load

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
system.load.1{$scope}
```

```
system.load.5{$scope}
```

```
system.load.15{$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = AVG(system_load_1), query1_2 = AVG(system_load_5), query1_3 = AVG(system_load_15) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| KEEP time_bucket, query1, query1_2, query1_3
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket`

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** CORRECT

#### CPU usage (%)

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
system.cpu.idle{$scope}
```

```
system.cpu.system{$scope}
```

```
system.cpu.iowait{$scope}
```

```
system.cpu.user{$scope}
```

```
system.cpu.stolen{$scope}
```

```
system.cpu.guest{$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = AVG(system_cpu_idle), query2 = AVG(system_cpu_system), query3 = AVG(system_cpu_iowait), query4 = AVG(system_cpu_user), query5 = AVG(system_cpu_stolen), query6 = AVG(system_cpu_guest) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| KEEP time_bucket, query1, query2, query3, query4, query5, query6
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket`

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** CORRECT

#### I/O wait (%)

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
max:system.cpu.iowait{$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = MAX(system_cpu_iowait) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| KEEP time_bucket, query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket`

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** CORRECT

#### System memory

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:system.mem.usable{$scope}
```

```
sum:system.mem.total{$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected ES|QL XY because widget uses a multi-query formula
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = SUM(system_mem_usable), query2 = SUM(system_mem_total) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| EVAL query2_query1 = (query2 - query1)
| KEEP time_bucket, query1, query2_query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: multi-query formula → ES|QL for query-side computation

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket`

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** CORRECT

#### Network traffic (per sec)

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:system.net.bytes_rcvd{$scope}
```

```
sum:system.net.bytes_sent{$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = SUM(system_net_bytes_rcvd), query1_2 = SUM(system_net_bytes_sent) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| KEEP time_bucket, query1, query1_2
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket`

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** CORRECT

</details>

<details>
<summary>Template Variables (1)</summary>

- `$scope` → tag: `None`, default: `*`

</details>

---

### Datadog: Redis - Overview

**File:** `redis.json` — **Panels:** 43

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| About Redis | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 8013519185925578 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 2021637053460700 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Overview | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Hit rate | `query_value` → `metric` | warning | **CORRECT** | avg:redis.stats.keyspace_hits{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Blocked clients | `query_value` → `metric` | warning | **EXPECTED_LIMITATION** | sum:redis.clients.blocked{$scope,$host} | — |
| Redis keyspace | `query_value` → `metric` | warning | **EXPECTED_LIMITATION** | sum:redis.keys{$scope,$host} | — |
| Unsaved changes | `query_value` → `metric` | warning | **EXPECTED_LIMITATION** | sum:redis.rdb.changes_since_last{$scope,$host} | — |
| Primary link down | `query_value` → `metric` | warning | **EXPECTED_LIMITATION** | sum:redis.replication.master_link_down_since_seconds{$scope,$host} | — |
| 7896589211182748 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Performance Metrics | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Latency by Host | `timeseries` → `xy` | warning | **CORRECT** | avg:redis.info.latency_ms{$scope,$host} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| 18 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Slowlog duration | `timeseries` → `xy` | not_feasible | **EXPECTED_LIMITATION** | sum:redis.slowlog.micros.95percentile{$scope,$host} by {name,command} | — |
| Slowlog query rates | `toplist` → `table` | warning | **CORRECT** | sum:redis.slowlog.micros.count{$host,$scope} by {command,name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Average replication delay (offset) | `timeseries` → `xy` | warning | **CORRECT** | avg:redis.replication.delay{$host,$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Average CPU usage | `timeseries` → `xy` | warning | **CORRECT** | avg:redis.cpu.sys{$host,$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Cache hit rate | `query_value` → `metric` | warning | **CORRECT** | avg:redis.stats.keyspace_hits{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Cache hit rate | `timeseries` → `xy` | warning | **CORRECT** | avg:redis.stats.keyspace_hits{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Memory Metrics | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Percent Used Memory by Host | `timeseries` → `xy` | warning | **CORRECT** | avg:redis.mem.used{$scope,$host} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| 24 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Evictions | `timeseries` → `xy` | warning | **CORRECT** | sum:redis.keys.evicted{$scope,$host} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Total allocated memory | `timeseries` → `xy` | warning | **CORRECT** | sum:redis.mem.rss{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Fragmentation ratio | `query_value` → `metric` | warning | **EXPECTED_LIMITATION** | avg:redis.mem.fragmentation_ratio{$scope,$host} | — |
| Fragmentation ratio | `timeseries` → `xy` | warning | **CORRECT** | avg:redis.mem.fragmentation_ratio{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Base Activity Metrics | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Connected clients | `timeseries` → `xy` | warning | **CORRECT** | sum:redis.net.clients{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| 12 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Blocked clients | `timeseries` → `xy` | warning | **CORRECT** | sum:redis.clients.blocked{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Connected replicas | `timeseries` → `xy` | warning | **CORRECT** | sum:redis.net.slaves{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Rejected connections | `timeseries` → `xy` | not_feasible | **EXPECTED_LIMITATION** | sum:redis.net.rejected{$scope,$host} | — |
| Commands per second | `query_value` → `metric` | warning | **CORRECT** | sum:redis.net.commands{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Key Metrics | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Total keys | `timeseries` → `xy` | warning | **CORRECT** | sum:redis.keys{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Current total | `query_value` → `metric` | warning | **CORRECT** | sum:redis.keys{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| 4351331682136830 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Expired keys | `timeseries` → `xy` | warning | **CORRECT** | sum:redis.keys.expired{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Keys with expiration | `query_value` → `metric` | warning | **CORRECT** | sum:redis.expires{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Key length distribution | `distribution` → `xy` | warning | **CORRECT** | sum:redis.key.length{$scope, $host, $key} by {key} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Logs | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Error Logs | `list_stream` → `table` | warning | **CORRECT** | source:redis $scope $host status:error | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend AND KQL("(se... |
| All Logs | `list_stream` → `table` | warning | **CORRECT** | source:redis $scope $host | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend AND KQL("ser... |

<details>
<summary>Detailed traces (43 panels)</summary>

#### About Redis

**Translation path:** `group` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (group):**

**Pipeline trace:**

- `plan` / `datadog.plan.group_widget` → selected group backend

**Plan:**

- Backend: `group`
- Kibana type: `group`
- Data source: ``
- Reasons: group/container widget

**Verdict:** EXPECTED_LIMITATION

#### 8013519185925578

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (note):**

**Pipeline trace:**

- `plan` / `datadog.plan.text_widget` → selected markdown for text widget note

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: text widget (note)

**Verdict:** EXPECTED_LIMITATION

#### 2021637053460700

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (note):**

**Pipeline trace:**

- `plan` / `datadog.plan.text_widget` → selected markdown for text widget note

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: text widget (note)

**Verdict:** EXPECTED_LIMITATION

#### Overview

**Translation path:** `group` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (group):**

**Pipeline trace:**

- `plan` / `datadog.plan.group_widget` → selected group backend

**Plan:**

- Backend: `group`
- Kibana type: `group`
- Data source: ``
- Reasons: group/container widget

**Verdict:** EXPECTED_LIMITATION

#### Hit rate

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
avg:redis.stats.keyspace_hits{$scope,$host}
```

```
avg:redis.stats.keyspace_misses{$scope,$host}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = AVG(redis_stats_keyspace_hits), query2 = AVG(redis_stats_keyspace_misses) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| EVAL value = ((query1 / (query1 + query2)) * 100)
| STATS value = AVG(value)
| KEEP value
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket`

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** CORRECT

#### Blocked clients

**Translation path:** `lens` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:redis.clients.blocked{$scope,$host}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected lens metric panel
- `translate_lens` / `datadog.translate.lens_single_query` → translated Lens metric widget

**Plan:**

- Backend: `lens`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → lens metric panel

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** EXPECTED_LIMITATION

#### Redis keyspace

**Translation path:** `lens` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:redis.keys{$scope,$host}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected lens metric panel
- `translate_lens` / `datadog.translate.lens_single_query` → translated Lens metric widget

**Plan:**

- Backend: `lens`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → lens metric panel

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** EXPECTED_LIMITATION

#### Unsaved changes

**Translation path:** `lens` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:redis.rdb.changes_since_last{$scope,$host}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected lens metric panel
- `translate_lens` / `datadog.translate.lens_single_query` → translated Lens metric widget

**Plan:**

- Backend: `lens`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → lens metric panel

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** EXPECTED_LIMITATION

#### Primary link down

**Translation path:** `lens` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:redis.replication.master_link_down_since_seconds{$scope,$host}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected lens metric panel
- `translate_lens` / `datadog.translate.lens_single_query` → translated Lens metric widget

**Plan:**

- Backend: `lens`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → lens metric panel

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** EXPECTED_LIMITATION

#### 7896589211182748

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (note):**

**Pipeline trace:**

- `plan` / `datadog.plan.text_widget` → selected markdown for text widget note

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: text widget (note)

**Verdict:** EXPECTED_LIMITATION

#### Performance Metrics

**Translation path:** `group` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (group):**

**Pipeline trace:**

- `plan` / `datadog.plan.group_widget` → selected group backend

**Plan:**

- Backend: `group`
- Kibana type: `group`
- Data source: ``
- Reasons: group/container widget

**Verdict:** EXPECTED_LIMITATION

#### Latency by Host

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
avg:redis.info.latency_ms{$scope,$host} by {host}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query2 = AVG(redis_info_latency_ms) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), host.name
| EVAL latency_of_the_redis_info_command = query2
| KEEP time_bucket, host.name, latency_of_the_redis_info_command
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query2`
- Output groups: `time_bucket, host.name`

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** CORRECT

#### 18

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (note):**

**Pipeline trace:**

- `plan` / `datadog.plan.text_widget` → selected markdown for text widget note

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: text widget (note)

**Verdict:** EXPECTED_LIMITATION

#### Slowlog duration

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:redis.slowlog.micros.95percentile{$scope,$host} by {name,command}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Warnings:** Template variable filters applied via Kibana dashboard controls; translation error: formula syntax not recognized: top(query1 / 1000, 10, 'mean', 'desc')

**Semantic losses:** formula syntax not recognized: top(query1 / 1000, 10, 'mean', 'desc')

**Verdict:** EXPECTED_LIMITATION

#### Slowlog query rates

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (toplist):**

```
sum:redis.slowlog.micros.count{$host,$scope} by {command,name}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_toplist` → selected esql toplist table
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (table):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = SUM(redis_slowlog_micros_count) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), command, name
| STATS query1 = AVG(query1) BY command, name
| KEEP command, name, query1
| SORT query1 DESC
| LIMIT 10
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: top list → esql table with ORDER BY + LIMIT

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket, command, name`

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** CORRECT

</details>

<details>
<summary>Template Variables (3)</summary>

- `$scope` → tag: ``, default: `*`
- `$host` → tag: `host`, default: `*`
- `$key` → tag: `key`, default: `*`

</details>

---

### Datadog: System Overview - Sample

**File:** `sample_dashboard.json` — **Panels:** 11

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| CPU Usage by Host | `timeseries` → `xy` | warning | **CORRECT** | avg:system.cpu.user{$host} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Memory Usage with Rollup | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:system.mem.usable{env:$env} by {host}.rollup(avg, 60) | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Current CPU Average | `query_value` → `metric` | ok | **CORRECT** | avg:system.cpu.user{*} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Top Hosts by CPU | `toplist` → `table` | ok | **CORRECT** | avg:system.cpu.user{*} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| Disk I/O (Formula: Read + Write) | `timeseries` → `xy` | ok | **CORRECT** | avg:system.disk.read_time{*} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend \| STATS ... |
| 6 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Log Error Rate | `timeseries` → `xy` | ok | **CORRECT** | service:web status:error | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend AND service.... |
| Log Entries by Service | `table` → `table` | ok | **CORRECT** | status:error @http.url:/api/* | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend AND log.leve... |
| Request Latency Distribution | `heatmap` → `heatmap` | ok | **CORRECT** | avg:trace.flask.request.duration{service:web} by {resource_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend AND servi... |
| Monitor Summary | `manage_status` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | — | — |
| Network Bytes In | `query_value` → `metric` | ok | **CORRECT** | sum:system.net.bytes_rcvd{host:web01,!env:staging} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend AND host.... |

<details>
<summary>Detailed traces (11 panels)</summary>

#### CPU Usage by Host

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
avg:system.cpu.user{$host} by {host}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = AVG(system_cpu_user) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), host.name
| KEEP time_bucket, host.name, query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket, host.name`

**Warnings:** Template variable filters applied via Kibana dashboard controls

**Verdict:** CORRECT

#### Memory Usage with Rollup

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
avg:system.mem.usable{env:$env} by {host}.rollup(avg, 60)
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = AVG(system_mem_usable) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), host.name
| KEEP time_bucket, host.name, query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket, host.name`

**Warnings:** Template variable filters applied via Kibana dashboard controls; rollup interval is approximated in ES|QL

**Verdict:** MINOR_ISSUE

#### Current CPU Average

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
avg:system.cpu.user{*}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = AVG(system_cpu_user) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| EVAL value = query1
| STATS value = LAST(value, time_bucket)
| KEEP value
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket`

**Verdict:** CORRECT

#### Top Hosts by CPU

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (toplist):**

```
avg:system.cpu.user{*} by {host}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_toplist` → selected esql toplist table
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (table):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = AVG(system_cpu_user) BY host.name
| KEEP host.name, query1
| SORT query1 DESC
| LIMIT 10
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: top list → esql table with ORDER BY + LIMIT

**Query IR:**

- Output metric: `query1`
- Output groups: `host.name`

**Verdict:** CORRECT

#### Disk I/O (Formula: Read + Write)

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
avg:system.disk.read_time{*} by {host}
```

```
avg:system.disk.write_time{*} by {host}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected ES|QL XY because widget uses a multi-query formula
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS query1 = AVG(system_disk_read_time), query2 = AVG(system_disk_write_time) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), host.name
| EVAL total_i_o = (query1 + query2)
| KEEP time_bucket, host.name, total_i_o
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: multi-query formula → ES|QL for query-side computation

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket, host.name`

**Verdict:** CORRECT

#### 6

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (note):**

**Pipeline trace:**

- `plan` / `datadog.plan.text_widget` → selected markdown for text widget note

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: text widget (note)

**Verdict:** EXPECTED_LIMITATION

#### Log Error Rate

**Translation path:** `esql_log` · **Query language:** `datadog_log` · **Readiness:** `—`

**Source (timeseries):**

```
service:web status:error
```

**Pipeline trace:**

- `plan` / `datadog.plan.log_timeseries` → selected esql log timeseries
- `translate_log` / `datadog.translate.log_direct_esql` → translated log widget with direct ES|QL filters

**Translated (xy):**

```
FROM logs-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend AND service.name == "web" AND log.level == "error"
| STATS count = COUNT(*) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `logs`
- Reasons: log timeseries (count by bucket) → esql XY

**Query IR:**

- Output metric: `count`
- Output groups: `time_bucket`

**Verdict:** CORRECT

#### Log Entries by Service

**Translation path:** `esql_log` · **Query language:** `datadog_log` · **Readiness:** `—`

**Source (table):**

```
status:error @http.url:/api/*
```

**Pipeline trace:**

- `plan` / `datadog.plan.log_table` → selected esql log table
- `translate_log` / `datadog.translate.log_direct_esql` → translated log widget with direct ES|QL filters

**Translated (table):**

```
FROM logs-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend AND log.level == "error" AND http.url LIKE "/api/*"
| STATS count = COUNT(*) BY service.name
| SORT count DESC
| LIMIT 100
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `logs`
- Reasons: log table → esql table

**Query IR:**

- Output metric: `count`
- Output groups: `service.name`

**Verdict:** CORRECT

#### Request Latency Distribution

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (heatmap):**

```
avg:trace.flask.request.duration{service:web} by {resource_name}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_heatmap_distribution` → selected ES|QL for heatmap
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (heatmap):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend AND service.name == "web"
| STATS query1 = AVG(trace_flask_request_duration) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), resource_name
| KEEP time_bucket, resource_name, query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `heatmap`
- Data source: `metrics`
- Reasons: heatmap → ES|QL

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket, resource_name`

**Verdict:** CORRECT

#### Monitor Summary

**Translation path:** `blocked` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (manage_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.unsupported_widget` → blocked unsupported widget type manage_status

**Plan:**

- Backend: `blocked`
- Kibana type: `markdown`
- Data source: ``
- Reasons: unsupported widget type: manage_status

**Verdict:** EXPECTED_LIMITATION

#### Network Bytes In

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:system.net.bytes_rcvd{host:web01,!env:staging}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend AND host.name == "web01" AND deployment.environment != "staging"
| STATS query1 = SUM(system_net_bytes_rcvd) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| EVAL value = query1
| STATS value = LAST(value, time_bucket)
| KEEP value
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket`

**Verdict:** CORRECT

</details>

<details>
<summary>Template Variables (2)</summary>

- `$host` → tag: `host`, default: `*`
- `$env` → tag: `env`, default: `prod`

</details>

---

<!-- /GENERATED:PER_DASHBOARD_TRACES -->

---

## Appendix: Panel Status Summary

<!-- GENERATED:APPENDIX_STATS -->
From the latest trace run:

```
Total panels found:  175
  OK:                    35 (20.0%)
  Warning:              100 (57.1%)
  Requires manual:        3 (1.7%)
  Not feasible:          14 (8.0%)
  Skipped:               23 (13.1%)
```

Verdict breakdown:

```
  CORRECT:                   61
  MINOR_ISSUE:                2
  EXPECTED_LIMITATION:      112
```
<!-- /GENERATED:APPENDIX_STATS -->

---

## Appendix: Not-Feasible Panel Breakdown

<!-- GENERATED:NOT_FEASIBLE_BREAKDOWN -->
Every panel marked `not_feasible` in the trace run (14 total):

| Panel Title | Dashboard | Source | Reason |
|-------------|-----------|--------|--------|
| 9 | Docker - Overview | datadog | — |
| 10 | Docker - Overview | datadog | — |
| Kubelets up | Kubernetes - Overview | datadog | — |
| Kubelet Ping | Kubernetes - Overview | datadog | — |
| CPU utilization per node | Kubernetes - Overview | datadog | — |
| Memory usage per node | Kubernetes - Overview | datadog | — |
| Agent connection to NGINX | NGINX - Overview | datadog | — |
| Nginx metric monitors | NGINX - Overview | datadog | — |
| Nginx Watchdog alerts | NGINX - Overview | datadog | — |
| Requests per second by host | NGINX - Overview | datadog | — |
| Change in overall requests per second | NGINX - Overview | datadog | change calculation is approximated; translation error: metric widget with grouped query needs a redu... |
| Slowlog duration | Redis - Overview | datadog | Template variable filters applied via Kibana dashboard controls; translation error: formula syntax n... |
| Rejected connections | Redis - Overview | datadog | Template variable filters applied via Kibana dashboard controls; translation error: unsupported form... |
| Monitor Summary | System Overview - Sample | datadog | — |

**Pattern analysis:**

- **2×** Template variable filters applied via Kibana dashboard contr
- **1×** change calculation is approximated
- **1×** translation error: metric widget with grouped query needs a 
- **1×** translation error: formula syntax not recognized: top(query1
- **1×** translation error: unsupported formula function: diff
<!-- /GENERATED:NOT_FEASIBLE_BREAKDOWN -->

---

*Last generated: 2026-04-14 08:59 UTC*
