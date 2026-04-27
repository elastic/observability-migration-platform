# Grafana Pipeline Trace

> **Auto-generated.** Regenerate with:
>
> ```bash
> python scripts/audit_pipeline.py --update-docs
> python scripts/audit_pipeline.py --update-docs --source grafana   # Grafana only
> ```
>
> Static narrative lives in `docs/sources/grafana-trace.tpl.md`.
> See also: [Grafana Adapter](grafana.md) | [Shared Pipeline Overview](../pipeline-trace.md) | [Datadog Trace](datadog-trace.md)

This document traces every Grafana dashboard in `infra/grafana/dashboards/`
through the migration pipeline, showing source PromQL/LogQL, each translation
step, the emitted Kibana query, and a semantic verdict.

---

## Translation Paths

The Grafana adapter selects one of four paths per panel target, in order of
preference:

1. **Native PROMQL** (`--native-promql`) — wraps the original PromQL in
   `PROMQL index=… value=(expr)`. Used for Elastic Serverless; highest
   fidelity for `rate()`, `increase()`, grouped aggregations.
2. **Rule-engine ES|QL** — parses PromQL AST via `promql-parser`, classifies
   the expression family, runs it through the rule pipeline, renders ES|QL.
3. **LLM fallback ES|QL** — for panels the rule engine marks `not_feasible`,
   optionally asks a local LLM. Structurally validated.
4. **Native ES|QL passthrough** — pre-existing Elasticsearch queries are kept
   unchanged.

### Rule Engine Pipeline

```
QUERY_PREPROCESSORS → QUERY_CLASSIFIERS → QUERY_TRANSLATORS →
QUERY_POSTPROCESSORS → QUERY_VALIDATORS → PANEL_TRANSLATORS →
VARIABLE_TRANSLATORS
```

Each stage is a priority-ordered registry. Rules are matched and applied in
order; the first translator that produces output wins.

### Template Variables → Controls

Grafana `query`-type variables are translated into Kibana dashboard controls.
The label field from `label_values(metric, label)` is resolved through the
schema resolver to its ECS/OTel equivalent (e.g. `instance` → `service.instance.id`).
Variable-driven label filters in PromQL are dropped from individual panel
queries because the Kibana control applies the filter at dashboard level.

---

## Dashboard Summary

<!-- GENERATED:DASHBOARD_SUMMARY -->
| Source | Dashboard | Panels | Migrated | Warnings | Manual | Not Feasible | Skipped |
|--------|-----------|--------|----------|----------|--------|--------------|---------|
| grafana | Diverse Panel Types Test | 11 | 1 | 7 | 0 | 2 | 1 |
| grafana | Home - Migration Test Lab | 6 | 2 | 3 | 0 | 1 | 0 |
| grafana | Kubernetes / Views / Global | 30 | 2 | 24 | 0 | 0 | 4 |
| grafana | Node Exporter Full | 132 | 0 | 114 | 0 | 2 | 16 |
| grafana | Prometheus 2.0 (by FUSAKLA) | 44 | 6 | 33 | 5 | 0 | 0 |

**5 dashboards, 223 panels** audited from `infra/grafana/dashboards/`.
<!-- /GENERATED:DASHBOARD_SUMMARY -->

<!-- GENERATED:VERDICT_SUMMARY -->
## Verdict Summary

| Verdict | Count | Meaning |
|---------|-------|---------|
| **CORRECT** | 8 | Translation is semantically accurate |
| **MINOR_ISSUE** | 179 | Translated with approximations — review recommended |
| **EXPECTED_LIMITATION** | 36 | Known unsupported feature — placeholder or skip |
<!-- /GENERATED:VERDICT_SUMMARY -->

<!-- GENERATED:WARNING_PATTERNS -->
## Top Warning Patterns

| Count | Warning |
|------:|---------|
| 165 | Variable-driven label filters applied via Kibana dashboard controls |
| 80 | Merged compatible panel targets into a single ES\|QL query |
| 73 | No explicit aggregation; using AVG (correct for gauge metrics) |
| 35 | Grafana panel description is not carried into Kibana YAML automatically |
| 27 | Grafana panel has 1 field override(s); verify visual mappings manually |
| 27 | Wrapped irate in AVG() to support grouped TS queries |
| 21 | Approximated PromQL arithmetic using same-bucket ES\|QL math |
| 7 | Grafana panel has 2 field override(s); verify visual mappings manually |
| 6 | Grafana panel has 18 field override(s); verify visual mappings manually |
| 6 | Grafana panel has 19 field override(s); verify visual mappings manually |
| 5 | Panel has 2 PromQL targets but only 1 could be migrated (dropped targets are Windows-specific) |
| 5 | Panel has 2 PromQL targets but only 1 could be migrated |
| 5 | Grafana panel has 20 field override(s); verify visual mappings manually |
| 5 | Grafana panel has 17 field override(s); verify visual mappings manually |
| 5 | Visible panel targets did not expose PromQL-compatible expressions |
<!-- /GENERATED:WARNING_PATTERNS -->

---

## Per-Dashboard Traces

<!-- GENERATED:PER_DASHBOARD_TRACES -->
### Grafana: Diverse Panel Types Test

**File:** `diverse-panels-test.json` — **Panels:** 11

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| System Metrics | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Request Latency Heatmap | `heatmap` → `heatmap` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(http_request_duration_seconds_bucket[5m])) by (le) | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Traffic Distribution | `piechart` → `pie` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(http_requests_total{instance=~"$instance"}[5m])) by (handler) | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Top Endpoints | `barchart` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | topk(10, sum(rate(http_requests_total[5m])) by (handler)) | — |
| CPU Usage | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | 100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100) | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Memory Usage | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100 | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Uptime | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | time() - node_boot_time_seconds | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Disk Usage per Mount | `bargauge` → `bar` | migrated_with_warnings | **MINOR_ISSUE** | 100 - ((node_filesystem_avail_bytes{mountpoint!~".*pods.*"} / node_filesystem_si... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Active Alerts | `table` → `datatable` | migrated_with_warnings | **MINOR_ISSUE** | ALERTS{alertstate="firing"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Notes | `text` → `markdown` | migrated | **EXPECTED_LIMITATION** | — | — |
| Application Logs | `logs` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | {job="app"} \|= "error" | — |

<details>
<summary>Detailed traces (9 panels)</summary>

#### Request Latency Heatmap

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (heatmap):**

```
sum(rate(http_request_duration_seconds_bucket[5m])) by (le)
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel`
- `panel_translators` / `datatable_panel`
- `panel_translators` / `pie_panel`
- `panel_translators` / `fallback_line_panel` → fell back to line panel

**Translated (heatmap):**

```
TS metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = SUM(RATE(http_request_duration_seconds_bucket, 5m)) BY time_bucket = TBUCKET(5 minute), le
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `http_request_duration_seconds_bucket`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `le`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_request_duration_seconds_bucket`
- Output groups: `time_bucket, le`

**Visual IR:**

- Kibana type: `heatmap`
- Layout: x=0, y=0, w=48, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated as line chart (no direct heatmap mapping)

**Verdict:** MINOR_ISSUE

#### Traffic Distribution

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (piechart):**

```
sum(rate(http_requests_total{instance=~"$instance"}[5m])) by (handler)
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel`
- `panel_translators` / `datatable_panel`
- `panel_translators` / `pie_panel` → mapped to pie panel

**Translated (pie):**

```
TS metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE http_requests_total IS NOT NULL
| STATS http_requests_total = SUM(RATE(http_requests_total, 5m)) BY time_bucket = TBUCKET(5 minute), handler
| SORT time_bucket ASC
| STATS http_requests_total = LAST(http_requests_total, time_bucket) BY handler
| KEEP handler, http_requests_total
```

**Query IR:**

- Family: `range_agg`
- Metric: `http_requests_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `handler`
- Output shape: `table`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_requests_total`
- Output groups: `handler`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `pie`
- Layout: x=0, y=12, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, metrics, breakdowns, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Variable-driven label filters applied via Kibana dashboard controls

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Top Endpoints

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (barchart):**

```
topk(10, sum(rate(http_requests_total[5m])) by (handler))
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=unknown backend=ast
- `query_classifiers` / `fragment_guardrails` → topk requires manual redesign

**Query IR:**

- Family: `unknown`
- Metric: `http_requests_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `topk`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Semantic losses: topk requires manual redesign

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=24, y=12, w=24, h=12
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** topk requires manual redesign

**Semantic losses:** topk requires manual redesign

**Verdict:** EXPECTED_LIMITATION

#### CPU Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE mode == "idle"
| WHERE node_cpu_seconds_total IS NOT NULL
| STATS node_cpu_seconds_total_mode_idle_rate_avg = AVG(RATE(node_cpu_seconds_total, 5m)) BY time_bucket = TBUCKET(5 minute)
| EVAL node_cpu_seconds_total_mode_idle_rate_avg_calc = node_cpu_seconds_total_mode_idle_rate_avg * 100
| EVAL computed_value = (100 - node_cpu_seconds_total_mode_idle_rate_avg_calc)
| KEEP time_bucket, computed_value
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `-`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Output groups: `time_bucket`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math

**Verdict:** MINOR_ISSUE

#### Memory Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (gauge):**

```
(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE node_memory_MemAvailable_bytes IS NOT NULL OR node_memory_MemTotal_bytes IS NOT NULL
| STATS node_memory_MemAvailable_bytes = AVG(node_memory_MemAvailable_bytes), node_memory_MemTotal_bytes = AVG(node_memory_MemTotal_bytes) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| EVAL computed_value = ((1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = LAST(computed_value, time_bucket)
| KEEP time_bucket, computed_value
| EVAL _gauge_min = 0, _gauge_max = 100, _gauge_goal = 70
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `*`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math

**Visual IR:**

- Kibana type: `gauge`
- Layout: x=24, y=0, w=12, h=12
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; No explicit aggregation; using AVG (correct for gauge metrics)

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math

**Verdict:** MINOR_ISSUE

#### Uptime

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
time() - node_boot_time_seconds
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=uptime backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family uptime bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family` → translated uptime expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE node_boot_time_seconds IS NOT NULL
| STATS start_time_ms = MAX(node_boot_time_seconds * 1000)
| EVAL node_boot_time_seconds_uptime_seconds = DATE_DIFF("seconds", TO_DATETIME(start_time_ms), NOW())
| KEEP node_boot_time_seconds_uptime_seconds
```

**Query IR:**

- Family: `uptime`
- Metric: `node_boot_time_seconds`
- Binary op: `-`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `node_boot_time_seconds_uptime_seconds`
- Semantic losses: Approximated time() - metric as uptime from metric timestamp

**Visual IR:**

- Kibana type: `metric`
- Layout: x=36, y=0, w=12, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated time() - metric as uptime from metric timestamp

**Semantic losses:** Approximated time() - metric as uptime from metric timestamp

**Verdict:** MINOR_ISSUE

#### Disk Usage per Mount

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (bargauge):**

```
100 - ((node_filesystem_avail_bytes{mountpoint!~".*pods.*"} / node_filesystem_size_bytes) * 100)
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel` → approximated bargauge panel

**Translated (bar):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE node_filesystem_avail_bytes IS NOT NULL OR node_filesystem_size_bytes IS NOT NULL
| STATS node_filesystem_avail_bytes_mountpoint_pods = AVG(CASE((NOT (mountpoint RLIKE ".*pods.*")), node_filesystem_avail_bytes, NULL)), node_filesystem_size_bytes = AVG(node_filesystem_size_bytes) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), mountpoint
| EVAL computed_value = (100 - ((node_filesystem_avail_bytes_mountpoint_pods / node_filesystem_size_bytes) * 100))
| SORT time_bucket ASC
| STATS computed_value = LAST(computed_value, time_bucket) BY mountpoint
| KEEP mountpoint, computed_value
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `-`
- Output shape: `table`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Output groups: `mountpoint`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math

**Visual IR:**

- Kibana type: `bar`
- Layout: x=36, y=12, w=12, h=6
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; No explicit aggregation; using AVG (correct for gauge metrics); Approximated bargauge as bar chart

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math

**Verdict:** MINOR_ISSUE

#### Active Alerts

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (table):**

```
ALERTS{alertstate="firing"}
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel`
- `panel_translators` / `datatable_panel` → mapped to datatable panel

**Translated (datatable):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE alertstate == "firing"
| WHERE ALERTS IS NOT NULL
| STATS ALERTS = AVG(ALERTS) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), ALERTS = LAST(ALERTS, time_bucket)
| KEEP time_bucket, ALERTS
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `ALERTS`
- Output shape: `table`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `ALERTS`

**Visual IR:**

- Kibana type: `datatable`
- Layout: x=0, y=18, w=48, h=9
- Presentation kind: `esql`
- Config keys: type, query, metrics

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- field_overrides: 1

**Warnings:** Grafana panel has 1 field override(s); verify visual mappings manually; ALERTS{} is a Prometheus meta-metric exposing per-alert label sets; ES|QL aggregation collapses individual alerts into a single value; No explicit aggregation; using AVG (correct for gauge metrics)

**Notes:** Grafana panel has 1 field override(s); verify visual mappings manually

**Verdict:** MINOR_ISSUE

#### Application Logs

**Translation path:** `logql` · **Query language:** `logql` · **Readiness:** `manual_only`

**Source (logs):**

```
{job="app"} |= "error"
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=unknown backend=regex
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family`
- `query_translators` / `fragment_extract` → extracted fragment fields via regex
- `query_translators` / `scalar_outer_agg`
- `query_translators` / `resolve_labels`
- `query_translators` / `counter_detection`
- `query_translators` / `source_type` → selected FROM source
- `query_translators` / `time_filter` → applied time filter @timestamp >= ?_tstart AND @timestamp < ?_tend
- `query_translators` / `bucket` → applied bucket time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
- `query_translators` / `stats_expression`
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required` → missing metric name

**Query IR:**

- Family: `unknown`
- Output shape: `event_rows`
- Source lang: `logql`
- Target index: `metrics-prometheus-*`

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=24, y=27, w=24, h=6
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `logql`

**Inventory:**

- targets: 1

**Warnings:** AST parse failed (unexpected character: '|'), using regex fragment parser; Could not extract metric name

**Verdict:** EXPECTED_LIMITATION

</details>

<details>
<summary>Controls / Variables (1)</summary>

- `instance` (type: `options`)

</details>

---

### Grafana: Home - Migration Test Lab

**File:** `home.json` — **Panels:** 6

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Untitled | `text` → `markdown` | migrated | **EXPECTED_LIMITATION** | — | — |
| Prometheus Targets Up | `stat` → `metric` | migrated | **CORRECT** | count(up == 1) | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Scrape Duration by Job | `timeseries` → `line` | migrated_with_warnings | **CORRECT** | scrape_duration_seconds | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Memory Usage % | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100 | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Top Metrics by Series Count | `bargauge` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | topk(10, count by (__name__)({__name__=~".+"})) | — |
| Target Health Status | `table` → `datatable` | migrated_with_warnings | **CORRECT** | up | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |

<details>
<summary>Detailed traces (5 panels)</summary>

#### Prometheus Targets Up

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
count(up == 1)
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated aggregation with pre-aggregation comparison filter
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE up == 1
| STATS up_count = COUNT(*)
```

**Query IR:**

- Family: `simple_agg`
- Metric: `up_count`
- Outer agg: `count`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `up_count`

**Visual IR:**

- Kibana type: `metric`
- Layout: x=0, y=6, w=16, h=9
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Verdict:** CORRECT

#### Scrape Duration by Job

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
scrape_duration_seconds
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE scrape_duration_seconds IS NOT NULL
| STATS scrape_duration_seconds = AVG(scrape_duration_seconds) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), job
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `scrape_duration_seconds`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `scrape_duration_seconds`
- Output groups: `time_bucket, job`

**Visual IR:**

- Kibana type: `line`
- Layout: x=16, y=6, w=32, h=15
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** No explicit aggregation; using AVG (correct for gauge metrics)

**Verdict:** CORRECT

#### Memory Usage %

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (gauge):**

```
(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE node_memory_MemAvailable_bytes IS NOT NULL OR node_memory_MemTotal_bytes IS NOT NULL
| STATS node_memory_MemAvailable_bytes = AVG(node_memory_MemAvailable_bytes), node_memory_MemTotal_bytes = AVG(node_memory_MemTotal_bytes) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| EVAL computed_value = ((1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = LAST(computed_value, time_bucket)
| KEEP time_bucket, computed_value
| EVAL _gauge_min = 0, _gauge_max = 100, _gauge_goal = 70
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `*`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math

**Visual IR:**

- Kibana type: `gauge`
- Layout: x=0, y=21, w=16, h=9
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; No explicit aggregation; using AVG (correct for gauge metrics)

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math

**Verdict:** MINOR_ISSUE

#### Top Metrics by Series Count

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (bargauge):**

```
topk(10, count by (__name__)({__name__=~".+"}))
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=unknown backend=ast
- `query_classifiers` / `fragment_guardrails` → PromQL metric-name introspection via __name__ requires manual redesign; topk requires manual redesign

**Query IR:**

- Family: `unknown`
- Outer agg: `topk`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Semantic losses: PromQL metric-name introspection via __name__ requires manual redesign, topk requires manual redesign

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=0, y=30, w=24, h=12
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** PromQL metric-name introspection via __name__ requires manual redesign; topk requires manual redesign

**Semantic losses:** PromQL metric-name introspection via __name__ requires manual redesign; topk requires manual redesign

**Verdict:** EXPECTED_LIMITATION

#### Target Health Status

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (table):**

```
up
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel`
- `panel_translators` / `datatable_panel` → mapped to datatable panel

**Translated (datatable):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE up IS NOT NULL
| STATS up = AVG(up) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), up = LAST(up, time_bucket)
| KEEP time_bucket, up
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `up`
- Output shape: `table`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `up`

**Visual IR:**

- Kibana type: `datatable`
- Layout: x=24, y=30, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, metrics

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** No explicit aggregation; using AVG (correct for gauge metrics)

**Verdict:** CORRECT

</details>

---

### Grafana: Kubernetes / Views / Global

**File:** `k8s-views-global.json` — **Panels:** 30

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Overview | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Resources | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Kubernetes | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Network | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Global CPU  Usage | `bargauge` → `bar` | migrated_with_warnings | **MINOR_ISSUE** | avg(sum by (instance, cpu) (rate(node_cpu_seconds_total{mode!~"idle\|iowait\|ste... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Global RAM Usage | `bargauge` → `bar` | migrated_with_warnings | **MINOR_ISSUE** | sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"} - node_memory_Mem... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Nodes | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | count(count by (node) (kube_node_info{cluster="$cluster"})) | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Kubernetes Resource Count | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_namespace_labels{cluster="$cluster"}) \|\|\| sum(kube_pod_container_sta... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Namespaces | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | count(kube_namespace_created{cluster="$cluster"}) | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| CPU Usage | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(node_cpu_seconds_total{mode!~"idle\|iowait\|steal", cluster="$cluster",... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| RAM Usage | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"} - node_memory_Mem... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Running Pods | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_pod_status_phase{phase="Running", cluster="$cluster"}) | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Cluster CPU Utilization | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | avg(sum by (instance, cpu) (rate(node_cpu_seconds_total{mode!~"idle\|iowait\|ste... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Cluster Memory Utilization | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"} - node_memory_Mem... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| CPU Utilization by namespace | `timeseries` → `line` | migrated | **CORRECT** | sum(rate(container_cpu_usage_seconds_total{image!="", cluster="$cluster"}[$__rat... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Memory Utilization by namespace | `timeseries` → `line` | migrated | **CORRECT** | sum(container_memory_working_set_bytes{image!="", cluster="$cluster"}) by (names... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| CPU Utilization by instance | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | avg(sum by (instance, cpu) (rate(node_cpu_seconds_total{mode!~"idle\|iowait\|ste... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Memory Utilization by instance | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"} - node_memory_Mem... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| CPU Throttled seconds by namespace | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(container_cpu_cfs_throttled_seconds_total{image!="", cluster="$cluster"... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| CPU Core Throttled by instance | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(node_cpu_core_throttles_total{cluster="$cluster", job="$job"}[$__rate_i... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Kubernetes Pods QoS classes | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_pod_status_qos_class{cluster="$cluster"}) by (qos_class) \|\|\| sum(kub... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Kubernetes Pods Status Reason | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_pod_status_reason{cluster="$cluster"}) by (reason) | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| OOM Events by namespace | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(increase(container_oom_events_total{cluster="$cluster"}[$__rate_interval])) ... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Container Restarts by namespace | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(increase(kube_pod_container_status_restarts_total{cluster="$cluster"}[$__rat... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Global Network Utilization by device | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(node_network_receive_bytes_total{device!~"(veth\|azv\|lxc).*", cluster=... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Network Saturation - Packets dropped | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(node_network_receive_drop_total{cluster="$cluster", job="$job"}[$__rate... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Network Received by namespace | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(container_network_receive_bytes_total{cluster="$cluster"}[$__rate_inter... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Total Network Received (with all virtual devices) by instance | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(node_network_receive_bytes_total{cluster="$cluster", job="$job"}[$__rat... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Network Received (without loopback)  by instance | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(node_network_receive_bytes_total{device!~"(veth\|azv\|lxc\|lo).*", clus... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Network Received (loopback only) by instance | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(node_network_receive_bytes_total{device="lo", cluster="$cluster", job="... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |

<details>
<summary>Detailed traces (26 panels)</summary>

#### Global CPU  Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (bargauge):**

```
avg(sum by (instance, cpu) (rate(node_cpu_seconds_total{mode!~"idle|iowait|steal", cluster="$cluster", job="$job"}[$__rate_interval]))) ||| avg(sum by (core) (rate(windows_cpu_time_total{mode!="idle", cluster="$cluster"}[$__rate_interval]))) ||| sum(kube_pod_container_resource_requests{resource="cpu", cluster="$cluster"}) / sum(machine_cpu_cores{cluster="$cluster"}) ||| sum(kube_pod_container_resource_limits{resource="cpu", cluster="$cluster"}) / sum(machine_cpu_cores{cluster="$cluster"})
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel` → approximated bargauge panel

**Translated (bar):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE kube_pod_container_resource_requests IS NOT NULL OR machine_cpu_cores IS NOT NULL OR kube_pod_container_resource_limits IS NOT NULL
| STATS kube_pod_container_resource_requests_Requests_resource_cpu_c = SUM(CASE((resource == "cpu"), kube_pod_container_resource_requests, NULL)), machine_cpu_cores_Requests_cluster_sum = SUM(machine_cpu_cores), kube_pod_container_resource_limits_Limits_resource_cpu_clust = SUM(CASE((resource == "cpu"), kube_pod_container_resource_limits, NULL)), machine_cpu_cores_Limits_cluster_sum = SUM(machine_cpu_cores) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| EVAL Requests = (kube_pod_container_resource_requests_Requests_resource_cpu_c / machine_cpu_cores_Requests_cluster_sum)
| EVAL Limits = (kube_pod_container_resource_limits_Limits_resource_cpu_clust / machine_cpu_cores_Limits_cluster_sum)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), Requests = LAST(Requests, time_bucket), Limits = LAST(Limits, time_bucket)
| KEEP time_bucket, Requests, Limits
| EVAL __labels = MV_APPEND("Requests", "Limits"), __values = MV_APPEND(TO_STRING(Requests), TO_STRING(Limits))
| EVAL __pairs = MV_ZIP(__labels, __values, "~")
| MV_EXPAND __pairs
| EVAL label = MV_FIRST(SPLIT(__pairs, "~")), value = TO_DOUBLE(MV_LAST(SPLIT(__pairs, "~")))
| KEEP label, value
| SORT label ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `/`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `Requests`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `bar`
- Layout: x=0, y=0, w=12, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 4
- transformations: 2

**Warnings:** Grafana panel has 2 transformation(s); manual review recommended; Approximated PromQL arithmetic using same-bucket ES|QL math; Variable-driven label filters applied via Kibana dashboard controls; Merged compatible panel targets into a single ES|QL query; Dropped 2 incompatible target(s); showing 2 mergeable targets (dropped targets are Windows-specific)

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Notes:** Grafana panel has 2 transformation(s); manual review recommended

**Verdict:** MINOR_ISSUE

#### Global RAM Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (bargauge):**

```
sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"} - node_memory_MemAvailable_bytes{cluster="$cluster", job="$job"}) / sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"}) ||| sum(windows_memory_available_bytes{cluster="$cluster"} + windows_memory_cache_bytes{cluster="$cluster"}) / sum(windows_os_visible_memory_bytes{cluster="$cluster"}) ||| sum(kube_pod_container_resource_requests{resource="memory", cluster="$cluster"}) / sum(machine_memory_bytes{cluster="$cluster"}) ||| sum(kube_pod_container_resource_limits{resource="memory", cluster="$cluster"}) / sum(machine_memory_bytes{cluster="$cluster"})
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel` → approximated bargauge panel

**Translated (bar):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE kube_pod_container_resource_requests IS NOT NULL OR machine_memory_bytes IS NOT NULL OR kube_pod_container_resource_limits IS NOT NULL
| STATS kube_pod_container_resource_requests_Requests_resource_memor = SUM(CASE((resource == "memory"), kube_pod_container_resource_requests, NULL)), machine_memory_bytes_Requests_cluster_sum = SUM(machine_memory_bytes), kube_pod_container_resource_limits_Limits_resource_memory_cl = SUM(CASE((resource == "memory"), kube_pod_container_resource_limits, NULL)), machine_memory_bytes_Limits_cluster_sum = SUM(machine_memory_bytes) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| EVAL Requests = (kube_pod_container_resource_requests_Requests_resource_memor / machine_memory_bytes_Requests_cluster_sum)
| EVAL Limits = (kube_pod_container_resource_limits_Limits_resource_memory_cl / machine_memory_bytes_Limits_cluster_sum)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), Requests = LAST(Requests, time_bucket), Limits = LAST(Limits, time_bucket)
| KEEP time_bucket, Requests, Limits
| EVAL __labels = MV_APPEND("Requests", "Limits"), __values = MV_APPEND(TO_STRING(Requests), TO_STRING(Limits))
| EVAL __pairs = MV_ZIP(__labels, __values, "~")
| MV_EXPAND __pairs
| EVAL label = MV_FIRST(SPLIT(__pairs, "~")), value = TO_DOUBLE(MV_LAST(SPLIT(__pairs, "~")))
| KEEP label, value
| SORT label ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `/`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `Requests`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `bar`
- Layout: x=12, y=0, w=12, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 4
- transformations: 2

**Warnings:** Grafana panel has 2 transformation(s); manual review recommended; Approximated PromQL arithmetic using same-bucket ES|QL math; Variable-driven label filters applied via Kibana dashboard controls; Merged compatible panel targets into a single ES|QL query; Dropped 2 incompatible target(s); showing 2 mergeable targets (dropped targets are Windows-specific)

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Notes:** Grafana panel has 2 transformation(s); manual review recommended

**Verdict:** MINOR_ISSUE

#### Nodes

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
count(count by (node) (kube_node_info{cluster="$cluster"}))
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=nested_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family nested_agg bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family` → translated nested count(count()) expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE kube_node_info IS NOT NULL
| STATS kube_node_info_count = COUNT_DISTINCT(node)
```

**Query IR:**

- Family: `nested_agg`
- Metric: `kube_node_info_count`
- Outer agg: `count`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kube_node_info_count`
- Semantic losses: Dropped variable-driven label filters during migration, Approximated nested count(count()) as COUNT_DISTINCT(node)

**Visual IR:**

- Kibana type: `metric`
- Layout: x=24, y=0, w=4, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Variable-driven label filters applied via Kibana dashboard controls; Approximated nested count(count()) as COUNT_DISTINCT(node)

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated nested count(count()) as COUNT_DISTINCT(node)

**Verdict:** MINOR_ISSUE

#### Kubernetes Resource Count

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(kube_namespace_labels{cluster="$cluster"}) ||| sum(kube_pod_container_status_running{cluster="$cluster"}) ||| sum(kube_pod_status_phase{phase="Running", cluster="$cluster"}) ||| sum(kube_service_info{cluster="$cluster"}) ||| sum(kube_endpoint_info{cluster="$cluster"}) ||| sum(kube_ingress_info{cluster="$cluster"}) ||| sum(kube_deployment_labels{cluster="$cluster"}) ||| sum(kube_statefulset_labels{cluster="$cluster"}) ||| sum(kube_daemonset_labels{cluster="$cluster"}) ||| sum(kube_persistentvolumeclaim_info{cluster="$cluster"}) ||| sum(kube_hpa_labels{cluster="$cluster"}) ||| sum(kube_configmap_info{cluster="$cluster"}) ||| sum(kube_secret_info{cluster="$cluster"}) ||| sum(kube_networkpolicy_labels{cluster="$cluster"}) ||| count(count by (node) (kube_node_info{cluster="$cluster"}))
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE kube_namespace_labels IS NOT NULL OR kube_pod_container_status_running IS NOT NULL OR kube_pod_status_phase IS NOT NULL OR kube_service_info IS NOT NULL OR kube_endpoint_info IS NOT NULL OR kube_ingress_info IS NOT NULL OR kube_deployment_labels IS NOT NULL OR kube_statefulset_labels IS NOT NULL OR kube_daemonset_labels IS NOT NULL OR kube_persistentvolumeclaim_info IS NOT NULL OR kube_hpa_labels IS NOT NULL OR kube_configmap_info IS NOT NULL OR kube_secret_info IS NOT NULL OR kube_networkpolicy_labels IS NOT NULL OR kube_node_info IS NOT NULL
| STATS kube_namespace_labels_A_cluster_sum = SUM(kube_namespace_labels), kube_pod_container_status_running_B_cluster_sum = SUM(kube_pod_container_status_running), kube_pod_status_phase_O_phase_Running_cluster_sum = SUM(CASE((phase == "Running"), kube_pod_status_phase, NULL)), kube_service_info_C_cluster_sum = SUM(kube_service_info), kube_endpoint_info_D_cluster_sum = SUM(kube_endpoint_info), kube_ingress_info_E_cluster_sum = SUM(kube_ingress_info), kube_deployment_labels_F_cluster_sum = SUM(kube_deployment_labels), kube_statefulset_labels_G_cluster_sum = SUM(kube_statefulset_labels), kube_daemonset_labels_H_cluster_sum = SUM(kube_daemonset_labels), kube_persistentvolumeclaim_info_I_cluster_sum = SUM(kube_persistentvolumeclaim_info), kube_hpa_labels_J_cluster_sum = SUM(kube_hpa_labels), kube_configmap_info_K_cluster_sum = SUM(kube_configmap_info), kube_secret_info_L_cluster_sum = SUM(kube_secret_info), kube_networkpolicy_labels_M_cluster_sum = SUM(kube_networkpolicy_labels), kube_node_info_N_cluster_count = COUNT_DISTINCT(node) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| EVAL Namespaces = kube_namespace_labels_A_cluster_sum
| EVAL Running_Containers = kube_pod_container_status_running_B_cluster_sum
| EVAL Running_Pods = kube_pod_status_phase_O_phase_Running_cluster_sum
| EVAL Services = kube_service_info_C_cluster_sum
| EVAL Endpoints = kube_endpoint_info_D_cluster_sum
| EVAL Ingresses = kube_ingress_info_E_cluster_sum
| EVAL Deployments = kube_deployment_labels_F_cluster_sum
| EVAL Statefulsets = kube_statefulset_labels_G_cluster_sum
| EVAL Daemonsets = kube_daemonset_labels_H_cluster_sum
| EVAL Persistent_Volume_Claims = kube_persistentvolumeclaim_info_I_cluster_sum
| EVAL Horizontal_Pod_Autoscalers = kube_hpa_labels_J_cluster_sum
| EVAL Configmaps = kube_configmap_info_K_cluster_sum
| EVAL Secrets = kube_secret_info_L_cluster_sum
| EVAL Network_Policies = kube_networkpolicy_labels_M_cluster_sum
| EVAL Nodes = kube_node_info_N_cluster_count
| KEEP time_bucket, Namespaces, Running_Containers, Running_Pods, Services, Endpoints, Ingresses, Deployments, Statefulsets, Daemonsets, Persistent_Volume_Claims, Horizontal_Pod_Autoscalers, Configmaps, Secrets, Network_Policies, Nodes
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `kube_namespace_labels`
- Outer agg: `sum`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `Namespaces`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration, Approximated nested count(count()) as COUNT_DISTINCT(node)

**Visual IR:**

- Kibana type: `line`
- Layout: x=28, y=0, w=20, h=18
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 15

**Warnings:** Variable-driven label filters applied via Kibana dashboard controls; Approximated nested count(count()) as COUNT_DISTINCT(node); Merged compatible panel targets into a single ES|QL query

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated nested count(count()) as COUNT_DISTINCT(node)

**Verdict:** MINOR_ISSUE

#### Namespaces

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
count(kube_namespace_created{cluster="$cluster"})
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated count of counter metric
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE kube_namespace_created IS NOT NULL
| STATS series_present = COUNT(*) BY service.instance.id
| STATS kube_namespace_created_count = COUNT(*)
```

**Query IR:**

- Family: `simple_agg`
- Metric: `kube_namespace_created_count`
- Outer agg: `count`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kube_namespace_created_count`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=24, y=18, w=4, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Variable-driven label filters applied via Kibana dashboard controls

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### CPU Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
sum(rate(node_cpu_seconds_total{mode!~"idle|iowait|steal", cluster="$cluster", job="$job"}[$__rate_interval])) ||| sum(rate(windows_cpu_time_total{mode!="idle", cluster="$cluster"}[$__rate_interval])) ||| sum(kube_pod_container_resource_requests{resource="cpu", cluster="$cluster"}) ||| sum(kube_pod_container_resource_limits{resource="cpu", cluster="$cluster"}) ||| sum(machine_cpu_cores{cluster="$cluster"})
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE NOT (mode RLIKE "idle|iowait|steal")
| WHERE node_cpu_seconds_total IS NOT NULL
| STATS node_cpu_seconds_total = SUM(RATE(node_cpu_seconds_total, 5m)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), node_cpu_seconds_total = LAST(node_cpu_seconds_total, time_bucket)
| KEEP time_bucket, node_cpu_seconds_total
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `node_cpu_seconds_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `node_cpu_seconds_total`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=0, y=24, w=12, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 5
- transformations: 2

**Warnings:** Grafana panel has 2 transformation(s); manual review recommended; Variable-driven label filters applied via Kibana dashboard controls; Panel has 5 PromQL targets but only 1 could be migrated (dropped targets are Windows-specific)

**Semantic losses:** Dropped variable-driven label filters during migration

**Notes:** Grafana panel has 2 transformation(s); manual review recommended

**Verdict:** MINOR_ISSUE

#### RAM Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"} - node_memory_MemAvailable_bytes{cluster="$cluster", job="$job"}) ||| sum(windows_os_visible_memory_bytes{cluster="$cluster"} - windows_memory_available_bytes{cluster="$cluster"} - windows_memory_cache_bytes{cluster="$cluster"}) ||| sum(kube_pod_container_resource_requests{resource="memory", cluster="$cluster"}) ||| sum(kube_pod_container_resource_limits{resource="memory", cluster="$cluster"}) ||| sum(machine_memory_bytes{cluster="$cluster"})
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=unknown backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family`
- `query_translators` / `fragment_extract` → extracted fragment fields via ast
- `query_translators` / `scalar_outer_agg`
- `query_translators` / `resolve_labels`
- `query_translators` / `counter_detection`
- `query_translators` / `source_type` → selected FROM source
- `query_translators` / `time_filter` → applied time filter @timestamp >= ?_tstart AND @timestamp < ?_tend
- `query_translators` / `bucket` → applied bucket time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
- `query_translators` / `stats_expression` → built stats expression SUM(node_memory_MemTotal_bytes)
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql` → rendered ES|QL query
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS node_memory_MemTotal_bytes = SUM(node_memory_MemTotal_bytes) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| SORT time_bucket ASC
```

**Query IR:**

- Family: `unknown`
- Metric: `node_memory_MemTotal_bytes`
- Outer agg: `sum`
- Binary op: `-`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`

**Visual IR:**

- Kibana type: `metric`
- Layout: x=12, y=24, w=12, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 5
- transformations: 2

**Warnings:** Grafana panel has 2 transformation(s); manual review recommended; Panel has 5 PromQL targets but only 1 could be migrated (dropped targets are Windows-specific)

**Notes:** Grafana panel has 2 transformation(s); manual review recommended

**Verdict:** MINOR_ISSUE

#### Running Pods

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
sum(kube_pod_status_phase{phase="Running", cluster="$cluster"})
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE phase == "Running"
| WHERE kube_pod_status_phase IS NOT NULL
| STATS kube_pod_status_phase = SUM(kube_pod_status_phase) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kube_pod_status_phase = LAST(kube_pod_status_phase, time_bucket)
| KEEP time_bucket, kube_pod_status_phase
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `kube_pod_status_phase`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kube_pod_status_phase`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=24, y=24, w=4, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Variable-driven label filters applied via Kibana dashboard controls

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Cluster CPU Utilization

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
avg(sum by (instance, cpu) (rate(node_cpu_seconds_total{mode!~"idle|iowait|steal", cluster="$cluster", job="$job"}[$__rate_interval]))) ||| 1 - avg(rate(windows_cpu_time_total{cluster="$cluster",mode="idle"}[$__rate_interval]))
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=unknown backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family`
- `query_translators` / `fragment_extract` → extracted fragment fields via ast
- `query_translators` / `scalar_outer_agg`
- `query_translators` / `resolve_labels`
- `query_translators` / `counter_detection`
- `query_translators` / `source_type` → selected TS source
- `query_translators` / `time_filter` → applied time filter @timestamp >= ?_tstart AND @timestamp < ?_tend
- `query_translators` / `bucket` → applied bucket time_bucket = TBUCKET(5 minute)
- `query_translators` / `stats_expression` → built stats expression AVG(RATE(node_cpu_seconds_total, 5m))
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql` → rendered ES|QL query
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS node_cpu_seconds_total = AVG(RATE(node_cpu_seconds_total, 5m)) BY time_bucket = TBUCKET(5 minute), instance, cpu
| SORT time_bucket ASC
```

**Query IR:**

- Family: `unknown`
- Metric: `node_cpu_seconds_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `avg`
- Group labels: `instance, cpu`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2
- transformations: 1

**Warnings:** Grafana panel has 1 transformation(s); manual review recommended; Panel has 2 PromQL targets but only 1 could be migrated (dropped targets are Windows-specific)

**Notes:** Grafana panel has 1 transformation(s); manual review recommended

**Verdict:** MINOR_ISSUE

#### Cluster Memory Utilization

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"} - node_memory_MemAvailable_bytes{cluster="$cluster", job="$job"}) / sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"}) ||| sum(windows_os_visible_memory_bytes{cluster="$cluster"} - windows_memory_available_bytes{cluster="$cluster"}) / sum(windows_os_visible_memory_bytes{cluster="$cluster"})
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family`
- `query_translators` / `fragment_extract` → extracted fragment fields via ast
- `query_translators` / `scalar_outer_agg`
- `query_translators` / `resolve_labels`
- `query_translators` / `counter_detection`
- `query_translators` / `source_type` → selected FROM source
- `query_translators` / `time_filter` → applied time filter @timestamp >= ?_tstart AND @timestamp < ?_tend
- `query_translators` / `bucket` → applied bucket time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
- `query_translators` / `stats_expression` → built stats expression SUM(node_memory_MemTotal_bytes)
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql` → rendered ES|QL query
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS node_memory_MemTotal_bytes = SUM(node_memory_MemTotal_bytes) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `node_memory_MemTotal_bytes`
- Outer agg: `sum`
- Binary op: `/`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=0, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2
- transformations: 1

**Warnings:** Grafana panel has 1 transformation(s); manual review recommended; Panel has 2 PromQL targets but only 1 could be migrated (dropped targets are Windows-specific)

**Notes:** Grafana panel has 1 transformation(s); manual review recommended

**Verdict:** MINOR_ISSUE

#### CPU Utilization by namespace

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(rate(container_cpu_usage_seconds_total{image!="", cluster="$cluster"}[$__rate_interval])) by (namespace)
+ on (namespace)
(sum(rate(windows_container_cpu_usage_seconds_total{container_id!="", cluster="$cluster"}[$__rate_interval]) * on (container_id) group_left (container, pod, namespace) max by ( container, container_id, pod, namespace) (kube_pod_container_info{container_id!="", cluster="$cluster"}) OR kube_namespace_created{cluster="$cluster"} * 0) by (namespace))
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family`
- `query_translators` / `fragment_extract` → extracted fragment fields via ast
- `query_translators` / `scalar_outer_agg`
- `query_translators` / `resolve_labels`
- `query_translators` / `counter_detection`
- `query_translators` / `source_type` → selected TS source
- `query_translators` / `time_filter` → applied time filter @timestamp >= ?_tstart AND @timestamp < ?_tend
- `query_translators` / `bucket` → applied bucket time_bucket = TBUCKET(5 minute)
- `query_translators` / `stats_expression` → built stats expression SUM(RATE(container_cpu_usage_seconds_total, 5m))
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql` → rendered ES|QL query
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS container_cpu_usage_seconds_total = SUM(RATE(container_cpu_usage_seconds_total, 5m)) BY time_bucket = TBUCKET(5 minute), namespace
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `container_cpu_usage_seconds_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `namespace`
- Binary op: `+`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=12, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Verdict:** CORRECT

#### Memory Utilization by namespace

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(container_memory_working_set_bytes{image!="", cluster="$cluster"}) by (namespace)
+ on (namespace)
(sum(windows_container_memory_usage_commit_bytes{container_id!="", cluster="$cluster"} * on (container_id) group_left (container, pod, namespace) max by ( container, container_id, pod, namespace) (kube_pod_container_info{container_id!="", cluster="$cluster"}) OR kube_namespace_created{cluster="$cluster"} * 0) by (namespace))
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family`
- `query_translators` / `fragment_extract` → extracted fragment fields via ast
- `query_translators` / `scalar_outer_agg`
- `query_translators` / `resolve_labels`
- `query_translators` / `counter_detection`
- `query_translators` / `source_type` → selected FROM source
- `query_translators` / `time_filter` → applied time filter @timestamp >= ?_tstart AND @timestamp < ?_tend
- `query_translators` / `bucket` → applied bucket time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
- `query_translators` / `stats_expression` → built stats expression SUM(container_memory_working_set_bytes)
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql` → rendered ES|QL query
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS container_memory_working_set_bytes = SUM(container_memory_working_set_bytes) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), namespace
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `container_memory_working_set_bytes`
- Outer agg: `sum`
- Group labels: `namespace`
- Binary op: `+`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=12, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Verdict:** CORRECT

#### CPU Utilization by instance

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
avg(sum by (instance, cpu) (rate(node_cpu_seconds_total{mode!~"idle|iowait|steal", cluster="$cluster", job="$job"}[$__rate_interval]))) by (instance) ||| avg(sum by (instance,core) (rate(windows_cpu_time_total{mode!="idle", cluster="$cluster"}[$__rate_interval]))) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=unknown backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family`
- `query_translators` / `fragment_extract` → extracted fragment fields via ast
- `query_translators` / `scalar_outer_agg`
- `query_translators` / `resolve_labels`
- `query_translators` / `counter_detection`
- `query_translators` / `source_type` → selected TS source
- `query_translators` / `time_filter` → applied time filter @timestamp >= ?_tstart AND @timestamp < ?_tend
- `query_translators` / `bucket` → applied bucket time_bucket = TBUCKET(5 minute)
- `query_translators` / `stats_expression` → built stats expression AVG(RATE(node_cpu_seconds_total, 5m))
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql` → rendered ES|QL query
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS node_cpu_seconds_total = AVG(RATE(node_cpu_seconds_total, 5m)) BY time_bucket = TBUCKET(5 minute), instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `unknown`
- Metric: `node_cpu_seconds_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `avg`
- Group labels: `instance`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=24, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Panel has 2 PromQL targets but only 1 could be migrated (dropped targets are Windows-specific)

**Verdict:** MINOR_ISSUE

#### Memory Utilization by instance

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"} - node_memory_MemAvailable_bytes{cluster="$cluster", job="$job"}) by (instance) ||| sum(windows_os_visible_memory_bytes{cluster="$cluster"} - windows_memory_available_bytes{cluster="$cluster"}) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=unknown backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family`
- `query_translators` / `fragment_extract` → extracted fragment fields via ast
- `query_translators` / `scalar_outer_agg`
- `query_translators` / `resolve_labels`
- `query_translators` / `counter_detection`
- `query_translators` / `source_type` → selected FROM source
- `query_translators` / `time_filter` → applied time filter @timestamp >= ?_tstart AND @timestamp < ?_tend
- `query_translators` / `bucket` → applied bucket time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
- `query_translators` / `stats_expression` → built stats expression SUM(node_memory_MemTotal_bytes)
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql` → rendered ES|QL query
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS node_memory_MemTotal_bytes = SUM(node_memory_MemTotal_bytes) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `unknown`
- Metric: `node_memory_MemTotal_bytes`
- Outer agg: `sum`
- Group labels: `instance`
- Binary op: `-`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=24, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Panel has 2 PromQL targets but only 1 could be migrated (dropped targets are Windows-specific)

**Verdict:** MINOR_ISSUE

#### CPU Throttled seconds by namespace

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(rate(container_cpu_cfs_throttled_seconds_total{image!="", cluster="$cluster"}[$__rate_interval])) by (namespace) > 0
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter` → applied post-aggregation filter > 0
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE image != ""
| WHERE container_cpu_cfs_throttled_seconds_total IS NOT NULL
| STATS container_cpu_cfs_throttled_seconds_total = SUM(RATE(container_cpu_cfs_throttled_seconds_total, 5m)) BY time_bucket = TBUCKET(5 minute), namespace
| WHERE container_cpu_cfs_throttled_seconds_total > 0
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `container_cpu_cfs_throttled_seconds_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `namespace`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_cpu_cfs_throttled_seconds_total`
- Output groups: `time_bucket, namespace`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=36, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Variable-driven label filters applied via Kibana dashboard controls

**Semantic losses:** Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Controls / Variables (2)</summary>

- `cluster` (type: `options`)
- `job` (type: `options`)

</details>

---

### Grafana: Node Exporter Full

**File:** `node-exporter-full.json` — **Panels:** 132

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Quick CPU / Mem / Disk | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Basic CPU / Mem / Net / Disk | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| CPU / Memory / Net / Disk | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Memory Meminfo | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Memory Vmstat | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| System Timesync | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| System Processes | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| System Misc | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Hardware Misc | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Systemd | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Storage Disk | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Storage Filesystem | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Network Traffic | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Network Sockstat | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Network Netstat | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Node Exporter | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Pressure | `bargauge` → `bar` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_pressure_cpu_waiting_seconds_total{instance="$node",job="$job"}[$__ra... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| CPU Busy | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | 100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle", instance="$node"}[$__rat... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Sys Load | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | scalar(node_load1{instance="$node",job="$job"}) * 100 / count(count(node_cpu_sec... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| RAM Used | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | (1 - (node_memory_MemAvailable_bytes{instance="$node", job="$job"} / node_memory... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| SWAP Used | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | ((node_memory_SwapTotal_bytes{instance="$node",job="$job"} - node_memory_SwapFre... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Root FS Used | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | 100 - ((node_filesystem_avail_bytes{instance="$node",job="$job",mountpoint="/",f... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| CPU Cores | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu)) | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Uptime | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | node_time_seconds{instance="$node",job="$job"} - node_boot_time_seconds{instance... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| RootFS Total | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | node_filesystem_size_bytes{instance="$node",job="$job",mountpoint="/",fstype!="r... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| RAM Total | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_MemTotal_bytes{instance="$node",job="$job"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| SWAP Total | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_SwapTotal_bytes{instance="$node",job="$job"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| CPU Basic | `timeseries` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | sum(irate(node_cpu_seconds_total{instance="$node",job="$job", mode="system"}[$__... | — |
| Memory Basic | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_MemTotal_bytes{instance="$node",job="$job"} \|\|\| node_memory_MemTo... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Network Traffic Basic | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_network_receive_bytes_total{instance="$node",job="$job"}[$__rate_inte... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Disk Space Used Basic | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | 100 - ((node_filesystem_avail_bytes{instance="$node",job="$job",device!~'rootfs'... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| CPU | `timeseries` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | sum(irate(node_cpu_seconds_total{instance="$node",job="$job", mode="system"}[$__... | — |
| Memory Stack | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_MemTotal_bytes{instance="$node",job="$job"} - node_memory_MemFree_by... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Network Traffic | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_network_receive_bytes_total{instance="$node",job="$job"}[$__rate_inte... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Disk Space Used | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_filesystem_size_bytes{instance="$node",job="$job",device!~'rootfs'} - node_... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Disk IOps | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_disk_reads_completed_total{instance="$node",job="$job",device=~"$disk... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| I/O Usage Read / Write | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_disk_read_bytes_total{instance="$node",job="$job",device=~"$diskdevic... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| I/O Utilization | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_disk_io_time_seconds_total{instance="$node",job="$job",device=~"$disk... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| CPU spent seconds in guests (VMs) | `timeseries` → `bar` | migrated_with_warnings | **MINOR_ISSUE** | sum by(instance) (irate(node_cpu_guest_seconds_total{instance="$node",job="$job"... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Memory Active / Inactive | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_Inactive_bytes{instance="$node",job="$job"} \|\|\| node_memory_Activ... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Memory Committed | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_Committed_AS_bytes{instance="$node",job="$job"} \|\|\| node_memory_C... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Memory Active / Inactive Detail | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_Inactive_file_bytes{instance="$node",job="$job"} \|\|\| node_memory_... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Memory Writeback and Dirty | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_Writeback_bytes{instance="$node",job="$job"} \|\|\| node_memory_Writ... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Memory Shared and Mapped | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_Mapped_bytes{instance="$node",job="$job"} \|\|\| node_memory_Shmem_b... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Memory Slab | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_SUnreclaim_bytes{instance="$node",job="$job"} \|\|\| node_memory_SRe... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Memory Vmalloc | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_VmallocChunk_bytes{instance="$node",job="$job"} \|\|\| node_memory_V... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Memory Bounce | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_Bounce_bytes{instance="$node",job="$job"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Memory Anonymous | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_AnonHugePages_bytes{instance="$node",job="$job"} \|\|\| node_memory_... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Memory Kernel / CPU | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_KernelStack_bytes{instance="$node",job="$job"} \|\|\| node_memory_Pe... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Memory HugePages Counter | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_HugePages_Free{instance="$node",job="$job"} \|\|\| node_memory_HugeP... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Memory HugePages Size | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_HugePages_Total{instance="$node",job="$job"} \|\|\| node_memory_Huge... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Memory DirectMap | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_DirectMap1G_bytes{instance="$node",job="$job"} \|\|\| node_memory_Di... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Memory Unevictable and MLocked | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_Unevictable_bytes{instance="$node",job="$job"} \|\|\| node_memory_Ml... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Memory NFS | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_NFS_Unstable_bytes{instance="$node",job="$job"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Memory Pages In / Out | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_vmstat_pgpgin{instance="$node",job="$job"}[$__rate_interval]) \|\|\| ... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Memory Pages Swap In / Out | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_vmstat_pswpin{instance="$node",job="$job"}[$__rate_interval]) \|\|\| ... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Memory Page Faults | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_vmstat_pgfault{instance="$node",job="$job"}[$__rate_interval]) \|\|\|... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| OOM Killer | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_vmstat_oom_kill{instance="$node",job="$job"}[$__rate_interval]) | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Time Synchronized Drift | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_timex_estimated_error_seconds{instance="$node",job="$job"} \|\|\| node_time... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Time PLL Adjust | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_timex_loop_time_constant{instance="$node",job="$job"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Time Synchronized Status | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_timex_sync_status{instance="$node",job="$job"} \|\|\| node_timex_frequency_... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Time Misc | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_timex_tick_seconds{instance="$node",job="$job"} \|\|\| node_timex_tai_offse... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Processes Status | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_procs_blocked{instance="$node",job="$job"} \|\|\| node_procs_running{instan... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Processes State | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_processes_state{instance="$node",job="$job"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Processes  Forks | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_forks_total{instance="$node",job="$job"}[$__rate_interval]) | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Processes Memory | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(process_virtual_memory_bytes{instance="$node",job="$job"}[$__rate_interval... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| PIDs Number and Limit | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_processes_pids{instance="$node",job="$job"} \|\|\| node_processes_max_proce... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Process schedule stats Running / Waiting | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_schedstat_running_seconds_total{instance="$node",job="$job"}[$__rate_... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Threads Number and Limit | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_processes_threads{instance="$node",job="$job"} \|\|\| node_processes_max_th... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Context Switches / Interrupts | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_context_switches_total{instance="$node",job="$job"}[$__rate_interval]... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| System Load | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_load1{instance="$node",job="$job"} \|\|\| node_load5{instance="$node",job="... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| CPU Frequency Scaling | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_cpu_scaling_frequency_hertz{instance="$node",job="$job"} \|\|\| avg(node_cp... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Pressure Stall Information | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | rate(node_pressure_cpu_waiting_seconds_total{instance="$node",job="$job"}[$__rat... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Interrupts Detail | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_interrupts_total{instance="$node",job="$job"}[$__rate_interval]) | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Schedule timeslices executed by each cpu | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_schedstat_timeslices_total{instance="$node",job="$job"}[$__rate_inter... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Entropy | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_entropy_available_bits{instance="$node",job="$job"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| CPU time spent in user and system contexts | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(process_cpu_seconds_total{instance="$node",job="$job"}[$__rate_interval]) | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| File Descriptors | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | process_max_fds{instance="$node",job="$job"} \|\|\| process_open_fds{instance="$... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Hardware temperature monitor | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_hwmon_temp_celsius{instance="$node",job="$job"} * on(chip) group_left(chip_... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Throttle cooling device | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_cooling_device_cur_state{instance="$node",job="$job"} \|\|\| node_cooling_d... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Power supply | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_power_supply_online{instance="$node",job="$job"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Systemd Sockets | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_systemd_socket_accepted_connections_total{instance="$node",job="$job"... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Systemd Units State | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_systemd_units{instance="$node",job="$job",state="activating"} \|\|\| node_s... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Disk IOps Completed | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_disk_reads_completed_total{instance="$node",job="$job"}[$__rate_inter... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Disk R/W Data | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_disk_read_bytes_total{instance="$node",job="$job"}[$__rate_interval])... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Disk Average Wait Time | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_disk_read_time_seconds_total{instance="$node",job="$job"}[$__rate_int... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Average Queue Size | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_disk_io_time_weighted_seconds_total{instance="$node",job="$job"}[$__r... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Disk R/W Merged | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_disk_reads_merged_total{instance="$node",job="$job"}[$__rate_interval... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Time Spent Doing I/Os | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_disk_io_time_seconds_total{instance="$node",job="$job"}[$__rate_inter... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Instantaneous Queue Size | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_disk_io_now{instance="$node",job="$job"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Disk IOps Discards completed / merged | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_disk_discards_completed_total{instance="$node",job="$job"}[$__rate_in... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Filesystem space available | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_filesystem_avail_bytes{instance="$node",job="$job",device!~'rootfs'} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| File Nodes Free | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_filesystem_files_free{instance="$node",job="$job",device!~'rootfs'} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| File Descriptor | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_filefd_maximum{instance="$node",job="$job"} \|\|\| node_filefd_allocated{in... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| File Nodes Size | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_filesystem_files{instance="$node",job="$job",device!~'rootfs'} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Filesystem in ReadOnly / Error | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_filesystem_readonly{instance="$node",job="$job",device!~'rootfs'} \|\|\| no... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Network Traffic by Packets | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_network_receive_packets_total{instance="$node",job="$job"}[$__rate_in... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Network Traffic Errors | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_network_receive_errs_total{instance="$node",job="$job"}[$__rate_inter... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Network Traffic Drop | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_network_receive_drop_total{instance="$node",job="$job"}[$__rate_inter... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Network Traffic Compressed | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_network_receive_compressed_total{instance="$node",job="$job"}[$__rate... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Network Traffic Multicast | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_network_receive_multicast_total{instance="$node",job="$job"}[$__rate_... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Network Traffic Fifo | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_network_receive_fifo_total{instance="$node",job="$job"}[$__rate_inter... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Network Traffic Frame | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_network_receive_frame_total{instance="$node",job="$job"}[$__rate_inte... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Network Traffic Carrier | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_network_transmit_carrier_total{instance="$node",job="$job"}[$__rate_i... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Network Traffic Colls | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_network_transmit_colls_total{instance="$node",job="$job"}[$__rate_int... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| NF Conntrack | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_nf_conntrack_entries{instance="$node",job="$job"} \|\|\| node_nf_conntrack_... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| ARP Entries | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_arp_entries{instance="$node",job="$job"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| MTU | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_network_mtu_bytes{instance="$node",job="$job"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Speed | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_network_speed_bytes{instance="$node",job="$job"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Queue Length | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_network_transmit_queue_length{instance="$node",job="$job"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Softnet Packets | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_softnet_processed_total{instance="$node",job="$job"}[$__rate_interval... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Softnet Out of Quota | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_softnet_times_squeezed_total{instance="$node",job="$job"}[$__rate_int... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Network Operational Status | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_network_up{operstate="up",instance="$node",job="$job"} \|\|\| node_network_... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Sockstat TCP | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_sockstat_TCP_alloc{instance="$node",job="$job"} \|\|\| node_sockstat_TCP_in... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Sockstat UDP | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_sockstat_UDPLITE_inuse{instance="$node",job="$job"} \|\|\| node_sockstat_UD... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Sockstat FRAG / RAW | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_sockstat_FRAG_inuse{instance="$node",job="$job"} \|\|\| node_sockstat_RAW_i... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Sockstat Memory Size | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_sockstat_TCP_mem_bytes{instance="$node",job="$job"} \|\|\| node_sockstat_UD... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Sockstat Used | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_sockstat_sockets_used{instance="$node",job="$job"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Netstat IP In / Out Octets | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_IpExt_InOctets{instance="$node",job="$job"}[$__rate_interval]... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Netstat IP Forwarding | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_Ip_Forwarding{instance="$node",job="$job"}[$__rate_interval]) | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| ICMP In / Out | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_Icmp_InMsgs{instance="$node",job="$job"}[$__rate_interval]) \... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| ICMP Errors | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_Icmp_InErrors{instance="$node",job="$job"}[$__rate_interval]) | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| UDP In / Out | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_Udp_InDatagrams{instance="$node",job="$job"}[$__rate_interval... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| UDP Errors | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_Udp_InErrors{instance="$node",job="$job"}[$__rate_interval]) ... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| TCP In / Out | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_Tcp_InSegs{instance="$node",job="$job"}[$__rate_interval]) \|... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| TCP Errors | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_TcpExt_ListenOverflows{instance="$node",job="$job"}[$__rate_i... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| TCP Connections | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_netstat_Tcp_CurrEstab{instance="$node",job="$job"} \|\|\| node_netstat_Tcp_... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| TCP SynCookie | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_TcpExt_SyncookiesFailed{instance="$node",job="$job"}[$__rate_... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| TCP Direct Transition | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_Tcp_ActiveOpens{instance="$node",job="$job"}[$__rate_interval... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| TCP Stat | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_tcp_connection_states{state="established",instance="$node",job="$job"} \|\|... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Node Exporter Scrape Time | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_scrape_collector_duration_seconds{instance="$node",job="$job"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Node Exporter Scrape | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_scrape_collector_success{instance="$node",job="$job"} \|\|\| node_textfile_... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |

<details>
<summary>Detailed traces (116 panels)</summary>

#### Pressure

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (bargauge):**

```
irate(node_pressure_cpu_waiting_seconds_total{instance="$node",job="$job"}[$__rate_interval]) ||| irate(node_pressure_memory_waiting_seconds_total{instance="$node",job="$job"}[$__rate_interval]) ||| irate(node_pressure_io_waiting_seconds_total{instance="$node",job="$job"}[$__rate_interval])
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel` → approximated bargauge panel

**Translated (bar):**

```
TS metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE node_pressure_cpu_waiting_seconds_total IS NOT NULL OR node_pressure_memory_waiting_seconds_total IS NOT NULL OR node_pressure_io_waiting_seconds_total IS NOT NULL
| STATS node_pressure_cpu_waiting_seconds_total_CPU_some_instance_jo = IRATE(node_pressure_cpu_waiting_seconds_total, 5m), node_pressure_memory_waiting_seconds_total_Memory_some_insta = IRATE(node_pressure_memory_waiting_seconds_total, 5m), node_pressure_io_waiting_seconds_total_I_O_some_instance_job = IRATE(node_pressure_io_waiting_seconds_total, 5m) BY time_bucket = TBUCKET(5 minute)
| EVAL CPU = node_pressure_cpu_waiting_seconds_total_CPU_some_instance_jo
| EVAL Mem = node_pressure_memory_waiting_seconds_total_Memory_some_insta
| EVAL I_O = node_pressure_io_waiting_seconds_total_I_O_some_instance_job
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), CPU = LAST(CPU, time_bucket), Mem = LAST(Mem, time_bucket), I_O = LAST(I_O, time_bucket)
| KEEP time_bucket, CPU, Mem, I_O
| EVAL __labels = MV_APPEND(MV_APPEND("CPU", "Mem"), "I/O"), __values = MV_APPEND(MV_APPEND(TO_STRING(CPU), TO_STRING(Mem)), TO_STRING(I_O))
| EVAL __pairs = MV_ZIP(__labels, __values, "~")
| MV_EXPAND __pairs
| EVAL label = MV_FIRST(SPLIT(__pairs, "~")), value = TO_DOUBLE(MV_LAST(SPLIT(__pairs, "~")))
| KEEP label, value
| SORT label ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `node_pressure_cpu_waiting_seconds_total`
- Range func: `irate`
- Range window: `5m`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `CPU`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `bar`
- Layout: x=0, y=0, w=6, h=6
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 3
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Variable-driven label filters applied via Kibana dashboard controls; Merged compatible panel targets into a single ES|QL query; Approximated bargauge as bar chart

**Semantic losses:** Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### CPU Busy

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (gauge):**

```
100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle", instance="$node"}[$__rate_interval])))
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
TS metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE mode == "idle"
| WHERE node_cpu_seconds_total IS NOT NULL
| STATS node_cpu_seconds_total_mode_idle_instance_rate_avg = AVG(RATE(node_cpu_seconds_total, 5m)) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (100 * (1 - node_cpu_seconds_total_mode_idle_instance_rate_avg))
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = LAST(computed_value, time_bucket)
| KEEP time_bucket, computed_value
| EVAL _gauge_min = 0, _gauge_max = 100, _gauge_goal = 85
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `*`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `gauge`
- Layout: x=6, y=0, w=6, h=6
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Variable-driven label filters applied via Kibana dashboard controls

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### Sys Load

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (gauge):**

```
scalar(node_load1{instance="$node",job="$job"}) * 100 / count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu))
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE node_load1 IS NOT NULL OR node_cpu_seconds_total IS NOT NULL
| STATS node_load1_instance_job_label_job = AVG(node_load1), node_cpu_seconds_total_instance_job_label_job_count = COUNT_DISTINCT(cpu) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| EVAL computed_value = ((node_load1_instance_job_label_job * 100) / node_cpu_seconds_total_instance_job_label_job_count)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = LAST(computed_value, time_bucket)
| KEEP time_bucket, computed_value
| EVAL _gauge_min = 0, _gauge_max = 100, _gauge_goal = 85
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `/`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Approximated scalar() as a direct metric value, Approximated nested count(count()) as COUNT_DISTINCT(cpu)

**Visual IR:**

- Kibana type: `gauge`
- Layout: x=12, y=0, w=6, h=6
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Variable-driven label filters applied via Kibana dashboard controls; Approximated scalar() as a direct metric value; Approximated nested count(count()) as COUNT_DISTINCT(cpu)

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Approximated scalar() as a direct metric value; Approximated nested count(count()) as COUNT_DISTINCT(cpu)

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### RAM Used

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (gauge):**

```
(1 - (node_memory_MemAvailable_bytes{instance="$node", job="$job"} / node_memory_MemTotal_bytes{instance="$node", job="$job"})) * 100
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE node_memory_MemAvailable_bytes IS NOT NULL OR node_memory_MemTotal_bytes IS NOT NULL
| STATS node_memory_MemAvailable_bytes_instance_job_label_job = AVG(node_memory_MemAvailable_bytes), node_memory_MemTotal_bytes_instance_job_label_job = AVG(node_memory_MemTotal_bytes) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| EVAL computed_value = ((1 - (node_memory_MemAvailable_bytes_instance_job_label_job / node_memory_MemTotal_bytes_instance_job_label_job)) * 100)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = LAST(computed_value, time_bucket)
| KEEP time_bucket, computed_value
| EVAL _gauge_min = 0, _gauge_max = 100, _gauge_goal = 80
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `*`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `gauge`
- Layout: x=18, y=0, w=6, h=6
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Variable-driven label filters applied via Kibana dashboard controls; No explicit aggregation; using AVG (correct for gauge metrics)

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### SWAP Used

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (gauge):**

```
((node_memory_SwapTotal_bytes{instance="$node",job="$job"} - node_memory_SwapFree_bytes{instance="$node",job="$job"}) / (node_memory_SwapTotal_bytes{instance="$node",job="$job"})) * 100
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE node_memory_SwapTotal_bytes IS NOT NULL OR node_memory_SwapFree_bytes IS NOT NULL
| STATS node_memory_SwapTotal_bytes_instance_job_label_job = AVG(node_memory_SwapTotal_bytes), node_memory_SwapFree_bytes_instance_job_label_job = AVG(node_memory_SwapFree_bytes) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| EVAL computed_value = (((node_memory_SwapTotal_bytes_instance_job_label_job - node_memory_SwapFree_bytes_instance_job_label_job) / node_memory_SwapTotal_bytes_instance_job_label_job) * 100)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = LAST(computed_value, time_bucket)
| KEEP time_bucket, computed_value
| EVAL _gauge_min = 0, _gauge_max = 100, _gauge_goal = 10
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `*`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `gauge`
- Layout: x=24, y=0, w=6, h=6
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Variable-driven label filters applied via Kibana dashboard controls; No explicit aggregation; using AVG (correct for gauge metrics)

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### Root FS Used

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (gauge):**

```
100 - ((node_filesystem_avail_bytes{instance="$node",job="$job",mountpoint="/",fstype!="rootfs"} * 100) / node_filesystem_size_bytes{instance="$node",job="$job",mountpoint="/",fstype!="rootfs"})
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE mountpoint == "/"
| WHERE fstype != "rootfs"
| WHERE node_filesystem_avail_bytes IS NOT NULL OR node_filesystem_size_bytes IS NOT NULL
| STATS node_filesystem_avail_bytes_instance_job_label_job = AVG(node_filesystem_avail_bytes), node_filesystem_size_bytes_instance_job_label_job = AVG(node_filesystem_size_bytes) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| EVAL computed_value = (100 - ((node_filesystem_avail_bytes_instance_job_label_job * 100) / node_filesystem_size_bytes_instance_job_label_job))
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = LAST(computed_value, time_bucket)
| KEEP time_bucket, computed_value
| EVAL _gauge_min = 0, _gauge_max = 100, _gauge_goal = 80
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `-`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `gauge`
- Layout: x=30, y=0, w=6, h=6
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Variable-driven label filters applied via Kibana dashboard controls; No explicit aggregation; using AVG (correct for gauge metrics)

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### CPU Cores

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu))
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=nested_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family nested_agg bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family` → translated nested count(count()) expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE node_cpu_seconds_total IS NOT NULL
| STATS node_cpu_seconds_total_count = COUNT_DISTINCT(cpu)
```

**Query IR:**

- Family: `nested_agg`
- Metric: `node_cpu_seconds_total_count`
- Outer agg: `count`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `node_cpu_seconds_total_count`
- Semantic losses: Dropped variable-driven label filters during migration, Approximated nested count(count()) as COUNT_DISTINCT(cpu)

**Visual IR:**

- Kibana type: `metric`
- Layout: x=36, y=0, w=4, h=3
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Variable-driven label filters applied via Kibana dashboard controls; Approximated nested count(count()) as COUNT_DISTINCT(cpu)

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated nested count(count()) as COUNT_DISTINCT(cpu)

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### Uptime

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
node_time_seconds{instance="$node",job="$job"} - node_boot_time_seconds{instance="$node",job="$job"}
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE node_time_seconds IS NOT NULL OR node_boot_time_seconds IS NOT NULL
| STATS node_time_seconds_instance_job_label_job = AVG(node_time_seconds), node_boot_time_seconds_instance_job_label_job = AVG(node_boot_time_seconds) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| EVAL computed_value = (node_time_seconds_instance_job_label_job - node_boot_time_seconds_instance_job_label_job)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = LAST(computed_value, time_bucket)
| KEEP time_bucket, computed_value
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `-`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=40, y=0, w=8, h=3
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Variable-driven label filters applied via Kibana dashboard controls; No explicit aggregation; using AVG (correct for gauge metrics)

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### RootFS Total

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
node_filesystem_size_bytes{instance="$node",job="$job",mountpoint="/",fstype!="rootfs"}
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE mountpoint == "/"
| WHERE fstype != "rootfs"
| WHERE node_filesystem_size_bytes IS NOT NULL
| STATS node_filesystem_size_bytes = AVG(node_filesystem_size_bytes) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), node_filesystem_size_bytes = LAST(node_filesystem_size_bytes, time_bucket)
| KEEP time_bucket, node_filesystem_size_bytes
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `node_filesystem_size_bytes`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `node_filesystem_size_bytes`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=36, y=6, w=4, h=3
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Variable-driven label filters applied via Kibana dashboard controls; No explicit aggregation; using AVG (correct for gauge metrics)

**Semantic losses:** Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### RAM Total

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
node_memory_MemTotal_bytes{instance="$node",job="$job"}
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE node_memory_MemTotal_bytes IS NOT NULL
| STATS node_memory_MemTotal_bytes = AVG(node_memory_MemTotal_bytes) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), node_memory_MemTotal_bytes = LAST(node_memory_MemTotal_bytes, time_bucket)
| KEEP time_bucket, node_memory_MemTotal_bytes
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `node_memory_MemTotal_bytes`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `node_memory_MemTotal_bytes`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=40, y=6, w=4, h=3
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Variable-driven label filters applied via Kibana dashboard controls; No explicit aggregation; using AVG (correct for gauge metrics)

**Semantic losses:** Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### SWAP Total

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
node_memory_SwapTotal_bytes{instance="$node",job="$job"}
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE node_memory_SwapTotal_bytes IS NOT NULL
| STATS node_memory_SwapTotal_bytes = AVG(node_memory_SwapTotal_bytes) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), node_memory_SwapTotal_bytes = LAST(node_memory_SwapTotal_bytes, time_bucket)
| KEEP time_bucket, node_memory_SwapTotal_bytes
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `node_memory_SwapTotal_bytes`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `node_memory_SwapTotal_bytes`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=44, y=6, w=4, h=3
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Variable-driven label filters applied via Kibana dashboard controls; No explicit aggregation; using AVG (correct for gauge metrics)

**Semantic losses:** Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### CPU Basic

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (timeseries):**

```
sum(irate(node_cpu_seconds_total{instance="$node",job="$job", mode="system"}[$__rate_interval])) / scalar(count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu)))
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → binary expression requires unsafe measure merge; marked not_feasible
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`

**Query IR:**

- Family: `binary_expr`
- Binary op: `/`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=0, y=0, w=24, h=11
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 6
- field_overrides: 7
- has_description: True

**Warnings:** Grafana panel has 7 field override(s); verify visual mappings manually; Grafana panel description is not carried into Kibana YAML automatically; PromQL arithmetic with divergent filters/groupings cannot be translated safely yet

**Notes:** Grafana panel has 7 field override(s); verify visual mappings manually; Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** EXPECTED_LIMITATION

#### Memory Basic

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
node_memory_MemTotal_bytes{instance="$node",job="$job"} ||| node_memory_MemTotal_bytes{instance="$node",job="$job"} - node_memory_MemFree_bytes{instance="$node",job="$job"} - (node_memory_Cached_bytes{instance="$node",job="$job"} + node_memory_Buffers_bytes{instance="$node",job="$job"} + node_memory_SReclaimable_bytes{instance="$node",job="$job"}) ||| node_memory_Cached_bytes{instance="$node",job="$job"} + node_memory_Buffers_bytes{instance="$node",job="$job"} + node_memory_SReclaimable_bytes{instance="$node",job="$job"} ||| node_memory_MemFree_bytes{instance="$node",job="$job"} ||| (node_memory_SwapTotal_bytes{instance="$node",job="$job"} - node_memory_SwapFree_bytes{instance="$node",job="$job"})
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_memory_MemFree_bytes IS NOT NULL OR node_memory_Cached_bytes IS NOT NULL OR node_memory_Buffers_bytes IS NOT NULL OR node_memory_SReclaimable_bytes IS NOT NULL OR node_memory_SwapTotal_bytes IS NOT NULL OR node_memory_SwapFree_bytes IS NOT NULL
| STATS node_memory_MemTotal_bytes_A_instance_job_label_job = AVG(node_memory_MemTotal_bytes), node_memory_MemTotal_bytes_B_instance_job_label_job = AVG(node_memory_MemTotal_bytes), node_memory_MemFree_bytes_B_instance_job_label_job = AVG(node_memory_MemFree_bytes), node_memory_Cached_bytes_B_instance_job_label_job = AVG(node_memory_Cached_bytes), node_memory_Buffers_bytes_B_instance_job_label_job = AVG(node_memory_Buffers_bytes), node_memory_SReclaimable_bytes_B_instance_job_label_job = AVG(node_memory_SReclaimable_bytes), node_memory_Cached_bytes_C_instance_job_label_job = AVG(node_memory_Cached_bytes), node_memory_Buffers_bytes_C_instance_job_label_job = AVG(node_memory_Buffers_bytes), node_memory_SReclaimable_bytes_C_instance_job_label_job = AVG(node_memory_SReclaimable_bytes), node_memory_MemFree_bytes_D_instance_job_label_job = AVG(node_memory_MemFree_bytes), node_memory_SwapTotal_bytes_E_instance_job_label_job = AVG(node_memory_SwapTotal_bytes), node_memory_SwapFree_bytes_E_instance_job_label_job = AVG(node_memory_SwapFree_bytes) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| EVAL RAM_Total = node_memory_MemTotal_bytes_A_instance_job_label_job
| EVAL RAM_Used = ((node_memory_MemTotal_bytes_B_instance_job_label_job - node_memory_MemFree_bytes_B_instance_job_label_job) - ((node_memory_Cached_bytes_B_instance_job_label_job + node_memory_Buffers_bytes_B_instance_job_label_job) + node_memory_SReclaimable_bytes_B_instance_job_label_job))
| EVAL RAM_Cache___Buffer = ((node_memory_Cached_bytes_C_instance_job_label_job + node_memory_Buffers_bytes_C_instance_job_label_job) + node_memory_SReclaimable_bytes_C_instance_job_label_job)
| EVAL RAM_Free = node_memory_MemFree_bytes_D_instance_job_label_job
| EVAL SWAP_Used = (node_memory_SwapTotal_bytes_E_instance_job_label_job - node_memory_SwapFree_bytes_E_instance_job_label_job)
| KEEP time_bucket, RAM_Total, RAM_Used, RAM_Cache___Buffer, RAM_Free, SWAP_Used
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `node_memory_MemTotal_bytes`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `RAM_Total`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `area`
- Layout: x=24, y=0, w=24, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 5
- field_overrides: 23
- has_description: True

**Warnings:** Grafana panel has 23 field override(s); verify visual mappings manually; Grafana panel description is not carried into Kibana YAML automatically; Variable-driven label filters applied via Kibana dashboard controls; No explicit aggregation; using AVG (correct for gauge metrics); Merged compatible panel targets into a single ES|QL query

**Semantic losses:** Dropped variable-driven label filters during migration

**Notes:** Grafana panel has 23 field override(s); verify visual mappings manually; Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### Network Traffic Basic

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
irate(node_network_receive_bytes_total{instance="$node",job="$job"}[$__rate_interval])*8 ||| irate(node_network_transmit_bytes_total{instance="$node",job="$job"}[$__rate_interval])*8
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE node_network_receive_bytes_total IS NOT NULL OR node_network_transmit_bytes_total IS NOT NULL
| STATS node_network_receive_bytes_total_A_instance_job_label_job_ir = AVG(IRATE(node_network_receive_bytes_total, 5m)), node_network_transmit_bytes_total_B_instance_job_label_job_i = AVG(IRATE(node_network_transmit_bytes_total, 5m)) BY time_bucket = TBUCKET(5 minute), device
| EVAL recv___device = (node_network_receive_bytes_total_A_instance_job_label_job_ir * 8)
| EVAL trans___device = (node_network_transmit_bytes_total_B_instance_job_label_job_i * 8)
| KEEP time_bucket, device, recv___device, trans___device
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `*`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `recv___device`
- Output groups: `time_bucket, device`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=11, w=24, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2
- field_overrides: 24
- has_description: True

**Warnings:** Grafana panel has 24 field override(s); verify visual mappings manually; Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Variable-driven label filters applied via Kibana dashboard controls; Wrapped irate in AVG() to support grouped TS queries

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Notes:** Grafana panel has 24 field override(s); verify visual mappings manually; Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### Disk Space Used Basic

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
100 - ((node_filesystem_avail_bytes{instance="$node",job="$job",device!~'rootfs'} * 100) / node_filesystem_size_bytes{instance="$node",job="$job",device!~'rootfs'})
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE NOT (device RLIKE "rootfs")
| WHERE node_filesystem_avail_bytes IS NOT NULL OR node_filesystem_size_bytes IS NOT NULL
| STATS node_filesystem_avail_bytes_instance_job_label_job = AVG(node_filesystem_avail_bytes), node_filesystem_size_bytes_instance_job_label_job = AVG(node_filesystem_size_bytes) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), mountpoint
| EVAL computed_value = (100 - ((node_filesystem_avail_bytes_instance_job_label_job * 100) / node_filesystem_size_bytes_instance_job_label_job))
| KEEP time_bucket, mountpoint, computed_value
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `-`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Output groups: `time_bucket, mountpoint`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=11, w=24, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Variable-driven label filters applied via Kibana dashboard controls; No explicit aggregation; using AVG (correct for gauge metrics)

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Controls / Variables (2)</summary>

- `Job` (type: `options`)
- `Host` (type: `options`)

</details>

---

### Grafana: Prometheus 2.0 (by FUSAKLA)

**File:** `prometheus-all.json` — **Panels:** 44

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Untitled | `text` → `markdown` | migrated | **EXPECTED_LIMITATION** | — | — |
| Uptime | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | time() - process_start_time_seconds{instance="$instance"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Total count of time series | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | prometheus_tsdb_head_series{instance="$instance"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Version | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | prometheus_build_info{instance="$instance"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Actual head block length | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | prometheus_tsdb_head_max_time{instance="$instance"} - prometheus_tsdb_head_min_t... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Untitled | `text` → `markdown` | migrated | **EXPECTED_LIMITATION** | — | — |
| 2 | `singlestat` → `metric` | migrated | **CORRECT** | 2 | ROW constant_value = 2.0 |
| Query elapsed time | `graph` → `area` | migrated_with_warnings | **MINOR_ISSUE** | max(prometheus_engine_query_duration_seconds{instance="$instance"}) by (instance... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Head series created/deleted | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(increase(prometheus_tsdb_head_series_created_total{instance="$instance"}[$ag... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Prometheus errors | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(increase(prometheus_target_scrapes_exceeded_sample_limit_total{instance="$in... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Scrape delay (counts with 1m scrape interval) | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | prometheus_target_interval_length_seconds{instance="$instance",quantile="0.99"} ... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Rule evaulation duration | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(prometheus_evaluator_duration_seconds{instance="$instance"}) by (instance, q... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Request count | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(increase(http_requests_total{instance="$instance"}[$aggregation_interval])) ... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Request duration per handler | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | max(sum(http_request_duration_microseconds{instance="$instance"}) by (instance, ... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Request size by handler | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(increase(http_request_size_bytes{instance="$instance", quantile="0.99"}[$agg... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Cont of concurent queries | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(prometheus_engine_queries{instance="$instance"}) by (instance, handler) \|\|... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Alert queue size | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(prometheus_notifications_queue_capacity{instance="$instance"})by (instance) ... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Count of discovered alertmanagers | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(prometheus_notifications_alertmanagers_discovered{instance="$instance"}) by ... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Alerting errors | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(increase(prometheus_notifications_dropped_total{instance="$instance"}[$aggre... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Consul SD sync count | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | increase(prometheus_target_sync_length_seconds_count{scrape_job="consul", instan... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Marathon SD sync count | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | increase(prometheus_target_sync_length_seconds_count{scrape_job="marathon", inst... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Kubernetes SD sync count | `graph` → `line` | migrated | **CORRECT** | increase(prometheus_target_sync_length_seconds_count{scrape_job="kubernetes"}[$a... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Service discovery errors | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(increase(prometheus_target_scrapes_exceeded_sample_limit_total{instance="$in... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Reloaded block from disk | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(increase(prometheus_tsdb_reloads_total{instance="$instance"}[30m])) by (inst... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Loaded data blocks | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(prometheus_tsdb_blocks_loaded{instance="$instance"}) by (instance) | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Time series total count | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | prometheus_tsdb_head_series{instance="$instance"} | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Samples Appended per second | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(prometheus_tsdb_head_samples_appended_total{instance="$instance"}[$aggr... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Head chunks count | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(prometheus_tsdb_head_chunks{instance="$instance"}) by (instance) | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Length of head block | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | max(prometheus_tsdb_head_max_time{instance="$instance"}) by (instance) - min(pro... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Head Chunks Created/Deleted per second | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(prometheus_tsdb_head_chunks_created_total{instance="$instance"}[$aggreg... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Compaction duration | `graph` → `line` | migrated | **CORRECT** | sum(increase(prometheus_tsdb_compaction_duration_sum{instance="$instance"}[30m])... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Go Garbage collection duration | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(prometheus_tsdb_head_gc_duration_seconds{instance="$instance"}) by (instance... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| WAL truncate duration seconds | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(prometheus_tsdb_wal_truncate_duration_seconds{instance="$instance"}) by (ins... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| WAL fsync duration seconds | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(tsdb_wal_fsync_duration_seconds{instance="$instance"}) by (instance, quantil... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Memory | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(process_resident_memory_bytes{instance="$instance"}) by (instance) \|\|\| su... | FROM metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_ten... |
| Allocations per second | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | rate(go_memstats_alloc_bytes_total{instance="$instance"}[$aggregation_interval]) | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| CPU per second | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(process_cpu_seconds_total{instance="$instance"}[$aggregation_interval])... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |
| Heapster rows | `text` → `markdown` | migrated | **EXPECTED_LIMITATION** | — | — |
| CPU usage/s | `graph` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Memory usage | `graph` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Network rx[IN] / tx[OUT] in bytes/s | `graph` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Disk usage | `graph` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Number of free INODES | `graph` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Net errors | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(increase(net_conntrack_dialer_conn_failed_total{instance="$instance"}[$aggre... | TS metrics-prometheus-* \| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend ... |

<details>
<summary>Detailed traces (36 panels)</summary>

#### Uptime

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
time() - process_start_time_seconds{instance="$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=uptime backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family uptime bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family` → translated uptime expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE process_start_time_seconds IS NOT NULL
| STATS start_time_ms = MAX(process_start_time_seconds * 1000)
| EVAL process_start_time_seconds_uptime_seconds = DATE_DIFF("seconds", TO_DATETIME(start_time_ms), NOW())
| KEEP process_start_time_seconds_uptime_seconds
```

**Query IR:**

- Family: `uptime`
- Metric: `process_start_time_seconds`
- Binary op: `-`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `process_start_time_seconds_uptime_seconds`
- Semantic losses: Dropped variable-driven label filters during migration, Approximated time() - metric as uptime from metric timestamp

**Visual IR:**

- Kibana type: `metric`
- Layout: x=0, y=0, w=8, h=8
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Variable-driven label filters applied via Kibana dashboard controls; Approximated time() - metric as uptime from metric timestamp

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated time() - metric as uptime from metric timestamp

**Verdict:** MINOR_ISSUE

#### Total count of time series

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
prometheus_tsdb_head_series{instance="$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE prometheus_tsdb_head_series IS NOT NULL
| STATS prometheus_tsdb_head_series = AVG(prometheus_tsdb_head_series) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), prometheus_tsdb_head_series = LAST(prometheus_tsdb_head_series, time_bucket)
| KEEP time_bucket, prometheus_tsdb_head_series
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `prometheus_tsdb_head_series`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `prometheus_tsdb_head_series`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=8, y=0, w=16, h=8
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Variable-driven label filters applied via Kibana dashboard controls; No explicit aggregation; using AVG (correct for gauge metrics)

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Version

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
prometheus_build_info{instance="$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE prometheus_build_info IS NOT NULL
| STATS prometheus_build_info = AVG(prometheus_build_info) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), prometheus_build_info = LAST(prometheus_build_info, time_bucket)
| KEEP time_bucket, prometheus_build_info
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `prometheus_build_info`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `prometheus_build_info`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=24, y=0, w=8, h=8
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Variable-driven label filters applied via Kibana dashboard controls; No explicit aggregation; using AVG (correct for gauge metrics)

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Actual head block length

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
prometheus_tsdb_head_max_time{instance="$instance"} - prometheus_tsdb_head_min_time{instance="$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE prometheus_tsdb_head_max_time IS NOT NULL OR prometheus_tsdb_head_min_time IS NOT NULL
| STATS prometheus_tsdb_head_max_time_instance = AVG(prometheus_tsdb_head_max_time), prometheus_tsdb_head_min_time_instance = AVG(prometheus_tsdb_head_min_time) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| EVAL computed_value = (prometheus_tsdb_head_max_time_instance - prometheus_tsdb_head_min_time_instance)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = LAST(computed_value, time_bucket)
| KEEP time_bucket, computed_value
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `-`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=32, y=0, w=8, h=8
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Variable-driven label filters applied via Kibana dashboard controls; No explicit aggregation; using AVG (correct for gauge metrics)

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### 2

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
2
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=scalar backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family scalar bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family` → translated scalar constant
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
ROW constant_value = 2.0
```

**Query IR:**

- Family: `scalar`
- Metric: `constant_value`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `constant_value`

**Visual IR:**

- Kibana type: `metric`
- Layout: x=44, y=0, w=4, h=8
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Verdict:** CORRECT

#### Query elapsed time

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
max(prometheus_engine_query_duration_seconds{instance="$instance"}) by (instance, slice)
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE prometheus_engine_query_duration_seconds IS NOT NULL
| STATS prometheus_engine_query_duration_seconds = MAX(prometheus_engine_query_duration_seconds) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), slice, instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `prometheus_engine_query_duration_seconds`
- Outer agg: `max`
- Group labels: `instance, slice`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `prometheus_engine_query_duration_seconds`
- Output groups: `time_bucket, slice, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `area`
- Layout: x=0, y=0, w=16, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Variable-driven label filters applied via Kibana dashboard controls

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Head series created/deleted

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(increase(prometheus_tsdb_head_series_created_total{instance="$instance"}[$aggregation_interval])) by (instance) ||| sum(increase(prometheus_tsdb_head_series_removed_total{instance="$instance"}[$aggregation_interval])) by (instance) * -1
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE prometheus_tsdb_head_series_created_total IS NOT NULL OR prometheus_tsdb_head_series_removed_total IS NOT NULL
| STATS prometheus_tsdb_head_series_created_total_A_instance_increas = SUM(INCREASE(prometheus_tsdb_head_series_created_total, 5m)), prometheus_tsdb_head_series_removed_total_B_instance_increas = SUM(INCREASE(prometheus_tsdb_head_series_removed_total, 5m)) BY time_bucket = TBUCKET(5 minute), instance
| EVAL prometheus_tsdb_head_series_removed_total_B_instance_increas_calc = prometheus_tsdb_head_series_removed_total_B_instance_increas * -1
| EVAL created_on____instance = prometheus_tsdb_head_series_created_total_A_instance_increas
| EVAL removed_on____instance = prometheus_tsdb_head_series_removed_total_B_instance_increas_calc
| KEEP time_bucket, instance, created_on____instance, removed_on____instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `prometheus_tsdb_head_series_created_total`
- Range func: `increase`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `instance`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `created_on____instance`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=16, y=0, w=16, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Variable-driven label filters applied via Kibana dashboard controls; Merged compatible panel targets into a single ES|QL query

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Prometheus errors

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(increase(prometheus_target_scrapes_exceeded_sample_limit_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0 ||| sum(increase(prometheus_target_scrapes_sample_duplicate_timestamp_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0 ||| sum(increase(prometheus_target_scrapes_sample_out_of_bounds_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0 ||| sum(increase(prometheus_target_scrapes_sample_out_of_order_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0 ||| sum(increase(prometheus_rule_evaluation_failures_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0 ||| sum(increase(prometheus_tsdb_compactions_failed_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0 ||| sum(increase(prometheus_tsdb_reloads_failures_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0 ||| sum(increase(prometheus_tsdb_head_series_not_found{instance="$instance"}[$aggregation_interval])) by (instance) > 0 ||| sum(increase(prometheus_evaluator_iterations_missed_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0 ||| sum(increase(prometheus_evaluator_iterations_skipped_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter` → applied post-aggregation filter > 0
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE prometheus_target_scrapes_exceeded_sample_limit_total IS NOT NULL OR prometheus_target_scrapes_sample_duplicate_timestamp_total IS NOT NULL OR prometheus_target_scrapes_sample_out_of_bounds_total IS NOT NULL OR prometheus_target_scrapes_sample_out_of_order_total IS NOT NULL OR prometheus_rule_evaluation_failures_total IS NOT NULL OR prometheus_tsdb_compactions_failed_total IS NOT NULL OR prometheus_tsdb_reloads_failures_total IS NOT NULL OR prometheus_tsdb_head_series_not_found IS NOT NULL OR prometheus_evaluator_iterations_missed_total IS NOT NULL OR prometheus_evaluator_iterations_skipped_total IS NOT NULL
| STATS prometheus_target_scrapes_exceeded_sample_limit_total_A_inst = SUM(INCREASE(prometheus_target_scrapes_exceeded_sample_limit_total, 5m)), prometheus_target_scrapes_sample_duplicate_timestamp_total_B = SUM(INCREASE(prometheus_target_scrapes_sample_duplicate_timestamp_total, 5m)), prometheus_target_scrapes_sample_out_of_bounds_total_C_insta = SUM(INCREASE(prometheus_target_scrapes_sample_out_of_bounds_total, 5m)), prometheus_target_scrapes_sample_out_of_order_total_D_instan = SUM(INCREASE(prometheus_target_scrapes_sample_out_of_order_total, 5m)), prometheus_rule_evaluation_failures_total_G_instance_increas = SUM(INCREASE(prometheus_rule_evaluation_failures_total, 5m)), prometheus_tsdb_compactions_failed_total_K_instance_increase = SUM(INCREASE(prometheus_tsdb_compactions_failed_total, 5m)), prometheus_tsdb_reloads_failures_total_L_instance_increase_s = SUM(INCREASE(prometheus_tsdb_reloads_failures_total, 5m)), prometheus_tsdb_head_series_not_found_N_instance_increase_su = SUM(INCREASE(prometheus_tsdb_head_series_not_found, 5m)), prometheus_evaluator_iterations_missed_total_O_instance_incr = SUM(INCREASE(prometheus_evaluator_iterations_missed_total, 5m)), prometheus_evaluator_iterations_skipped_total_P_instance_inc = SUM(INCREASE(prometheus_evaluator_iterations_skipped_total, 5m)) BY time_bucket = TBUCKET(5 minute), instance
| EVAL exceeded_sample_limit_on____instance = CASE(prometheus_target_scrapes_exceeded_sample_limit_total_A_inst > 0, prometheus_target_scrapes_exceeded_sample_limit_total_A_inst, NULL)
| EVAL duplicate_timestamp_on____instance = CASE(prometheus_target_scrapes_sample_duplicate_timestamp_total_B > 0, prometheus_target_scrapes_sample_duplicate_timestamp_total_B, NULL)
| EVAL out_of_bounds_on____instance = CASE(prometheus_target_scrapes_sample_out_of_bounds_total_C_insta > 0, prometheus_target_scrapes_sample_out_of_bounds_total_C_insta, NULL)
| EVAL out_of_order_on____instance = CASE(prometheus_target_scrapes_sample_out_of_order_total_D_instan > 0, prometheus_target_scrapes_sample_out_of_order_total_D_instan, NULL)
| EVAL rule_evaluation_failure_on____instance = CASE(prometheus_rule_evaluation_failures_total_G_instance_increas > 0, prometheus_rule_evaluation_failures_total_G_instance_increas, NULL)
| EVAL tsdb_compactions_failed_on____instance = CASE(prometheus_tsdb_compactions_failed_total_K_instance_increase > 0, prometheus_tsdb_compactions_failed_total_K_instance_increase, NULL)
| EVAL tsdb_reloads_failures_on____instance = CASE(prometheus_tsdb_reloads_failures_total_L_instance_increase_s > 0, prometheus_tsdb_reloads_failures_total_L_instance_increase_s, NULL)
| EVAL head_series_not_found_on____instance = CASE(prometheus_tsdb_head_series_not_found_N_instance_increase_su > 0, prometheus_tsdb_head_series_not_found_N_instance_increase_su, NULL)
| EVAL evaluator_iterations_missed_on____instance = CASE(prometheus_evaluator_iterations_missed_total_O_instance_incr > 0, prometheus_evaluator_iterations_missed_total_O_instance_incr, NULL)
| EVAL evaluator_iterations_skipped_on____instance = CASE(prometheus_evaluator_iterations_skipped_total_P_instance_inc > 0, prometheus_evaluator_iterations_skipped_total_P_instance_inc, NULL)
| KEEP time_bucket, instance, exceeded_sample_limit_on____instance, duplicate_timestamp_on____instance, out_of_bounds_on____instance, out_of_order_on____instance, rule_evaluation_failure_on____instance, tsdb_compactions_failed_on____instance, tsdb_reloads_failures_on____instance, head_series_not_found_on____instance, evaluator_iterations_missed_on____instance, evaluator_iterations_skipped_on____instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `prometheus_target_scrapes_exceeded_sample_limit_total`
- Range func: `increase`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `instance`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `exceeded_sample_limit_on____instance`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=32, y=0, w=16, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 10

**Warnings:** Variable-driven label filters applied via Kibana dashboard controls; Merged compatible panel targets into a single ES|QL query

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Scrape delay (counts with 1m scrape interval)

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
prometheus_target_interval_length_seconds{instance="$instance",quantile="0.99"} - $scrape_interval
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE prometheus_target_interval_length_seconds IS NOT NULL OR label_scrape_interval IS NOT NULL
| STATS prometheus_target_interval_length_seconds_instance_quantile = AVG(CASE((quantile == "0.99"), prometheus_target_interval_length_seconds, NULL)), label_scrape_interval = AVG(label_scrape_interval) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), instance
| EVAL computed_value = (prometheus_target_interval_length_seconds_instance_quantile - label_scrape_interval)
| KEEP time_bucket, instance, computed_value
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `-`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Output groups: `time_bucket, instance`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Variable-driven label filters applied via Kibana dashboard controls; No explicit aggregation; using AVG (correct for gauge metrics)

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Rule evaulation duration

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(prometheus_evaluator_duration_seconds{instance="$instance"}) by (instance, quantile)
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE prometheus_evaluator_duration_seconds IS NOT NULL
| STATS prometheus_evaluator_duration_seconds = SUM(prometheus_evaluator_duration_seconds) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), instance, quantile
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `prometheus_evaluator_duration_seconds`
- Outer agg: `sum`
- Group labels: `instance, quantile`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `prometheus_evaluator_duration_seconds`
- Output groups: `time_bucket, instance, quantile`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=0, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Variable-driven label filters applied via Kibana dashboard controls

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Request count

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(increase(http_requests_total{instance="$instance"}[$aggregation_interval])) by (instance, handler) > 0
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter` → applied post-aggregation filter > 0
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE http_requests_total IS NOT NULL
| STATS http_requests_total = SUM(INCREASE(http_requests_total, 5m)) BY time_bucket = TBUCKET(5 minute), handler, instance
| WHERE http_requests_total > 0
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `http_requests_total`
- Range func: `increase`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `instance, handler`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_requests_total`
- Output groups: `time_bucket, handler, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=12, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Variable-driven label filters applied via Kibana dashboard controls

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Request duration per handler

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
max(sum(http_request_duration_microseconds{instance="$instance"}) by (instance, handler, quantile)) by (instance, handler) > 0
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=nested_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family nested_agg bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family` → translated nested max expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter` → applied post-aggregation filter > 0
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| STATS inner_val = SUM(http_request_duration_microseconds) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), instance, handler, quantile
| STATS http_request_duration_microseconds_max = MAX(inner_val) BY time_bucket
| WHERE http_request_duration_microseconds_max > 0
| SORT time_bucket ASC
```

**Query IR:**

- Family: `nested_agg`
- Metric: `http_request_duration_microseconds_max`
- Outer agg: `max`
- Group labels: `instance, handler`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_request_duration_microseconds_max`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=12, y=0, w=12, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Variable-driven label filters applied via Kibana dashboard controls

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Request size by handler

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(increase(http_request_size_bytes{instance="$instance", quantile="0.99"}[$aggregation_interval])) by (instance, handler) > 0
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter` → applied post-aggregation filter > 0
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE quantile == "0.99"
| WHERE http_request_size_bytes IS NOT NULL
| STATS http_request_size_bytes = SUM(INCREASE(http_request_size_bytes, 5m)) BY time_bucket = TBUCKET(5 minute), handler, instance
| WHERE http_request_size_bytes > 0
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `http_request_size_bytes`
- Range func: `increase`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `instance, handler`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_request_size_bytes`
- Output groups: `time_bucket, handler, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=0, w=12, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Variable-driven label filters applied via Kibana dashboard controls

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Cont of concurent queries

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(prometheus_engine_queries{instance="$instance"}) by (instance, handler) ||| sum(prometheus_engine_queries_concurrent_max{instance="$instance"}) by (instance, handler)
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE prometheus_engine_queries IS NOT NULL OR prometheus_engine_queries_concurrent_max IS NOT NULL
| STATS prometheus_engine_queries_A_instance_sum = SUM(prometheus_engine_queries), prometheus_engine_queries_concurrent_max_B_instance_sum = SUM(prometheus_engine_queries_concurrent_max) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), instance, handler
| EVAL Current_count = prometheus_engine_queries_A_instance_sum
| EVAL Max_count = prometheus_engine_queries_concurrent_max_B_instance_sum
| KEEP time_bucket, instance, handler, Current_count, Max_count
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `prometheus_engine_queries`
- Outer agg: `sum`
- Group labels: `instance, handler`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `Current_count`
- Output groups: `time_bucket, instance, handler`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=36, y=0, w=12, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Variable-driven label filters applied via Kibana dashboard controls; Merged compatible panel targets into a single ES|QL query

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Alert queue size

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(prometheus_notifications_queue_capacity{instance="$instance"})by (instance) ||| sum(prometheus_notifications_queue_length{instance="$instance"})by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
FROM metrics-prometheus-*
| WHERE @timestamp >= ?_tstart AND @timestamp < ?_tend
| WHERE prometheus_notifications_queue_capacity IS NOT NULL OR prometheus_notifications_queue_length IS NOT NULL
| STATS prometheus_notifications_queue_capacity_A_instance_sum = SUM(prometheus_notifications_queue_capacity), prometheus_notifications_queue_length_B_instance_sum = SUM(prometheus_notifications_queue_length) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), instance
| EVAL Alert_queue_capacity = prometheus_notifications_queue_capacity_A_instance_sum
| EVAL Alert_queue_size_on = prometheus_notifications_queue_length_B_instance_sum
| KEEP time_bucket, instance, Alert_queue_capacity, Alert_queue_size_on
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `prometheus_notifications_queue_capacity`
- Outer agg: `sum`
- Group labels: `instance`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `Alert_queue_capacity`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=16, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Variable-driven label filters applied via Kibana dashboard controls; Merged compatible panel targets into a single ES|QL query

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Controls / Variables (1)</summary>

- `Instance` (type: `options`)

</details>

---

<!-- /GENERATED:PER_DASHBOARD_TRACES -->

---

## Appendix: Panel Status Summary

<!-- GENERATED:APPENDIX_STATS -->
From the latest trace run:

```
Total panels found:  223
  Migrated:              11 (4.9%)
  With warnings:        181 (81.2%)
  Requires manual:        5 (2.2%)
  Not feasible:           5 (2.2%)
  Skipped:               21 (9.4%)
```

Verdict breakdown:

```
  CORRECT:                    8
  MINOR_ISSUE:              179
  EXPECTED_LIMITATION:       36
```
<!-- /GENERATED:APPENDIX_STATS -->

---

## Appendix: Not-Feasible Panel Breakdown

<!-- GENERATED:NOT_FEASIBLE_BREAKDOWN -->
Every panel marked `not_feasible` in the trace run (5 total):

| Panel Title | Dashboard | Source | Reason |
|-------------|-----------|--------|--------|
| Top Endpoints | Diverse Panel Types Test | grafana | topk requires manual redesign |
| Application Logs | Diverse Panel Types Test | grafana | AST parse failed (unexpected character: '\|'), using regex fragment parser; Could not extract metric ... |
| Top Metrics by Series Count | Home - Migration Test Lab | grafana | PromQL metric-name introspection via __name__ requires manual redesign; topk requires manual redesig... |
| CPU Basic | Node Exporter Full | grafana | Grafana panel has 7 field override(s); verify visual mappings manually; Grafana panel description is... |
| CPU | Node Exporter Full | grafana | Grafana panel has 8 field override(s); verify visual mappings manually; PromQL arithmetic with diver... |

**Pattern analysis:**

- **2×** topk requires manual redesign
- **2×** PromQL arithmetic with divergent filters/groupings cannot be
- **1×** AST parse failed (unexpected character: '|'), using regex fr
- **1×** Could not extract metric name
- **1×** PromQL metric-name introspection via __name__ requires manua
- **1×** Grafana panel has 7 field override(s); verify visual mapping
- **1×** Grafana panel description is not carried into Kibana YAML au
- **1×** Grafana panel has 8 field override(s); verify visual mapping
<!-- /GENERATED:NOT_FEASIBLE_BREAKDOWN -->

---

*Last generated: 2026-04-14 08:59 UTC*
