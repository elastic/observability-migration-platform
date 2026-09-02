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

Tag-backed Datadog `template_variables` are translated into Kibana dashboard
controls. Each variable's `tag` is resolved through the active field profile
to an Elasticsearch field, and the control applies dashboard-level filtering.
This does not cover Datadog's free-form `$scope`, log-query substitutions, or
dynamic group-by variables; those shapes are reported as explicit
degradations/manual work rather than being presented as clean replacements.

---

## Dashboard Summary

<!-- GENERATED:DASHBOARD_SUMMARY -->
| Source | Dashboard | Panels | Migrated | Warnings | Manual | Not Feasible | Skipped | Rows |
|--------|-----------|--------|----------|----------|--------|--------------|---------|------|
| datadog | Apache - Overview | 22 | 12 | 9 | 1 | 0 | 0 | 0 |
| datadog | Celery Overview | 17 | 10 | 1 | 2 | 0 | 4 | 0 |
| datadog | Consul Overview | 27 | 16 | 2 | 4 | 0 | 5 | 0 |
| datadog | Docker - Overview | 28 | 6 | 19 | 1 | 2 | 0 | 0 |
| datadog | HAProxy - Overview | 29 | 21 | 1 | 1 | 0 | 6 | 0 |
| datadog | Kafka, Zookeeper and Kafka Consumer Overview | 55 | 38 | 6 | 1 | 1 | 9 | 0 |
| datadog | Kubernetes - Overview | 57 | 2 | 41 | 4 | 0 | 10 | 0 |
| datadog | MongoDB - Overview | 43 | 12 | 21 | 1 | 0 | 9 | 0 |
| datadog | MySQL - Overview | 11 | 0 | 11 | 0 | 0 | 0 | 0 |
| datadog | NGINX - Overview | 27 | 15 | 3 | 2 | 1 | 6 | 0 |
| datadog | Postgres - Metrics | 9 | 0 | 9 | 0 | 0 | 0 | 0 |
| datadog | RabbitMQ Overview (OpenMetrics Version) | 47 | 28 | 9 | 3 | 1 | 6 | 0 |
| datadog | Redis - Overview | 43 | 7 | 29 | 0 | 0 | 7 | 0 |
| datadog | Datadog Kitchen Sink Canary | 25 | 16 | 5 | 3 | 0 | 1 | 0 |
| datadog | System Overview - Sample | 11 | 9 | 1 | 1 | 0 | 0 | 0 |

**15 dashboards, 451 panels** audited from `infra/datadog/dashboards/`.
<!-- /GENERATED:DASHBOARD_SUMMARY -->

<!-- GENERATED:VERDICT_SUMMARY -->
## Verdict Summary

| Verdict | Count | Meaning |
|---------|-------|---------|
| **CORRECT** | 111 | Translation is semantically accurate |
| **MINOR_ISSUE** | 168 | Translated with approximations — review recommended |
| **EXPECTED_LIMITATION** | 172 | Known unsupported feature — placeholder or skip |
<!-- /GENERATED:VERDICT_SUMMARY -->

<!-- GENERATED:WARNING_PATTERNS -->
## Top Warning Patterns

| Count | Warning |
|------:|---------|
| 136 | Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana |
| 10 | rollup interval is approximated in ES\|QL |
| 9 | as_count interval semantics are approximated in ES\|QL |
| 7 | fill(zero) only applies to null values in returned rows; empty buckets may still be omitted |
| 5 | hostmap visual approximated as a grouped table; host dimensions and metric values are preserved, but Datadog tile coloring is not |
| 5 | rate semantics approximated with delta over observed bucket span; when switching Agent→OTel collection, map counters with --metric-map-file (transform/unit_scale) so RATE() emits against the OTel counter field |
| 5 | top(10) on timeseries approximated as ranked table of top-10 groups — ES\|QL cannot filter to N series in a single pass |
| 4 | XY chart grouped by multiple tags (haproxy_service, release); composited into a single breakdown column |
| 4 | default_zero() only coalesces returned rows; missing series or empty buckets may still be omitted |
| 3 | XY chart grouped by multiple tags (worker, task); composited into a single breakdown column |
| 3 | XY chart grouped by multiple tags (consul_service_id, consul_datacenter, host.name); composited into a single breakdown column |
| 3 | XY chart grouped by multiple tags (service.name, deployment.environment); composited into a single breakdown column |
| 3 | distribution widget approximated as its requested aggregation plus p50/p90/p99 percentile time series (ES\|QL has no native distribution histogram panel) |
| 3 | Log filter with template variable '$scope' was omitted because Datadog log template substitutions cannot be bound exactly in the translated query; recreate the filter in Kibana |
| 2 | Data source 'event_stream' has no direct Kibana equivalent; panel will be a placeholder |
<!-- /GENERATED:WARNING_PATTERNS -->

---

## Per-Dashboard Traces

<!-- GENERATED:PER_DASHBOARD_TRACES -->
### Datadog: Apache - Overview

**File:** `apache.json` — **Panels:** 22

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| 0 | `image` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 1 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Rate of requests | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:apache.net.request_per_s{$host,$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Apache process CPU usage (top 10 hosts) | `toplist` → `table` | warning | **MINOR_ISSUE** | top(avg:apache.performance.cpu_load{$host,$scope} by {host}, 10, 'mean', 'desc') | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 4 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Status of worker threads | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:apache.scoreboard.disabled{$host,$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 6 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Async connections | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:apache.conns_async_closing{$host,$scope}.rollup(max) | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Requests per second per host | `hostmap` → `table` | warning | **MINOR_ISSUE** | avg:apache.net.request_per_s{$scope} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Total async connections | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:apache.conns_total{$host,$scope}.rollup(max) | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 10 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 11 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 12 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Server can connect | `check_status` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Minimum uptime | `query_value` → `metric` | warning | **MINOR_ISSUE** | min:apache.performance.uptime{$host,$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Bytes served | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:apache.net.bytes{$host,$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 16 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 17 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Rate of bytes served | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:apache.net.bytes_per_s{$host,$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 19 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Log Events | `log_stream` → `table` | ok | **CORRECT** | source:apache | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND service... |
| 3962684562665668 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |

<details>
<summary>Detailed traces (22 panels)</summary>

#### 0

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (image):**

**Pipeline trace:**

- `plan` / `datadog.plan.text_widget` → selected markdown for text widget image

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: text widget (image)

**Verdict:** EXPECTED_LIMITATION

#### 1

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

#### Rate of requests

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:apache.net.request_per_s{$host,$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = SUM(apache_net_request_per_s) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `value`
- Output groups: `time_bucket`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### Apache process CPU usage (top 10 hosts)

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (toplist):**

```
top(avg:apache.performance.cpu_load{$host,$scope} by {host}, 10, 'mean', 'desc')
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_toplist` → selected esql toplist table
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (table):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS _bucket_value = AVG(apache_performance_cpu_load) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), host.name
| STATS value = AVG(_bucket_value) BY host.name
| SORT value DESC
| LIMIT 10
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: top list → esql table with ORDER BY + LIMIT

**Query IR:**

- Output metric: `value`
- Output groups: `host.name`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### 4

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

#### Status of worker threads

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:apache.scoreboard.disabled{$host,$scope}
```

```
sum:apache.scoreboard.starting_up{$host,$scope}
```

```
sum:apache.scoreboard.reading_request{$host,$scope}
```

```
sum:apache.scoreboard.sending_reply{$host,$scope}
```

```
sum:apache.scoreboard.keepalive{$host,$scope}
```

```
sum:apache.scoreboard.dns_lookup{$host,$scope}
```

```
sum:apache.scoreboard.closing_connection{$host,$scope}
```

```
sum:apache.scoreboard.logging{$host,$scope}
```

```
sum:apache.scoreboard.gracefully_finishing{$host,$scope}
```

```
sum:apache.scoreboard.waiting_for_connection{$host,$scope}
```

```
sum:apache.scoreboard.idle_cleanup{$host,$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query0 = SUM(apache_scoreboard_disabled), query1 = SUM(apache_scoreboard_starting_up), query2 = SUM(apache_scoreboard_reading_request), query3 = SUM(apache_scoreboard_sending_reply), query4 = SUM(apache_scoreboard_keepalive), query5 = SUM(apache_scoreboard_dns_lookup), query6 = SUM(apache_scoreboard_closing_connection), query7 = SUM(apache_scoreboard_logging), query8 = SUM(apache_scoreboard_gracefully_finishing), query9 = SUM(apache_scoreboard_waiting_for_connection), query10 = SUM(apache_scoreboard_idle_cleanup) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| KEEP time_bucket, query0, query1, query2, query3, query4, query5, query6, query7, query8, query9, query10
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query0`
- Output groups: `time_bucket`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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

#### Async connections

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:apache.conns_async_closing{$host,$scope}.rollup(max)
```

```
sum:apache.conns_async_writing{$host,$scope}.rollup(max)
```

```
sum:apache.conns_async_keep_alive{$host,$scope}.rollup(max)
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query0 = SUM(apache_conns_async_closing), query1 = SUM(apache_conns_async_writing), query2 = SUM(apache_conns_async_keep_alive) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| KEEP time_bucket, query0, query1, query2
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query0`
- Output groups: `time_bucket`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana; rollup interval is approximated in ES|QL

**Verdict:** MINOR_ISSUE

#### Requests per second per host

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (hostmap):**

```
avg:apache.net.request_per_s{$scope} by {host}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_hostmap` → selected ES|QL table for grouped hostmap
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (table):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = AVG(apache_net_request_per_s) BY host.name
| SORT value DESC
| LIMIT 100
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: grouped hostmap → data-preserving ES|QL table

**Query IR:**

- Output metric: `value`
- Output groups: `host.name`

**Warnings:** hostmap visual approximated as a grouped table; host dimensions and metric values are preserved, but Datadog tile coloring is not; Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### Total async connections

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:apache.conns_total{$host,$scope}.rollup(max)
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = SUM(apache_conns_total) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `value`
- Output groups: `time_bucket`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana; rollup interval is approximated in ES|QL

**Verdict:** MINOR_ISSUE

#### 10

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

#### 11

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

#### 12

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

#### Server can connect

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (check_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.status_placeholder` → selected markdown placeholder for status widget check_status

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: status widget (check_status) — placeholder for manual setup as an Elastic Synthetics check or Alert rule

**Verdict:** EXPECTED_LIMITATION

#### Minimum uptime

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
min:apache.performance.uptime{$host,$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS _bucket_value = MIN(apache_performance_uptime) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| WHERE _bucket_value IS NOT NULL
| STATS value = LAST(_bucket_value, time_bucket)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Controls / Variables (1)</summary>

- `host` (type: `options`)

</details>

<details>
<summary>Template Variables (2)</summary>

- `$host` → tag: `host`, default: `*`
- `$scope` → tag: `None`, default: `*`

</details>

---

### Datadog: Celery Overview

**File:** `celery.json` — **Panels:** 17

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| About Celery | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 7248787798500294 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 2783259848144032 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Overview | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 628034008442880 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Celery Flower OpenMetrics endpoint health | `check_status` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Celery Monitors | `manage_status` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Celery Monitoring | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 109699765942540 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Celery Worker Status | `query_table` → `table` | ok | **CORRECT** | avg:celery.flower.worker.online{$worker, $host, $endpoint} by {worker} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Number of tasks currently executing by worker | `toplist` → `table` | warning | **MINOR_ISSUE** | sum:celery.flower.events.count{$task,$endpoint} by {worker,task,type}.as_count() | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Task Prefetch Time at Worker | `timeseries` → `xy` | ok | **CORRECT** | sum:celery.flower.task.prefetch_time.seconds{$task,$endpoint} by {worker,task} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Number of  Prefetch Tasks at Worker | `timeseries` → `xy` | ok | **CORRECT** | sum:celery.flower.worker.prefetched_tasks{$task,$endpoint} by {worker,task} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Number of tasks currently executing by worker | `timeseries` → `xy` | ok | **CORRECT** | sum:celery.flower.worker.executing_tasks{$task,$endpoint} by {worker} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Number of tasks currently executing by worker | `timeseries` → `xy` | ok | **CORRECT** | sum:celery.flower.events.created{$task,$endpoint} by {worker,task} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Logs | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 1205253880262830 | `list_stream` → `table` | ok | **CORRECT** | source:celery | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND service... |

<details>
<summary>Detailed traces (17 panels)</summary>

#### About Celery

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

#### 7248787798500294

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

#### 2783259848144032

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

#### 628034008442880

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

#### Celery Flower OpenMetrics endpoint health

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (check_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.status_placeholder` → selected markdown placeholder for status widget check_status

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: status widget (check_status) — placeholder for manual setup as an Elastic Synthetics check or Alert rule

**Verdict:** EXPECTED_LIMITATION

#### Celery Monitors

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (manage_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.status_placeholder` → selected markdown placeholder for status widget manage_status

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: status widget (manage_status) — placeholder for manual setup as an Elastic Synthetics check or Alert rule

**Verdict:** EXPECTED_LIMITATION

#### Celery Monitoring

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

#### 109699765942540

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

#### Celery Worker Status

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_table):**

```
avg:celery.flower.worker.online{$worker, $host, $endpoint} by {worker}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_table` → selected esql table
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (table):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(celery_flower_worker_online) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), worker
| EVAL online = query1
| STATS online = AVG(online) BY worker
| KEEP worker, online
| SORT online DESC
| LIMIT 500
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: table → esql table

**Query IR:**

- Output metric: `online`
- Output groups: `worker`

**Verdict:** CORRECT

#### Number of tasks currently executing by worker

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (toplist):**

```
sum:celery.flower.events.count{$task,$endpoint} by {worker,task,type}.as_count()
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_toplist` → selected esql toplist table
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (table):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(celery_flower_events_count) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), worker, task, type
| WHERE query1 IS NOT NULL
| STATS query1 = LAST(query1, time_bucket) BY worker, task, type
| KEEP worker, task, type, query1
| SORT query1 ASC
| LIMIT 500
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: top list → esql table with ORDER BY + LIMIT

**Query IR:**

- Output metric: `query1`
- Output groups: `worker, task, type`

**Warnings:** as_count interval semantics are approximated in ES|QL

**Verdict:** MINOR_ISSUE

#### Task Prefetch Time at Worker

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:celery.flower.task.prefetch_time.seconds{$task,$endpoint} by {worker,task}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(celery_flower_task_prefetch_time_seconds) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), worker, task
| KEEP time_bucket, worker, task, query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket, worker, task`

**Warnings:** XY chart grouped by multiple tags (worker, task); composited into a single breakdown column

**Verdict:** CORRECT

#### Number of  Prefetch Tasks at Worker

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:celery.flower.worker.prefetched_tasks{$task,$endpoint} by {worker,task}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(celery_flower_worker_prefetched_tasks) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), worker, task
| EVAL tasks = query1
| KEEP time_bucket, worker, task, tasks
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `tasks`
- Output groups: `time_bucket, worker, task`

**Warnings:** XY chart grouped by multiple tags (worker, task); composited into a single breakdown column

**Verdict:** CORRECT

#### Number of tasks currently executing by worker

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:celery.flower.worker.executing_tasks{$task,$endpoint} by {worker}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(celery_flower_worker_executing_tasks) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), worker
| KEEP time_bucket, worker, query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket, worker`

**Verdict:** CORRECT

#### Number of tasks currently executing by worker

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:celery.flower.events.created{$task,$endpoint} by {worker,task}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(celery_flower_events_created) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), worker, task
| KEEP time_bucket, worker, task, query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket, worker, task`

**Warnings:** XY chart grouped by multiple tags (worker, task); composited into a single breakdown column

**Verdict:** CORRECT

</details>

<details>
<summary>Controls / Variables (4)</summary>

- `endpoint` (type: `options`)
- `host` (type: `options`)
- `task` (type: `options`)
- `worker` (type: `options`)

</details>

<details>
<summary>Template Variables (4)</summary>

- `$endpoint` → tag: `endpoint`, default: `*`
- `$host` → tag: `host`, default: `*`
- `$task` → tag: `task`, default: `*`
- `$worker` → tag: `worker`, default: `*`

</details>

---

### Datadog: Consul Overview

**File:** `consul.json` — **Panels:** 27

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| About Consul | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 5050150571228312 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 5453680244314134 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Overview | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 2570179001713344 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Healthy | `check_status` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Can Connect | `check_status` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Consul Monitor Status | `manage_status` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Catalog Node and Services | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 3272632007036748 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Nodes Critical | `timeseries` → `xy` | ok | **CORRECT** | sum:consul.catalog.nodes_critical{$consul_service_id, $host, $consul_datacenter}... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Nodes Up | `timeseries` → `xy` | ok | **CORRECT** | sum:consul.catalog.nodes_up{$host, $consul_datacenter, $consul_service_id} by {c... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Nodes Warning | `timeseries` → `xy` | ok | **CORRECT** | sum:consul.catalog.nodes_warning{$consul_service_id,$host, $consul_datacenter} b... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Nodes Passing | `timeseries` → `xy` | ok | **CORRECT** | sum:consul.catalog.nodes_passing{$consul_service_id,$host, $consul_datacenter} b... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Services Warning | `sunburst` → `partition` | ok | **CORRECT** | sum:consul.catalog.services_warning{$host} by {consul_node_id,consul_datacenter,... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Services Critical | `query_table` → `table` | ok | **CORRECT** | sum:consul.catalog.services_critical{$host} by {consul_node_id,consul_datacenter... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Raft Leader Metrics | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 1689828293212788 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Leader Last Contact with Followers (in ms) | `timeseries` → `xy` | ok | **CORRECT** | sum:consul.raft.leader.lastContact.max{$host} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Latency of Leader Commit to Disk | `timeseries` → `xy` | ok | **CORRECT** | sum:consul.raft.leader.dispatchLog.max{$host} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| New Leader Events | `list_stream` → `table` | requires_manual | **EXPECTED_LIMITATION** |  | — |
| Consul Raft Commit Time | `timeseries` → `xy` | ok | **CORRECT** | sum:consul.raft.commitTime.avg{$host} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Consul Logs | `list_stream` → `table` | ok | **CORRECT** | source:consul $host  | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND KQL("se... |
| Memberlist Messages | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 6987405009110066 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Memberlist TCP | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:consul.memberlist.tcp.connect{$host}.as_count() | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Memberlist UDP | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:consul.memberlist.tcp.sent{$host}.as_count() | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |

<details>
<summary>Detailed traces (27 panels)</summary>

#### About Consul

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

#### 5050150571228312

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

#### 5453680244314134

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

#### 2570179001713344

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

#### Healthy

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (check_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.status_placeholder` → selected markdown placeholder for status widget check_status

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: status widget (check_status) — placeholder for manual setup as an Elastic Synthetics check or Alert rule

**Verdict:** EXPECTED_LIMITATION

#### Can Connect

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (check_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.status_placeholder` → selected markdown placeholder for status widget check_status

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: status widget (check_status) — placeholder for manual setup as an Elastic Synthetics check or Alert rule

**Verdict:** EXPECTED_LIMITATION

#### Consul Monitor Status

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (manage_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.status_placeholder` → selected markdown placeholder for status widget manage_status

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: status widget (manage_status) — placeholder for manual setup as an Elastic Synthetics check or Alert rule

**Verdict:** EXPECTED_LIMITATION

#### Catalog Node and Services

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

#### 3272632007036748

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

#### Nodes Critical

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:consul.catalog.nodes_critical{$consul_service_id, $host, $consul_datacenter} by {consul_service_id,consul_datacenter,host}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(consul_catalog_nodes_critical) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), consul_service_id, consul_datacenter, host.name
| KEEP time_bucket, consul_service_id, consul_datacenter, host.name, query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket, consul_service_id, consul_datacenter, host.name`

**Warnings:** XY chart grouped by multiple tags (consul_service_id, consul_datacenter, host.name); composited into a single breakdown column

**Verdict:** CORRECT

#### Nodes Up

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:consul.catalog.nodes_up{$host, $consul_datacenter, $consul_service_id} by {consul_service_id,consul_datacenter,host}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(consul_catalog_nodes_up) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), consul_service_id, consul_datacenter, host.name
| KEEP time_bucket, consul_service_id, consul_datacenter, host.name, query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket, consul_service_id, consul_datacenter, host.name`

**Warnings:** XY chart grouped by multiple tags (consul_service_id, consul_datacenter, host.name); composited into a single breakdown column

**Verdict:** CORRECT

#### Nodes Warning

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:consul.catalog.nodes_warning{$consul_service_id,$host, $consul_datacenter} by {consul_service_id,consul_datacenter}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(consul_catalog_nodes_warning) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), consul_service_id, consul_datacenter
| KEEP time_bucket, consul_service_id, consul_datacenter, query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket, consul_service_id, consul_datacenter`

**Warnings:** XY chart grouped by multiple tags (consul_service_id, consul_datacenter); composited into a single breakdown column

**Verdict:** CORRECT

#### Nodes Passing

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:consul.catalog.nodes_passing{$consul_service_id,$host, $consul_datacenter} by {consul_service_id,consul_datacenter,host}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(consul_catalog_nodes_passing) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), consul_service_id, consul_datacenter, host.name
| KEEP time_bucket, consul_service_id, consul_datacenter, host.name, query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket, consul_service_id, consul_datacenter, host.name`

**Warnings:** XY chart grouped by multiple tags (consul_service_id, consul_datacenter, host.name); composited into a single breakdown column

**Verdict:** CORRECT

#### Services Warning

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (sunburst):**

```
sum:consul.catalog.services_warning{$host} by {consul_node_id,consul_datacenter,host}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_partition` → selected ES|QL partition chart for sunburst
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (partition):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(consul_catalog_services_warning) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), consul_node_id, consul_datacenter, host.name
| STATS query1 = SUM(query1) BY consul_node_id, consul_datacenter, host.name
| KEEP consul_node_id, consul_datacenter, host.name, query1
| SORT query1 DESC
| LIMIT 100
```

**Plan:**

- Backend: `esql`
- Kibana type: `partition`
- Data source: `metrics`
- Reasons: sunburst → ES|QL partition chart

**Query IR:**

- Output metric: `query1`
- Output groups: `consul_node_id, consul_datacenter, host.name`

**Verdict:** CORRECT

</details>

<details>
<summary>Controls / Variables (3)</summary>

- `host` (type: `options`)
- `consul_service_id` (type: `options`)
- `consul_datacenter` (type: `options`)

</details>

<details>
<summary>Template Variables (3)</summary>

- `$host` → tag: `@host`, default: `*`
- `$consul_service_id` → tag: `@consul_service_id`, default: `*`
- `$consul_datacenter` → tag: `consul_datacenter`, default: `*`

</details>

---

### Datadog: Docker - Overview

**File:** `docker.json` — **Panels:** 28

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Running containers by image | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:docker.containers.running{$scope} by {docker_image}.fill(0) | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Most RAM-intensive containers | `toplist` → `table` | warning | **MINOR_ISSUE** | top(avg:docker.mem.rss{$scope} by {container_name}, 5, 'max', 'desc') | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Most CPU-intensive containers | `toplist` → `table` | warning | **MINOR_ISSUE** | top(avg:docker.cpu.user{$scope} by {container_name}, 5, 'max', 'desc') | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Memory by container | `heatmap` → `heatmap` | warning | **MINOR_ISSUE** | avg:docker.mem.rss{$scope} by {container_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Running containers | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:docker.containers.running{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Stopped containers | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:docker.containers.stopped{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| CPU by container | `heatmap` → `heatmap` | warning | **MINOR_ISSUE** | avg:docker.cpu.user{$scope} by {container_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| CPU user by image | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:docker.cpu.user{$scope} by {docker_image}.fill(0) | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| RSS memory by image | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:docker.mem.rss{$scope} by {docker_image}.fill(0) | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 9 | `event_stream` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | — | — |
| 10 | `event_timeline` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | — | — |
| Running container change | `query_value` → `metric` | requires_manual | **EXPECTED_LIMITATION** | 100*(sum:docker.containers.running{$scope}/timeshift(sum:docker.containers.runni... | — |
| CPU system by image | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:docker.cpu.system{$scope} by {docker_image}.fill(0) | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 13 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Cache memory by image | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:docker.mem.cache{$scope} by {docker_image} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 15 | `image` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 16 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 17 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Swap by image | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:docker.mem.swap{$scope} by {docker_image} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 19 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Avg. I/O bytes read by image | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:docker.io.read_bytes{$scope} by {docker_image} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Avg. I/O bytes written by image | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:docker.io.write_bytes{$scope} by {docker_image} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 22 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Avg. rx bytes by image | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:docker.net.bytes_rcvd{$scope} by {docker_image} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Avg. tx bytes by image | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:docker.net.bytes_sent{$scope} by {docker_image} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Most tx-intensive containers | `toplist` → `table` | warning | **MINOR_ISSUE** | top(avg:docker.net.bytes_sent{$scope} by {container_name}, 5, 'max', 'desc') | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| tx by container | `heatmap` → `heatmap` | warning | **MINOR_ISSUE** | avg:docker.net.bytes_sent{$scope} by {container_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Running containers by image | `toplist` → `table` | warning | **MINOR_ISSUE** | top(timeshift(sum:docker.containers.running{$scope} by {docker_image}.fill(60), ... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |

<details>
<summary>Detailed traces (28 panels)</summary>

#### Running containers by image

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:docker.containers.running{$scope} by {docker_image}.fill(0)
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = SUM(docker_containers_running) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), docker_image
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `value`
- Output groups: `time_bucket, docker_image`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### Most RAM-intensive containers

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (toplist):**

```
top(avg:docker.mem.rss{$scope} by {container_name}, 5, 'max', 'desc')
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_toplist` → selected esql toplist table
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (table):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS _bucket_value = AVG(docker_mem_rss) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), container.name
| STATS value = MAX(_bucket_value) BY container.name
| SORT value DESC
| LIMIT 5
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: top list → esql table with ORDER BY + LIMIT

**Query IR:**

- Output metric: `value`
- Output groups: `container.name`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### Most CPU-intensive containers

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (toplist):**

```
top(avg:docker.cpu.user{$scope} by {container_name}, 5, 'max', 'desc')
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_toplist` → selected esql toplist table
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (table):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS _bucket_value = AVG(docker_cpu_user) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), container.name
| STATS value = MAX(_bucket_value) BY container.name
| SORT value DESC
| LIMIT 5
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: top list → esql table with ORDER BY + LIMIT

**Query IR:**

- Output metric: `value`
- Output groups: `container.name`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = AVG(docker_mem_rss) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), container.name
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### Running containers

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:docker.containers.running{$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS _bucket_value = SUM(docker_containers_running) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| WHERE _bucket_value IS NOT NULL
| STATS value = LAST(_bucket_value, time_bucket)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### Stopped containers

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:docker.containers.stopped{$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS _bucket_value = SUM(docker_containers_stopped) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| WHERE _bucket_value IS NOT NULL
| STATS value = LAST(_bucket_value, time_bucket)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = AVG(docker_cpu_user) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), container.name
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### CPU user by image

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
avg:docker.cpu.user{$scope} by {docker_image}.fill(0)
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = AVG(docker_cpu_user) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), docker_image
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `value`
- Output groups: `time_bucket, docker_image`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### RSS memory by image

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
avg:docker.mem.rss{$scope} by {docker_image}.fill(0)
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = AVG(docker_mem_rss) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), docker_image
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `value`
- Output groups: `time_bucket, docker_image`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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

- `plan` / `datadog.plan.unparsed_query` → selected markdown because the query uses unsupported timeshift()

**Plan:**

- Backend: `markdown`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: query uses Datadog timeshift(), which has no ES|QL equivalent

**Warnings:** timeshift() is not translatable; rebuild this widget in Kibana or drop the function

**Verdict:** EXPECTED_LIMITATION

#### CPU system by image

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
avg:docker.cpu.system{$scope} by {docker_image}.fill(0)
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = AVG(docker_cpu_system) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), docker_image
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `value`
- Output groups: `time_bucket, docker_image`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:docker.mem.cache{$scope} by {docker_image}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = SUM(docker_mem_cache) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), docker_image
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `value`
- Output groups: `time_bucket, docker_image`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Template Variables (1)</summary>

- `$scope` → tag: `None`, default: `*`

</details>

---

### Datadog: HAProxy - Overview

**File:** `haproxy.json` — **Panels:** 29

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| About HAProxy | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 2451661469305854 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 6085654256880802 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Overview | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Incoming Success Rate (non-5xx responses) | `query_value` → `table` | warning | **MINOR_ISSUE** | sum:haproxy.backend.response.4xx{*,*,$backend} by {haproxy_service} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Incoming Requests Volume | `query_value` → `metric` | ok | **CORRECT** | sum:haproxy.backend.response.4xx{*,*,$backend} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Errors by Service | `sunburst` → `partition` | ok | **CORRECT** | sum:haproxy.backend.response.4xx{*,*,$backend} by {service} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Frontend | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Frontend Response codes | `timeseries` → `xy` | ok | **CORRECT** | sum:haproxy.frontend.response.2xx{*,*,$frontend,$release} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 6952725003844530 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 2xx Frontend Responses | `timeseries` → `xy` | ok | **CORRECT** | sum:haproxy.frontend.response.2xx{*,*,$frontend,$release} by {haproxy_service,re... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 3xx Frontend Responses | `timeseries` → `xy` | ok | **CORRECT** | sum:haproxy.frontend.response.3xx{*,*,$frontend,$release} by {haproxy_service,re... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 4xx Frontend Responses | `timeseries` → `xy` | ok | **CORRECT** | sum:haproxy.frontend.response.4xx{*,*,$frontend,$release} by {haproxy_service,re... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 5xx Frontend Responses | `timeseries` → `xy` | ok | **CORRECT** | sum:haproxy.frontend.response.5xx{*,*,$frontend,$release} by {haproxy_service,re... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Logs | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| HAProxy Logs | `list_stream` → `table` | ok | **CORRECT** | source:*haproxy* | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND service... |
| Backend | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Backend response codes by Release | `timeseries` → `xy` | ok | **CORRECT** | sum:haproxy.backend.response.2xx{*,*,$backend} by {haproxy_service} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 2xx Backend Responses by Release | `timeseries` → `xy` | ok | **CORRECT** | sum:haproxy.backend.response.2xx{*,*,$backend} by {haproxy_service} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 3xx Backend Responses by Release | `timeseries` → `xy` | ok | **CORRECT** | sum:haproxy.backend.response.3xx{*,*,$backend} by {haproxy_service} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 1863184948995790 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 4xx Backend Responses by Release | `timeseries` → `xy` | ok | **CORRECT** | sum:haproxy.backend.response.4xx{*,*,$backend} by {haproxy_service} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 5xx Backend Responses by Release | `timeseries` → `xy` | ok | **CORRECT** | sum:haproxy.backend.response.5xx{*,*,$backend} by {haproxy_service} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 3773616402244664 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Backend p99 Response Time | `timeseries` → `xy` | ok | **CORRECT** | avg:haproxy.backend.response.time{*,*,*,*} by {release} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Pod Statistics | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Top Resource Consumers | `query_table` → `table` | ok | **CORRECT** | sum:kubernetes.cpu.usage.total{*,*,*,*,*,*,kube_namespace:ingress-haproxy,*,shor... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND k8s.... |
| CPU Usage by Pod | `timeseries` → `xy` | requires_manual | **MINOR_ISSUE** | sum:kubernetes.cpu.usage.total{*,*,*,*,*,*,kube_namespace:ingress-haproxy,*,shor... | — |
| Memory Usage % | `timeseries` → `xy` | ok | **CORRECT** | max:kubernetes.memory.usage{team:logs,*,*,*,datacenter:us1.prod.dog,*,*,*,kube_c... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND team... |

<details>
<summary>Detailed traces (29 panels)</summary>

#### About HAProxy

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

#### 2451661469305854

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

#### 6085654256880802

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

#### Incoming Success Rate (non-5xx responses)

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:haproxy.backend.response.4xx{*,*,$backend} by {haproxy_service}
```

```
sum:haproxy.backend.response.3xx{*,*,$backend} by {haproxy_service}
```

```
sum:haproxy.backend.response.2xx{*,*,$backend} by {haproxy_service}
```

```
sum:haproxy.backend.response.5xx{*,*,$backend} by {haproxy_service}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (table):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query3 = SUM(haproxy_backend_response_4xx), query1 = SUM(haproxy_backend_response_3xx), query2 = SUM(haproxy_backend_response_2xx), query4 = SUM(haproxy_backend_response_5xx) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), haproxy_service
| EVAL value = ((((query3 + query1) + query2) / (((query3 + query1) + query2) + query4)) * 100)
| STATS value = AVG(value) BY haproxy_service
| KEEP haproxy_service, value
| SORT value DESC
| LIMIT 100
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`
- Output groups: `haproxy_service`

**Warnings:** Grouped single-value formula widget approximated as a summary table (one value per group); a metric tile shows only a single number

**Verdict:** MINOR_ISSUE

#### Incoming Requests Volume

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:haproxy.backend.response.4xx{*,*,$backend}
```

```
sum:haproxy.backend.response.3xx{*,*,$backend}
```

```
sum:haproxy.backend.response.2xx{*,*,$backend}
```

```
sum:haproxy.backend.response.5xx{*,*,$backend}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(haproxy_backend_response_4xx), query3 = SUM(haproxy_backend_response_3xx), query2 = SUM(haproxy_backend_response_2xx), query4 = SUM(haproxy_backend_response_5xx) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| EVAL value = (((query1 + query3) + query2) + query4)
| STATS value = AVG(value)
| KEEP value
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Verdict:** CORRECT

#### Errors by Service

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (sunburst):**

```
sum:haproxy.backend.response.4xx{*,*,$backend} by {service}
```

```
sum:haproxy.backend.response.5xx{*,*,$backend} by {service}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_partition` → selected ES|QL partition chart for sunburst
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (partition):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(haproxy_backend_response_4xx), query2 = SUM(haproxy_backend_response_5xx) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), service.name
| EVAL query1_query2 = (query1 + query2)
| STATS query1_query2 = AVG(query1_query2) BY service.name
| KEEP service.name, query1_query2
| SORT query1_query2 DESC
| LIMIT 100
```

**Plan:**

- Backend: `esql`
- Kibana type: `partition`
- Data source: `metrics`
- Reasons: sunburst → ES|QL partition chart

**Query IR:**

- Output metric: `query1_query2`
- Output groups: `service.name`

**Verdict:** CORRECT

#### Frontend

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

#### Frontend Response codes

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:haproxy.frontend.response.2xx{*,*,$frontend,$release}
```

```
sum:haproxy.frontend.response.1xx{*,*,$frontend,$release}
```

```
sum:haproxy.frontend.response.3xx{*,*,$frontend,$release}
```

```
sum:haproxy.frontend.response.4xx{*,*,$frontend,$release}
```

```
sum:haproxy.frontend.response.5xx{*,*,$frontend,$release}
```

```
sum:haproxy.frontend.response.other{*,*,$frontend,$release}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(haproxy_frontend_response_2xx), query2 = SUM(haproxy_frontend_response_1xx), query3 = SUM(haproxy_frontend_response_3xx), query4 = SUM(haproxy_frontend_response_4xx), query5 = SUM(haproxy_frontend_response_5xx), query6 = SUM(haproxy_frontend_response_other) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| EVAL f_2xx = query1, f_1xx = query2, f_3xx = query3, f_4xx = query4, f_5xx = query5, other = query6
| KEEP time_bucket, f_2xx, f_1xx, f_3xx, f_4xx, f_5xx, other
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `f_2xx`
- Output groups: `time_bucket`

**Verdict:** CORRECT

#### 6952725003844530

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

#### 2xx Frontend Responses

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:haproxy.frontend.response.2xx{*,*,$frontend,$release} by {haproxy_service,release}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(haproxy_frontend_response_2xx) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), haproxy_service, release
| KEEP time_bucket, haproxy_service, release, query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket, haproxy_service, release`

**Warnings:** XY chart grouped by multiple tags (haproxy_service, release); composited into a single breakdown column

**Verdict:** CORRECT

#### 3xx Frontend Responses

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:haproxy.frontend.response.3xx{*,*,$frontend,$release} by {haproxy_service,release}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(haproxy_frontend_response_3xx) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), haproxy_service, release
| KEEP time_bucket, haproxy_service, release, query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket, haproxy_service, release`

**Warnings:** XY chart grouped by multiple tags (haproxy_service, release); composited into a single breakdown column

**Verdict:** CORRECT

#### 4xx Frontend Responses

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:haproxy.frontend.response.4xx{*,*,$frontend,$release} by {haproxy_service,release}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(haproxy_frontend_response_4xx) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), haproxy_service, release
| KEEP time_bucket, haproxy_service, release, query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket, haproxy_service, release`

**Warnings:** XY chart grouped by multiple tags (haproxy_service, release); composited into a single breakdown column

**Verdict:** CORRECT

#### 5xx Frontend Responses

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:haproxy.frontend.response.5xx{*,*,$frontend,$release} by {haproxy_service,release}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(haproxy_frontend_response_5xx) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), haproxy_service, release
| KEEP time_bucket, haproxy_service, release, query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket, haproxy_service, release`

**Warnings:** XY chart grouped by multiple tags (haproxy_service, release); composited into a single breakdown column

**Verdict:** CORRECT

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

</details>

<details>
<summary>Controls / Variables (3)</summary>

- `frontend` (type: `options`)
- `backend` (type: `options`)
- `release` (type: `options`)

</details>

<details>
<summary>Template Variables (3)</summary>

- `$frontend` → tag: `haproxy_service`, default: `*`
- `$backend` → tag: `haproxy_service`, default: `*`
- `$release` → tag: `release`, default: `*`

</details>

---

### Datadog: Kafka, Zookeeper and Kafka Consumer Overview

**File:** `kafka.json` — **Panels:** 55

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| 5924016577024848 | `image` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 7914240170882312 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Data Streams Monitoring | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 8756831542082047 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Kafka Topology for $topic, $env | `topology_map` → `markdown` | not_feasible | **EXPECTED_LIMITATION** |  | — |
| Topic Health | `query_table` → `table` | warning | **MINOR_ISSUE** | count:data_streams.latency{type:kafka AND direction:out AND (pathway_type:edge O... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND ((ty... |
| Clusters Health | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Offline Partitions | `query_value` → `metric` | ok | **CORRECT** | sum:kafka.replication.offline_partitions_count{*}.weighted() | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Under Replicated Partitions | `query_value` → `metric` | ok | **CORRECT** | sum:kafka.replication.under_replicated_partitions{$env}.weighted() | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| ISR Delta | `query_value` → `metric` | ok | **CORRECT** | max:kafka.replication.isr_expands.rate{$env} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Follower Fetch Latency | `query_value` → `metric` | ok | **CORRECT** | avg:kafka.request.fetch_follower.time.avg{$env} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Consumer Fetch Latency | `query_value` → `metric` | ok | **CORRECT** | avg:kafka.request.fetch_consumer.time.avg{$env} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Produce Latency | `query_value` → `metric` | ok | **CORRECT** | avg:kafka.request.produce.time.avg{$env} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Kafka Monitors | `manage_status` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Broker Metrics | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 3570061571959430 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Clean and Unclean Leader Elections | `timeseries` → `xy` | ok | **CORRECT** | avg:kafka.replication.leader_elections.rate{$env} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Broker Network Throughput | `timeseries` → `xy` | ok | **CORRECT** | sum:kafka.net.bytes_in.rate{$env} by {env}.weighted() | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Producer and Fetch Request Purgatory  | `timeseries` → `xy` | ok | **CORRECT** | sum:kafka.request.producer_request_purgatory.size{$env} by {env}.weighted() | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Request Times | `timeseries` → `xy` | ok | **CORRECT** | avg:kafka.request.produce.time.avg{$env} by {env} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Producer Metrics | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 4804179441675328 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Bytes Out by Topic | `timeseries` → `xy` | ok | **CORRECT** | sum:kafka.producer.bytes_out{$env} by {topic,env}.weighted() | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Request and Response Rate | `timeseries` → `xy` | ok | **CORRECT** | sum:kafka.producer.request_rate{$env} by {host}.weighted() | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Average Request Latency | `timeseries` → `xy` | ok | **CORRECT** | sum:kafka.producer.request_latency_avg{$env} by {env}.weighted() | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| I/O Wait Time | `timeseries` → `xy` | ok | **CORRECT** | sum:kafka.producer.io_wait{$env} by {env}.weighted() | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Consumer Metrics | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 2581435872418256 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Consumer Lag by Group | `timeseries` → `xy` | ok | **CORRECT** | sum:kafka.consumer_lag{$env,$consumer_group} by {host,consumer_group,env} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Bytes Consumed | `timeseries` → `xy` | ok | **CORRECT** | sum:kafka.consumer.bytes_in{$env,$consumer_group} by {consumer_group,env}.weight... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Messages Consumed | `timeseries` → `xy` | ok | **CORRECT** | sum:kafka.consumer.messages_in{$env,$consumer_group} by {client-id,env}.weighted... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Min Fetch Rate | `timeseries` → `xy` | ok | **CORRECT** | sum:kafka.consumer.fetch_rate{$env,$consumer_group} by {env,consumer_group}.weig... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Lag, Throughput and Message Size | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 8236156193990667 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Top 10 Max Kafka Lag by env | `timeseries` → `table` | warning | **MINOR_ISSUE** | max:data_streams.kafka.lag_seconds{$topic ,$env} by {env,topic} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Incoming messages by env, producing service for $topic | `timeseries` → `xy` | warning | **MINOR_ISSUE** | count(v: v>=0):data_streams.latency{direction:out,pathway_type:full,type:kafka,$... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND dire... |
| Outgoing messages by env, consuming service for $topic | `timeseries` → `xy` | warning | **MINOR_ISSUE** | count(v: v>=0):data_streams.latency{direction:in,pathway_type:full,type:kafka,$t... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND dire... |
| Top 10 p95 message size by env for $topic | `timeseries` → `table` | warning | **MINOR_ISSUE** | p95:data_streams.payload_size{type:kafka,$topic,$env} by {topic,env} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND type... |
| Distribution of message size for $topic | `distribution` → `xy` | warning | **MINOR_ISSUE** | avg:data_streams.payload_size{type:kafka,$topic,$env} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND type... |
| Zookeeper Metrics | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 3441872316411158 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| ZK File Descriptors  | `timeseries` → `xy` | ok | **CORRECT** | avg:zookeeper.max_file_descriptor_count{$env} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Active Connections | `heatmap` → `heatmap` | ok | **CORRECT** | sum:zookeeper.connections{$env} by {service,host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Pending Syncs (Leader) | `timeseries` → `xy` | ok | **CORRECT** | sum:zookeeper.pending_syncs{$env} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Average Request Latency | `timeseries` → `xy` | ok | **CORRECT** | sum:zookeeper.avg_latency{$env} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Outstanding Requests | `heatmap` → `heatmap` | ok | **CORRECT** | sum:zookeeper.outstanding_requests{$env} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Number of Commits (Leader) | `timeseries` → `xy` | ok | **CORRECT** | sum:zookeeper.commit_count{$env,$consumer_group} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Broker JVM Metrics | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 5500612573375596 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| JVM GC Count by Type | `timeseries` → `xy` | ok | **CORRECT** | sum:jvm.gc.major_collection_count{$env} by {type,env}.weighted() | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| JVM GC Time by Type | `timeseries` → `xy` | ok | **CORRECT** | sum:jvm.gc.major_collection_time{$env} by {type,env}.weighted() | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Logs | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 7549984154998154 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Count per Log Status | `timeseries` → `xy` | ok | **CORRECT** | source:kafka | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND service... |
| Error Logs | `list_stream` → `table` | ok | **CORRECT** | source:kafka status:error | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND service... |

<details>
<summary>Detailed traces (55 panels)</summary>

#### 5924016577024848

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (image):**

**Pipeline trace:**

- `plan` / `datadog.plan.text_widget` → selected markdown for text widget image

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: text widget (image)

**Verdict:** EXPECTED_LIMITATION

#### 7914240170882312

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

#### Data Streams Monitoring

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

#### 8756831542082047

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

#### Kafka Topology for $topic, $env

**Translation path:** `blocked` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (topology_map):**

```

```

**Pipeline trace:**

- `plan` / `datadog.plan.unsupported_widget` → blocked unsupported widget type topology_map

**Plan:**

- Backend: `blocked`
- Kibana type: `markdown`
- Data source: `data_streams`
- Reasons: unsupported widget type: topology_map

**Verdict:** EXPECTED_LIMITATION

#### Topic Health

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_table):**

```
count:data_streams.latency{type:kafka AND direction:out AND (pathway_type:edge OR pathway_type:partial_edge) AND $topic AND $env} by {topic,env}.as_rate()
```

```
count:data_streams.latency{type:kafka AND direction:in AND (pathway_type:edge OR pathway_type:partial_edge) AND $topic AND $env} by {topic,env}.as_rate()
```

```
max:data_streams.kafka.lag_seconds{$topic,$env} by {topic,env}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_table` → selected esql table
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (table):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND ((type == "kafka" AND direction == "out" AND (pathway_type == "edge" OR pathway_type == "partial_edge")) OR (type == "kafka" AND direction == "in" AND (pathway_type == "edge" OR pathway_type == "partial_edge")))
| STATS query1 = COUNT(*) / (DATE_DIFF("seconds", MIN(@timestamp), MAX(@timestamp)) + 1) WHERE type == "kafka" AND direction == "out" AND (pathway_type == "edge" OR pathway_type == "partial_edge"), query2 = COUNT(*) / (DATE_DIFF("seconds", MIN(@timestamp), MAX(@timestamp)) + 1) WHERE type == "kafka" AND direction == "in" AND (pathway_type == "edge" OR pathway_type == "partial_edge"), query3 = MAX(data_streams_kafka_lag_seconds) BY time_bucket = BUCKET(@timestamp, 20, ?_tstart, ?_tend), topic, deployment.environment
| EVAL messages_in = query1, messages_out = query2, max_kafka_lag = query3
| STATS messages_in = AVG(messages_in), messages_out = AVG(messages_out), max_kafka_lag = LAST(max_kafka_lag, time_bucket) BY topic, deployment.environment
| KEEP topic, deployment.environment, messages_in, messages_out, max_kafka_lag
| SORT messages_in DESC
| LIMIT 500
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: table → esql table

**Query IR:**

- Output metric: `messages_in`
- Output groups: `topic, deployment.environment`

**Warnings:** rate semantics approximated with delta over observed bucket span; when switching Agent→OTel collection, map counters with --metric-map-file (transform/unit_scale) so RATE() emits against the OTel counter field

**Verdict:** MINOR_ISSUE

#### Clusters Health

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

#### Offline Partitions

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:kafka.replication.offline_partitions_count{*}.weighted()
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(kafka_replication_offline_partitions_count) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| EVAL value = query1
| STATS value = AVG(value)
| KEEP value
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Verdict:** CORRECT

#### Under Replicated Partitions

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:kafka.replication.under_replicated_partitions{$env}.weighted()
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(kafka_replication_under_replicated_partitions) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| EVAL value = query1
| STATS value = AVG(value)
| KEEP value
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Verdict:** CORRECT

#### ISR Delta

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
max:kafka.replication.isr_expands.rate{$env}
```

```
max:kafka.replication.isr_shrinks.rate{$env}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = MAX(kafka_replication_isr_expands_rate), query2 = MAX(kafka_replication_isr_shrinks_rate) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| EVAL value = (query1 - query2)
| STATS value = AVG(value)
| KEEP value
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Verdict:** CORRECT

#### Follower Fetch Latency

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
avg:kafka.request.fetch_follower.time.avg{$env}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(kafka_request_fetch_follower_time_avg) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| EVAL value = query1
| STATS value = AVG(value)
| KEEP value
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Verdict:** CORRECT

#### Consumer Fetch Latency

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
avg:kafka.request.fetch_consumer.time.avg{$env}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(kafka_request_fetch_consumer_time_avg) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| EVAL value = query1
| STATS value = AVG(value)
| KEEP value
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Verdict:** CORRECT

#### Produce Latency

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
avg:kafka.request.produce.time.avg{$env}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(kafka_request_produce_time_avg) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| EVAL value = query1
| STATS value = AVG(value)
| KEEP value
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Verdict:** CORRECT

#### Kafka Monitors

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (manage_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.status_placeholder` → selected markdown placeholder for status widget manage_status

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: status widget (manage_status) — placeholder for manual setup as an Elastic Synthetics check or Alert rule

**Verdict:** EXPECTED_LIMITATION

#### Broker Metrics

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
<summary>Controls / Variables (3)</summary>

- `env` (type: `options`)
- `consumer_group` (type: `options`)
- `topic` (type: `options`)

</details>

<details>
<summary>Template Variables (3)</summary>

- `$env` → tag: `env`, default: `*`
- `$consumer_group` → tag: `consumer_group`, default: `*`
- `$topic` → tag: `topic`, default: `*`

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
| Clusters | `query_value` → `metric` | warning | **MINOR_ISSUE** | avg:kubernetes.pods.running{$scope,$label,$node,$service,$deployment,$statefulse... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Nodes | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:kubernetes_state.node.count{$scope,$label,$node,$service,$namespace,$cluster... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Namespaces | `query_value` → `metric` | warning | **MINOR_ISSUE** | avg:kubernetes.pods.running{$scope,$label,$node,$service,$deployment,$statefulse... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| DaemonSets | `query_value` → `metric` | warning | **MINOR_ISSUE** | avg:kubernetes_state.daemonset.desired{$scope,$label,$node,$service,$daemonset,$... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Services | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:kubernetes_state.service.count{$scope,$label,$node,$service,$namespace,$clus... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Deployments | `query_value` → `metric` | warning | **MINOR_ISSUE** | avg:kubernetes_state.deployment.replicas{$scope,$label,$node,$service,$deploymen... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Pods | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:kubernetes.pods.running{$scope,$label,$node,$service,$deployment,$statefulse... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Containers | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:kubernetes.containers.running{$scope,$label,$node,$service,$deployment,$stat... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Kubelets up | `check_status` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Kubelet Ping | `check_status` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Events | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Events per node | `timeseries` → `xy` | requires_manual | **EXPECTED_LIMITATION** |  | — |
| Event logs per node | `list_stream` → `table` | requires_manual | **EXPECTED_LIMITATION** |  | — |
| Pods | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Ready state by node | `toplist` → `table` | warning | **MINOR_ISSUE** | sum:kubernetes_state.pod.ready{$scope,$cluster,$namespace,$deployment,$statefuls... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND cond... |
| Running pods per node | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:kubernetes.pods.running{$scope,$deployment,$statefulset,$replicaset,$daemons... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Running by namespace | `toplist` → `table` | warning | **MINOR_ISSUE** | sum:kubernetes.pods.running{$scope,$namespace,$cluster,$deployment,$statefulset,... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Running pods per namespace | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:kubernetes.pods.running{$scope,$cluster,$namespace,$deployment,$statefulset,... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Failure by namespaces | `toplist` → `table` | warning | **MINOR_ISSUE** | sum:kubernetes_state.pod.status_phase{$scope,$cluster,$namespace,$deployment,$st... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND k8s.... |
| CrashloopBackOff by Pod | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:kubernetes_state.container.status_report.count.waiting{$cluster,$namespace,$... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND reas... |
| DaemonSets | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Ready | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:kubernetes_state.daemonset.ready{$scope,$daemonset,$cluster,$label,$namespac... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Pods ready | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:kubernetes_state.daemonset.ready{$scope,$daemonset,$service,$namespace,$labe... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Desired | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:kubernetes_state.daemonset.desired{$scope,$daemonset,$cluster,$label,$namesp... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Pods desired | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:kubernetes_state.daemonset.desired{$scope,$daemonset,$service,$namespace,$la... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Deployments | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Pods desired | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:kubernetes_state.deployment.replicas_desired{$scope,$deployment,$cluster,$la... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Pods desired | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:kubernetes_state.deployment.replicas_desired{$scope,$deployment,$cluster,$la... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Pods available | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:kubernetes_state.deployment.replicas_available{$scope,$deployment,$cluster,$... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Pods available | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:kubernetes_state.deployment.replicas_available{$scope,$deployment,$service,$... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Pods unavailable | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:kubernetes_state.deployment.replicas_unavailable{$scope,$deployment,$cluster... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Pods unavailable | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:kubernetes_state.deployment.replicas_unavailable{$scope,$deployment,$service... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| ReplicaSets | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Ready | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:kubernetes_state.replicaset.replicas_ready{$scope,$deployment,$replicaset,$c... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Ready | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:kubernetes_state.replicaset.replicas_ready{$scope,$service,$namespace,$deplo... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Not ready | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:kubernetes_state.replicaset.replicas_desired{$scope,$deployment,$replicaset,... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Not ready | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:kubernetes_state.replicaset.replicas_desired{$scope,$service,$namespace,$dep... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Containers | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Container states | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:kubernetes_state.container.running{$scope,$deployment,$statefulset,$replicas... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Resource Utilization | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| CPU utilization per node | `hostmap` → `table` | warning | **MINOR_ISSUE** | sum:kubernetes.cpu.usage.total{$scope,$cluster,$label,$node} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Sum Kubernetes CPU requests per node | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:kubernetes.cpu.requests{$scope,$deployment,$statefulset,$replicaset,$daemons... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Most CPU-intensive pods | `toplist` → `table` | warning | **MINOR_ISSUE** | sum:kubernetes.cpu.usage.total{$scope,$deployment,$statefulset,$replicaset,$daem... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND k8s.... |
| Memory usage per node | `hostmap` → `table` | warning | **MINOR_ISSUE** | sum:kubernetes.memory.usage{$scope,$cluster,$label,$node} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Sum Kubernetes memory requests per node | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:kubernetes.memory.requests{$scope,$deployment,$statefulset,$replicaset,$daem... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Most memory-intensive pods | `toplist` → `table` | warning | **MINOR_ISSUE** | sum:kubernetes.memory.usage{$scope,$deployment,$statefulset,$replicaset,$daemons... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND k8s.... |
| Disk I/O & Network | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Network in per node | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:kubernetes.network.rx_bytes{$scope,$deployment,$statefulset,$replicaset,$dae... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Network out per node | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:kubernetes.network.tx_bytes{$scope,$deployment,$statefulset,$replicaset,$dae... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Network errors per node | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:kubernetes.network.rx_errors{$scope,$deployment,$statefulset,$replicaset,$da... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Network errors per pod | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:kubernetes.network.rx_errors{$scope,$deployment,$statefulset,$replicaset,$da... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Disk writes per node | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:kubernetes.io.write_bytes{$scope,$service,$namespace,$deployment,$statefulse... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Disk reads per node | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:kubernetes.io.read_bytes{$scope,$service,$namespace,$deployment,$statefulset... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(kubernetes_pods_running) BY k8s.cluster.name
| WHERE query1 > 0
| STATS value = COUNT(*)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### Nodes

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:kubernetes_state.node.count{$scope,$label,$node,$service,$namespace,$cluster}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS _bucket_value = SUM(kubernetes_state_node_count) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| STATS value = AVG(_bucket_value)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(kubernetes_pods_running) BY k8s.cluster.name, k8s.namespace.name
| WHERE query1 > 0
| STATS value = COUNT(*)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(kubernetes_state_daemonset_desired) BY k8s.cluster.name, k8s.namespace.name, k8s.daemonset.name
| WHERE query1 > 0
| STATS value = COUNT(*)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### Services

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:kubernetes_state.service.count{$scope,$label,$node,$service,$namespace,$cluster}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS _bucket_value = SUM(kubernetes_state_service_count) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| STATS value = AVG(_bucket_value)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(kubernetes_state_deployment_replicas) BY k8s.cluster.name, k8s.namespace.name, k8s.deployment.name
| WHERE query1 > 0
| STATS value = COUNT(*)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### Pods

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:kubernetes.pods.running{$scope,$label,$node,$service,$deployment,$statefulset,$replicaset,$daemonset,$namespace,$cluster}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS _bucket_value = SUM(kubernetes_pods_running) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| STATS value = AVG(_bucket_value)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### Containers

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:kubernetes.containers.running{$scope,$label,$node,$service,$deployment,$statefulset,$replicaset,$daemonset,$namespace,$cluster}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS _bucket_value = SUM(kubernetes_containers_running) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| STATS value = AVG(_bucket_value)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### Kubelets up

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (check_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.status_placeholder` → selected markdown placeholder for status widget check_status

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: status widget (check_status) — placeholder for manual setup as an Elastic Synthetics check or Alert rule

**Verdict:** EXPECTED_LIMITATION

#### Kubelet Ping

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (check_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.status_placeholder` → selected markdown placeholder for status widget check_status

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: status widget (check_status) — placeholder for manual setup as an Elastic Synthetics check or Alert rule

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
<summary>Controls / Variables (9)</summary>

- `cluster` (type: `options`)
- `namespace` (type: `options`)
- `deployment` (type: `options`)
- `daemonset` (type: `options`)
- `statefulset` (type: `options`)
- `replicaset` (type: `options`)
- `service` (type: `options`)
- `node` (type: `options`)
- `label` (type: `options`)

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

### Datadog: MongoDB - Overview

**File:** `mongodb.json` — **Panels:** 43

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| About MongoDB | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 7601198205415224 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 7939246017097054 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Overview | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Averaged uptime | `query_value` → `metric` | warning | **MINOR_ISSUE** | avg:mongodb.uptime{$scope,$replset_name}.rollup(avg, 60) | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Healthy members | `check_status` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Shards count | `query_value` → `metric` | ok | **CORRECT** | sum:mongodb.replset.health{sharding_cluster_role:shardsvr} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND shar... |
| Primary count | `query_value` → `metric` | ok | **CORRECT** | sum:mongodb.replset.health{sharding_cluster_role:shardsvr,replset_state:primary} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND shar... |
| Secondary count | `query_value` → `metric` | ok | **CORRECT** | sum:mongodb.replset.health{sharding_cluster_role:shardsvr,replset_state:secondar... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND shar... |
| Configsvr count | `query_value` → `metric` | ok | **CORRECT** | sum:mongodb.replset.health{sharding_cluster_role:configsvr} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND shar... |
| Replication | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Oplog usage | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:mongodb.oplog.logsizemb{$scope,$replset_name}.fill(zero) | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Maximum lag per replica set | `timeseries` → `xy` | warning | **MINOR_ISSUE** | max:mongodb.replset.optime_lag{$scope,$replset_name} by {replset_name}.fill(zero... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 14 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Maximum lag per replica set | `toplist` → `table` | warning | **MINOR_ISSUE** | max:mongodb.replset.optime_lag{$scope,$replset_name} by {replset_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Resource Utilization | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 21 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Percentage of client connections used (%) | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:mongodb.connections.current{*,*,$scope,$replset_name} by {replset_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Average memory usage | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:mongodb.mem.resident{$scope,$replset_name}.fill(zero) | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 27 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 24 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Number of page faults per host | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:mongodb.extra_info.page_faultsps{$scope,$replset_name} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Data size per replica set | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:mongodb.stats.datasize{$scope,$replset_name} by {replset_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Throughput | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Read requests per second | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:mongodb.opcounters.getmoreps{$scope,$replset_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Network I/O | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:mongodb.network.bytesinps{*,*,$scope,$replset_name}.fill(zero) | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Write requests per second | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:mongodb.opcounters.deleteps{$scope,$replset_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Query operations per second | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:mongodb.opcounters.queryps{$scope,$replset_name}.fill(zero) | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Most Used Replica Sets | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| For reads | `toplist` → `table` | warning | **MINOR_ISSUE** | sum:mongodb.opcounters.getmoreps{$scope,$replset_name} by {replset_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| For writes | `toplist` → `table` | warning | **MINOR_ISSUE** | sum:mongodb.opcounters.deleteps{$scope,$replset_name} by {replset_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Latencies | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Command Latency | `timeseries` → `xy` | warning | **MINOR_ISSUE** | max:mongodb.oplatencies.commands.latency{$scope,$replset_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Read Latency | `timeseries` → `xy` | warning | **MINOR_ISSUE** | max:mongodb.oplatencies.reads.latency{$scope,$replset_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Write Latency | `timeseries` → `xy` | warning | **MINOR_ISSUE** | max:mongodb.oplatencies.writes.latency{$scope,$replset_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Sharding Stats | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Count of current sessions opened to the sharded cluster. | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:mongodb.sessions.count{$scope,$replset_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Total chunks count | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:mongodb.chunks.total{$scope,$replset_name}.rollup(avg, 60).fill(zero) | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Jumbo chunks count | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:mongodb.chunks.jumbo{$scope,$replset_name}.rollup(avg, 60).fill(zero) | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 34 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 35 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Logs | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| MongoDB Logs | `log_stream` → `table` | warning | **MINOR_ISSUE** | source:mongodb $scope $replset_name | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND KQL("se... |

<details>
<summary>Detailed traces (43 panels)</summary>

#### About MongoDB

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

#### 7601198205415224

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

#### 7939246017097054

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

#### Averaged uptime

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
avg:mongodb.uptime{$scope,$replset_name}.rollup(avg, 60)
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(mongodb_uptime) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| EVAL value = query1
| WHERE value IS NOT NULL
| STATS value = LAST(value, time_bucket)
| KEEP value
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana; rollup interval is approximated in ES|QL

**Verdict:** MINOR_ISSUE

#### Healthy members

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (check_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.status_placeholder` → selected markdown placeholder for status widget check_status

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: status widget (check_status) — placeholder for manual setup as an Elastic Synthetics check or Alert rule

**Verdict:** EXPECTED_LIMITATION

#### Shards count

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:mongodb.replset.health{sharding_cluster_role:shardsvr}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND sharding_cluster_role == "shardsvr"
| STATS query2 = SUM(mongodb_replset_health) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| EVAL value = query2
| STATS value = MAX(value)
| KEEP value
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Verdict:** CORRECT

#### Primary count

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:mongodb.replset.health{sharding_cluster_role:shardsvr,replset_state:primary}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND sharding_cluster_role == "shardsvr" AND replset_state == "primary"
| STATS query1 = SUM(mongodb_replset_health) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| EVAL value = query1
| STATS value = MAX(value)
| KEEP value
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Verdict:** CORRECT

#### Secondary count

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:mongodb.replset.health{sharding_cluster_role:shardsvr,replset_state:secondary}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND sharding_cluster_role == "shardsvr" AND replset_state == "secondary"
| STATS query1 = SUM(mongodb_replset_health) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| EVAL value = query1
| STATS value = MAX(value)
| KEEP value
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Verdict:** CORRECT

#### Configsvr count

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:mongodb.replset.health{sharding_cluster_role:configsvr}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND sharding_cluster_role == "configsvr"
| STATS query2 = SUM(mongodb_replset_health) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| EVAL value = query2
| STATS value = MAX(value)
| KEEP value
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Verdict:** CORRECT

#### Replication

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

#### Oplog usage

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
avg:mongodb.oplog.logsizemb{$scope,$replset_name}.fill(zero)
```

```
avg:mongodb.oplog.usedsizemb{$scope,$replset_name}.fill(zero)
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query4 = AVG(mongodb_oplog_logsizemb), query6 = AVG(mongodb_oplog_usedsizemb) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| EVAL oplog_log_size = query4, oplog_used_size = query6
| KEEP time_bucket, oplog_log_size, oplog_used_size
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `oplog_log_size`
- Output groups: `time_bucket`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana; fill(zero) only applies to null values in returned rows; empty buckets may still be omitted

**Verdict:** MINOR_ISSUE

#### Maximum lag per replica set

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
max:mongodb.replset.optime_lag{$scope,$replset_name} by {replset_name}.fill(zero).rollup(avg, 15)
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = MAX(mongodb_replset_optime_lag) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), replset_name
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `value`
- Output groups: `time_bucket, replset_name`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana; rollup interval is approximated in ES|QL; fill(zero) only applies to null values in returned rows; empty buckets may still be omitted

**Verdict:** MINOR_ISSUE

#### 14

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

#### Maximum lag per replica set

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (toplist):**

```
max:mongodb.replset.optime_lag{$scope,$replset_name} by {replset_name}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_toplist` → selected esql toplist table
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (table):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = MAX(mongodb_replset_optime_lag) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), replset_name
| WHERE query1 IS NOT NULL
| STATS query1 = LAST(query1, time_bucket) BY replset_name
| KEEP replset_name, query1
| SORT query1 DESC
| LIMIT 100
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: top list → esql table with ORDER BY + LIMIT

**Query IR:**

- Output metric: `query1`
- Output groups: `replset_name`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Controls / Variables (1)</summary>

- `replset_name` (type: `options`)

</details>

<details>
<summary>Template Variables (2)</summary>

- `$scope` → tag: ``, default: `*`
- `$replset_name` → tag: `replset_name`, default: `*`

</details>

---

### Datadog: MySQL - Overview

**File:** `mysql.json` — **Panels:** 11

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| MySQL connections | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:mysql.net.connections{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| MySQL reads and writes (per sec) | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:mysql.innodb.data_reads{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| MySQL fsync op count (per sec) | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:mysql.innodb.os_log_fsyncs{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| MySQL slow queries | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:mysql.performance.slow_queries{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| MySQL locking rate (per sec) | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:mysql.performance.table_locks_waited{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| MySQL CPU time (per sec) | `timeseries` → `xy` | warning | **MINOR_ISSUE** | mysql.performance.user_time{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| System load | `timeseries` → `xy` | warning | **MINOR_ISSUE** | system.load.1{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| CPU usage (%) | `timeseries` → `xy` | warning | **MINOR_ISSUE** | system.cpu.idle{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| I/O wait (%) | `timeseries` → `xy` | warning | **MINOR_ISSUE** | max:system.cpu.iowait{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| System memory | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:system.mem.usable{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Network traffic (per sec) | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:system.net.bytes_rcvd{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |

<details>
<summary>Detailed traces (11 panels)</summary>

#### MySQL connections

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:mysql.net.connections{$scope}
```

```
sum:mysql.net.max_connections{$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(mysql_net_connections), query1_2 = SUM(mysql_net_max_connections) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### MySQL reads and writes (per sec)

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:mysql.innodb.data_reads{$scope}
```

```
avg:mysql.innodb.data_writes{$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(mysql_innodb_data_reads), query1_2 = AVG(mysql_innodb_data_writes) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### MySQL fsync op count (per sec)

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:mysql.innodb.os_log_fsyncs{$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(mysql_innodb_os_log_fsyncs) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### MySQL slow queries

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:mysql.performance.slow_queries{$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(mysql_performance_slow_queries) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### MySQL locking rate (per sec)

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
sum:mysql.performance.table_locks_waited{$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(mysql_performance_table_locks_waited), query1_first = FIRST(mysql_performance_table_locks_waited, @timestamp) WHERE mysql_performance_table_locks_waited IS NOT NULL, query1_last = LAST(mysql_performance_table_locks_waited, @timestamp) WHERE mysql_performance_table_locks_waited IS NOT NULL, bucket_span_seconds = DATE_DIFF("seconds", MIN(@timestamp), MAX(@timestamp)) + 1 BY time_bucket = BUCKET(@timestamp, 20, ?_tstart, ?_tend)
| EVAL rate_query1 = (query1_last - query1_first) / bucket_span_seconds
| KEEP time_bucket, rate_query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `rate_query1`
- Output groups: `time_bucket`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana; rate() on a query reference is approximated with bucket FIRST/LAST deltas; values may differ for non-monotonic gauges

**Verdict:** MINOR_ISSUE

#### MySQL CPU time (per sec)

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
mysql.performance.user_time{$scope}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(mysql_performance_user_time) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(system_load_1), query1_2 = AVG(system_load_5), query1_3 = AVG(system_load_15) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(system_cpu_idle), query2 = AVG(system_cpu_system), query3 = AVG(system_cpu_iowait), query4 = AVG(system_cpu_user), query5 = AVG(system_cpu_stolen), query6 = AVG(system_cpu_guest) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = MAX(system_cpu_iowait) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(system_mem_usable), query2 = SUM(system_mem_total) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(system_net_bytes_rcvd), query1_2 = SUM(system_net_bytes_sent) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Template Variables (1)</summary>

- `$scope` → tag: ``, default: `*`

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
| Dropped connections, last 15m | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:nginx.net.conn_dropped_per_s{*}.as_count() | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Agent connection to NGINX | `check_status` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| 4071809555103542 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 6613327356980228 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Nginx metric monitors | `manage_status` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Anomaly Detection | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Nginx Watchdog alerts | `event_stream` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | — | — |
| 2165182479929144 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Logs | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 518123602946722 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| NGINX Error logs | `log_stream` → `table` | ok | **CORRECT** | source:nginx @http.status_code:(404 OR 500) | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND service... |
| Requests | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Requests per second | `query_value` → `metric` | ok | **CORRECT** | avg:nginx.net.request_per_s{*} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Requests per second by host | `hostmap` → `table` | warning | **MINOR_ISSUE** | avg:nginx.net.request_per_s{*} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 8663159993822306 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 183855449379928 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Requests: reading, writing, waiting | `timeseries` → `xy` | ok | **CORRECT** | sum:nginx.net.reading{$Host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Change in overall requests per second | `change` → `table` | warning | **MINOR_ISSUE** | sum:nginx.net.request_per_s{*} by {service} | FROM metrics-* \| WHERE @timestamp >= NOW() - 14 days \| STATS current_value = S... |
| 4851971395880802 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Connections  | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Dropped connections per second | `timeseries` → `xy` | ok | **CORRECT** | sum:nginx.net.conn_dropped_per_s{$Host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Active connections per second | `timeseries` → `xy` | ok | **CORRECT** | sum:nginx.net.connections{$Host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS _bucket_value = SUM(nginx_net_conn_dropped_per_s) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| STATS value = MAX(_bucket_value)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Warnings:** as_count interval semantics are approximated in ES|QL

**Verdict:** MINOR_ISSUE

#### Agent connection to NGINX

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (check_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.status_placeholder` → selected markdown placeholder for status widget check_status

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: status widget (check_status) — placeholder for manual setup as an Elastic Synthetics check or Alert rule

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

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (manage_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.status_placeholder` → selected markdown placeholder for status widget manage_status

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: status widget (manage_status) — placeholder for manual setup as an Elastic Synthetics check or Alert rule

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND service.name == "nginx" AND ((TO_STRING(http.status_code) == "404" OR TO_STRING(http.status_code) == "500"))
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

- Output metric: `message`
- Output groups: `@timestamp`

**Verdict:** CORRECT

</details>

<details>
<summary>Controls / Variables (1)</summary>

- `Host` (type: `options`)

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
| Rows fetched / returned / inserted / updated (per sec) | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:postgresql.rows_fetched{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Connections | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:postgresql.connections{$scope} by {db} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Inserts / updates / deletes (per sec) | `timeseries` → `xy` | warning | **MINOR_ISSUE** | postgresql.rows_inserted{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Disk utilization (%) | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:system.io.util{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| System load | `timeseries` → `xy` | warning | **MINOR_ISSUE** | system.load.1{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| CPU usage (%) | `timeseries` → `xy` | warning | **MINOR_ISSUE** | system.cpu.idle{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| I/O wait (%) | `timeseries` → `xy` | warning | **MINOR_ISSUE** | max:system.cpu.iowait{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| System memory | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:system.mem.usable{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Network traffic (per sec) | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:system.net.bytes_rcvd{$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(postgresql_rows_fetched), query1_2 = AVG(postgresql_rows_returned), query1_3 = AVG(postgresql_rows_inserted), query1_4 = AVG(postgresql_rows_updated) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(postgresql_connections) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), db
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(postgresql_rows_inserted), query2 = AVG(postgresql_rows_deleted), query3 = AVG(postgresql_rows_updated) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(system_io_util) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(system_load_1), query1_2 = AVG(system_load_5), query1_3 = AVG(system_load_15) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(system_cpu_idle), query2 = AVG(system_cpu_system), query3 = AVG(system_cpu_iowait), query4 = AVG(system_cpu_user), query5 = AVG(system_cpu_stolen), query6 = AVG(system_cpu_guest) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = MAX(system_cpu_iowait) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(system_mem_usable), query2 = SUM(system_mem_total) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(system_net_bytes_rcvd), query1_2 = SUM(system_net_bytes_sent) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Template Variables (1)</summary>

- `$scope` → tag: `None`, default: `*`

</details>

---

### Datadog: RabbitMQ Overview (OpenMetrics Version)

**File:** `rabbitmq.json` — **Panels:** 47

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| New group | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 6507010436924774 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| 7472223195597488 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Overview | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 7225439286749620 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| RMQ Monitor Summary | `manage_status` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| OpenMetrics Status | `check_status` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Node Status | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 4274214467814750 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Queue Pipeline per Node | `sunburst` → `partition` | warning | **MINOR_ISSUE** | sum:rabbitmq.queues.created.count{$node_name} by {rabbitmq_node}.as_count() | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| % Usage of Node Memory | `timeseries` → `xy` | ok | **CORRECT** | avg:rabbitmq.process.resident_memory_bytes{$node_name} by {rabbitmq_node} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Consumers | `timeseries` → `xy` | ok | **CORRECT** | avg:rabbitmq.global.consumers{$node_name} by {rabbitmq_node} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Difference of Opened vs Closed Connections | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:rabbitmq.connections.opened.count{$node_name} by {rabbitmq_node}.as_count() | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 6698344132861859 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Available Disk Space | `timeseries` → `xy` | ok | **CORRECT** | avg:rabbitmq.disk_space.available_bytes{$node_name} by {rabbitmq_node} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| % Usage of Erlang Processes | `timeseries` → `xy` | ok | **CORRECT** | avg:rabbitmq.erlang.vm.process_count{$node_name} by {rabbitmq_node} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| % Usage of Ports | `timeseries` → `xy` | ok | **CORRECT** | avg:rabbitmq.erlang.vm.port_count{$node_name} by {rabbitmq_node} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Open Channels | `query_value` → `metric` | ok | **CORRECT** | avg:rabbitmq.channels{$node_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| % Usage of TCP Sockets | `timeseries` → `xy` | ok | **CORRECT** | avg:rabbitmq.process.open_tcp_sockets{$node_name} by {rabbitmq_node} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| % Usage of File Descriptors | `timeseries` → `xy` | ok | **CORRECT** | avg:rabbitmq.process.open_fds{$node_name} by {rabbitmq_node} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Messages Pipelines | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 7327292918773178 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Messages Pipeline - Queue | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:rabbitmq.queue.messages.ready{$queue} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Messages per queue and node | `sunburst` → `partition` | ok | **CORRECT** | sum:rabbitmq.queue.messages{*} by {rabbitmq_queue,rabbitmq_node} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Messages Pipeline - Channel | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:rabbitmq.channel.messages.delivered.ack.count{*}.as_count() | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Packets in a Connection | `sunburst` → `partition` | requires_manual | **MINOR_ISSUE** | sum:rabbitmq.connection.incoming_packets.count{$rabbitmq_conn_state}.as_count() | — |
| Queue Metrics | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Queue Depth | `timeseries` → `xy` | ok | **CORRECT** | avg:rabbitmq.queue.messages{$queue} by {queue} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Ready Messages | `timeseries` → `xy` | ok | **CORRECT** | avg:rabbitmq.queue.messages.ready{$queue} by {queue} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Published Messages | `timeseries` → `xy` | ok | **CORRECT** | avg:rabbitmq.queue.messages.published.count{$queue} by {queue} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Delivered Messages | `timeseries` → `xy` | ok | **CORRECT** | avg:rabbitmq.queue.messages.delivered.count{$queue} by {queue} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Redelivered Messages | `timeseries` → `xy` | ok | **CORRECT** | avg:rabbitmq.queue.messages.redelivered.count{$queue} by {queue} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Acknowledged Messages | `timeseries` → `xy` | ok | **CORRECT** | avg:rabbitmq.queue.messages.acked.count{$queue} by {queue} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Unacknowledged Messages | `timeseries` → `xy` | ok | **CORRECT** | avg:rabbitmq.queue.messages.unacked{$queue} by {queue} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Consumers per Queue | `timeseries` → `xy` | ok | **CORRECT** | avg:rabbitmq.queue.consumers{$queue} by {queue} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Queue Consumers' Ability to Take New Messages | `timeseries` → `xy` | ok | **CORRECT** | avg:rabbitmq.queue.consumer_utilisation{$queue} by {queue} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 5680810966566357 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| RabbitMQ Topology for $queue | `topology_map` → `markdown` | not_feasible | **EXPECTED_LIMITATION** |  | — |
| Top 10 Max Time in Queue by env | `timeseries` → `table` | warning | **MINOR_ISSUE** | max:data_streams.latency{type:rabbitmq AND pathway_type:edge AND direction:in AN... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND type... |
| Incoming Messages by env, Producing Service | `timeseries` → `xy` | warning | **MINOR_ISSUE** | count(v: v>=0):data_streams.latency{type:rabbitmq AND direction:in AND pathway_t... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND type... |
| Outgoing Messages by env, Consuming Service | `timeseries` → `xy` | warning | **MINOR_ISSUE** | count(v: v>=0):data_streams.latency{type:rabbitmq AND direction:in AND pathway_t... | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND type... |
| Top 10 p95 Message Size by env | `timeseries` → `table` | warning | **MINOR_ISSUE** | p95:data_streams.payload_size{type:rabbitmq,topic:$queue.value} by {topic,env} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND type... |
| Distribution of Message Size | `distribution` → `xy` | warning | **MINOR_ISSUE** | avg:data_streams.payload_size{type:rabbitmq,topic:*} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND type... |
| Logs | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| 218491216894336 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Count per Log Status | `timeseries` → `xy` | ok | **CORRECT** | source:rabbitmq $node_name | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND KQL("se... |
| Error Logs for RabbitMQ | `list_stream` → `table` | ok | **CORRECT** | source:rabbitmq status:error | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND service... |

<details>
<summary>Detailed traces (47 panels)</summary>

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

#### 6507010436924774

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

#### 7472223195597488

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

#### 7225439286749620

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

#### RMQ Monitor Summary

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (manage_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.status_placeholder` → selected markdown placeholder for status widget manage_status

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: status widget (manage_status) — placeholder for manual setup as an Elastic Synthetics check or Alert rule

**Verdict:** EXPECTED_LIMITATION

#### OpenMetrics Status

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (check_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.status_placeholder` → selected markdown placeholder for status widget check_status

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: status widget (check_status) — placeholder for manual setup as an Elastic Synthetics check or Alert rule

**Verdict:** EXPECTED_LIMITATION

#### Node Status

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

#### 4274214467814750

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

#### Queue Pipeline per Node

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (sunburst):**

```
sum:rabbitmq.queues.created.count{$node_name} by {rabbitmq_node}.as_count()
```

```
sum:rabbitmq.queues.deleted.count{$node_name} by {rabbitmq_node}.as_count()
```

```
sum:rabbitmq.queues.declared.count{$node_name} by {rabbitmq_node}.as_count()
```

```
sum:rabbitmq.queues{$node_name} by {rabbitmq_node}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_partition` → selected ES|QL partition chart for sunburst
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (partition):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(rabbitmq_queues_created_count), query2 = SUM(rabbitmq_queues_deleted_count), query3 = SUM(rabbitmq_queues_declared_count), query4 = SUM(rabbitmq_queues) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), rabbitmq_node
| EVAL query1_query2_query3_query4 = (((query1 + query2) + query3) + query4)
| STATS query1_query2_query3_query4 = SUM(query1_query2_query3_query4) BY rabbitmq_node
| KEEP rabbitmq_node, query1_query2_query3_query4
| SORT query1_query2_query3_query4 DESC
| LIMIT 100
```

**Plan:**

- Backend: `esql`
- Kibana type: `partition`
- Data source: `metrics`
- Reasons: sunburst → ES|QL partition chart

**Query IR:**

- Output metric: `query1_query2_query3_query4`
- Output groups: `rabbitmq_node`

**Warnings:** as_count interval semantics are approximated in ES|QL

**Verdict:** MINOR_ISSUE

#### % Usage of Node Memory

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
avg:rabbitmq.process.resident_memory_bytes{$node_name} by {rabbitmq_node}
```

```
avg:rabbitmq.resident_memory_limit_bytes{$node_name} by {rabbitmq_node}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected ES|QL XY because widget uses a multi-query formula
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(rabbitmq_process_resident_memory_bytes), query2 = AVG(rabbitmq_resident_memory_limit_bytes) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), rabbitmq_node
| EVAL query1_query2_100 = ((query1 / query2) * 100)
| KEEP time_bucket, rabbitmq_node, query1_query2_100
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: multi-query formula → ES|QL for query-side computation

**Query IR:**

- Output metric: `query1_query2_100`
- Output groups: `time_bucket, rabbitmq_node`

**Verdict:** CORRECT

#### Consumers

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
avg:rabbitmq.global.consumers{$node_name} by {rabbitmq_node}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(rabbitmq_global_consumers) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), rabbitmq_node
| KEEP time_bucket, rabbitmq_node, query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket, rabbitmq_node`

**Verdict:** CORRECT

#### Difference of Opened vs Closed Connections

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
avg:rabbitmq.connections.opened.count{$node_name} by {rabbitmq_node}.as_count()
```

```
avg:rabbitmq.connections.closed.count{$node_name} by {rabbitmq_node}.as_count()
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected ES|QL XY because widget uses a multi-query formula
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(rabbitmq_connections_opened_count), query2 = AVG(rabbitmq_connections_closed_count) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), rabbitmq_node
| EVAL query1_query2 = (query1 - query2)
| KEEP time_bucket, rabbitmq_node, query1_query2
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: multi-query formula → ES|QL for query-side computation

**Query IR:**

- Output metric: `query1_query2`
- Output groups: `time_bucket, rabbitmq_node`

**Warnings:** as_count interval semantics are approximated in ES|QL

**Verdict:** MINOR_ISSUE

#### 6698344132861859

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

#### Available Disk Space

**Translation path:** `esql_formula` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
avg:rabbitmq.disk_space.available_bytes{$node_name} by {rabbitmq_node}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(rabbitmq_disk_space_available_bytes) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), rabbitmq_node
| KEEP time_bucket, rabbitmq_node, query1
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `query1`
- Output groups: `time_bucket, rabbitmq_node`

**Verdict:** CORRECT

</details>

<details>
<summary>Controls / Variables (3)</summary>

- `node_name` (type: `options`)
- `queue` (type: `options`)
- `rabbitmq_conn_state` (type: `options`)

</details>

<details>
<summary>Template Variables (3)</summary>

- `$node_name` → tag: `rabbitmq_node`, default: `*`
- `$queue` → tag: `queue`, default: `*`
- `$rabbitmq_conn_state` → tag: `rabbitmq_conn_state`, default: `*`

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
| Hit rate | `query_value` → `metric` | warning | **MINOR_ISSUE** | avg:redis.stats.keyspace_hits{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Blocked clients | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:redis.clients.blocked{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Redis keyspace | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:redis.keys{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Unsaved changes | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:redis.rdb.changes_since_last{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Primary link down | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:redis.replication.master_link_down_since_seconds{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 7896589211182748 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Performance Metrics | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Latency by Host | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:redis.info.latency_ms{$scope,$host} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 18 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Slowlog duration | `timeseries` → `table` | warning | **MINOR_ISSUE** | sum:redis.slowlog.micros.95percentile{$scope,$host} by {name,command} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Slowlog query rates | `toplist` → `table` | warning | **MINOR_ISSUE** | sum:redis.slowlog.micros.count{$host,$scope} by {command,name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Average replication delay (offset) | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:redis.replication.delay{$host,$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Average CPU usage | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:redis.cpu.sys{$host,$scope} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Cache hit rate | `query_value` → `metric` | warning | **MINOR_ISSUE** | avg:redis.stats.keyspace_hits{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Cache hit rate | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:redis.stats.keyspace_hits{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Memory Metrics | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Percent Used Memory by Host | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:redis.mem.used{$scope,$host} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 24 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Evictions | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:redis.keys.evicted{$scope,$host} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Total allocated memory | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:redis.mem.rss{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Fragmentation ratio | `query_value` → `metric` | warning | **MINOR_ISSUE** | avg:redis.mem.fragmentation_ratio{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Fragmentation ratio | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:redis.mem.fragmentation_ratio{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Base Activity Metrics | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Connected clients | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:redis.net.clients{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 12 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Blocked clients | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:redis.clients.blocked{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Connected replicas | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:redis.net.slaves{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Rejected connections | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:redis.net.rejected{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Commands per second | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:redis.net.commands{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Key Metrics | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Total keys | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:redis.keys{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Current total | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:redis.keys{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 4351331682136830 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Expired keys | `timeseries` → `xy` | warning | **MINOR_ISSUE** | sum:redis.keys.expired{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Keys with expiration | `query_value` → `metric` | warning | **MINOR_ISSUE** | sum:redis.expires{$scope,$host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Key length distribution | `distribution` → `xy` | warning | **MINOR_ISSUE** | sum:redis.key.length{$scope, $host, $key} by {key} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Logs | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| Error Logs | `list_stream` → `table` | warning | **MINOR_ISSUE** | source:redis $scope $host status:error | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND KQL("(s... |
| All Logs | `list_stream` → `table` | warning | **MINOR_ISSUE** | source:redis $scope $host | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND KQL("se... |

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(redis_stats_keyspace_hits), query2 = AVG(redis_stats_keyspace_misses) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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

- Output metric: `value`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### Blocked clients

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:redis.clients.blocked{$scope,$host}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS _bucket_value = SUM(redis_clients_blocked) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| STATS value = MAX(_bucket_value)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### Redis keyspace

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:redis.keys{$scope,$host}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS _bucket_value = SUM(redis_keys) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| STATS value = MAX(_bucket_value)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### Unsaved changes

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:redis.rdb.changes_since_last{$scope,$host}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS _bucket_value = SUM(redis_rdb_changes_since_last) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| STATS value = AVG(_bucket_value)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

#### Primary link down

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
sum:redis.replication.master_link_down_since_seconds{$scope,$host}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (metric):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS _bucket_value = SUM(redis_replication_master_link_down_since_seconds) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| STATS value = AVG(_bucket_value)
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query2 = AVG(redis_info_latency_ms) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), host.name
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

- Output metric: `latency_of_the_redis_info_command`
- Output groups: `time_bucket, host.name`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

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
- `translate_metric` / `datadog.translate.metric_formula` → translated metric formula pipeline

**Translated (table):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(redis_slowlog_micros_95percentile) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), name, command
| EVAL top_query1_1000_10_mean_desc = (query1 / 1000)
| KEEP time_bucket, name, command, top_query1_1000_10_mean_desc
| STATS _rank = AVG(top_query1_1000_10_mean_desc) BY name, command
| SORT _rank DESC
| LIMIT 10
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `_rank`
- Output groups: `name, command`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana; top(10) on timeseries approximated as ranked table of top-10 groups — ES|QL cannot filter to N series in a single pass

**Verdict:** MINOR_ISSUE

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = SUM(redis_slowlog_micros_count) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), command, name
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
- Output groups: `command, name`

**Warnings:** Datadog $scope template variable cannot be represented by a single Kibana control and was omitted; recreate the scope filters manually in Kibana

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Controls / Variables (2)</summary>

- `host` (type: `options`)
- `key` (type: `options`)

</details>

<details>
<summary>Template Variables (3)</summary>

- `$scope` → tag: ``, default: `*`
- `$host` → tag: `host`, default: `*`
- `$key` → tag: `key`, default: `*`

</details>

---

### Datadog: Datadog Kitchen Sink Canary

**File:** `kitchen-sink-canary.json` — **Panels:** 25

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| timeseries widget | `timeseries` → `xy` | ok | **CORRECT** | avg:redis_memory_used_bytes{*} by {instance} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| query_value widget | `query_value` → `table` | warning | **MINOR_ISSUE** | avg:redis_memory_used_bytes{*} by {instance} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| toplist widget | `toplist` → `table` | ok | **CORRECT** | avg:redis_memory_used_bytes{*} by {instance} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| bar_chart widget | `bar_chart` → `table` | ok | **CORRECT** | avg:redis_memory_used_bytes{*} by {instance} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| table widget | `table` → `table` | ok | **CORRECT** | avg:redis_memory_used_bytes{*} by {instance} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| query_table widget | `query_table` → `table` | ok | **CORRECT** | avg:redis_memory_used_bytes{*} by {instance} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| heatmap widget | `heatmap` → `heatmap` | ok | **CORRECT** | avg:redis_memory_used_bytes{*} by {instance} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| distribution widget | `distribution` → `xy` | warning | **MINOR_ISSUE** | avg:redis_memory_used_bytes{*} by {instance} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| change widget | `change` → `table` | warning | **MINOR_ISSUE** | avg:redis_memory_used_bytes{*} by {instance} | FROM metrics-* \| WHERE @timestamp >= NOW() - 2 hours \| STATS current_value = A... |
| pie widget | `pie` → `partition` | ok | **CORRECT** | avg:redis_memory_used_bytes{*} by {instance} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| treemap widget | `treemap` → `treemap` | ok | **CORRECT** | avg:redis_memory_used_bytes{*} by {instance} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| sunburst widget | `sunburst` → `partition` | ok | **CORRECT** | avg:redis_memory_used_bytes{*} by {instance} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| scatterplot widget | `scatterplot` → `xy` | warning | **CORRECT** | avg:redis_memory_used_bytes{*} by {instance} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| geomap widget | `geomap` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | avg:redis_memory_used_bytes{*} by {instance} | — |
| hostmap widget | `hostmap` → `table` | warning | **MINOR_ISSUE** | avg:redis_memory_used_bytes{*} by {instance} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| log_stream widget | `log_stream` → `table` | ok | **CORRECT** | * | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| SORT @ti... |
| list_stream widget | `list_stream` → `table` | ok | **CORRECT** | * | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| SORT @ti... |
| note widget | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| free_text widget | `free_text` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| image widget | `image` → `image` | ok | **EXPECTED_LIMITATION** | — | — |
| iframe widget | `iframe` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| check_status widget | `check_status` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| manage_status widget | `manage_status` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| group widget | `group` → `group` | skipped | **EXPECTED_LIMITATION** | — | — |
| nested timeseries | `timeseries` → `xy` | ok | **CORRECT** | avg:redis_memory_used_bytes{*} by {instance} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |

<details>
<summary>Detailed traces (25 panels)</summary>

#### timeseries widget

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (timeseries):**

```
avg:redis_memory_used_bytes{*} by {instance}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_timeseries` → selected esql XY panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = AVG(redis_memory_used_bytes) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), instance
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: timeseries → esql XY panel

**Query IR:**

- Output metric: `value`
- Output groups: `time_bucket, instance`

**Verdict:** CORRECT

#### query_value widget

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_value):**

```
avg:redis_memory_used_bytes{*} by {instance}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_query_value` → selected esql metric panel
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (table):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS _bucket_value = AVG(redis_memory_used_bytes) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), instance
| WHERE _bucket_value IS NOT NULL
| STATS value = LAST(_bucket_value, time_bucket) BY instance
| SORT value DESC
| LIMIT 100
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`
- Output groups: `instance`

**Warnings:** Grouped single-value widget approximated as a summary table (one value per group); a metric tile shows only a single number

**Verdict:** MINOR_ISSUE

#### toplist widget

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (toplist):**

```
avg:redis_memory_used_bytes{*} by {instance}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_toplist` → selected esql toplist table
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (table):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = AVG(redis_memory_used_bytes) BY instance
| SORT value DESC
| LIMIT 10
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: top list → esql table with ORDER BY + LIMIT

**Query IR:**

- Output metric: `value`
- Output groups: `instance`

**Verdict:** CORRECT

#### bar_chart widget

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (bar_chart):**

```
avg:redis_memory_used_bytes{*} by {instance}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_toplist` → selected esql bar_chart table
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (table):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = AVG(redis_memory_used_bytes) BY instance
| SORT value DESC
| LIMIT 10
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: bar chart → esql table with ORDER BY + LIMIT

**Query IR:**

- Output metric: `value`
- Output groups: `instance`

**Verdict:** CORRECT

#### table widget

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (table):**

```
avg:redis_memory_used_bytes{*} by {instance}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_table` → selected esql table
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (table):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = AVG(redis_memory_used_bytes) BY instance
| SORT value DESC
| LIMIT 100
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: table → esql table

**Query IR:**

- Output metric: `value`
- Output groups: `instance`

**Verdict:** CORRECT

#### query_table widget

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (query_table):**

```
avg:redis_memory_used_bytes{*} by {instance}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_table` → selected esql table
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (table):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = AVG(redis_memory_used_bytes) BY instance
| SORT value DESC
| LIMIT 100
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: table → esql table

**Query IR:**

- Output metric: `value`
- Output groups: `instance`

**Verdict:** CORRECT

#### heatmap widget

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (heatmap):**

```
avg:redis_memory_used_bytes{*} by {instance}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_heatmap_distribution` → selected ES|QL for heatmap
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (heatmap):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = AVG(redis_memory_used_bytes) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), instance
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `heatmap`
- Data source: `metrics`
- Reasons: heatmap → ES|QL

**Query IR:**

- Output metric: `value`
- Output groups: `time_bucket, instance`

**Verdict:** CORRECT

#### distribution widget

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (distribution):**

```
avg:redis_memory_used_bytes{*} by {instance}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_heatmap_distribution` → selected ES|QL for distribution
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS avg = AVG(redis_memory_used_bytes), p50 = PERCENTILE(redis_memory_used_bytes, 50), p90 = PERCENTILE(redis_memory_used_bytes, 90), p99 = PERCENTILE(redis_memory_used_bytes, 99) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), instance
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: distribution → ES|QL

**Query IR:**

- Output metric: `avg`
- Output groups: `time_bucket, instance`

**Warnings:** distribution widget approximated as its requested aggregation plus p50/p90/p99 percentile time series (ES|QL has no native distribution histogram panel)

**Verdict:** MINOR_ISSUE

#### change widget

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (change):**

```
avg:redis_memory_used_bytes{*} by {instance}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_change` → selected ES|QL table for grouped change widget
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (table):**

```
FROM metrics-*
| WHERE @timestamp >= NOW() - 2 hours
| STATS current_value = AVG(redis_memory_used_bytes) WHERE @timestamp >= NOW() - 1 hours, previous_value = AVG(redis_memory_used_bytes) WHERE @timestamp >= NOW() - 2 hours AND @timestamp < NOW() - 1 hours BY instance
| WHERE current_value IS NOT NULL AND previous_value IS NOT NULL
| EVAL value = current_value - previous_value
| SORT value DESC
| LIMIT 100
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: change widget → ES|QL table (comparison shift)

**Query IR:**

- Output metric: `current_value`
- Output groups: `instance`

**Warnings:** change calculation is approximated; change widget live span was unavailable; defaulted to 1 hour

**Verdict:** MINOR_ISSUE

#### pie widget

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (pie):**

```
avg:redis_memory_used_bytes{*} by {instance}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_partition` → selected ES|QL partition chart for pie
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (partition):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = AVG(redis_memory_used_bytes) BY instance
| SORT value DESC
| LIMIT 100
```

**Plan:**

- Backend: `esql`
- Kibana type: `partition`
- Data source: `metrics`
- Reasons: pie → ES|QL partition chart

**Query IR:**

- Output metric: `value`
- Output groups: `instance`

**Verdict:** CORRECT

#### treemap widget

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (treemap):**

```
avg:redis_memory_used_bytes{*} by {instance}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_partition` → selected ES|QL partition chart for treemap
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (treemap):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = AVG(redis_memory_used_bytes) BY instance
| SORT value DESC
| LIMIT 100
```

**Plan:**

- Backend: `esql`
- Kibana type: `treemap`
- Data source: `metrics`
- Reasons: treemap → ES|QL partition chart

**Query IR:**

- Output metric: `value`
- Output groups: `instance`

**Verdict:** CORRECT

#### sunburst widget

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (sunburst):**

```
avg:redis_memory_used_bytes{*} by {instance}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_partition` → selected ES|QL partition chart for sunburst
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (partition):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = AVG(redis_memory_used_bytes) BY instance
| SORT value DESC
| LIMIT 100
```

**Plan:**

- Backend: `esql`
- Kibana type: `partition`
- Data source: `metrics`
- Reasons: sunburst → ES|QL partition chart

**Query IR:**

- Output metric: `value`
- Output groups: `instance`

**Verdict:** CORRECT

#### scatterplot widget

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (scatterplot):**

```
avg:redis_memory_used_bytes{*} by {instance}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_scatterplot` → selected ES|QL scatter plot
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (xy):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = AVG(redis_memory_used_bytes) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), instance
| SORT time_bucket
```

**Plan:**

- Backend: `esql`
- Kibana type: `xy`
- Data source: `metrics`
- Reasons: scatterplot → ES|QL XY scatter

**Query IR:**

- Output metric: `value`
- Output groups: `time_bucket, instance`

**Warnings:** scatter mode requires manual axis mapping verification

**Verdict:** CORRECT

#### geomap widget

**Translation path:** `markdown` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (geomap):**

```
avg:redis_memory_used_bytes{*} by {instance}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_geomap` → selected markdown for geomap

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: `metrics`
- Reasons: geomap requires Kibana Maps — not yet supported

**Warnings:** geomap migration needs dedicated Maps saved object support

**Verdict:** EXPECTED_LIMITATION

#### hostmap widget

**Translation path:** `esql_metric` · **Query language:** `datadog_metric` · **Readiness:** `—`

**Source (hostmap):**

```
avg:redis_memory_used_bytes{*} by {instance}
```

**Pipeline trace:**

- `plan` / `datadog.plan.metric_hostmap` → selected ES|QL table for grouped hostmap
- `translate_metric` / `datadog.translate.metric_single_query` → translated single metric query

**Translated (table):**

```
FROM metrics-*
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS value = AVG(redis_memory_used_bytes) BY instance
| SORT value DESC
| LIMIT 100
```

**Plan:**

- Backend: `esql`
- Kibana type: `table`
- Data source: `metrics`
- Reasons: grouped hostmap → data-preserving ES|QL table

**Query IR:**

- Output metric: `value`
- Output groups: `instance`

**Warnings:** hostmap visual approximated as a grouped table; host dimensions and metric values are preserved, but Datadog tile coloring is not

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Controls / Variables (1)</summary>

- `instance` (type: `options`)

</details>

<details>
<summary>Template Variables (1)</summary>

- `$instance` → tag: `instance`, default: `*`

</details>

---

### Datadog: System Overview - Sample

**File:** `sample_dashboard.json` — **Panels:** 11

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| CPU Usage by Host | `timeseries` → `xy` | ok | **CORRECT** | avg:system.cpu.user{$host} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Memory Usage with Rollup | `timeseries` → `xy` | warning | **MINOR_ISSUE** | avg:system.mem.usable{env:$env} by {host}.rollup(avg, 60) | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Current CPU Average | `query_value` → `metric` | ok | **CORRECT** | avg:system.cpu.user{*} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Top Hosts by CPU | `toplist` → `table` | ok | **CORRECT** | avg:system.cpu.user{*} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| Disk I/O (Formula: Read + Write) | `timeseries` → `xy` | ok | **CORRECT** | avg:system.disk.read_time{*} by {host} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend \| STATS... |
| 6 | `note` → `markdown` | ok | **EXPECTED_LIMITATION** | — | — |
| Log Error Rate | `timeseries` → `xy` | ok | **CORRECT** | service:web status:error | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND service... |
| Log Entries by Service | `table` → `table` | ok | **CORRECT** | status:error @http.url:/api/* | FROM logs-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND log.lev... |
| Request Latency Distribution | `heatmap` → `heatmap` | ok | **CORRECT** | avg:trace.flask.request.duration{service:web} by {resource_name} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND serv... |
| Monitor Summary | `manage_status` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Network Bytes In | `query_value` → `metric` | ok | **CORRECT** | sum:system.net.bytes_rcvd{host:web01,!env:staging} | FROM metrics-* \| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND host... |

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(system_cpu_user) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), host.name
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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(system_mem_usable) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), host.name
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

**Warnings:** rollup interval is approximated in ES|QL

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(system_cpu_user) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| EVAL value = query1
| WHERE value IS NOT NULL
| STATS value = LAST(value, time_bucket)
| KEEP value
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
| STATS query1 = AVG(system_disk_read_time), query2 = AVG(system_disk_write_time) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), host.name
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

- Output metric: `total_i_o`
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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND service.name == "web" AND log.level == "error"
| STATS count = COUNT(*) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND log.level == "error" AND http.url LIKE "/api/*"
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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND service.name == "web"
| STATS query1 = AVG(trace_flask_request_duration) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend), resource_name
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

**Translation path:** `markdown` · **Query language:** `datadog_widget` · **Readiness:** `—`

**Source (manage_status):**

**Pipeline trace:**

- `plan` / `datadog.plan.status_placeholder` → selected markdown placeholder for status widget manage_status

**Plan:**

- Backend: `markdown`
- Kibana type: `markdown`
- Data source: ``
- Reasons: status widget (manage_status) — placeholder for manual setup as an Elastic Synthetics check or Alert rule

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
| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend AND host.name == "web01" AND deployment.environment != "staging"
| STATS query1 = SUM(system_net_bytes_rcvd) BY time_bucket = BUCKET(@timestamp, 75, ?_tstart, ?_tend)
| EVAL value = query1
| WHERE value IS NOT NULL
| STATS value = LAST(value, time_bucket)
| KEEP value
```

**Plan:**

- Backend: `esql`
- Kibana type: `metric`
- Data source: `metrics`
- Reasons: single-value metric → esql metric panel

**Query IR:**

- Output metric: `value`

**Verdict:** CORRECT

</details>

<details>
<summary>Controls / Variables (2)</summary>

- `host` (type: `options`)
- `env` (type: `options`)

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
Elements:            451 total (451 panels)
Renderable panels:   451
  OK:                   192 (42.6%)
  Warning:              167 (37.0%)
  Requires manual:       24 (5.3%)
  Not feasible:           5 (1.1%)
  Skipped:               63 (14.0%)
```

Verdict breakdown:

```
  CORRECT:                  111
  MINOR_ISSUE:              168
  EXPECTED_LIMITATION:      172
```
<!-- /GENERATED:APPENDIX_STATS -->

---

## Appendix: Not-Feasible Panel Breakdown

<!-- GENERATED:NOT_FEASIBLE_BREAKDOWN -->
Every panel marked `not_feasible` in the trace run (5 total):

| Panel Title | Dashboard | Source | Reason |
|-------------|-----------|--------|--------|
| 9 | Docker - Overview | datadog | — |
| 10 | Docker - Overview | datadog | — |
| Kafka Topology for $topic, $env | Kafka, Zookeeper and Kafka Consumer Overview | datadog | — |
| Nginx Watchdog alerts | NGINX - Overview | datadog | — |
| RabbitMQ Topology for $queue | RabbitMQ Overview (OpenMetrics Version) | datadog | — |
<!-- /GENERATED:NOT_FEASIBLE_BREAKDOWN -->

---

*Last generated: 2026-09-02 14:04 UTC*
