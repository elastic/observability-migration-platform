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

1. **Native PROMQL** (the preferred path; when `--es-url` is set, target detection
   downgrades to ES|QL translation only if the `PROMQL` command is confirmed
   unsupported — an inconclusive probe keeps native and warns; `--translation-mode`
   can explicitly request native PROMQL where supported or force ES|QL) — wraps
   the original PromQL in `PROMQL index=… value=(expr)`. Used for Elastic
   Serverless; highest fidelity for `rate()`, `increase()`, grouped
   aggregations.
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
| Source | Dashboard | Panels | Migrated | Warnings | Manual | Not Feasible | Skipped | Rows |
|--------|-----------|--------|----------|----------|--------|--------------|---------|------|
| grafana | Diverse Panel Types Test | 11 | 4 | 7 | 0 | 0 | 0 | 1 |
| grafana | Express Prometheus Middleware | 23 | 1 | 22 | 0 | 0 | 0 | 1 |
| grafana | Home - Migration Test Lab | 6 | 3 | 2 | 0 | 1 | 0 | 0 |
| grafana | Kubernetes / Views / Global | 26 | 11 | 15 | 0 | 0 | 0 | 4 |
| grafana | Kitchen Sink Panel Canary | 16 | 9 | 7 | 0 | 0 | 0 | 0 |
| grafana | Multi Pattern Coverage | 10 | 5 | 4 | 0 | 0 | 1 | 1 |
| grafana | Node Exporter Full | 117 | 43 | 74 | 0 | 0 | 0 | 16 |
| grafana | Prometheus 2.0 (by FUSAKLA) | 45 | 29 | 11 | 5 | 0 | 0 | 0 |
| grafana | Redis Dashboard for Prometheus Redis Exporter (helm stable/redis-ha) | 12 | 9 | 3 | 0 | 0 | 0 | 0 |

**9 dashboards, 266 panels** audited from `infra/grafana/dashboards/`.
<!-- /GENERATED:DASHBOARD_SUMMARY -->

<!-- GENERATED:VERDICT_SUMMARY -->
## Verdict Summary

| Verdict | Count | Meaning |
|---------|-------|---------|
| **CORRECT** | 11 | Translation is semantically accurate |
| **MINOR_ISSUE** | 238 | Translated with approximations — review recommended |
| **EXPECTED_LIMITATION** | 40 | Known unsupported feature — placeholder or skip |
<!-- /GENERATED:VERDICT_SUMMARY -->

<!-- GENERATED:WARNING_PATTERNS -->
## Top Warning Patterns

| Count | Warning |
|------:|---------|
| 56 | Composited multi-label grouping (instance, job) into a single XY breakdown column |
| 35 | Grafana panel description is not carried into the migrated Kibana panel automatically |
| 27 | Grafana panel has 1 field override(s); verify visual mappings manually |
| 22 | Approximated PromQL arithmetic using same-bucket ES\|QL math |
| 20 | Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value |
| 14 | PromQL series labels were not retained; output is bucket-level and may collapse multiple source series |
| 9 | Grafana panel has 1 value mapping(s) (e.g. 0 -> 'Down', null -> 'N/A'); Kibana panel mappings assign colors, not display text, so the raw value is shown instead |
| 7 | Grafana panel has 2 field override(s); verify visual mappings manually |
| 6 | Grafana panel has 18 field override(s); verify visual mappings manually |
| 6 | Grafana panel has 19 field override(s); verify visual mappings manually |
| 5 | Approximated bargauge as bar chart |
| 5 | Applied Grafana transformation 'calculateField' as ES\|QL rewrite |
| 5 | Approximated grouped stat panel as summary table |
| 5 | Grafana panel has 20 field override(s); verify visual mappings manually |
| 5 | Grafana panel has 17 field override(s); verify visual mappings manually |
<!-- /GENERATED:WARNING_PATTERNS -->

---

## Per-Dashboard Traces

<!-- GENERATED:PER_DASHBOARD_TRACES -->
### Grafana: Diverse Panel Types Test

**File:** `diverse-panels-test.json` — **Panels:** 12

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| System Metrics | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Request Latency Heatmap | `heatmap` → `heatmap` | migrated | **CORRECT** | sum(rate(http_request_duration_seconds_bucket[5m])) by (le) | TS metrics-prometheus-* \| WHERE http_request_duration_seconds_bucket IS NOT NUL... |
| Traffic Distribution | `piechart` → `pie` | migrated | **MINOR_ISSUE** | sum(rate(http_requests_total{instance=~"$instance"}[5m])) by (handler) | TS metrics-prometheus-* \| WHERE http_requests_total IS NOT NULL \| STATS http_r... |
| Top Endpoints | `barchart` → `bar` | migrated_with_warnings | **CORRECT** | topk(10, sum(rate(http_requests_total[5m])) by (handler)) | TS metrics-prometheus-* \| WHERE http_requests_total IS NOT NULL \| STATS _bucke... |
| CPU Usage | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | 100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100) | TS metrics-prometheus-* \| WHERE mode == "idle" \| WHERE node_cpu_seconds_total ... |
| Memory Usage | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100 | TS metrics-prometheus-* \| WHERE node_memory_MemAvailable_bytes IS NOT NULL OR n... |
| Uptime | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | time() - node_boot_time_seconds | FROM metrics-prometheus-* \| WHERE node_boot_time_seconds IS NOT NULL \| STATS s... |
| Disk Usage per Mount | `bargauge` → `bar` | migrated_with_warnings | **MINOR_ISSUE** | 100 - ((node_filesystem_avail_bytes{mountpoint!~".*pods.*"} / node_filesystem_si... | TS metrics-prometheus-* \| WHERE node_filesystem_avail_bytes IS NOT NULL OR node... |
| Active Alerts | `table` → `datatable` | migrated_with_warnings | **MINOR_ISSUE** | ALERTS{alertstate="firing"} | TS metrics-prometheus-* \| WHERE alertstate == "firing" \| WHERE ALERTS IS NOT N... |
| Notes | `text` → `markdown` | migrated | **EXPECTED_LIMITATION** | — | — |
| Application Logs | `logs` → `datatable` | migrated_with_warnings | **MINOR_ISSUE** | {job="app"} \|= "error" | FROM logs-* \| WHERE job == "app" \| WHERE message LIKE "*error*" \| KEEP @times... |
| Dashboard Links | `dashboard_links` → `links` | migrated | **EXPECTED_LIMITATION** | — | — |

<details>
<summary>Detailed traces (9 panels)</summary>

#### Request Latency Heatmap

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (heatmap):**

```
sum(rate(http_request_duration_seconds_bucket[5m])) by (le)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel`
- `panel_translators` / `datatable_panel`
- `panel_translators` / `heatmap_panel` → mapped to heatmap panel

**Translated (heatmap):**

```
TS metrics-prometheus-*
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = SUM(RATE(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), le
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
- Config keys: type, query, x_axis, y_axis, metric

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Verdict:** CORRECT

#### Traffic Distribution

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (piechart):**

```
sum(rate(http_requests_total{instance=~"$instance"}[5m])) by (handler)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel`
- `panel_translators` / `datatable_panel`
- `panel_translators` / `heatmap_panel`
- `panel_translators` / `pie_panel` → mapped to pie panel

**Translated (pie):**

```
TS metrics-prometheus-*
| WHERE http_requests_total IS NOT NULL
| STATS http_requests_total = SUM(RATE(http_requests_total)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), handler
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

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Top Endpoints

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (barchart):**

```
topk(10, sum(rate(http_requests_total[5m])) by (handler))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=topk backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family topk bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family` → translated grouped topk expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to bar panel

**Translated (bar):**

```
TS metrics-prometheus-*
| WHERE http_requests_total IS NOT NULL
| STATS _bucket_value = SUM(RATE(http_requests_total)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), handler
| SORT time_bucket ASC
| STATS value = LAST(_bucket_value, time_bucket) BY handler
| KEEP handler, value
| SORT value DESC
| LIMIT 10
```

**Query IR:**

- Family: `topk`
- Metric: `http_requests_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `handler`
- Output shape: `table`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `value`
- Output groups: `handler`

**Visual IR:**

- Kibana type: `bar`
- Layout: x=24, y=12, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Translated grouped topk() as latest-bucket ES|QL top N

**Verdict:** CORRECT

#### CPU Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE mode == "idle"
| WHERE node_cpu_seconds_total IS NOT NULL
| STATS node_cpu_seconds_total_mode_idle_rate_avg = AVG(RATE(node_cpu_seconds_total)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
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

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math

**Verdict:** MINOR_ISSUE

#### Memory Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (gauge):**

```
(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemAvailable_bytes IS NOT NULL OR node_memory_MemTotal_bytes IS NOT NULL
| STATS node_memory_MemAvailable_bytes = AVG(LAST_OVER_TIME(node_memory_MemAvailable_bytes)), node_memory_MemTotal_bytes = AVG(LAST_OVER_TIME(node_memory_MemTotal_bytes)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| EVAL computed_value = ((1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100)
| STATS computed_value = LAST(computed_value, time_bucket)
| KEEP computed_value
| EVAL _gauge_min = 0, _gauge_max = 100, _gauge_goal = 70
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `*`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Collapsed all series of `node_memory_MemAvailable_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Collapsed all series of `node_memory_MemTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Visual IR:**

- Kibana type: `gauge`
- Layout: x=24, y=0, w=12, h=12
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `node_memory_MemAvailable_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_MemTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `node_memory_MemAvailable_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_MemTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Verdict:** MINOR_ISSUE

#### Uptime

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
time() - node_boot_time_seconds
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=uptime backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family uptime bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family` → translated uptime expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
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

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel` → approximated bargauge panel

**Translated (bar):**

```
TS metrics-prometheus-*
| WHERE node_filesystem_avail_bytes IS NOT NULL OR node_filesystem_size_bytes IS NOT NULL
| STATS node_filesystem_avail_bytes_mountpoint_pods = AVG(LAST_OVER_TIME(CASE(((NOT (mountpoint RLIKE ".*pods.*") OR (mountpoint IS NULL AND NOT ("" RLIKE ".*pods.*")))), node_filesystem_avail_bytes, NULL), 5m)), node_filesystem_size_bytes = AVG(LAST_OVER_TIME(node_filesystem_size_bytes)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), mountpoint
| EVAL computed_value = (100 - ((node_filesystem_avail_bytes_mountpoint_pods / node_filesystem_size_bytes) * 100))
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Approximated bargauge as bar chart

**Visual IR:**

- Kibana type: `bar`
- Layout: x=36, y=6, w=12, h=6
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Approximated bargauge as bar chart

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Approximated bargauge as bar chart

**Verdict:** MINOR_ISSUE

#### Active Alerts

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (table):**

```
ALERTS{alertstate="firing"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel`
- `panel_translators` / `datatable_panel` → mapped to datatable panel

**Translated (datatable):**

```
TS metrics-prometheus-*
| WHERE alertstate == "firing"
| WHERE ALERTS IS NOT NULL
| STATS ALERTS = MAX(LAST_OVER_TIME(ALERTS)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| STATS time_bucket = MAX(time_bucket), ALERTS = MAX(ALERTS)
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
- Layout: x=0, y=12, w=48, h=9
- Presentation kind: `esql`
- Config keys: type, query, metrics, breakdowns

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- field_overrides: 1

**Warnings:** Grafana panel has 1 field override(s); verify visual mappings manually; ALERTS{} is a Prometheus meta-metric exposing per-alert label sets; ES|QL aggregation collapses individual alerts into a single value

**Notes:** Grafana panel has 1 field override(s); verify visual mappings manually

**Verdict:** MINOR_ISSUE

#### Application Logs

**Translation path:** `logql` · **Query language:** `logql` · **Readiness:** `logs_fielding_needed`

**Source (logs):**

```
{job="app"} |= "error"
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=logql_stream backend=regex
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family logql_stream bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family` → translated LogQL logs query
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel`
- `panel_translators` / `datatable_panel` → mapped to datatable panel

**Translated (datatable):**

```
FROM logs-*
| WHERE job == "app"
| WHERE message LIKE "*error*"
| KEEP @timestamp, job, message
| SORT @timestamp DESC
| LIMIT 200
```

**Query IR:**

- Family: `logql_stream`
- Metric: `message`
- Output shape: `event_rows`
- Source lang: `logql`
- Target index: `logs-*`
- Output metric: `message`
- Output groups: `@timestamp, job`
- Semantic losses: Approximated Loki logs panel as an ES|QL datatable

**Visual IR:**

- Kibana type: `datatable`
- Layout: x=24, y=21, w=24, h=8
- Presentation kind: `esql`
- Config keys: type, query, metrics, breakdowns

**Operational IR:**

- Query language: `logql`

**Inventory:**

- targets: 1

**Warnings:** Approximated Loki logs panel as an ES|QL datatable

**Semantic losses:** Approximated Loki logs panel as an ES|QL datatable

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Controls / Variables (1)</summary>

- `instance` (type: `esql`)

</details>

---

### Grafana: Express Prometheus Middleware

**File:** `express-14565.json` — **Panels:** 24

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| HTTP Requests | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Count by class | `gauge` → `datatable` | migrated_with_warnings | **MINOR_ISSUE** | sum(  http_requests_total{instance="$instance",status=~".{1,2}"} or  on() label_... | TS metrics-prometheus-* \| WHERE http_requests_total IS NOT NULL \| STATS series... |
| Request duration average by request | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | http_request_duration_seconds_sum{instance="$instance"} / http_request_duration_... | TS metrics-prometheus-* \| WHERE http_request_duration_seconds_sum IS NOT NULL O... |
| Request count by request | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | http_requests_total{instance="$instance"} | TS metrics-prometheus-* \| WHERE http_requests_total IS NOT NULL \| STATS http_r... |
| Request duration 95th percentile | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | histogram_quantile(0.95, sum by (job, le) (rate(http_request_duration_seconds_bu... | TS metrics-prometheus-* \| WHERE http_request_duration_seconds IS NOT NULL \| ST... |
| Request duration 99th percentile | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | histogram_quantile(0.99, sum by (job, le) (rate(http_request_duration_seconds_bu... | TS metrics-prometheus-* \| WHERE http_request_duration_seconds IS NOT NULL \| ST... |
| Request duration up to 5ms by request | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | http_request_duration_seconds_bucket{instance="$instance",le="0.005"} | TS metrics-prometheus-* \| WHERE le == "0.005" \| WHERE http_request_duration_se... |
| Request duration up to 10ms by request | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | http_request_duration_seconds_bucket{instance="$instance",le="0.01"} | TS metrics-prometheus-* \| WHERE le == "0.01" \| WHERE http_request_duration_sec... |
| Request duration up to 25ms by request | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | http_request_duration_seconds_bucket{instance="$instance",le="0.025"} | TS metrics-prometheus-* \| WHERE le == "0.025" \| WHERE http_request_duration_se... |
| Request duration up to 50ms by request | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | http_request_duration_seconds_bucket{instance="$instance",le="0.05"} | TS metrics-prometheus-* \| WHERE le == "0.05" \| WHERE http_request_duration_sec... |
| Request duration up to 100ms by request | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | http_request_duration_seconds_bucket{instance="$instance",le="0.1"} | TS metrics-prometheus-* \| WHERE le == "0.1" \| WHERE http_request_duration_seco... |
| Request duration up to 250ms by request | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | http_request_duration_seconds_bucket{instance="$instance",le="0.25"} | TS metrics-prometheus-* \| WHERE le == "0.25" \| WHERE http_request_duration_sec... |
| Request duration up to 500ms by request | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | http_request_duration_seconds_bucket{instance="$instance",le="0.5"} | TS metrics-prometheus-* \| WHERE le == "0.5" \| WHERE http_request_duration_seco... |
| Request duration up to 1s by request | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | http_request_duration_seconds_bucket{instance="$instance",le="1"} | TS metrics-prometheus-* \| WHERE (le == "1" OR le == "1.0") \| WHERE http_reques... |
| Request duration up to 2.5s by request | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | http_request_duration_seconds_bucket{instance="$instance",le="2.5"} | TS metrics-prometheus-* \| WHERE le == "2.5" \| WHERE http_request_duration_seco... |
| Request duration up to 5s by request | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | http_request_duration_seconds_bucket{instance="$instance",le="5"} | TS metrics-prometheus-* \| WHERE (le == "5" OR le == "5.0") \| WHERE http_reques... |
| Request duration up to 10s by request | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | http_request_duration_seconds_bucket{instance="$instance",le="10"} | TS metrics-prometheus-* \| WHERE (le == "10" OR le == "10.0") \| WHERE http_requ... |
| Request duration up to Infinity by request | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | http_request_duration_seconds_bucket{instance="$instance",le="+Inf"} | TS metrics-prometheus-* \| WHERE le == "+Inf" \| WHERE http_request_duration_sec... |
| Request rate | `timeseries` → `line` | migrated | **MINOR_ISSUE** | sum(rate(http_requests_total{instance="$instance"}[$__rate_interval])) | TS metrics-prometheus-* \| WHERE http_requests_total IS NOT NULL \| STATS http_r... |
| 4xx by request | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | http_requests_total{instance="$instance",status=~"4.."} | TS metrics-prometheus-* \| WHERE status RLIKE "4.." \| WHERE http_requests_total... |
| 5xx by request | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | http_requests_total{instance="$instance",status=~"5.."} | TS metrics-prometheus-* \| WHERE status RLIKE "5.." \| WHERE http_requests_total... |
| 4xx or 5xx by request | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | http_requests_total{instance="$instance",status=~"4.."} or http_requests_total{i... | TS metrics-prometheus-* \| WHERE (status RLIKE "4.." OR status RLIKE "5..") \| W... |
| Instance CPU usage | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | ((sum(process_cpu_seconds_total{instance="$instance"}) - avg(rate(node_cpu_secon... | TS metrics-prometheus-* \| WHERE process_cpu_seconds_total IS NOT NULL OR node_c... |
| Instance RAM usage | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(process_resident_memory_bytes{instance="$instance"}) / sum(node_memory_MemTo... | TS metrics-prometheus-* \| WHERE process_resident_memory_bytes IS NOT NULL OR no... |

<details>
<summary>Detailed traces (23 panels)</summary>

#### Count by class

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (gauge):**

```
sum(
 http_requests_total{instance="$instance",status=~".{1,2}"} or
 on() label_replace(vector(0),"status","0","","")
) ||| sum(
 http_requests_total{instance="$instance",status=~"1.."} or
 on() label_replace(vector(0),"status","100","","")
) ||| sum(
 http_requests_total{instance="$instance",status=~"2.."} or
 on() label_replace(vector(0),"status","200","","")
) ||| sum(
 http_requests_total{instance="$instance",status=~"3.."} or
 on() label_replace(vector(0),"status","300","","")
) ||| sum(
 http_requests_total{instance="$instance",status=~"4.."} or
 on() label_replace(vector(0),"status","400","","")
) ||| sum(
 http_requests_total{instance="$instance",status=~"5.."} or
 on() label_replace(vector(0),"status","500","","")
) ||| sum(
 http_requests_total{instance="$instance",status=~".*"} or
 on() label_replace(vector(0),"status","0","","")
)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note` → noted or-vector zero-fill approximation
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → approximated grouped gauge as datatable

**Translated (datatable):**

```
TS metrics-prometheus-*
| WHERE http_requests_total IS NOT NULL
| STATS series_0xx = SUM(LAST_OVER_TIME(CASE((status RLIKE ".{1,2}"), http_requests_total, NULL), 5m)), series_1xx = SUM(LAST_OVER_TIME(CASE((status RLIKE "1.."), http_requests_total, NULL), 5m)), series_2xx = SUM(LAST_OVER_TIME(CASE((status RLIKE "2.."), http_requests_total, NULL), 5m)), series_3xx = SUM(LAST_OVER_TIME(CASE((status RLIKE "3.."), http_requests_total, NULL), 5m)), series_4xx = SUM(LAST_OVER_TIME(CASE((status RLIKE "4.."), http_requests_total, NULL), 5m)), series_5xx = SUM(LAST_OVER_TIME(CASE((status RLIKE "5.."), http_requests_total, NULL), 5m)), Total = SUM(LAST_OVER_TIME(http_requests_total)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| STATS series_0xx = MAX(series_0xx), series_1xx = MAX(series_1xx), series_2xx = MAX(series_2xx), series_3xx = MAX(series_3xx), series_4xx = MAX(series_4xx), series_5xx = MAX(series_5xx), Total = MAX(Total)
| KEEP series_0xx, series_1xx, series_2xx, series_3xx, series_4xx, series_5xx, Total
```

**Query IR:**

- Family: `simple_agg`
- Metric: `http_requests_total`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `series_0xx`
- Semantic losses: Dropped variable-driven label filters during migration, Approximated PromQL 'or vector(N)' zero-fill fallback by dropping the constant operand; time ranges with no data appear as gaps instead of the fallback value, Approximated grouped gauge panel as summary table

**Visual IR:**

- Kibana type: `datatable`
- Layout: x=0, y=0, w=48, h=12
- Presentation kind: `esql`
- Config keys: type, query, metrics

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 7

**Warnings:** Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value; Approximated PromQL 'or vector(N)' zero-fill fallback by dropping the constant operand; time ranges with no data appear as gaps instead of the fallback value; Approximated grouped gauge panel as summary table

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated PromQL 'or vector(N)' zero-fill fallback by dropping the constant operand; time ranges with no data appear as gaps instead of the fallback value; Approximated grouped gauge panel as summary table

**Verdict:** MINOR_ISSUE

#### Request duration average by request

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
http_request_duration_seconds_sum{instance="$instance"} / http_request_duration_seconds_count{instance="$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE http_request_duration_seconds_sum IS NOT NULL OR http_request_duration_seconds_count IS NOT NULL
| STATS http_request_duration_seconds_sum_instance = MAX(LAST_OVER_TIME(http_request_duration_seconds_sum)), http_request_duration_seconds_count_instance = MAX(LAST_OVER_TIME(http_request_duration_seconds_count)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), method, path, status
| EVAL computed_value = (http_request_duration_seconds_sum_instance / http_request_duration_seconds_count_instance)
| EVAL legend = CONCAT(COALESCE(TO_STRING(method), ""), " ", COALESCE(TO_STRING(path), ""), " - ", COALESCE(TO_STRING(status), ""))
| KEEP time_bucket, method, path, status, computed_value, legend
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `/`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Output groups: `time_bucket, method, path, status`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=12, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Request count by request

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
http_requests_total{instance="$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE http_requests_total IS NOT NULL
| STATS http_requests_total = MAX(LAST_OVER_TIME(http_requests_total)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), method, path, status
| EVAL legend = CONCAT(COALESCE(TO_STRING(method), ""), " ", COALESCE(TO_STRING(path), ""), " - ", COALESCE(TO_STRING(status), ""))
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `http_requests_total`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_requests_total`
- Output groups: `time_bucket, method, path, status`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=12, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Request duration 95th percentile

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
histogram_quantile(0.95, sum by (job, le) (rate(http_request_duration_seconds_bucket{instance="$instance"}[$__rate_interval])))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=histogram_quantile backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family` → translated histogram_quantile to PERCENTILE
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE http_request_duration_seconds IS NOT NULL
| STATS http_request_duration_seconds = PERCENTILE(http_request_duration_seconds, 95) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), job
| SORT time_bucket ASC
```

**Query IR:**

- Family: `histogram_quantile`
- Metric: `http_request_duration_seconds`
- Range window: `5m`
- Group labels: `job`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_request_duration_seconds`
- Output groups: `time_bucket, job`
- Semantic losses: Dropped variable-driven label filters during migration, histogram_quantile translated to an ES|QL PERCENTILE() aggregation; this is approximate — PERCENTILE uses t-digest, which treats histogram buckets as point masses rather than interpolating within them as Prometheus does, so results can diverge noticeably when traffic concentrates in a few wide buckets (the common latency shape). Prefer a target on ES >= 9.5 (native histogram_quantile) for exact results.

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=24, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** histogram_quantile target field type could not be determined; assumed exponential_histogram and emitted PERCENTILE(). If the field is a classic histogram, pin the mapping or re-run with field capabilities so TO_TDIGEST() is used; histogram_quantile translated to an ES|QL PERCENTILE() aggregation; this is approximate — PERCENTILE uses t-digest, which treats histogram buckets as point masses rather than interpolating within them as Prometheus does, so results can diverge noticeably when traffic concentrates in a few wide buckets (the common latency shape). Prefer a target on ES >= 9.5 (native histogram_quantile) for exact results.

**Semantic losses:** Dropped variable-driven label filters during migration; histogram_quantile translated to an ES|QL PERCENTILE() aggregation; this is approximate — PERCENTILE uses t-digest, which treats histogram buckets as point masses rather than interpolating within them as Prometheus does, so results can diverge noticeably when traffic concentrates in a few wide buckets (the common latency shape). Prefer a target on ES >= 9.5 (native histogram_quantile) for exact results.

**Verdict:** MINOR_ISSUE

#### Request duration 99th percentile

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
histogram_quantile(0.99, sum by (job, le) (rate(http_request_duration_seconds_bucket{instance="$instance"}[$__rate_interval])))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=histogram_quantile backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family` → translated histogram_quantile to PERCENTILE
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE http_request_duration_seconds IS NOT NULL
| STATS http_request_duration_seconds = PERCENTILE(http_request_duration_seconds, 99) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), job
| SORT time_bucket ASC
```

**Query IR:**

- Family: `histogram_quantile`
- Metric: `http_request_duration_seconds`
- Range window: `5m`
- Group labels: `job`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_request_duration_seconds`
- Output groups: `time_bucket, job`
- Semantic losses: Dropped variable-driven label filters during migration, histogram_quantile translated to an ES|QL PERCENTILE() aggregation; this is approximate — PERCENTILE uses t-digest, which treats histogram buckets as point masses rather than interpolating within them as Prometheus does, so results can diverge noticeably when traffic concentrates in a few wide buckets (the common latency shape). Prefer a target on ES >= 9.5 (native histogram_quantile) for exact results.

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=24, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** histogram_quantile target field type could not be determined; assumed exponential_histogram and emitted PERCENTILE(). If the field is a classic histogram, pin the mapping or re-run with field capabilities so TO_TDIGEST() is used; histogram_quantile translated to an ES|QL PERCENTILE() aggregation; this is approximate — PERCENTILE uses t-digest, which treats histogram buckets as point masses rather than interpolating within them as Prometheus does, so results can diverge noticeably when traffic concentrates in a few wide buckets (the common latency shape). Prefer a target on ES >= 9.5 (native histogram_quantile) for exact results.

**Semantic losses:** Dropped variable-driven label filters during migration; histogram_quantile translated to an ES|QL PERCENTILE() aggregation; this is approximate — PERCENTILE uses t-digest, which treats histogram buckets as point masses rather than interpolating within them as Prometheus does, so results can diverge noticeably when traffic concentrates in a few wide buckets (the common latency shape). Prefer a target on ES >= 9.5 (native histogram_quantile) for exact results.

**Verdict:** MINOR_ISSUE

#### Request duration up to 5ms by request

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
http_request_duration_seconds_bucket{instance="$instance",le="0.005"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE le == "0.005"
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), method, path, status
| EVAL legend = CONCAT(COALESCE(TO_STRING(method), ""), " ", COALESCE(TO_STRING(path), ""), " - ", COALESCE(TO_STRING(status), ""))
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `http_request_duration_seconds_bucket`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_request_duration_seconds_bucket`
- Output groups: `time_bucket, method, path, status`
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

**Warnings:** Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Request duration up to 10ms by request

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
http_request_duration_seconds_bucket{instance="$instance",le="0.01"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE le == "0.01"
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), method, path, status
| EVAL legend = CONCAT(COALESCE(TO_STRING(method), ""), " ", COALESCE(TO_STRING(path), ""), " - ", COALESCE(TO_STRING(status), ""))
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `http_request_duration_seconds_bucket`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_request_duration_seconds_bucket`
- Output groups: `time_bucket, method, path, status`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=36, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Request duration up to 25ms by request

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
http_request_duration_seconds_bucket{instance="$instance",le="0.025"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE le == "0.025"
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), method, path, status
| EVAL legend = CONCAT(COALESCE(TO_STRING(method), ""), " ", COALESCE(TO_STRING(path), ""), " - ", COALESCE(TO_STRING(status), ""))
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `http_request_duration_seconds_bucket`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_request_duration_seconds_bucket`
- Output groups: `time_bucket, method, path, status`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=48, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Request duration up to 50ms by request

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
http_request_duration_seconds_bucket{instance="$instance",le="0.05"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE le == "0.05"
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), method, path, status
| EVAL legend = CONCAT(COALESCE(TO_STRING(method), ""), " ", COALESCE(TO_STRING(path), ""), " - ", COALESCE(TO_STRING(status), ""))
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `http_request_duration_seconds_bucket`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_request_duration_seconds_bucket`
- Output groups: `time_bucket, method, path, status`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=48, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Request duration up to 100ms by request

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
http_request_duration_seconds_bucket{instance="$instance",le="0.1"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE le == "0.1"
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), method, path, status
| EVAL legend = CONCAT(COALESCE(TO_STRING(method), ""), " ", COALESCE(TO_STRING(path), ""), " - ", COALESCE(TO_STRING(status), ""))
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `http_request_duration_seconds_bucket`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_request_duration_seconds_bucket`
- Output groups: `time_bucket, method, path, status`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=60, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Request duration up to 250ms by request

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
http_request_duration_seconds_bucket{instance="$instance",le="0.25"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE le == "0.25"
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), method, path, status
| EVAL legend = CONCAT(COALESCE(TO_STRING(method), ""), " ", COALESCE(TO_STRING(path), ""), " - ", COALESCE(TO_STRING(status), ""))
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `http_request_duration_seconds_bucket`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_request_duration_seconds_bucket`
- Output groups: `time_bucket, method, path, status`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=60, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Request duration up to 500ms by request

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
http_request_duration_seconds_bucket{instance="$instance",le="0.5"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE le == "0.5"
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), method, path, status
| EVAL legend = CONCAT(COALESCE(TO_STRING(method), ""), " ", COALESCE(TO_STRING(path), ""), " - ", COALESCE(TO_STRING(status), ""))
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `http_request_duration_seconds_bucket`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_request_duration_seconds_bucket`
- Output groups: `time_bucket, method, path, status`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=72, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Request duration up to 1s by request

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
http_request_duration_seconds_bucket{instance="$instance",le="1"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE (le == "1" OR le == "1.0")
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), method, path, status
| EVAL legend = CONCAT(COALESCE(TO_STRING(method), ""), " ", COALESCE(TO_STRING(path), ""), " - ", COALESCE(TO_STRING(status), ""))
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `http_request_duration_seconds_bucket`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_request_duration_seconds_bucket`
- Output groups: `time_bucket, method, path, status`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=72, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Request duration up to 2.5s by request

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
http_request_duration_seconds_bucket{instance="$instance",le="2.5"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE le == "2.5"
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), method, path, status
| EVAL legend = CONCAT(COALESCE(TO_STRING(method), ""), " ", COALESCE(TO_STRING(path), ""), " - ", COALESCE(TO_STRING(status), ""))
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `http_request_duration_seconds_bucket`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_request_duration_seconds_bucket`
- Output groups: `time_bucket, method, path, status`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=84, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Request duration up to 5s by request

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
http_request_duration_seconds_bucket{instance="$instance",le="5"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE (le == "5" OR le == "5.0")
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), method, path, status
| EVAL legend = CONCAT(COALESCE(TO_STRING(method), ""), " ", COALESCE(TO_STRING(path), ""), " - ", COALESCE(TO_STRING(status), ""))
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `http_request_duration_seconds_bucket`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_request_duration_seconds_bucket`
- Output groups: `time_bucket, method, path, status`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=84, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Controls / Variables (2)</summary>

- `Instance:` (type: `esql`)
- `Node Exporter:` (type: `esql`)

</details>

---

### Grafana: Home - Migration Test Lab

**File:** `home.json` — **Panels:** 6

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Untitled | `text` → `markdown` | migrated | **EXPECTED_LIMITATION** | — | — |
| Prometheus Targets Up | `stat` → `metric` | migrated_with_warnings | **CORRECT** | count(up == 1) | FROM metrics-prometheus-* \| WHERE up == 1 \| STATS series_present = COUNT(*) BY... |
| Scrape Duration by Job | `timeseries` → `line` | migrated | **CORRECT** | scrape_duration_seconds | TS metrics-prometheus-* \| WHERE scrape_duration_seconds IS NOT NULL \| STATS sc... |
| Memory Usage % | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100 | TS metrics-prometheus-* \| WHERE node_memory_MemAvailable_bytes IS NOT NULL OR n... |
| Top Metrics by Series Count | `bargauge` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | topk(10, count by (__name__)({__name__=~".+"})) | — |
| Target Health Status | `table` → `datatable` | migrated | **CORRECT** | up | TS metrics-prometheus-* \| WHERE up IS NOT NULL \| STATS up = MAX(LAST_OVER_TIME... |

<details>
<summary>Detailed traces (5 panels)</summary>

#### Prometheus Targets Up

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
count(up == 1)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated count() over a comparison by distinct series
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
| WHERE up == 1
| STATS series_present = COUNT(*) BY service.instance.id
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

**Warnings:** count() over a comparison collapses matching samples to series by the instance dimension only (the query supplied no grouping labels); if matching series share an instance but differ on another label such as job, the count may be under-stated — add the distinguishing label to by(...) or verify the series identity

**Verdict:** CORRECT

#### Scrape Duration by Job

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
scrape_duration_seconds
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE scrape_duration_seconds IS NOT NULL
| STATS scrape_duration_seconds = MAX(LAST_OVER_TIME(scrape_duration_seconds)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `scrape_duration_seconds`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `scrape_duration_seconds`
- Output groups: `time_bucket`

**Visual IR:**

- Kibana type: `line`
- Layout: x=16, y=6, w=32, h=15
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Verdict:** CORRECT

#### Memory Usage %

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (gauge):**

```
(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemAvailable_bytes IS NOT NULL OR node_memory_MemTotal_bytes IS NOT NULL
| STATS node_memory_MemAvailable_bytes = AVG(LAST_OVER_TIME(node_memory_MemAvailable_bytes)), node_memory_MemTotal_bytes = AVG(LAST_OVER_TIME(node_memory_MemTotal_bytes)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| EVAL computed_value = ((1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100)
| STATS computed_value = LAST(computed_value, time_bucket)
| KEEP computed_value
| EVAL _gauge_min = 0, _gauge_max = 100, _gauge_goal = 70
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `*`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Collapsed all series of `node_memory_MemAvailable_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Collapsed all series of `node_memory_MemTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Visual IR:**

- Kibana type: `gauge`
- Layout: x=0, y=15, w=16, h=9
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `node_memory_MemAvailable_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_MemTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `node_memory_MemAvailable_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_MemTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Verdict:** MINOR_ISSUE

#### Top Metrics by Series Count

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (bargauge):**

```
topk(10, count by (__name__)({__name__=~".+"}))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=unknown backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails` → PromQL metric-name introspection via __name__ requires manual redesign

**Query IR:**

- Family: `unknown`
- Outer agg: `topk`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Semantic losses: PromQL metric-name introspection via __name__ requires manual redesign

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=0, y=24, w=24, h=12
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** PromQL metric-name introspection via __name__ requires manual redesign

**Semantic losses:** PromQL metric-name introspection via __name__ requires manual redesign

**Verdict:** EXPECTED_LIMITATION

#### Target Health Status

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (table):**

```
up
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel`
- `panel_translators` / `datatable_panel` → mapped to datatable panel

**Translated (datatable):**

```
TS metrics-prometheus-*
| WHERE up IS NOT NULL
| STATS up = MAX(LAST_OVER_TIME(up)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| STATS time_bucket = MAX(time_bucket), up = MAX(up)
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
- Layout: x=24, y=24, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, metrics, breakdowns

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

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
| Global CPU  Usage | `bargauge` → `bar` | migrated_with_warnings | **MINOR_ISSUE** | avg(sum by (instance, cpu) (rate(node_cpu_seconds_total{mode!~"idle\|iowait\|ste... | TS metrics-prometheus-* \| WHERE kube_pod_container_resource_requests IS NOT NUL... |
| Global RAM Usage | `bargauge` → `bar` | migrated_with_warnings | **MINOR_ISSUE** | sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"} - node_memory_Mem... | TS metrics-prometheus-* \| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_... |
| Nodes | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | count(count by (node) (kube_node_info{cluster="$cluster"})) | FROM metrics-prometheus-* \| WHERE kube_node_info IS NOT NULL \| STATS kube_node... |
| Kubernetes Resource Count | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_namespace_labels{cluster="$cluster"}) \|\|\| sum(kube_pod_container_sta... | TS metrics-prometheus-* \| WHERE kube_namespace_labels IS NOT NULL OR kube_pod_c... |
| Namespaces | `stat` → `metric` | migrated | **MINOR_ISSUE** | count(kube_namespace_created{cluster="$cluster"}) | FROM metrics-prometheus-* \| WHERE kube_namespace_created IS NOT NULL \| STATS s... |
| CPU Usage | `stat` → `datatable` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(node_cpu_seconds_total{mode!~"idle\|iowait\|steal", cluster="$cluster",... | TS metrics-prometheus-* \| WHERE node_cpu_seconds_total IS NOT NULL OR windows_c... |
| RAM Usage | `stat` → `datatable` | migrated_with_warnings | **MINOR_ISSUE** | sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"} - node_memory_Mem... | TS metrics-prometheus-* \| STATS node_memory_MemTotal_bytes_cluster_job_sum_Real... |
| Running Pods | `stat` → `metric` | migrated | **MINOR_ISSUE** | sum(kube_pod_status_phase{phase="Running", cluster="$cluster"}) | TS metrics-prometheus-* \| WHERE phase == "Running" \| WHERE kube_pod_status_pha... |
| Cluster CPU Utilization | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | avg(sum by (instance, cpu) (rate(node_cpu_seconds_total{mode!~"idle\|iowait\|ste... | TS metrics-prometheus-* \| WHERE (NOT (mode RLIKE "idle\|iowait\|steal") OR (mod... |
| Cluster Memory Utilization | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"} - node_memory_Mem... | TS metrics-prometheus-* \| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_... |
| CPU Utilization by namespace | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(container_cpu_usage_seconds_total{image!="", cluster="$cluster"}[$__rat... | TS metrics-prometheus-* \| WHERE (image != "" OR (image IS NULL AND "" != "")) \... |
| Memory Utilization by namespace | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(container_memory_working_set_bytes{image!="", cluster="$cluster"}) by (names... | TS metrics-prometheus-* \| WHERE (image != "" OR (image IS NULL AND "" != "")) \... |
| CPU Utilization by instance | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | avg(sum by (instance, cpu) (rate(node_cpu_seconds_total{mode!~"idle\|iowait\|ste... | TS metrics-prometheus-* \| WHERE (NOT (mode RLIKE "idle\|iowait\|steal") OR (mod... |
| Memory Utilization by instance | `timeseries` → `line` | migrated | **MINOR_ISSUE** | sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"} - node_memory_Mem... | TS metrics-prometheus-* \| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_... |
| CPU Throttled seconds by namespace | `timeseries` → `line` | migrated | **MINOR_ISSUE** | sum(rate(container_cpu_cfs_throttled_seconds_total{image!="", cluster="$cluster"... | TS metrics-prometheus-* \| WHERE (image != "" OR (image IS NULL AND "" != "")) \... |
| CPU Core Throttled by instance | `timeseries` → `line` | migrated | **MINOR_ISSUE** | sum(rate(node_cpu_core_throttles_total{cluster="$cluster", job="$job"}[$__rate_i... | TS metrics-prometheus-* \| WHERE node_cpu_core_throttles_total IS NOT NULL \| ST... |
| Kubernetes Pods QoS classes | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_pod_status_qos_class{cluster="$cluster"}) by (qos_class) \|\|\| sum(kub... | TS metrics-prometheus-* \| WHERE kube_pod_status_qos_class IS NOT NULL OR kube_p... |
| Kubernetes Pods Status Reason | `timeseries` → `line` | migrated | **MINOR_ISSUE** | sum(kube_pod_status_reason{cluster="$cluster"}) by (reason) | TS metrics-prometheus-* \| WHERE kube_pod_status_reason IS NOT NULL \| STATS kub... |
| OOM Events by namespace | `timeseries` → `line` | migrated | **MINOR_ISSUE** | sum(increase(container_oom_events_total{cluster="$cluster"}[$__rate_interval])) ... | TS metrics-prometheus-* \| WHERE container_oom_events_total IS NOT NULL \| STATS... |
| Container Restarts by namespace | `timeseries` → `line` | migrated | **MINOR_ISSUE** | sum(increase(kube_pod_container_status_restarts_total{cluster="$cluster"}[$__rat... | TS metrics-prometheus-* \| WHERE kube_pod_container_status_restarts_total IS NOT... |
| Global Network Utilization by device | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(node_network_receive_bytes_total{device!~"(veth\|azv\|lxc).*", cluster=... | TS metrics-prometheus-* \| WHERE (NOT (device RLIKE "(veth\|azv\|lxc).*") OR (de... |
| Network Saturation - Packets dropped | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(node_network_receive_drop_total{cluster="$cluster", job="$job"}[$__rate... | TS metrics-prometheus-* \| WHERE node_network_receive_drop_total IS NOT NULL OR ... |
| Network Received by namespace | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(container_network_receive_bytes_total{cluster="$cluster"}[$__rate_inter... | TS metrics-prometheus-* \| WHERE container_network_receive_bytes_total IS NOT NU... |
| Total Network Received (with all virtual devices) by instance | `timeseries` → `line` | migrated | **MINOR_ISSUE** | sum(rate(node_network_receive_bytes_total{cluster="$cluster", job="$job"}[$__rat... | TS metrics-prometheus-* \| WHERE node_network_receive_bytes_total IS NOT NULL OR... |
| Network Received (without loopback)  by instance | `timeseries` → `line` | migrated | **MINOR_ISSUE** | sum(rate(node_network_receive_bytes_total{device!~"(veth\|azv\|lxc\|lo).*", clus... | TS metrics-prometheus-* \| WHERE node_network_receive_bytes_total IS NOT NULL OR... |
| Network Received (loopback only) by instance | `timeseries` → `line` | migrated | **MINOR_ISSUE** | sum(rate(node_network_receive_bytes_total{device="lo", cluster="$cluster", job="... | TS metrics-prometheus-* \| WHERE device == "lo" \| WHERE node_network_receive_by... |

<details>
<summary>Detailed traces (26 panels)</summary>

#### Global CPU  Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (bargauge):**

```
avg(sum by (instance, cpu) (rate(node_cpu_seconds_total{mode!~"idle|iowait|steal", cluster="$cluster", job="$job"}[$__rate_interval]))) ||| avg(sum by (core) (rate(windows_cpu_time_total{mode!="idle", cluster="$cluster"}[$__rate_interval]))) ||| sum(kube_pod_container_resource_requests{resource="cpu", cluster="$cluster"}) / sum(machine_cpu_cores{cluster="$cluster"}) ||| sum(kube_pod_container_resource_limits{resource="cpu", cluster="$cluster"}) / sum(machine_cpu_cores{cluster="$cluster"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel` → approximated bargauge panel

**Translated (bar):**

```
TS metrics-prometheus-*
| WHERE kube_pod_container_resource_requests IS NOT NULL OR machine_cpu_cores IS NOT NULL OR kube_pod_container_resource_limits IS NOT NULL
| STATS kube_pod_container_resource_requests_Requests_lhs = SUM(CASE((resource == "cpu"), kube_pod_container_resource_requests, NULL)), machine_cpu_cores_Requests_rhs = SUM(machine_cpu_cores), kube_pod_container_resource_limits_Limits_lhs = SUM(CASE((resource == "cpu"), kube_pod_container_resource_limits, NULL)), machine_cpu_cores_Limits_rhs = SUM(machine_cpu_cores) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| EVAL Requests = (kube_pod_container_resource_requests_Requests_lhs / machine_cpu_cores_Requests_rhs)
| EVAL Limits = (kube_pod_container_resource_limits_Limits_lhs / machine_cpu_cores_Limits_rhs)
| STATS Requests = MAX(Requests), Limits = MAX(Limits)
| KEEP Requests, Limits
| EVAL __labels = MV_APPEND("Requests", "Limits"), __values = MV_APPEND(COALESCE(TO_STRING(Requests), ""), COALESCE(TO_STRING(Limits), ""))
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
- Semantic losses: Dropped variable-driven label filters during migration, Dropped 2 incompatible target(s); showing 2 mergeable targets (1 of the dropped targets are Windows-specific), Approximated bargauge as bar chart

**Visual IR:**

- Kibana type: `bar`
- Layout: x=0, y=0, w=12, h=16
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 4
- transformations: 2

**Warnings:** Grafana panel has 2 transformation(s); manual review recommended; Dropped 2 incompatible target(s); showing 2 mergeable targets (1 of the dropped targets are Windows-specific); Approximated bargauge as bar chart

**Semantic losses:** Dropped variable-driven label filters during migration; Dropped 2 incompatible target(s); showing 2 mergeable targets (1 of the dropped targets are Windows-specific); Approximated bargauge as bar chart

**Notes:** Grafana panel has 2 transformation(s); manual review recommended

**Verdict:** MINOR_ISSUE

#### Global RAM Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (bargauge):**

```
sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"} - node_memory_MemAvailable_bytes{cluster="$cluster", job="$job"}) / sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"}) ||| sum(windows_memory_available_bytes{cluster="$cluster"} + windows_memory_cache_bytes{cluster="$cluster"}) / sum(windows_os_visible_memory_bytes{cluster="$cluster"}) ||| sum(kube_pod_container_resource_requests{resource="memory", cluster="$cluster"}) / sum(machine_memory_bytes{cluster="$cluster"}) ||| sum(kube_pod_container_resource_limits{resource="memory", cluster="$cluster"}) / sum(machine_memory_bytes{cluster="$cluster"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel` → approximated bargauge panel

**Translated (bar):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_memory_MemAvailable_bytes IS NOT NULL OR windows_memory_available_bytes IS NOT NULL OR windows_memory_cache_bytes IS NOT NULL OR windows_os_visible_memory_bytes IS NOT NULL OR kube_pod_container_resource_requests IS NOT NULL OR machine_memory_bytes IS NOT NULL OR kube_pod_container_resource_limits IS NOT NULL
| STATS node_memory_MemTotal_bytes_Real_Linux_lhs_lhs = SUM(node_memory_MemTotal_bytes), node_memory_MemAvailable_bytes_Real_Linux_lhs_rhs = SUM(node_memory_MemAvailable_bytes), node_memory_MemTotal_bytes_Real_Linux_rhs = SUM(node_memory_MemTotal_bytes), windows_memory_available_bytes_Real_Windows_lhs_lhs = SUM(windows_memory_available_bytes), windows_memory_cache_bytes_Real_Windows_lhs_rhs = SUM(windows_memory_cache_bytes), windows_os_visible_memory_bytes_Real_Windows_rhs = SUM(windows_os_visible_memory_bytes), kube_pod_container_resource_requests_Requests_lhs = SUM(CASE((resource == "memory"), kube_pod_container_resource_requests, NULL)), machine_memory_bytes_Requests_rhs = SUM(machine_memory_bytes), kube_pod_container_resource_limits_Limits_lhs = SUM(CASE((resource == "memory"), kube_pod_container_resource_limits, NULL)), machine_memory_bytes_Limits_rhs = SUM(machine_memory_bytes) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| EVAL Real_Linux = ((node_memory_MemTotal_bytes_Real_Linux_lhs_lhs - node_memory_MemAvailable_bytes_Real_Linux_lhs_rhs) / node_memory_MemTotal_bytes_Real_Linux_rhs)
| EVAL Real_Windows = ((windows_memory_available_bytes_Real_Windows_lhs_lhs + windows_memory_cache_bytes_Real_Windows_lhs_rhs) / windows_os_visible_memory_bytes_Real_Windows_rhs)
| EVAL Requests = (kube_pod_container_resource_requests_Requests_lhs / machine_memory_bytes_Requests_rhs)
| EVAL Limits = (kube_pod_container_resource_limits_Limits_lhs / machine_memory_bytes_Limits_rhs)
| STATS Real_Linux = MAX(Real_Linux), Real_Windows = MAX(Real_Windows), Requests = MAX(Requests), Limits = MAX(Limits)
| EVAL Real = CASE((CASE(Real_Linux IS NOT NULL, 1, 0) + CASE(Real_Windows IS NOT NULL, 1, 0)) > 0, (COALESCE(Real_Linux, 0) + COALESCE(Real_Windows, 0)) / (CASE(Real_Linux IS NOT NULL, 1, 0) + CASE(Real_Windows IS NOT NULL, 1, 0)), NULL)
| KEEP Requests, Limits, Real
| EVAL __labels = MV_APPEND(MV_APPEND("Requests", "Limits"), "Real"), __values = MV_APPEND(MV_APPEND(COALESCE(TO_STRING(Requests), ""), COALESCE(TO_STRING(Limits), "")), COALESCE(TO_STRING(Real), ""))
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
- Semantic losses: Dropped variable-driven label filters during migration, Approximated bargauge as bar chart

**Visual IR:**

- Kibana type: `bar`
- Layout: x=12, y=0, w=12, h=16
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 4
- transformations: 2

**Warnings:** Grafana panel has 2 transformation(s); manual review recommended; Applied Grafana transformation 'calculateField' as ES|QL rewrite; Applied Grafana transformation 'organize' as ES|QL rewrite; Approximated bargauge as bar chart

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated bargauge as bar chart

**Notes:** Grafana panel has 2 transformation(s); manual review recommended

**Verdict:** MINOR_ISSUE

#### Nodes

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
count(count by (node) (kube_node_info{cluster="$cluster"}))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=nested_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family nested_agg bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family` → translated nested count(count()) expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
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
- Layout: x=24, y=0, w=4, h=8
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated nested count(count()) as COUNT_DISTINCT(node)

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated nested count(count()) as COUNT_DISTINCT(node)

**Verdict:** MINOR_ISSUE

#### Kubernetes Resource Count

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(kube_namespace_labels{cluster="$cluster"}) ||| sum(kube_pod_container_status_running{cluster="$cluster"}) ||| sum(kube_pod_status_phase{phase="Running", cluster="$cluster"}) ||| sum(kube_service_info{cluster="$cluster"}) ||| sum(kube_endpoint_info{cluster="$cluster"}) ||| sum(kube_ingress_info{cluster="$cluster"}) ||| sum(kube_deployment_labels{cluster="$cluster"}) ||| sum(kube_statefulset_labels{cluster="$cluster"}) ||| sum(kube_daemonset_labels{cluster="$cluster"}) ||| sum(kube_persistentvolumeclaim_info{cluster="$cluster"}) ||| sum(kube_hpa_labels{cluster="$cluster"}) ||| sum(kube_configmap_info{cluster="$cluster"}) ||| sum(kube_secret_info{cluster="$cluster"}) ||| sum(kube_networkpolicy_labels{cluster="$cluster"}) ||| count(count by (node) (kube_node_info{cluster="$cluster"}))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE kube_namespace_labels IS NOT NULL OR kube_pod_container_status_running IS NOT NULL OR kube_pod_status_phase IS NOT NULL OR kube_service_info IS NOT NULL OR kube_endpoint_info IS NOT NULL OR kube_ingress_info IS NOT NULL OR kube_deployment_labels IS NOT NULL OR kube_statefulset_labels IS NOT NULL OR kube_daemonset_labels IS NOT NULL OR kube_persistentvolumeclaim_info IS NOT NULL OR kube_hpa_labels IS NOT NULL OR kube_configmap_info IS NOT NULL OR kube_secret_info IS NOT NULL OR kube_networkpolicy_labels IS NOT NULL OR kube_node_info IS NOT NULL
| STATS Namespaces = SUM(kube_namespace_labels), Running_Containers = SUM(kube_pod_container_status_running), Running_Pods = SUM(CASE((phase == "Running"), kube_pod_status_phase, NULL)), Services = SUM(kube_service_info), Endpoints = SUM(kube_endpoint_info), Ingresses = SUM(kube_ingress_info), Deployments = SUM(kube_deployment_labels), Statefulsets = SUM(kube_statefulset_labels), Daemonsets = SUM(kube_daemonset_labels), Persistent_Volume_Claims = SUM(kube_persistentvolumeclaim_info), Horizontal_Pod_Autoscalers = SUM(kube_hpa_labels), Configmaps = SUM(kube_configmap_info), Secrets = SUM(kube_secret_info), Network_Policies = SUM(kube_networkpolicy_labels), Nodes = COUNT_DISTINCT(node) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
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
- Layout: x=28, y=0, w=20, h=24
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 15

**Warnings:** Approximated nested count(count()) as COUNT_DISTINCT(node)

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated nested count(count()) as COUNT_DISTINCT(node)

**Verdict:** MINOR_ISSUE

#### Namespaces

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
count(kube_namespace_created{cluster="$cluster"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated count of counter metric
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
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
- Layout: x=24, y=8, w=4, h=8
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### CPU Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
sum(rate(node_cpu_seconds_total{mode!~"idle|iowait|steal", cluster="$cluster", job="$job"}[$__rate_interval])) ||| sum(rate(windows_cpu_time_total{mode!="idle", cluster="$cluster"}[$__rate_interval])) ||| sum(kube_pod_container_resource_requests{resource="cpu", cluster="$cluster"}) ||| sum(kube_pod_container_resource_limits{resource="cpu", cluster="$cluster"}) ||| sum(machine_cpu_cores{cluster="$cluster"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → approximated grouped stat as datatable

**Translated (datatable):**

```
TS metrics-prometheus-*
| WHERE node_cpu_seconds_total IS NOT NULL OR windows_cpu_time_total IS NOT NULL OR kube_pod_container_resource_requests IS NOT NULL OR kube_pod_container_resource_limits IS NOT NULL OR machine_cpu_cores IS NOT NULL
| STATS Real_Linux = SUM(CASE(((NOT (mode RLIKE "idle|iowait|steal") OR (mode IS NULL AND NOT ("" RLIKE "idle|iowait|steal")))), RATE(node_cpu_seconds_total), NULL)), Real_Windows = SUM(CASE(((mode != "idle" OR (mode IS NULL AND "" != "idle"))), RATE(windows_cpu_time_total), NULL)), Requests = SUM(LAST_OVER_TIME(CASE((resource == "cpu"), kube_pod_container_resource_requests, NULL), 5m)), Limits = SUM(LAST_OVER_TIME(CASE((resource == "cpu"), kube_pod_container_resource_limits, NULL), 5m)), Total = SUM(CASE(true, LAST_OVER_TIME(machine_cpu_cores, 5m), NULL)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| STATS Real_Linux = MAX(Real_Linux), Real_Windows = MAX(Real_Windows), Requests = MAX(Requests), Limits = MAX(Limits), Total = MAX(Total)
| EVAL Real = COALESCE(Real_Linux, 0) + COALESCE(Real_Windows, 0)
| KEEP Requests, Limits, Total, Real
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
- Output metric: `Requests`
- Semantic losses: Dropped variable-driven label filters during migration, Approximated grouped stat panel as summary table

**Visual IR:**

- Kibana type: `datatable`
- Layout: x=0, y=16, w=20, h=8
- Presentation kind: `esql`
- Config keys: type, query, metrics

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 5
- transformations: 2

**Warnings:** Grafana panel has 2 transformation(s); manual review recommended; Applied Grafana transformation 'calculateField' as ES|QL rewrite; Applied Grafana transformation 'organize' as ES|QL rewrite; Approximated grouped stat panel as summary table

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated grouped stat panel as summary table

**Notes:** Grafana panel has 2 transformation(s); manual review recommended

**Verdict:** MINOR_ISSUE

#### RAM Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"} - node_memory_MemAvailable_bytes{cluster="$cluster", job="$job"}) ||| sum(windows_os_visible_memory_bytes{cluster="$cluster"} - windows_memory_available_bytes{cluster="$cluster"} - windows_memory_cache_bytes{cluster="$cluster"}) ||| sum(kube_pod_container_resource_requests{resource="memory", cluster="$cluster"}) ||| sum(kube_pod_container_resource_limits{resource="memory", cluster="$cluster"}) ||| sum(machine_memory_bytes{cluster="$cluster"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → approximated grouped stat as datatable

**Translated (datatable):**

```
TS metrics-prometheus-*
| STATS node_memory_MemTotal_bytes_cluster_job_sum_Real_Linux = SUM(CASE((node_memory_MemTotal_bytes IS NOT NULL OR node_memory_MemAvailable_bytes IS NOT NULL), node_memory_MemTotal_bytes, NULL)), node_memory_MemAvailable_bytes_cluster_job_sum_Real_Linux = SUM(CASE((node_memory_MemTotal_bytes IS NOT NULL OR node_memory_MemAvailable_bytes IS NOT NULL), node_memory_MemAvailable_bytes, NULL)), computed_value_Real_Windows = SUM(CASE((windows_os_visible_memory_bytes IS NOT NULL) and (windows_memory_available_bytes IS NOT NULL) and (windows_memory_cache_bytes IS NOT NULL), ((windows_os_visible_memory_bytes - windows_memory_available_bytes) - windows_memory_cache_bytes), NULL)), kube_pod_container_resource_requests_Requests = SUM(CASE((resource == "memory") and (kube_pod_container_resource_requests IS NOT NULL), kube_pod_container_resource_requests, NULL)), kube_pod_container_resource_limits_Limits = SUM(CASE((resource == "memory") and (kube_pod_container_resource_limits IS NOT NULL), kube_pod_container_resource_limits, NULL)), machine_memory_bytes_Total = SUM(CASE((machine_memory_bytes IS NOT NULL), machine_memory_bytes, NULL)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| EVAL Real_Linux = (node_memory_MemTotal_bytes_cluster_job_sum_Real_Linux - node_memory_MemAvailable_bytes_cluster_job_sum_Real_Linux)
| EVAL Real_Windows = computed_value_Real_Windows
| EVAL Requests = kube_pod_container_resource_requests_Requests
| EVAL Limits = kube_pod_container_resource_limits_Limits
| EVAL Total = machine_memory_bytes_Total
| EVAL Real = CASE((CASE(Real_Linux IS NOT NULL, 1, 0) + CASE(Real_Windows IS NOT NULL, 1, 0)) > 0, (COALESCE(Real_Linux, 0) + COALESCE(Real_Windows, 0)) / (CASE(Real_Linux IS NOT NULL, 1, 0) + CASE(Real_Windows IS NOT NULL, 1, 0)), NULL)
| KEEP time_bucket, Requests, Limits, Total, Real
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `-`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `Requests`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration, Approximated grouped stat panel as summary table

**Visual IR:**

- Kibana type: `datatable`
- Layout: x=20, y=24, w=21, h=8
- Presentation kind: `esql`
- Config keys: type, query, metrics, breakdowns

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 5
- transformations: 2

**Warnings:** Grafana panel has 2 transformation(s); manual review recommended; Fused multi-target panel from independently translated ES|QL queries; Per-element arithmetic between co-located metrics evaluated per document before aggregation (exact for Prometheus layouts that store one document per label-set; PromQL's all-label matching guarantees the operands align); Applied Grafana transformation 'calculateField' as ES|QL rewrite; Applied Grafana transformation 'organize' as ES|QL rewrite

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated grouped stat panel as summary table

**Notes:** Grafana panel has 2 transformation(s); manual review recommended

**Verdict:** MINOR_ISSUE

#### Running Pods

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
sum(kube_pod_status_phase{phase="Running", cluster="$cluster"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE phase == "Running"
| WHERE kube_pod_status_phase IS NOT NULL
| STATS kube_pod_status_phase = SUM(kube_pod_status_phase) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| STATS kube_pod_status_phase = LAST(kube_pod_status_phase, time_bucket)
| KEEP kube_pod_status_phase
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
- Layout: x=41, y=24, w=7, h=8
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Cluster CPU Utilization

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
avg(sum by (instance, cpu) (rate(node_cpu_seconds_total{mode!~"idle|iowait|steal", cluster="$cluster", job="$job"}[$__rate_interval]))) ||| 1 - avg(rate(windows_cpu_time_total{cluster="$cluster",mode="idle"}[$__rate_interval]))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=nested_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family nested_agg bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family` → translated nested avg over rate expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE (NOT (mode RLIKE "idle|iowait|steal") OR (mode IS NULL AND NOT ("" RLIKE "idle|iowait|steal")))
| WHERE node_cpu_seconds_total IS NOT NULL
| STATS inner_val = SUM(RATE(node_cpu_seconds_total)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance, cpu
| STATS node_cpu_seconds_total_avg = AVG(inner_val) BY time_bucket
| SORT time_bucket ASC
```

**Query IR:**

- Family: `nested_agg`
- Metric: `node_cpu_seconds_total_avg`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `avg`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `node_cpu_seconds_total_avg`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration, Panel has 2 PromQL targets but only 1 could be migrated (dropped targets are Windows-specific)

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

**Semantic losses:** Dropped variable-driven label filters during migration; Panel has 2 PromQL targets but only 1 could be migrated (dropped targets are Windows-specific)

**Notes:** Grafana panel has 1 transformation(s); manual review recommended

**Verdict:** MINOR_ISSUE

#### Cluster Memory Utilization

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"} - node_memory_MemAvailable_bytes{cluster="$cluster", job="$job"}) / sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"}) ||| sum(windows_os_visible_memory_bytes{cluster="$cluster"} - windows_memory_available_bytes{cluster="$cluster"}) / sum(windows_os_visible_memory_bytes{cluster="$cluster"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_memory_MemAvailable_bytes IS NOT NULL OR windows_os_visible_memory_bytes IS NOT NULL OR windows_memory_available_bytes IS NOT NULL
| STATS node_memory_MemTotal_bytes_Linux_lhs_lhs = SUM(node_memory_MemTotal_bytes), node_memory_MemAvailable_bytes_Linux_lhs_rhs = SUM(node_memory_MemAvailable_bytes), node_memory_MemTotal_bytes_Linux_rhs = SUM(node_memory_MemTotal_bytes), windows_os_visible_memory_bytes_Windows_lhs_lhs = SUM(windows_os_visible_memory_bytes), windows_memory_available_bytes_Windows_lhs_rhs = SUM(windows_memory_available_bytes), windows_os_visible_memory_bytes_Windows_rhs = SUM(windows_os_visible_memory_bytes) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| EVAL Linux = ((node_memory_MemTotal_bytes_Linux_lhs_lhs - node_memory_MemAvailable_bytes_Linux_lhs_rhs) / node_memory_MemTotal_bytes_Linux_rhs)
| EVAL Windows = ((windows_os_visible_memory_bytes_Windows_lhs_lhs - windows_memory_available_bytes_Windows_lhs_rhs) / windows_os_visible_memory_bytes_Windows_rhs)
| EVAL Memory_usage_in = CASE((CASE(Linux IS NOT NULL, 1, 0) + CASE(Windows IS NOT NULL, 1, 0)) > 0, (COALESCE(Linux, 0) + COALESCE(Windows, 0)) / (CASE(Linux IS NOT NULL, 1, 0) + CASE(Windows IS NOT NULL, 1, 0)), NULL)
| KEEP time_bucket, Memory_usage_in
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `/`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `Memory_usage_in`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

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

**Warnings:** Grafana panel has 1 transformation(s); manual review recommended; Applied Grafana transformation 'calculateField' as ES|QL rewrite

**Semantic losses:** Dropped variable-driven label filters during migration

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

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE (image != "" OR (image IS NULL AND "" != ""))
| WHERE container_cpu_usage_seconds_total IS NOT NULL
| STATS container_cpu_usage_seconds_total_image_rate_sum = SUM(RATE(container_cpu_usage_seconds_total)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), namespace
| EVAL computed_value = container_cpu_usage_seconds_total_image_rate_sum
| KEEP time_bucket, namespace, computed_value
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `+`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Output groups: `time_bucket, namespace`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, PromQL mixed-OS '+ on(...) (windows_join or zero_fill)' : preferred left (Linux) operand and dropped the Windows join contribution; Windows namespaces will under-report until the join is redesigned

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=12, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; PromQL mixed-OS '+ on(...) (windows_join or zero_fill)' : preferred left (Linux) operand and dropped the Windows join contribution; Windows namespaces will under-report until the join is redesigned

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; PromQL mixed-OS '+ on(...) (windows_join or zero_fill)' : preferred left (Linux) operand and dropped the Windows join contribution; Windows namespaces will under-report until the join is redesigned

**Verdict:** MINOR_ISSUE

#### Memory Utilization by namespace

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(container_memory_working_set_bytes{image!="", cluster="$cluster"}) by (namespace)
+ on (namespace)
(sum(windows_container_memory_usage_commit_bytes{container_id!="", cluster="$cluster"} * on (container_id) group_left (container, pod, namespace) max by ( container, container_id, pod, namespace) (kube_pod_container_info{container_id!="", cluster="$cluster"}) OR kube_namespace_created{cluster="$cluster"} * 0) by (namespace))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE (image != "" OR (image IS NULL AND "" != ""))
| WHERE container_memory_working_set_bytes IS NOT NULL
| STATS container_memory_working_set_bytes_image_sum = SUM(container_memory_working_set_bytes) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), namespace
| EVAL computed_value = container_memory_working_set_bytes_image_sum
| KEEP time_bucket, namespace, computed_value
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `+`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Output groups: `time_bucket, namespace`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, PromQL mixed-OS '+ on(...) (windows_join or zero_fill)' : preferred left (Linux) operand and dropped the Windows join contribution; Windows namespaces will under-report until the join is redesigned

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=12, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; PromQL mixed-OS '+ on(...) (windows_join or zero_fill)' : preferred left (Linux) operand and dropped the Windows join contribution; Windows namespaces will under-report until the join is redesigned

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; PromQL mixed-OS '+ on(...) (windows_join or zero_fill)' : preferred left (Linux) operand and dropped the Windows join contribution; Windows namespaces will under-report until the join is redesigned

**Verdict:** MINOR_ISSUE

#### CPU Utilization by instance

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
avg(sum by (instance, cpu) (rate(node_cpu_seconds_total{mode!~"idle|iowait|steal", cluster="$cluster", job="$job"}[$__rate_interval]))) by (instance) ||| avg(sum by (instance,core) (rate(windows_cpu_time_total{mode!="idle", cluster="$cluster"}[$__rate_interval]))) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=nested_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family nested_agg bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family` → translated nested avg over rate expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE (NOT (mode RLIKE "idle|iowait|steal") OR (mode IS NULL AND NOT ("" RLIKE "idle|iowait|steal")))
| WHERE node_cpu_seconds_total IS NOT NULL
| STATS inner_val = SUM(RATE(node_cpu_seconds_total)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance, cpu
| STATS node_cpu_seconds_total_avg = AVG(inner_val) BY time_bucket
| SORT time_bucket ASC
```

**Query IR:**

- Family: `nested_agg`
- Metric: `node_cpu_seconds_total_avg`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `avg`
- Group labels: `instance`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `node_cpu_seconds_total_avg`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration, Panel has 2 PromQL targets but only 1 could be migrated (dropped targets are Windows-specific)

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

**Semantic losses:** Dropped variable-driven label filters during migration; Panel has 2 PromQL targets but only 1 could be migrated (dropped targets are Windows-specific)

**Verdict:** MINOR_ISSUE

#### Memory Utilization by instance

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"} - node_memory_MemAvailable_bytes{cluster="$cluster", job="$job"}) by (instance) ||| sum(windows_os_visible_memory_bytes{cluster="$cluster"} - windows_memory_available_bytes{cluster="$cluster"}) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_memory_MemAvailable_bytes IS NOT NULL OR windows_os_visible_memory_bytes IS NOT NULL OR windows_memory_available_bytes IS NOT NULL
| STATS node_memory_MemTotal_bytes_Linux_lhs = SUM(node_memory_MemTotal_bytes), node_memory_MemAvailable_bytes_Linux_rhs = SUM(node_memory_MemAvailable_bytes), windows_os_visible_memory_bytes_Windows_lhs = SUM(windows_os_visible_memory_bytes), windows_memory_available_bytes_Windows_rhs = SUM(windows_memory_available_bytes) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| EVAL instance = (node_memory_MemTotal_bytes_Linux_lhs - node_memory_MemAvailable_bytes_Linux_rhs)
| EVAL instance_Windows = (windows_os_visible_memory_bytes_Windows_lhs - windows_memory_available_bytes_Windows_rhs)
| KEEP time_bucket, instance, instance_Windows
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Group labels: `instance`
- Binary op: `-`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `instance`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=24, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### CPU Throttled seconds by namespace

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(rate(container_cpu_cfs_throttled_seconds_total{image!="", cluster="$cluster"}[$__rate_interval])) by (namespace) > 0
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter` → applied post-aggregation filter > 0
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE (image != "" OR (image IS NULL AND "" != ""))
| WHERE container_cpu_cfs_throttled_seconds_total IS NOT NULL
| STATS container_cpu_cfs_throttled_seconds_total = SUM(RATE(container_cpu_cfs_throttled_seconds_total)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), namespace
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

**Warnings:** Grafana panel description is not carried into the migrated Kibana panel automatically

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Controls / Variables (2)</summary>

- `cluster` (type: `esql`)
- `job` (type: `esql`)

</details>

---

### Grafana: Kitchen Sink Panel Canary

**File:** `kitchen-sink-canary.json` — **Panels:** 16

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| timeseries panel | `timeseries` → `line` | migrated | **MINOR_ISSUE** | sum(redis_memory_used_bytes{instance=~"$instance"}) by (instance) | TS metrics-prometheus-* \| WHERE redis_memory_used_bytes IS NOT NULL \| STATS re... |
| graph panel | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(redis_memory_used_bytes{instance=~"$instance"}) by (instance) | TS metrics-prometheus-* \| WHERE redis_memory_used_bytes IS NOT NULL \| STATS re... |
| stat panel | `stat` → `datatable` | migrated_with_warnings | **MINOR_ISSUE** | sum(redis_connected_clients{instance=~"$instance"}) by (instance) | TS metrics-prometheus-* \| WHERE redis_connected_clients IS NOT NULL \| STATS re... |
| singlestat panel | `singlestat` → `datatable` | migrated_with_warnings | **MINOR_ISSUE** | sum(redis_db_keys{instance=~"$instance"}) by (instance) | TS metrics-prometheus-* \| WHERE redis_db_keys IS NOT NULL \| STATS redis_db_key... |
| gauge panel | `gauge` → `datatable` | migrated_with_warnings | **MINOR_ISSUE** | sum(redis_memory_used_bytes{instance=~"$instance"}) by (instance) | TS metrics-prometheus-* \| WHERE redis_memory_used_bytes IS NOT NULL \| STATS re... |
| bargauge panel | `bargauge` → `bar` | migrated_with_warnings | **MINOR_ISSUE** | sum(redis_db_keys{instance=~"$instance"}) by (instance) | TS metrics-prometheus-* \| WHERE redis_db_keys IS NOT NULL \| STATS redis_db_key... |
| table panel | `table` → `datatable` | migrated | **MINOR_ISSUE** | sum(redis_db_keys{instance=~"$instance"}) by (instance) | TS metrics-prometheus-* \| WHERE redis_db_keys IS NOT NULL \| STATS redis_db_key... |
| table-old panel | `table-old` → `datatable` | migrated | **MINOR_ISSUE** | sum(redis_db_keys{instance=~"$instance"}) by (instance) | TS metrics-prometheus-* \| WHERE redis_db_keys IS NOT NULL \| STATS redis_db_key... |
| text panel | `text` → `markdown` | migrated | **EXPECTED_LIMITATION** | — | — |
| logs panel | `logs` → `datatable` | migrated | **MINOR_ISSUE** | sum(redis_db_keys{instance=~"$instance"}) by (instance) | TS metrics-prometheus-* \| WHERE redis_db_keys IS NOT NULL \| STATS redis_db_key... |
| heatmap panel | `heatmap` → `heatmap` | migrated_with_warnings | **MINOR_ISSUE** | sum(redis_commands_processed_total{instance=~"$instance"}) by (instance) | TS metrics-prometheus-* \| WHERE redis_commands_processed_total IS NOT NULL \| S... |
| piechart panel | `piechart` → `pie` | migrated | **MINOR_ISSUE** | sum(redis_db_keys{instance=~"$instance"}) by (instance) | TS metrics-prometheus-* \| WHERE redis_db_keys IS NOT NULL \| STATS redis_db_key... |
| grafana-piechart-panel panel | `grafana-piechart-panel` → `pie` | migrated | **MINOR_ISSUE** | sum(redis_db_keys{instance=~"$instance"}) by (instance) | TS metrics-prometheus-* \| WHERE redis_db_keys IS NOT NULL \| STATS redis_db_key... |
| barchart panel | `barchart` → `bar` | migrated | **MINOR_ISSUE** | sum(redis_db_keys{instance=~"$instance"}) by (instance) | TS metrics-prometheus-* \| WHERE redis_db_keys IS NOT NULL \| STATS redis_db_key... |
| state-timeline panel | `state-timeline` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(redis_up{instance=~"$instance"}) by (instance) | TS metrics-prometheus-* \| WHERE redis_up IS NOT NULL \| STATS redis_up = SUM(re... |
| status-history panel | `status-history` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(redis_up{instance=~"$instance"}) by (instance) | TS metrics-prometheus-* \| WHERE redis_up IS NOT NULL \| STATS redis_up = SUM(re... |

<details>
<summary>Detailed traces (15 panels)</summary>

#### timeseries panel

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(redis_memory_used_bytes{instance=~"$instance"}) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_memory_used_bytes IS NOT NULL
| STATS redis_memory_used_bytes = SUM(redis_memory_used_bytes) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `redis_memory_used_bytes`
- Outer agg: `sum`
- Group labels: `instance`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_memory_used_bytes`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### graph panel

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(redis_memory_used_bytes{instance=~"$instance"}) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_memory_used_bytes IS NOT NULL
| STATS redis_memory_used_bytes = SUM(redis_memory_used_bytes) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `redis_memory_used_bytes`
- Outer agg: `sum`
- Group labels: `instance`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_memory_used_bytes`
- Output groups: `time_bucket, instance`
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

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### stat panel

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
sum(redis_connected_clients{instance=~"$instance"}) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → approximated grouped stat as datatable

**Translated (datatable):**

```
TS metrics-prometheus-*
| WHERE redis_connected_clients IS NOT NULL
| STATS redis_connected_clients = SUM(redis_connected_clients) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| STATS redis_connected_clients = LAST(redis_connected_clients, time_bucket) BY instance
| KEEP instance, redis_connected_clients
```

**Query IR:**

- Family: `simple_agg`
- Metric: `redis_connected_clients`
- Outer agg: `sum`
- Group labels: `instance`
- Output shape: `table`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_connected_clients`
- Output groups: `instance`
- Semantic losses: Dropped variable-driven label filters during migration, Approximated grouped stat panel as summary table

**Visual IR:**

- Kibana type: `datatable`
- Layout: x=0, y=12, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, metrics, breakdowns

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated grouped stat panel as summary table

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated grouped stat panel as summary table

**Verdict:** MINOR_ISSUE

#### singlestat panel

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(redis_db_keys{instance=~"$instance"}) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → approximated grouped stat as datatable

**Translated (datatable):**

```
TS metrics-prometheus-*
| WHERE redis_db_keys IS NOT NULL
| STATS redis_db_keys = SUM(redis_db_keys) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| STATS redis_db_keys = LAST(redis_db_keys, time_bucket) BY instance
| KEEP instance, redis_db_keys
```

**Query IR:**

- Family: `simple_agg`
- Metric: `redis_db_keys`
- Outer agg: `sum`
- Group labels: `instance`
- Output shape: `table`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_db_keys`
- Output groups: `instance`
- Semantic losses: Dropped variable-driven label filters during migration, Approximated grouped stat panel as summary table

**Visual IR:**

- Kibana type: `datatable`
- Layout: x=24, y=12, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, metrics, breakdowns

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated grouped stat panel as summary table

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated grouped stat panel as summary table

**Verdict:** MINOR_ISSUE

#### gauge panel

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (gauge):**

```
sum(redis_memory_used_bytes{instance=~"$instance"}) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → approximated grouped gauge as datatable

**Translated (datatable):**

```
TS metrics-prometheus-*
| WHERE redis_memory_used_bytes IS NOT NULL
| STATS redis_memory_used_bytes = SUM(redis_memory_used_bytes) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| STATS redis_memory_used_bytes = LAST(redis_memory_used_bytes, time_bucket) BY instance
| KEEP instance, redis_memory_used_bytes
```

**Query IR:**

- Family: `simple_agg`
- Metric: `redis_memory_used_bytes`
- Outer agg: `sum`
- Group labels: `instance`
- Output shape: `table`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_memory_used_bytes`
- Output groups: `instance`
- Semantic losses: Dropped variable-driven label filters during migration, Approximated grouped gauge panel as summary table

**Visual IR:**

- Kibana type: `datatable`
- Layout: x=0, y=24, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, metrics, breakdowns

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated grouped gauge panel as summary table

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated grouped gauge panel as summary table

**Verdict:** MINOR_ISSUE

#### bargauge panel

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (bargauge):**

```
sum(redis_db_keys{instance=~"$instance"}) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel` → approximated bargauge panel

**Translated (bar):**

```
TS metrics-prometheus-*
| WHERE redis_db_keys IS NOT NULL
| STATS redis_db_keys = SUM(redis_db_keys) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| STATS redis_db_keys = LAST(redis_db_keys, time_bucket) BY instance
| KEEP instance, redis_db_keys
```

**Query IR:**

- Family: `simple_agg`
- Metric: `redis_db_keys`
- Outer agg: `sum`
- Group labels: `instance`
- Output shape: `table`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_db_keys`
- Output groups: `instance`
- Semantic losses: Dropped variable-driven label filters during migration, Approximated bargauge as bar chart

**Visual IR:**

- Kibana type: `bar`
- Layout: x=24, y=24, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated bargauge as bar chart

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated bargauge as bar chart

**Verdict:** MINOR_ISSUE

#### table panel

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (table):**

```
sum(redis_db_keys{instance=~"$instance"}) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel`
- `panel_translators` / `datatable_panel` → mapped to datatable panel

**Translated (datatable):**

```
TS metrics-prometheus-*
| WHERE redis_db_keys IS NOT NULL
| STATS redis_db_keys = SUM(redis_db_keys) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `redis_db_keys`
- Outer agg: `sum`
- Group labels: `instance`
- Output shape: `table`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_db_keys`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `datatable`
- Layout: x=0, y=36, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, metrics, breakdowns

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### table-old panel

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (table-old):**

```
sum(redis_db_keys{instance=~"$instance"}) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel`
- `panel_translators` / `datatable_panel` → mapped to datatable panel

**Translated (datatable):**

```
TS metrics-prometheus-*
| WHERE redis_db_keys IS NOT NULL
| STATS redis_db_keys = SUM(redis_db_keys) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `redis_db_keys`
- Outer agg: `sum`
- Group labels: `instance`
- Output shape: `table`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_db_keys`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `datatable`
- Layout: x=24, y=36, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, metrics, breakdowns

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### logs panel

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (logs):**

```
sum(redis_db_keys{instance=~"$instance"}) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel`
- `panel_translators` / `datatable_panel` → mapped to datatable panel

**Translated (datatable):**

```
TS metrics-prometheus-*
| WHERE redis_db_keys IS NOT NULL
| STATS redis_db_keys = SUM(redis_db_keys) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `redis_db_keys`
- Outer agg: `sum`
- Group labels: `instance`
- Output shape: `event_rows`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_db_keys`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `datatable`
- Layout: x=24, y=48, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, metrics, breakdowns

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### heatmap panel

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (heatmap):**

```
sum(redis_commands_processed_total{instance=~"$instance"}) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel`
- `panel_translators` / `datatable_panel`
- `panel_translators` / `heatmap_panel` → mapped to heatmap panel

**Translated (heatmap):**

```
TS metrics-prometheus-*
| WHERE redis_commands_processed_total IS NOT NULL
| STATS redis_commands_processed_total = SUM(LAST_OVER_TIME(redis_commands_processed_total)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `redis_commands_processed_total`
- Outer agg: `sum`
- Group labels: `instance`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_commands_processed_total`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `heatmap`
- Layout: x=0, y=60, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, x_axis, y_axis, metric

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### piechart panel

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (piechart):**

```
sum(redis_db_keys{instance=~"$instance"}) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel`
- `panel_translators` / `datatable_panel`
- `panel_translators` / `heatmap_panel`
- `panel_translators` / `pie_panel` → mapped to pie panel

**Translated (pie):**

```
TS metrics-prometheus-*
| WHERE redis_db_keys IS NOT NULL
| STATS redis_db_keys = SUM(redis_db_keys) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| STATS redis_db_keys = LAST(redis_db_keys, time_bucket) BY instance
| KEEP instance, redis_db_keys
```

**Query IR:**

- Family: `simple_agg`
- Metric: `redis_db_keys`
- Outer agg: `sum`
- Group labels: `instance`
- Output shape: `table`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_db_keys`
- Output groups: `instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `pie`
- Layout: x=24, y=60, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, metrics, breakdowns, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### grafana-piechart-panel panel

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (grafana-piechart-panel):**

```
sum(redis_db_keys{instance=~"$instance"}) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel`
- `panel_translators` / `datatable_panel`
- `panel_translators` / `heatmap_panel`
- `panel_translators` / `pie_panel` → mapped to pie panel

**Translated (pie):**

```
TS metrics-prometheus-*
| WHERE redis_db_keys IS NOT NULL
| STATS redis_db_keys = SUM(redis_db_keys) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `redis_db_keys`
- Outer agg: `sum`
- Group labels: `instance`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_db_keys`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `pie`
- Layout: x=0, y=72, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, metrics, breakdowns, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### barchart panel

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (barchart):**

```
sum(redis_db_keys{instance=~"$instance"}) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to bar panel

**Translated (bar):**

```
TS metrics-prometheus-*
| WHERE redis_db_keys IS NOT NULL
| STATS redis_db_keys = SUM(redis_db_keys) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `redis_db_keys`
- Outer agg: `sum`
- Group labels: `instance`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_db_keys`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `bar`
- Layout: x=24, y=72, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### state-timeline panel

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (state-timeline):**

```
sum(redis_up{instance=~"$instance"}) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_up IS NOT NULL
| STATS redis_up = SUM(redis_up) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `redis_up`
- Outer agg: `sum`
- Group labels: `instance`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_up`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=84, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Grafana state-timeline panel approximated as a Kibana line chart: the underlying time series is preserved, but Kibana has no discrete state-band visualization so state transitions render as line values

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### status-history panel

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (status-history):**

```
sum(redis_up{instance=~"$instance"}) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_up IS NOT NULL
| STATS redis_up = SUM(redis_up) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `redis_up`
- Outer agg: `sum`
- Group labels: `instance`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_up`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=84, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Grafana status-history panel approximated as a Kibana line chart: the underlying time series is preserved, but Kibana has no periodic discrete-state (status cell) visualization so values render as a line

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Controls / Variables (1)</summary>

- `instance` (type: `esql`)

</details>

---

### Grafana: Multi Pattern Coverage

**File:** `multi-pattern-coverage.json` — **Panels:** 11

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Combined Patterns | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Service Route Rate | `timeseries` → `line` | migrated | **CORRECT** | sum(rate(http_requests_total{status=~"2.."}[5m])) by (service, route) | TS metrics-prometheus-* \| WHERE status RLIKE "2.." \| WHERE http_requests_total... |
| Queue Depth Bullet | `bargauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | avg(queue_depth) | TS metrics-prometheus-* \| WHERE queue_depth IS NOT NULL \| STATS queue_depth = ... |
| Merged Request Streams | `timeseries` → `line` | migrated | **CORRECT** | rate(frontend_requests_total[5m]) \|\|\| rate(worker_jobs_total[5m]) | TS metrics-prometheus-* \| WHERE frontend_requests_total IS NOT NULL OR worker_j... |
| Partial Target Drop | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | rate(api_requests_total[5m]) \|\|\| avg(node_load1) \|\|\| histogram_quantile(0.... | TS metrics-prometheus-* \| STATS api_requests_total_A = CASE((api_requests_total... |
| Native ESQL Errors | `barchart` → `bar` | migrated | **CORRECT** | FROM metrics-* \| WHERE service.name == "api" \| STATS errors = SUM(http.server.... | FROM metrics-* \| WHERE service.name == "api" \| STATS errors = SUM(http.server.... |
| Namespace Pod Count | `stat` → `datatable` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_pod_info) by (pod) | TS metrics-prometheus-* \| WHERE kube_pod_info IS NOT NULL \| STATS kube_pod_inf... |
| SLO Burn Rate | `gauge` → `gauge` | migrated | **CORRECT** | avg(slo_burn_rate) | TS metrics-prometheus-* \| WHERE slo_burn_rate IS NOT NULL \| STATS slo_burn_rat... |
| Service Error Logs | `logs` → `datatable` | migrated_with_warnings | **MINOR_ISSUE** | {service="api"} \|~ "timeout\|error" | FROM logs-* \| WHERE service == "api" \| WHERE message RLIKE ".*timeout\|error.*... |
| Coverage Notes | `text` → `markdown` | migrated | **EXPECTED_LIMITATION** | — | — |
| Legacy Alert List | `alertlist` → `` | skipped | **EXPECTED_LIMITATION** | — | — |

<details>
<summary>Detailed traces (8 panels)</summary>

#### Service Route Rate

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(rate(http_requests_total{status=~"2.."}[5m])) by (service, route)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE status RLIKE "2.."
| WHERE http_requests_total IS NOT NULL
| STATS http_requests_total = SUM(RATE(http_requests_total)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), service, route
| EVAL legend = CONCAT(COALESCE(TO_STRING(service), ""), " / ", COALESCE(TO_STRING(route), ""))
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `http_requests_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `service, route`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_requests_total`
- Output groups: `time_bucket, service, route`

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=32, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Verdict:** CORRECT

#### Queue Depth Bullet

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (bargauge):**

```
avg(queue_depth)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel` → approximated bargauge panel

**Translated (gauge):**

```
TS metrics-prometheus-*
| WHERE queue_depth IS NOT NULL
| STATS queue_depth = AVG(queue_depth) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| STATS queue_depth = LAST(queue_depth, time_bucket)
| KEEP queue_depth
| EVAL _gauge_min = 0, _gauge_max = 500, _gauge_goal = 300
```

**Query IR:**

- Family: `simple_agg`
- Metric: `queue_depth`
- Outer agg: `avg`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `queue_depth`
- Semantic losses: Approximated bargauge as a bullet gauge

**Visual IR:**

- Kibana type: `gauge`
- Layout: x=32, y=0, w=16, h=12
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated bargauge as a bullet gauge

**Semantic losses:** Approximated bargauge as a bullet gauge

**Verdict:** MINOR_ISSUE

#### Merged Request Streams

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
rate(frontend_requests_total[5m]) ||| rate(worker_jobs_total[5m])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE frontend_requests_total IS NOT NULL OR worker_jobs_total IS NOT NULL
| STATS frontend = RATE(frontend_requests_total), worker = RATE(worker_jobs_total) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| KEEP time_bucket, frontend, worker
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `frontend_requests_total`
- Range func: `rate`
- Range window: `5m`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `frontend`
- Output groups: `time_bucket`

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Verdict:** CORRECT

#### Partial Target Drop

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
rate(api_requests_total[5m]) ||| avg(node_load1) ||| histogram_quantile(0.95, rate(api_request_duration_seconds_bucket[5m]))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| STATS api_requests_total_A = CASE((api_requests_total IS NOT NULL), RATE(api_requests_total), NULL), node_load1_B = AVG(CASE((node_load1 IS NOT NULL), node_load1, NULL)), api_request_duration_seconds_C = PERCENTILE(CASE((api_request_duration_seconds IS NOT NULL), api_request_duration_seconds, NULL), 95) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| EVAL api = api_requests_total_A
| EVAL load = node_load1_B
| EVAL p95 = api_request_duration_seconds_C
| KEEP time_bucket, api, load, p95
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `api_requests_total`
- Range func: `rate`
- Range window: `5m`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `api`
- Output groups: `time_bucket`
- Semantic losses: histogram_quantile translated to an ES|QL PERCENTILE() aggregation; this is approximate — PERCENTILE uses t-digest, which treats histogram buckets as point masses rather than interpolating within them as Prometheus does, so results can diverge noticeably when traffic concentrates in a few wide buckets (the common latency shape). Prefer a target on ES >= 9.5 (native histogram_quantile) for exact results.

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=0, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 3

**Warnings:** Fused multi-target panel from independently translated ES|QL queries; histogram_quantile target field type could not be determined; assumed exponential_histogram and emitted PERCENTILE(). If the field is a classic histogram, pin the mapping or re-run with field capabilities so TO_TDIGEST() is used; histogram_quantile translated to an ES|QL PERCENTILE() aggregation; this is approximate — PERCENTILE uses t-digest, which treats histogram buckets as point masses rather than interpolating within them as Prometheus does, so results can diverge noticeably when traffic concentrates in a few wide buckets (the common latency shape). Prefer a target on ES >= 9.5 (native histogram_quantile) for exact results.

**Semantic losses:** histogram_quantile translated to an ES|QL PERCENTILE() aggregation; this is approximate — PERCENTILE uses t-digest, which treats histogram buckets as point masses rather than interpolating within them as Prometheus does, so results can diverge noticeably when traffic concentrates in a few wide buckets (the common latency shape). Prefer a target on ES >= 9.5 (native histogram_quantile) for exact results.

**Verdict:** MINOR_ISSUE

#### Native ESQL Errors

**Translation path:** `native_esql` · **Query language:** `esql` · **Readiness:** `elastic_ready`

**Source (barchart):**

```
FROM metrics-* | WHERE service.name == "api" | STATS errors = SUM(http.server.errors) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), service.name | SORT time_bucket ASC
```

**Translated (bar):**

```
FROM metrics-* | WHERE service.name == "api" | STATS errors = SUM(http.server.errors) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), service.name | SORT time_bucket ASC
```

**Query IR:**

- Family: `native_esql`
- Output shape: `time_series`
- Source lang: `esql`
- Target index: `metrics-*`
- Output metric: `errors`
- Output groups: `time_bucket, service.name`

**Visual IR:**

- Kibana type: `bar`
- Layout: x=0, y=12, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `esql`

**Inventory:**

- targets: 1

**Verdict:** CORRECT

#### Namespace Pod Count

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
sum(kube_pod_info) by (pod)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → approximated grouped stat as datatable

**Translated (datatable):**

```
TS metrics-prometheus-*
| WHERE kube_pod_info IS NOT NULL
| STATS kube_pod_info = SUM(kube_pod_info) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), pod
| STATS kube_pod_info = LAST(kube_pod_info, time_bucket) BY pod
| KEEP pod, kube_pod_info
```

**Query IR:**

- Family: `simple_agg`
- Metric: `kube_pod_info`
- Outer agg: `sum`
- Group labels: `pod`
- Output shape: `table`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kube_pod_info`
- Output groups: `pod`
- Semantic losses: Approximated grouped stat panel as summary table

**Visual IR:**

- Kibana type: `datatable`
- Layout: x=24, y=12, w=12, h=12
- Presentation kind: `esql`
- Config keys: type, query, metrics, breakdowns

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated grouped stat panel as summary table

**Semantic losses:** Approximated grouped stat panel as summary table

**Verdict:** MINOR_ISSUE

#### SLO Burn Rate

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (gauge):**

```
avg(slo_burn_rate)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
TS metrics-prometheus-*
| WHERE slo_burn_rate IS NOT NULL
| STATS slo_burn_rate = AVG(slo_burn_rate) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| STATS slo_burn_rate = LAST(slo_burn_rate, time_bucket)
| KEEP slo_burn_rate
| EVAL _gauge_min = 0, _gauge_max = 100, _gauge_goal = 2
```

**Query IR:**

- Family: `simple_agg`
- Metric: `slo_burn_rate`
- Outer agg: `avg`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `slo_burn_rate`

**Visual IR:**

- Kibana type: `gauge`
- Layout: x=36, y=12, w=12, h=12
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Verdict:** CORRECT

#### Service Error Logs

**Translation path:** `logql` · **Query language:** `logql` · **Readiness:** `logs_fielding_needed`

**Source (logs):**

```
{service="api"} |~ "timeout|error"
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=logql_stream backend=regex
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family logql_stream bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family` → translated LogQL logs query
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel`
- `panel_translators` / `datatable_panel` → mapped to datatable panel

**Translated (datatable):**

```
FROM logs-*
| WHERE service == "api"
| WHERE message RLIKE ".*timeout|error.*"
| KEEP @timestamp, service, message
| SORT @timestamp DESC
| LIMIT 200
```

**Query IR:**

- Family: `logql_stream`
- Metric: `message`
- Output shape: `event_rows`
- Source lang: `logql`
- Target index: `logs-*`
- Output metric: `message`
- Output groups: `@timestamp, service`
- Semantic losses: Approximated Loki logs panel as an ES|QL datatable

**Visual IR:**

- Kibana type: `datatable`
- Layout: x=0, y=24, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, metrics, breakdowns

**Operational IR:**

- Query language: `logql`

**Inventory:**

- targets: 1

**Warnings:** Approximated Loki logs panel as an ES|QL datatable

**Semantic losses:** Approximated Loki logs panel as an ES|QL datatable

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Controls / Variables (2)</summary>

- `service` (type: `esql`)
- `namespace` (type: `esql`)

</details>

---

### Grafana: Node Exporter Full

**File:** `node-exporter-full.json` — **Panels:** 133

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
| Pressure | `bargauge` → `bar` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_pressure_cpu_waiting_seconds_total{instance="$node",job="$job"}[$__ra... | TS metrics-prometheus-* \| WHERE node_pressure_cpu_waiting_seconds_total IS NOT ... |
| CPU Busy | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | 100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle", instance="$node"}[$__rat... | TS metrics-prometheus-* \| WHERE mode == "idle" \| WHERE node_cpu_seconds_total ... |
| Sys Load | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | scalar(node_load1{instance="$node",job="$job"}) * 100 / count(count(node_cpu_sec... | TS metrics-prometheus-* \| WHERE node_load1 IS NOT NULL OR node_cpu_seconds_tota... |
| RAM Used | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | (1 - (node_memory_MemAvailable_bytes{instance="$node", job="$job"} / node_memory... | TS metrics-prometheus-* \| WHERE node_memory_MemAvailable_bytes IS NOT NULL OR n... |
| SWAP Used | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | ((node_memory_SwapTotal_bytes{instance="$node",job="$job"} - node_memory_SwapFre... | TS metrics-prometheus-* \| WHERE node_memory_SwapTotal_bytes IS NOT NULL OR node... |
| Root FS Used | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | 100 - ((node_filesystem_avail_bytes{instance="$node",job="$job",mountpoint="/",f... | TS metrics-prometheus-* \| WHERE mountpoint == "/" \| WHERE (fstype != "rootfs" ... |
| CPU Cores | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu)) | FROM metrics-prometheus-* \| WHERE node_cpu_seconds_total IS NOT NULL \| STATS n... |
| Uptime | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | node_time_seconds{instance="$node",job="$job"} - node_boot_time_seconds{instance... | TS metrics-prometheus-* \| WHERE node_time_seconds IS NOT NULL OR node_boot_time... |
| RootFS Total | `stat` → `metric` | migrated | **MINOR_ISSUE** | node_filesystem_size_bytes{instance="$node",job="$job",mountpoint="/",fstype!="r... | TS metrics-prometheus-* \| WHERE mountpoint == "/" \| WHERE (fstype != "rootfs" ... |
| RAM Total | `stat` → `metric` | migrated | **MINOR_ISSUE** | node_memory_MemTotal_bytes{instance="$node",job="$job"} | TS metrics-prometheus-* \| WHERE node_memory_MemTotal_bytes IS NOT NULL \| STATS... |
| SWAP Total | `stat` → `metric` | migrated | **MINOR_ISSUE** | node_memory_SwapTotal_bytes{instance="$node",job="$job"} | TS metrics-prometheus-* \| WHERE node_memory_SwapTotal_bytes IS NOT NULL \| STAT... |
| CPU Basic | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | sum(irate(node_cpu_seconds_total{instance="$node",job="$job", mode="system"}[$__... | TS metrics-prometheus-* \| WHERE node_cpu_seconds_total IS NOT NULL \| STATS nod... |
| Memory Basic | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_MemTotal_bytes{instance="$node",job="$job"} \|\|\| node_memory_MemTo... | TS metrics-prometheus-* \| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_... |
| Network Traffic Basic | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_network_receive_bytes_total{instance="$node",job="$job"}[$__rate_inte... | TS metrics-prometheus-* \| WHERE node_network_receive_bytes_total IS NOT NULL OR... |
| Disk Space Used Basic | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | 100 - ((node_filesystem_avail_bytes{instance="$node",job="$job",device!~'rootfs'... | TS metrics-prometheus-* \| WHERE (NOT (device RLIKE "rootfs") OR (device IS NULL... |
| CPU | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | sum(irate(node_cpu_seconds_total{instance="$node",job="$job", mode="system"}[$__... | TS metrics-prometheus-* \| WHERE node_cpu_seconds_total IS NOT NULL \| STATS nod... |
| Memory Stack | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_MemTotal_bytes{instance="$node",job="$job"} - node_memory_MemFree_by... | TS metrics-prometheus-* \| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_... |
| Network Traffic | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_network_receive_bytes_total{instance="$node",job="$job"}[$__rate_inte... | TS metrics-prometheus-* \| WHERE node_network_receive_bytes_total IS NOT NULL OR... |
| Disk Space Used | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_filesystem_size_bytes{instance="$node",job="$job",device!~'rootfs'} - node_... | TS metrics-prometheus-* \| WHERE (NOT (device RLIKE "rootfs") OR (device IS NULL... |
| Disk IOps | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_disk_reads_completed_total{instance="$node",job="$job",device=~"$disk... | TS metrics-prometheus-* \| WHERE node_disk_reads_completed_total IS NOT NULL OR ... |
| I/O Usage Read / Write | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_disk_read_bytes_total{instance="$node",job="$job",device=~"$diskdevic... | TS metrics-prometheus-* \| WHERE node_disk_read_bytes_total IS NOT NULL OR node_... |
| I/O Utilization | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_disk_io_time_seconds_total{instance="$node",job="$job",device=~"$disk... | TS metrics-prometheus-* \| WHERE node_disk_io_time_seconds_total IS NOT NULL \| ... |
| CPU spent seconds in guests (VMs) | `timeseries` → `bar` | migrated_with_warnings | **MINOR_ISSUE** | sum by(instance) (irate(node_cpu_guest_seconds_total{instance="$node",job="$job"... | TS metrics-prometheus-* \| STATS numerator_A = SUM(CASE((mode == "user"), IRATE(... |
| Memory Active / Inactive | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_Inactive_bytes{instance="$node",job="$job"} \|\|\| node_memory_Activ... | TS metrics-prometheus-* \| WHERE node_memory_Inactive_bytes IS NOT NULL OR node_... |
| Memory Committed | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_Committed_AS_bytes{instance="$node",job="$job"} \|\|\| node_memory_C... | TS metrics-prometheus-* \| WHERE node_memory_Committed_AS_bytes IS NOT NULL OR n... |
| Memory Active / Inactive Detail | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_Inactive_file_bytes{instance="$node",job="$job"} \|\|\| node_memory_... | TS metrics-prometheus-* \| WHERE node_memory_Inactive_file_bytes IS NOT NULL OR ... |
| Memory Writeback and Dirty | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_Writeback_bytes{instance="$node",job="$job"} \|\|\| node_memory_Writ... | TS metrics-prometheus-* \| WHERE node_memory_Writeback_bytes IS NOT NULL OR node... |
| Memory Shared and Mapped | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_Mapped_bytes{instance="$node",job="$job"} \|\|\| node_memory_Shmem_b... | TS metrics-prometheus-* \| WHERE node_memory_Mapped_bytes IS NOT NULL OR node_me... |
| Memory Slab | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_SUnreclaim_bytes{instance="$node",job="$job"} \|\|\| node_memory_SRe... | TS metrics-prometheus-* \| WHERE node_memory_SUnreclaim_bytes IS NOT NULL OR nod... |
| Memory Vmalloc | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_VmallocChunk_bytes{instance="$node",job="$job"} \|\|\| node_memory_V... | TS metrics-prometheus-* \| WHERE node_memory_VmallocChunk_bytes IS NOT NULL OR n... |
| Memory Bounce | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_Bounce_bytes{instance="$node",job="$job"} | TS metrics-prometheus-* \| WHERE node_memory_Bounce_bytes IS NOT NULL \| STATS n... |
| Memory Anonymous | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_AnonHugePages_bytes{instance="$node",job="$job"} \|\|\| node_memory_... | TS metrics-prometheus-* \| WHERE node_memory_AnonHugePages_bytes IS NOT NULL OR ... |
| Memory Kernel / CPU | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_KernelStack_bytes{instance="$node",job="$job"} \|\|\| node_memory_Pe... | TS metrics-prometheus-* \| WHERE node_memory_KernelStack_bytes IS NOT NULL OR no... |
| Memory HugePages Counter | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_HugePages_Free{instance="$node",job="$job"} \|\|\| node_memory_HugeP... | TS metrics-prometheus-* \| WHERE node_memory_HugePages_Free IS NOT NULL OR node_... |
| Memory HugePages Size | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_HugePages_Total{instance="$node",job="$job"} \|\|\| node_memory_Huge... | TS metrics-prometheus-* \| WHERE node_memory_HugePages_Total IS NOT NULL OR node... |
| Memory DirectMap | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_DirectMap1G_bytes{instance="$node",job="$job"} \|\|\| node_memory_Di... | TS metrics-prometheus-* \| WHERE node_memory_DirectMap1G_bytes IS NOT NULL OR no... |
| Memory Unevictable and MLocked | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_Unevictable_bytes{instance="$node",job="$job"} \|\|\| node_memory_Ml... | TS metrics-prometheus-* \| WHERE node_memory_Unevictable_bytes IS NOT NULL OR no... |
| Memory NFS | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_NFS_Unstable_bytes{instance="$node",job="$job"} | TS metrics-prometheus-* \| WHERE node_memory_NFS_Unstable_bytes IS NOT NULL \| S... |
| Memory Pages In / Out | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_vmstat_pgpgin{instance="$node",job="$job"}[$__rate_interval]) \|\|\| ... | TS metrics-prometheus-* \| WHERE node_vmstat_pgpgin IS NOT NULL OR node_vmstat_p... |
| Memory Pages Swap In / Out | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_vmstat_pswpin{instance="$node",job="$job"}[$__rate_interval]) \|\|\| ... | TS metrics-prometheus-* \| WHERE node_vmstat_pswpin IS NOT NULL OR node_vmstat_p... |
| Memory Page Faults | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_vmstat_pgfault{instance="$node",job="$job"}[$__rate_interval]) \|\|\|... | TS metrics-prometheus-* \| WHERE node_vmstat_pgfault IS NOT NULL OR node_vmstat_... |
| OOM Killer | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_vmstat_oom_kill{instance="$node",job="$job"}[$__rate_interval]) | TS metrics-prometheus-* \| WHERE node_vmstat_oom_kill IS NOT NULL \| STATS node_... |
| Time Synchronized Drift | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_timex_estimated_error_seconds{instance="$node",job="$job"} \|\|\| node_time... | TS metrics-prometheus-* \| WHERE node_timex_estimated_error_seconds IS NOT NULL ... |
| Time PLL Adjust | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_timex_loop_time_constant{instance="$node",job="$job"} | TS metrics-prometheus-* \| WHERE node_timex_loop_time_constant IS NOT NULL \| ST... |
| Time Synchronized Status | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_timex_sync_status{instance="$node",job="$job"} \|\|\| node_timex_frequency_... | TS metrics-prometheus-* \| WHERE node_timex_sync_status IS NOT NULL OR node_time... |
| Time Misc | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_timex_tick_seconds{instance="$node",job="$job"} \|\|\| node_timex_tai_offse... | TS metrics-prometheus-* \| WHERE node_timex_tick_seconds IS NOT NULL OR node_tim... |
| Processes Status | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_procs_blocked{instance="$node",job="$job"} \|\|\| node_procs_running{instan... | TS metrics-prometheus-* \| WHERE node_procs_blocked IS NOT NULL OR node_procs_ru... |
| Processes State | `timeseries` → `area` | migrated | **MINOR_ISSUE** | node_processes_state{instance="$node",job="$job"} | TS metrics-prometheus-* \| WHERE node_processes_state IS NOT NULL \| STATS node_... |
| Processes  Forks | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_forks_total{instance="$node",job="$job"}[$__rate_interval]) | TS metrics-prometheus-* \| WHERE node_forks_total IS NOT NULL \| STATS node_fork... |
| Processes Memory | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(process_virtual_memory_bytes{instance="$node",job="$job"}[$__rate_interval... | TS metrics-prometheus-* \| WHERE process_virtual_memory_bytes IS NOT NULL OR pro... |
| PIDs Number and Limit | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_processes_pids{instance="$node",job="$job"} \|\|\| node_processes_max_proce... | TS metrics-prometheus-* \| WHERE node_processes_pids IS NOT NULL OR node_process... |
| Process schedule stats Running / Waiting | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_schedstat_running_seconds_total{instance="$node",job="$job"}[$__rate_... | TS metrics-prometheus-* \| WHERE node_schedstat_running_seconds_total IS NOT NUL... |
| Threads Number and Limit | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_processes_threads{instance="$node",job="$job"} \|\|\| node_processes_max_th... | TS metrics-prometheus-* \| WHERE node_processes_threads IS NOT NULL OR node_proc... |
| Context Switches / Interrupts | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_context_switches_total{instance="$node",job="$job"}[$__rate_interval]... | TS metrics-prometheus-* \| WHERE node_context_switches_total IS NOT NULL OR node... |
| System Load | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_load1{instance="$node",job="$job"} \|\|\| node_load5{instance="$node",job="... | TS metrics-prometheus-* \| WHERE node_load1 IS NOT NULL OR node_load5 IS NOT NUL... |
| CPU Frequency Scaling | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_cpu_scaling_frequency_hertz{instance="$node",job="$job"} \|\|\| avg(node_cp... | TS metrics-prometheus-* \| WHERE node_cpu_scaling_frequency_hertz IS NOT NULL OR... |
| Pressure Stall Information | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | rate(node_pressure_cpu_waiting_seconds_total{instance="$node",job="$job"}[$__rat... | TS metrics-prometheus-* \| WHERE node_pressure_cpu_waiting_seconds_total IS NOT ... |
| Interrupts Detail | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_interrupts_total{instance="$node",job="$job"}[$__rate_interval]) | TS metrics-prometheus-* \| WHERE node_interrupts_total IS NOT NULL \| STATS node... |
| Schedule timeslices executed by each cpu | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_schedstat_timeslices_total{instance="$node",job="$job"}[$__rate_inter... | TS metrics-prometheus-* \| WHERE node_schedstat_timeslices_total IS NOT NULL \| ... |
| Entropy | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_entropy_available_bits{instance="$node",job="$job"} | TS metrics-prometheus-* \| WHERE node_entropy_available_bits IS NOT NULL \| STAT... |
| CPU time spent in user and system contexts | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(process_cpu_seconds_total{instance="$node",job="$job"}[$__rate_interval]) | TS metrics-prometheus-* \| WHERE process_cpu_seconds_total IS NOT NULL \| STATS ... |
| File Descriptors | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | process_max_fds{instance="$node",job="$job"} \|\|\| process_open_fds{instance="$... | TS metrics-prometheus-* \| WHERE process_max_fds IS NOT NULL OR process_open_fds... |
| Hardware temperature monitor | `timeseries` → `line` | migrated | **MINOR_ISSUE** | node_hwmon_temp_celsius{instance="$node",job="$job"} * on(chip) group_left(chip_... | TS metrics-prometheus-* \| WHERE node_hwmon_temp_celsius IS NOT NULL OR node_hwm... |
| Throttle cooling device | `timeseries` → `line` | migrated | **MINOR_ISSUE** | node_cooling_device_cur_state{instance="$node",job="$job"} \|\|\| node_cooling_d... | TS metrics-prometheus-* \| WHERE node_cooling_device_cur_state IS NOT NULL OR no... |
| Power supply | `timeseries` → `line` | migrated | **MINOR_ISSUE** | node_power_supply_online{instance="$node",job="$job"} | TS metrics-prometheus-* \| WHERE node_power_supply_online IS NOT NULL \| STATS n... |
| Systemd Sockets | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_systemd_socket_accepted_connections_total{instance="$node",job="$job"... | TS metrics-prometheus-* \| WHERE node_systemd_socket_accepted_connections_total ... |
| Systemd Units State | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_systemd_units{instance="$node",job="$job",state="activating"} \|\|\| node_s... | TS metrics-prometheus-* \| WHERE node_systemd_units IS NOT NULL \| STATS Activat... |
| Disk IOps Completed | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_disk_reads_completed_total{instance="$node",job="$job"}[$__rate_inter... | TS metrics-prometheus-* \| WHERE node_disk_reads_completed_total IS NOT NULL OR ... |
| Disk R/W Data | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_disk_read_bytes_total{instance="$node",job="$job"}[$__rate_interval])... | TS metrics-prometheus-* \| WHERE node_disk_read_bytes_total IS NOT NULL OR node_... |
| Disk Average Wait Time | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_disk_read_time_seconds_total{instance="$node",job="$job"}[$__rate_int... | TS metrics-prometheus-* \| WHERE node_disk_read_time_seconds_total IS NOT NULL O... |
| Average Queue Size | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_disk_io_time_weighted_seconds_total{instance="$node",job="$job"}[$__r... | TS metrics-prometheus-* \| WHERE node_disk_io_time_weighted_seconds_total IS NOT... |
| Disk R/W Merged | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_disk_reads_merged_total{instance="$node",job="$job"}[$__rate_interval... | TS metrics-prometheus-* \| WHERE node_disk_reads_merged_total IS NOT NULL OR nod... |
| Time Spent Doing I/Os | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_disk_io_time_seconds_total{instance="$node",job="$job"}[$__rate_inter... | TS metrics-prometheus-* \| WHERE node_disk_io_time_seconds_total IS NOT NULL OR ... |
| Instantaneous Queue Size | `timeseries` → `line` | migrated | **MINOR_ISSUE** | node_disk_io_now{instance="$node",job="$job"} | TS metrics-prometheus-* \| WHERE node_disk_io_now IS NOT NULL \| STATS node_disk... |
| Disk IOps Discards completed / merged | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_disk_discards_completed_total{instance="$node",job="$job"}[$__rate_in... | TS metrics-prometheus-* \| WHERE node_disk_discards_completed_total IS NOT NULL ... |
| Filesystem space available | `timeseries` → `line` | migrated | **MINOR_ISSUE** | node_filesystem_avail_bytes{instance="$node",job="$job",device!~'rootfs'} | TS metrics-prometheus-* \| WHERE (NOT (device RLIKE "rootfs") OR (device IS NULL... |
| File Nodes Free | `timeseries` → `line` | migrated | **MINOR_ISSUE** | node_filesystem_files_free{instance="$node",job="$job",device!~'rootfs'} | TS metrics-prometheus-* \| WHERE (NOT (device RLIKE "rootfs") OR (device IS NULL... |
| File Descriptor | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_filefd_maximum{instance="$node",job="$job"} \|\|\| node_filefd_allocated{in... | TS metrics-prometheus-* \| WHERE node_filefd_maximum IS NOT NULL OR node_filefd_... |
| File Nodes Size | `timeseries` → `line` | migrated | **MINOR_ISSUE** | node_filesystem_files{instance="$node",job="$job",device!~'rootfs'} | TS metrics-prometheus-* \| WHERE (NOT (device RLIKE "rootfs") OR (device IS NULL... |
| Filesystem in ReadOnly / Error | `timeseries` → `area` | migrated | **MINOR_ISSUE** | node_filesystem_readonly{instance="$node",job="$job",device!~'rootfs'} \|\|\| no... | TS metrics-prometheus-* \| WHERE (NOT (device RLIKE "rootfs") OR (device IS NULL... |
| Network Traffic by Packets | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_network_receive_packets_total{instance="$node",job="$job"}[$__rate_in... | TS metrics-prometheus-* \| WHERE node_network_receive_packets_total IS NOT NULL ... |
| Network Traffic Errors | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_network_receive_errs_total{instance="$node",job="$job"}[$__rate_inter... | TS metrics-prometheus-* \| WHERE node_network_receive_errs_total IS NOT NULL OR ... |
| Network Traffic Drop | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_network_receive_drop_total{instance="$node",job="$job"}[$__rate_inter... | TS metrics-prometheus-* \| WHERE node_network_receive_drop_total IS NOT NULL OR ... |
| Network Traffic Compressed | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_network_receive_compressed_total{instance="$node",job="$job"}[$__rate... | TS metrics-prometheus-* \| WHERE node_network_receive_compressed_total IS NOT NU... |
| Network Traffic Multicast | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_network_receive_multicast_total{instance="$node",job="$job"}[$__rate_... | TS metrics-prometheus-* \| WHERE node_network_receive_multicast_total IS NOT NUL... |
| Network Traffic Fifo | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_network_receive_fifo_total{instance="$node",job="$job"}[$__rate_inter... | TS metrics-prometheus-* \| WHERE node_network_receive_fifo_total IS NOT NULL OR ... |
| Network Traffic Frame | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_network_receive_frame_total{instance="$node",job="$job"}[$__rate_inte... | TS metrics-prometheus-* \| WHERE node_network_receive_frame_total IS NOT NULL \|... |
| Network Traffic Carrier | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_network_transmit_carrier_total{instance="$node",job="$job"}[$__rate_i... | TS metrics-prometheus-* \| WHERE node_network_transmit_carrier_total IS NOT NULL... |
| Network Traffic Colls | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_network_transmit_colls_total{instance="$node",job="$job"}[$__rate_int... | TS metrics-prometheus-* \| WHERE node_network_transmit_colls_total IS NOT NULL \... |
| NF Conntrack | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_nf_conntrack_entries{instance="$node",job="$job"} \|\|\| node_nf_conntrack_... | TS metrics-prometheus-* \| WHERE node_nf_conntrack_entries IS NOT NULL OR node_n... |
| ARP Entries | `timeseries` → `line` | migrated | **MINOR_ISSUE** | node_arp_entries{instance="$node",job="$job"} | TS metrics-prometheus-* \| WHERE node_arp_entries IS NOT NULL \| STATS node_arp_... |
| MTU | `timeseries` → `line` | migrated | **MINOR_ISSUE** | node_network_mtu_bytes{instance="$node",job="$job"} | TS metrics-prometheus-* \| WHERE node_network_mtu_bytes IS NOT NULL \| STATS nod... |
| Speed | `timeseries` → `line` | migrated | **MINOR_ISSUE** | node_network_speed_bytes{instance="$node",job="$job"} | TS metrics-prometheus-* \| WHERE node_network_speed_bytes IS NOT NULL \| STATS n... |
| Queue Length | `timeseries` → `line` | migrated | **MINOR_ISSUE** | node_network_transmit_queue_length{instance="$node",job="$job"} | TS metrics-prometheus-* \| WHERE node_network_transmit_queue_length IS NOT NULL ... |
| Softnet Packets | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_softnet_processed_total{instance="$node",job="$job"}[$__rate_interval... | TS metrics-prometheus-* \| WHERE node_softnet_processed_total IS NOT NULL OR nod... |
| Softnet Out of Quota | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_softnet_times_squeezed_total{instance="$node",job="$job"}[$__rate_int... | TS metrics-prometheus-* \| WHERE node_softnet_times_squeezed_total IS NOT NULL \... |
| Network Operational Status | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_network_up{operstate="up",instance="$node",job="$job"} \|\|\| node_network_... | TS metrics-prometheus-* \| STATS node_network_up_A = MAX(LAST_OVER_TIME(CASE((op... |
| Sockstat TCP | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_sockstat_TCP_alloc{instance="$node",job="$job"} \|\|\| node_sockstat_TCP_in... | TS metrics-prometheus-* \| WHERE node_sockstat_TCP_alloc IS NOT NULL OR node_soc... |
| Sockstat UDP | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_sockstat_UDPLITE_inuse{instance="$node",job="$job"} \|\|\| node_sockstat_UD... | TS metrics-prometheus-* \| WHERE node_sockstat_UDPLITE_inuse IS NOT NULL OR node... |
| Sockstat FRAG / RAW | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_sockstat_FRAG_inuse{instance="$node",job="$job"} \|\|\| node_sockstat_RAW_i... | TS metrics-prometheus-* \| WHERE node_sockstat_FRAG_inuse IS NOT NULL OR node_so... |
| Sockstat Memory Size | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_sockstat_TCP_mem_bytes{instance="$node",job="$job"} \|\|\| node_sockstat_UD... | TS metrics-prometheus-* \| WHERE node_sockstat_TCP_mem_bytes IS NOT NULL OR node... |
| Sockstat Used | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_sockstat_sockets_used{instance="$node",job="$job"} | TS metrics-prometheus-* \| WHERE node_sockstat_sockets_used IS NOT NULL \| STATS... |
| Netstat IP In / Out Octets | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_IpExt_InOctets{instance="$node",job="$job"}[$__rate_interval]... | TS metrics-prometheus-* \| WHERE node_netstat_IpExt_InOctets IS NOT NULL OR node... |
| Netstat IP Forwarding | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_Ip_Forwarding{instance="$node",job="$job"}[$__rate_interval]) | TS metrics-prometheus-* \| WHERE node_netstat_Ip_Forwarding IS NOT NULL \| STATS... |
| ICMP In / Out | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_Icmp_InMsgs{instance="$node",job="$job"}[$__rate_interval]) \... | TS metrics-prometheus-* \| WHERE node_netstat_Icmp_InMsgs IS NOT NULL OR node_ne... |
| ICMP Errors | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_Icmp_InErrors{instance="$node",job="$job"}[$__rate_interval]) | TS metrics-prometheus-* \| WHERE node_netstat_Icmp_InErrors IS NOT NULL \| STATS... |
| UDP In / Out | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_Udp_InDatagrams{instance="$node",job="$job"}[$__rate_interval... | TS metrics-prometheus-* \| WHERE node_netstat_Udp_InDatagrams IS NOT NULL OR nod... |
| UDP Errors | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_Udp_InErrors{instance="$node",job="$job"}[$__rate_interval]) ... | TS metrics-prometheus-* \| WHERE node_netstat_Udp_InErrors IS NOT NULL OR node_n... |
| TCP In / Out | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_Tcp_InSegs{instance="$node",job="$job"}[$__rate_interval]) \|... | TS metrics-prometheus-* \| WHERE node_netstat_Tcp_InSegs IS NOT NULL OR node_net... |
| TCP Errors | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_TcpExt_ListenOverflows{instance="$node",job="$job"}[$__rate_i... | TS metrics-prometheus-* \| WHERE node_netstat_TcpExt_ListenOverflows IS NOT NULL... |
| TCP Connections | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_netstat_Tcp_CurrEstab{instance="$node",job="$job"} \|\|\| node_netstat_Tcp_... | TS metrics-prometheus-* \| WHERE node_netstat_Tcp_CurrEstab IS NOT NULL OR node_... |
| TCP SynCookie | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_TcpExt_SyncookiesFailed{instance="$node",job="$job"}[$__rate_... | TS metrics-prometheus-* \| WHERE node_netstat_TcpExt_SyncookiesFailed IS NOT NUL... |
| TCP Direct Transition | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_netstat_Tcp_ActiveOpens{instance="$node",job="$job"}[$__rate_interval... | TS metrics-prometheus-* \| WHERE node_netstat_Tcp_ActiveOpens IS NOT NULL OR nod... |
| TCP Stat | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_tcp_connection_states{state="established",instance="$node",job="$job"} \|\|... | TS metrics-prometheus-* \| WHERE node_tcp_connection_states IS NOT NULL \| STATS... |
| Node Exporter Scrape Time | `timeseries` → `area` | migrated | **MINOR_ISSUE** | node_scrape_collector_duration_seconds{instance="$node",job="$job"} | TS metrics-prometheus-* \| WHERE node_scrape_collector_duration_seconds IS NOT N... |
| Node Exporter Scrape | `timeseries` → `line` | migrated | **MINOR_ISSUE** | node_scrape_collector_success{instance="$node",job="$job"} \|\|\| node_textfile_... | TS metrics-prometheus-* \| WHERE node_scrape_collector_success IS NOT NULL OR no... |
| Dashboard Links | `dashboard_links` → `links` | migrated | **EXPECTED_LIMITATION** | — | — |

<details>
<summary>Detailed traces (116 panels)</summary>

#### Pressure

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (bargauge):**

```
irate(node_pressure_cpu_waiting_seconds_total{instance="$node",job="$job"}[$__rate_interval]) ||| irate(node_pressure_memory_waiting_seconds_total{instance="$node",job="$job"}[$__rate_interval]) ||| irate(node_pressure_io_waiting_seconds_total{instance="$node",job="$job"}[$__rate_interval])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel` → approximated bargauge panel

**Translated (bar):**

```
TS metrics-prometheus-*
| WHERE node_pressure_cpu_waiting_seconds_total IS NOT NULL OR node_pressure_memory_waiting_seconds_total IS NOT NULL OR node_pressure_io_waiting_seconds_total IS NOT NULL
| STATS CPU = IRATE(node_pressure_cpu_waiting_seconds_total), Mem = IRATE(node_pressure_memory_waiting_seconds_total), I_O = IRATE(node_pressure_io_waiting_seconds_total) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| STATS CPU = MAX(CPU), Mem = MAX(Mem), I_O = MAX(I_O)
| KEEP CPU, Mem, I_O
| EVAL __labels = MV_APPEND(MV_APPEND("CPU", "Mem"), "I/O"), __values = MV_APPEND(MV_APPEND(COALESCE(TO_STRING(CPU), ""), COALESCE(TO_STRING(Mem), "")), COALESCE(TO_STRING(I_O), ""))
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
- Semantic losses: Dropped variable-driven label filters during migration, Approximated bargauge as bar chart

**Visual IR:**

- Kibana type: `bar`
- Layout: x=0, y=0, w=6, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 3
- has_description: True

**Warnings:** Grafana panel description is not carried into the migrated Kibana panel automatically; Approximated bargauge as bar chart

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated bargauge as bar chart

**Notes:** Grafana panel description is not carried into the migrated Kibana panel automatically

**Verdict:** MINOR_ISSUE

#### CPU Busy

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (gauge):**

```
100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle", instance="$node"}[$__rate_interval])))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
TS metrics-prometheus-*
| WHERE mode == "idle"
| WHERE node_cpu_seconds_total IS NOT NULL
| STATS node_cpu_seconds_total_mode_idle_rate_avg = AVG(RATE(node_cpu_seconds_total)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| EVAL computed_value = (100 * (1 - node_cpu_seconds_total_mode_idle_rate_avg))
| SORT time_bucket DESC
| LIMIT 2
| SORT time_bucket ASC
| LIMIT 1
| KEEP computed_value
| EVAL _gauge_min = 0, _gauge_max = 100, _gauge_goal = 85
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
- Layout: x=6, y=0, w=6, h=12
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- value_mappings: 1
- has_description: True

**Warnings:** Grafana panel has 1 value mapping(s) (e.g. 0 -> 'Down', null -> 'N/A'); Kibana panel mappings assign colors, not display text, so the raw value is shown instead; Grafana panel description is not carried into the migrated Kibana panel automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Notes:** Grafana panel has 1 value mapping(s) (e.g. 0 -> 'Down', null -> 'N/A'); Kibana panel mappings assign colors, not display text, so the raw value is shown instead; Grafana panel description is not carried into the migrated Kibana panel automatically

**Verdict:** MINOR_ISSUE

#### Sys Load

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (gauge):**

```
scalar(node_load1{instance="$node",job="$job"}) * 100 / count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
TS metrics-prometheus-*
| WHERE node_load1 IS NOT NULL OR node_cpu_seconds_total IS NOT NULL
| STATS node_load1_instance_job = AVG(LAST_OVER_TIME(node_load1)), node_cpu_seconds_total_instance_job_count = COUNT_DISTINCT(cpu) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| EVAL computed_value = ((node_load1_instance_job * 100) / node_cpu_seconds_total_instance_job_count)
| STATS computed_value = LAST(computed_value, time_bucket)
| KEEP computed_value
| EVAL _gauge_min = 0, _gauge_max = 100, _gauge_goal = 85
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `/`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Collapsed all series of `node_load1` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Approximated nested count(count()) as COUNT_DISTINCT(cpu)

**Visual IR:**

- Kibana type: `gauge`
- Layout: x=12, y=0, w=6, h=12
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- value_mappings: 1
- has_description: True

**Warnings:** Grafana panel has 1 value mapping(s) (e.g. 0 -> 'Down', null -> 'N/A'); Kibana panel mappings assign colors, not display text, so the raw value is shown instead; Grafana panel description is not carried into the migrated Kibana panel automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `node_load1` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Approximated nested count(count()) as COUNT_DISTINCT(cpu)

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `node_load1` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Approximated nested count(count()) as COUNT_DISTINCT(cpu)

**Notes:** Grafana panel has 1 value mapping(s) (e.g. 0 -> 'Down', null -> 'N/A'); Kibana panel mappings assign colors, not display text, so the raw value is shown instead; Grafana panel description is not carried into the migrated Kibana panel automatically

**Verdict:** MINOR_ISSUE

#### RAM Used

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (gauge):**

```
(1 - (node_memory_MemAvailable_bytes{instance="$node", job="$job"} / node_memory_MemTotal_bytes{instance="$node", job="$job"})) * 100
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemAvailable_bytes IS NOT NULL OR node_memory_MemTotal_bytes IS NOT NULL
| STATS node_memory_MemAvailable_bytes_instance_job = AVG(LAST_OVER_TIME(node_memory_MemAvailable_bytes)), node_memory_MemTotal_bytes_instance_job = AVG(LAST_OVER_TIME(node_memory_MemTotal_bytes)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| EVAL computed_value = ((1 - (node_memory_MemAvailable_bytes_instance_job / node_memory_MemTotal_bytes_instance_job)) * 100)
| STATS computed_value = LAST(computed_value, time_bucket)
| KEEP computed_value
| EVAL _gauge_min = 0, _gauge_max = 100, _gauge_goal = 80
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `*`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Collapsed all series of `node_memory_MemAvailable_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Collapsed all series of `node_memory_MemTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Visual IR:**

- Kibana type: `gauge`
- Layout: x=18, y=0, w=6, h=12
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2
- has_description: True

**Warnings:** Grafana panel description is not carried into the migrated Kibana panel automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `node_memory_MemAvailable_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_MemTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `node_memory_MemAvailable_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_MemTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Notes:** Grafana panel description is not carried into the migrated Kibana panel automatically

**Verdict:** MINOR_ISSUE

#### SWAP Used

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (gauge):**

```
((node_memory_SwapTotal_bytes{instance="$node",job="$job"} - node_memory_SwapFree_bytes{instance="$node",job="$job"}) / (node_memory_SwapTotal_bytes{instance="$node",job="$job"})) * 100
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
TS metrics-prometheus-*
| WHERE node_memory_SwapTotal_bytes IS NOT NULL OR node_memory_SwapFree_bytes IS NOT NULL
| STATS node_memory_SwapTotal_bytes_instance_job = AVG(LAST_OVER_TIME(node_memory_SwapTotal_bytes)), node_memory_SwapFree_bytes_instance_job = AVG(LAST_OVER_TIME(node_memory_SwapFree_bytes)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| EVAL computed_value = (((node_memory_SwapTotal_bytes_instance_job - node_memory_SwapFree_bytes_instance_job) / node_memory_SwapTotal_bytes_instance_job) * 100)
| STATS computed_value = LAST(computed_value, time_bucket)
| KEEP computed_value
| EVAL _gauge_min = 0, _gauge_max = 100, _gauge_goal = 10
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `*`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Collapsed all series of `node_memory_SwapTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Collapsed all series of `node_memory_SwapFree_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Visual IR:**

- Kibana type: `gauge`
- Layout: x=24, y=0, w=6, h=12
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- value_mappings: 1
- has_description: True

**Warnings:** Grafana panel has 1 value mapping(s) (e.g. 0 -> 'Down', null -> 'N/A'); Kibana panel mappings assign colors, not display text, so the raw value is shown instead; Grafana panel description is not carried into the migrated Kibana panel automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `node_memory_SwapTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_SwapFree_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `node_memory_SwapTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_SwapFree_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Notes:** Grafana panel has 1 value mapping(s) (e.g. 0 -> 'Down', null -> 'N/A'); Kibana panel mappings assign colors, not display text, so the raw value is shown instead; Grafana panel description is not carried into the migrated Kibana panel automatically

**Verdict:** MINOR_ISSUE

#### Root FS Used

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (gauge):**

```
100 - ((node_filesystem_avail_bytes{instance="$node",job="$job",mountpoint="/",fstype!="rootfs"} * 100) / node_filesystem_size_bytes{instance="$node",job="$job",mountpoint="/",fstype!="rootfs"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
TS metrics-prometheus-*
| WHERE mountpoint == "/"
| WHERE (fstype != "rootfs" OR (fstype IS NULL AND "" != "rootfs"))
| WHERE node_filesystem_avail_bytes IS NOT NULL OR node_filesystem_size_bytes IS NOT NULL
| STATS node_filesystem_avail_bytes_mountpoint_fstype_rootfs = AVG(LAST_OVER_TIME(node_filesystem_avail_bytes)), node_filesystem_size_bytes_mountpoint_fstype_rootfs = AVG(LAST_OVER_TIME(node_filesystem_size_bytes)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| EVAL computed_value = (100 - ((node_filesystem_avail_bytes_mountpoint_fstype_rootfs * 100) / node_filesystem_size_bytes_mountpoint_fstype_rootfs))
| STATS computed_value = LAST(computed_value, time_bucket)
| KEEP computed_value
| EVAL _gauge_min = 0, _gauge_max = 100, _gauge_goal = 80
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `-`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Collapsed all series of `node_filesystem_avail_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Collapsed all series of `node_filesystem_size_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Visual IR:**

- Kibana type: `gauge`
- Layout: x=30, y=0, w=6, h=12
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- value_mappings: 1
- has_description: True

**Warnings:** Grafana panel has 1 value mapping(s) (e.g. 0 -> 'Down', null -> 'N/A'); Kibana panel mappings assign colors, not display text, so the raw value is shown instead; Grafana panel description is not carried into the migrated Kibana panel automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `node_filesystem_avail_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_filesystem_size_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `node_filesystem_avail_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_filesystem_size_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Notes:** Grafana panel has 1 value mapping(s) (e.g. 0 -> 'Down', null -> 'N/A'); Kibana panel mappings assign colors, not display text, so the raw value is shown instead; Grafana panel description is not carried into the migrated Kibana panel automatically

**Verdict:** MINOR_ISSUE

#### CPU Cores

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=nested_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family nested_agg bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family` → translated nested count(count()) expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
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
- Layout: x=36, y=0, w=4, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- value_mappings: 1
- has_description: True

**Warnings:** Grafana panel has 1 value mapping(s) (e.g. 0 -> 'Down', null -> 'N/A'); Kibana panel mappings assign colors, not display text, so the raw value is shown instead; Grafana panel description is not carried into the migrated Kibana panel automatically; Approximated nested count(count()) as COUNT_DISTINCT(cpu)

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated nested count(count()) as COUNT_DISTINCT(cpu)

**Notes:** Grafana panel has 1 value mapping(s) (e.g. 0 -> 'Down', null -> 'N/A'); Kibana panel mappings assign colors, not display text, so the raw value is shown instead; Grafana panel description is not carried into the migrated Kibana panel automatically

**Verdict:** MINOR_ISSUE

#### Uptime

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
node_time_seconds{instance="$node",job="$job"} - node_boot_time_seconds{instance="$node",job="$job"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE node_time_seconds IS NOT NULL OR node_boot_time_seconds IS NOT NULL
| STATS node_time_seconds_instance_job = AVG(LAST_OVER_TIME(node_time_seconds)), node_boot_time_seconds_instance_job = AVG(LAST_OVER_TIME(node_boot_time_seconds)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| EVAL computed_value = (node_time_seconds_instance_job - node_boot_time_seconds_instance_job)
| STATS computed_value = LAST(computed_value, time_bucket)
| KEEP computed_value
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `-`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Collapsed all series of `node_time_seconds` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Collapsed all series of `node_boot_time_seconds` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Visual IR:**

- Kibana type: `metric`
- Layout: x=40, y=0, w=8, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- value_mappings: 1
- has_description: True

**Warnings:** Grafana panel has 1 value mapping(s) (e.g. 0 -> 'Down', null -> 'N/A'); Kibana panel mappings assign colors, not display text, so the raw value is shown instead; Grafana panel description is not carried into the migrated Kibana panel automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `node_time_seconds` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_boot_time_seconds` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `node_time_seconds` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_boot_time_seconds` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Notes:** Grafana panel has 1 value mapping(s) (e.g. 0 -> 'Down', null -> 'N/A'); Kibana panel mappings assign colors, not display text, so the raw value is shown instead; Grafana panel description is not carried into the migrated Kibana panel automatically

**Verdict:** MINOR_ISSUE

#### RootFS Total

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
node_filesystem_size_bytes{instance="$node",job="$job",mountpoint="/",fstype!="rootfs"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE mountpoint == "/"
| WHERE (fstype != "rootfs" OR (fstype IS NULL AND "" != "rootfs"))
| WHERE node_filesystem_size_bytes IS NOT NULL
| STATS node_filesystem_size_bytes = MAX(LAST_OVER_TIME(node_filesystem_size_bytes)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| STATS node_filesystem_size_bytes = LAST(node_filesystem_size_bytes, time_bucket)
| KEEP node_filesystem_size_bytes
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
- Layout: x=36, y=6, w=4, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- value_mappings: 1
- has_description: True

**Warnings:** Grafana panel has 1 value mapping(s) (e.g. 0 -> 'Down', null -> 'N/A'); Kibana panel mappings assign colors, not display text, so the raw value is shown instead; Grafana panel description is not carried into the migrated Kibana panel automatically

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### RAM Total

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
node_memory_MemTotal_bytes{instance="$node",job="$job"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemTotal_bytes IS NOT NULL
| STATS node_memory_MemTotal_bytes = MAX(LAST_OVER_TIME(node_memory_MemTotal_bytes)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| STATS node_memory_MemTotal_bytes = LAST(node_memory_MemTotal_bytes, time_bucket)
| KEEP node_memory_MemTotal_bytes
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
- Layout: x=40, y=6, w=4, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- value_mappings: 1
- has_description: True

**Warnings:** Grafana panel has 1 value mapping(s) (e.g. 0 -> 'Down', null -> 'N/A'); Kibana panel mappings assign colors, not display text, so the raw value is shown instead; Grafana panel description is not carried into the migrated Kibana panel automatically

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### SWAP Total

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
node_memory_SwapTotal_bytes{instance="$node",job="$job"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE node_memory_SwapTotal_bytes IS NOT NULL
| STATS node_memory_SwapTotal_bytes = MAX(LAST_OVER_TIME(node_memory_SwapTotal_bytes)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| STATS node_memory_SwapTotal_bytes = LAST(node_memory_SwapTotal_bytes, time_bucket)
| KEEP node_memory_SwapTotal_bytes
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
- Layout: x=44, y=6, w=4, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- value_mappings: 1
- has_description: True

**Warnings:** Grafana panel has 1 value mapping(s) (e.g. 0 -> 'Down', null -> 'N/A'); Kibana panel mappings assign colors, not display text, so the raw value is shown instead; Grafana panel description is not carried into the migrated Kibana panel automatically

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### CPU Basic

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(irate(node_cpu_seconds_total{instance="$node",job="$job", mode="system"}[$__rate_interval])) / scalar(count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu))) ||| sum(irate(node_cpu_seconds_total{instance="$node",job="$job", mode="user"}[$__rate_interval])) / scalar(count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu))) ||| sum(irate(node_cpu_seconds_total{instance="$node",job="$job", mode="iowait"}[$__rate_interval])) / scalar(count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu))) ||| sum(irate(node_cpu_seconds_total{instance="$node",job="$job", mode=~".*irq"}[$__rate_interval])) / scalar(count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu))) ||| sum(irate(node_cpu_seconds_total{instance="$node",job="$job",  mode!='idle',mode!='user',mode!='system',mode!='iowait',mode!='irq',mode!='softirq'}[$__rate_interval])) / scalar(count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu))) ||| sum(irate(node_cpu_seconds_total{instance="$node",job="$job", mode="idle"}[$__rate_interval])) / scalar(count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu)))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE node_cpu_seconds_total IS NOT NULL
| STATS node_cpu_seconds_total_A_lhs = SUM(CASE((mode == "system"), IRATE(node_cpu_seconds_total), NULL)), node_cpu_seconds_total_A_rhs = COUNT_DISTINCT(cpu), node_cpu_seconds_total_B_lhs = SUM(CASE((mode == "user"), IRATE(node_cpu_seconds_total), NULL)), node_cpu_seconds_total_B_rhs = COUNT_DISTINCT(cpu), node_cpu_seconds_total_C_lhs = SUM(CASE((mode == "iowait"), IRATE(node_cpu_seconds_total), NULL)), node_cpu_seconds_total_C_rhs = COUNT_DISTINCT(cpu), node_cpu_seconds_total_D_lhs = SUM(CASE((mode RLIKE ".*irq"), IRATE(node_cpu_seconds_total), NULL)), node_cpu_seconds_total_D_rhs = COUNT_DISTINCT(cpu), node_cpu_seconds_total_E_lhs = SUM(CASE(((mode != "idle" OR (mode IS NULL AND "" != "idle"))) and ((mode != "user" OR (mode IS NULL AND "" != "user"))) and ((mode != "system" OR (mode IS NULL AND "" != "system"))) and ((mode != "iowait" OR (mode IS NULL AND "" != "iowait"))) and ((mode != "irq" OR (mode IS NULL AND "" != "irq"))) and ((mode != "softirq" OR (mode IS NULL AND "" != "softirq"))), IRATE(node_cpu_seconds_total), NULL)), node_cpu_seconds_total_E_rhs = COUNT_DISTINCT(cpu), node_cpu_seconds_total_F_lhs = SUM(CASE((mode == "idle"), IRATE(node_cpu_seconds_total), NULL)), node_cpu_seconds_total_F_rhs = COUNT_DISTINCT(cpu) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| EVAL Busy_System = (node_cpu_seconds_total_A_lhs / node_cpu_seconds_total_A_rhs)
| EVAL Busy_User = (node_cpu_seconds_total_B_lhs / node_cpu_seconds_total_B_rhs)
| EVAL Busy_Iowait = (node_cpu_seconds_total_C_lhs / node_cpu_seconds_total_C_rhs)
| EVAL Busy_IRQs = (node_cpu_seconds_total_D_lhs / node_cpu_seconds_total_D_rhs)
| EVAL Busy_Other = (node_cpu_seconds_total_E_lhs / node_cpu_seconds_total_E_rhs)
| EVAL Idle = (node_cpu_seconds_total_F_lhs / node_cpu_seconds_total_F_rhs)
| KEEP time_bucket, Busy_System, Busy_User, Busy_Iowait, Busy_IRQs, Busy_Other, Idle
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `/`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `Busy_System`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration, Approximated nested count(count()) as COUNT_DISTINCT(cpu)

**Visual IR:**

- Kibana type: `area`
- Layout: x=0, y=0, w=24, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 6
- field_overrides: 7
- has_description: True

**Warnings:** Grafana panel has 7 field override(s); verify visual mappings manually; Grafana panel description is not carried into the migrated Kibana panel automatically; Approximated nested count(count()) as COUNT_DISTINCT(cpu)

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated nested count(count()) as COUNT_DISTINCT(cpu)

**Notes:** Grafana panel has 7 field override(s); verify visual mappings manually; Grafana panel description is not carried into the migrated Kibana panel automatically

**Verdict:** MINOR_ISSUE

#### Memory Basic

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
node_memory_MemTotal_bytes{instance="$node",job="$job"} ||| node_memory_MemTotal_bytes{instance="$node",job="$job"} - node_memory_MemFree_bytes{instance="$node",job="$job"} - (node_memory_Cached_bytes{instance="$node",job="$job"} + node_memory_Buffers_bytes{instance="$node",job="$job"} + node_memory_SReclaimable_bytes{instance="$node",job="$job"}) ||| node_memory_Cached_bytes{instance="$node",job="$job"} + node_memory_Buffers_bytes{instance="$node",job="$job"} + node_memory_SReclaimable_bytes{instance="$node",job="$job"} ||| node_memory_MemFree_bytes{instance="$node",job="$job"} ||| (node_memory_SwapTotal_bytes{instance="$node",job="$job"} - node_memory_SwapFree_bytes{instance="$node",job="$job"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_memory_Cached_bytes IS NOT NULL OR node_memory_Buffers_bytes IS NOT NULL OR node_memory_SReclaimable_bytes IS NOT NULL OR node_memory_MemFree_bytes IS NOT NULL OR node_memory_SwapTotal_bytes IS NOT NULL OR node_memory_SwapFree_bytes IS NOT NULL
| STATS RAM_Total = AVG(LAST_OVER_TIME(node_memory_MemTotal_bytes)), node_memory_Cached_bytes_B_rhs_lhs_lhs = AVG(LAST_OVER_TIME(node_memory_Cached_bytes)), node_memory_Buffers_bytes_B_rhs_lhs_rhs = AVG(LAST_OVER_TIME(node_memory_Buffers_bytes)), node_memory_SReclaimable_bytes_B_rhs_rhs = AVG(LAST_OVER_TIME(node_memory_SReclaimable_bytes)), node_memory_Cached_bytes_C_lhs_lhs = AVG(LAST_OVER_TIME(node_memory_Cached_bytes)), node_memory_Buffers_bytes_C_lhs_rhs = AVG(LAST_OVER_TIME(node_memory_Buffers_bytes)), node_memory_SReclaimable_bytes_C_rhs = AVG(LAST_OVER_TIME(node_memory_SReclaimable_bytes)), RAM_Free = AVG(LAST_OVER_TIME(node_memory_MemFree_bytes)), node_memory_SwapTotal_bytes_E_lhs = AVG(LAST_OVER_TIME(node_memory_SwapTotal_bytes)), node_memory_SwapFree_bytes_E_rhs = AVG(LAST_OVER_TIME(node_memory_SwapFree_bytes)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance, job
| EVAL RAM_Used = ((RAM_Total - RAM_Free) - ((node_memory_Cached_bytes_B_rhs_lhs_lhs + node_memory_Buffers_bytes_B_rhs_lhs_rhs) + node_memory_SReclaimable_bytes_B_rhs_rhs))
| EVAL RAM_Cache_Buffer = ((node_memory_Cached_bytes_C_lhs_lhs + node_memory_Buffers_bytes_C_lhs_rhs) + node_memory_SReclaimable_bytes_C_rhs)
| EVAL SWAP_Used = (node_memory_SwapTotal_bytes_E_lhs - node_memory_SwapFree_bytes_E_rhs)
| EVAL series_group = CONCAT(COALESCE(TO_STRING(instance), ""), " / ", COALESCE(TO_STRING(job), ""))
| KEEP time_bucket, instance, job, RAM_Total, RAM_Used, RAM_Cache_Buffer, RAM_Free, SWAP_Used, series_group
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `node_memory_MemTotal_bytes`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `RAM_Total`
- Output groups: `time_bucket, instance, job`
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

**Warnings:** Grafana panel has 23 field override(s); verify visual mappings manually; Grafana panel description is not carried into the migrated Kibana panel automatically; Composited multi-label grouping (instance, job) into a single XY breakdown column

**Semantic losses:** Dropped variable-driven label filters during migration

**Notes:** Grafana panel has 23 field override(s); verify visual mappings manually; Grafana panel description is not carried into the migrated Kibana panel automatically

**Verdict:** MINOR_ISSUE

#### Network Traffic Basic

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
irate(node_network_receive_bytes_total{instance="$node",job="$job"}[$__rate_interval])*8 ||| irate(node_network_transmit_bytes_total{instance="$node",job="$job"}[$__rate_interval])*8
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE node_network_receive_bytes_total IS NOT NULL OR node_network_transmit_bytes_total IS NOT NULL
| STATS node_network_receive_bytes_total_A_lhs = IRATE(node_network_receive_bytes_total), node_network_transmit_bytes_total_B_lhs = IRATE(node_network_transmit_bytes_total) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| EVAL recv = (node_network_receive_bytes_total_A_lhs * 8)
| EVAL trans = (node_network_transmit_bytes_total_B_lhs * 8)
| KEEP time_bucket, recv, trans
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `*`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `recv`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=11, w=24, h=10
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2
- field_overrides: 24
- has_description: True

**Warnings:** Grafana panel has 24 field override(s); verify visual mappings manually; Grafana panel description is not carried into the migrated Kibana panel automatically

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Disk Space Used Basic

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
100 - ((node_filesystem_avail_bytes{instance="$node",job="$job",device!~'rootfs'} * 100) / node_filesystem_size_bytes{instance="$node",job="$job",device!~'rootfs'})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE (NOT (device RLIKE "rootfs") OR (device IS NULL AND NOT ("" RLIKE "rootfs")))
| WHERE node_filesystem_avail_bytes IS NOT NULL OR node_filesystem_size_bytes IS NOT NULL
| STATS node_filesystem_avail_bytes_device_rootfs = AVG(LAST_OVER_TIME(node_filesystem_avail_bytes)), node_filesystem_size_bytes_device_rootfs = AVG(LAST_OVER_TIME(node_filesystem_size_bytes)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), mountpoint
| EVAL computed_value = (100 - ((node_filesystem_avail_bytes_device_rootfs * 100) / node_filesystem_size_bytes_device_rootfs))
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
- Layout: x=24, y=11, w=24, h=10
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into the migrated Kibana panel automatically; Approximated PromQL arithmetic using same-bucket ES|QL math

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into the migrated Kibana panel automatically

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Controls / Variables (2)</summary>

- `Job` (type: `esql`)
- `Host` (type: `esql`)

</details>

---

### Grafana: Prometheus 2.0 (by FUSAKLA)

**File:** `prometheus-all.json` — **Panels:** 45

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Untitled | `text` → `markdown` | migrated | **EXPECTED_LIMITATION** | — | — |
| Uptime | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | time() - process_start_time_seconds{instance="$instance"} | FROM metrics-prometheus-* \| WHERE process_start_time_seconds IS NOT NULL \| STA... |
| Total count of time series | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | prometheus_tsdb_head_series{instance="$instance"} | TS metrics-prometheus-* \| WHERE prometheus_tsdb_head_series IS NOT NULL \| STAT... |
| Version | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | prometheus_build_info{instance="$instance"} | TS metrics-prometheus-* \| WHERE prometheus_build_info IS NOT NULL \| STATS prom... |
| Actual head block length | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | prometheus_tsdb_head_max_time{instance="$instance"} - prometheus_tsdb_head_min_t... | TS metrics-prometheus-* \| WHERE prometheus_tsdb_head_max_time IS NOT NULL OR pr... |
| Untitled | `text` → `markdown` | migrated | **EXPECTED_LIMITATION** | — | — |
| 2 | `singlestat` → `metric` | migrated | **CORRECT** | 2 | ROW constant_value = 2.0 |
| Query elapsed time | `graph` → `area` | migrated_with_warnings | **MINOR_ISSUE** | max(prometheus_engine_query_duration_seconds{instance="$instance"}) by (instance... | TS metrics-prometheus-* \| WHERE prometheus_engine_query_duration_seconds IS NOT... |
| Head series created/deleted | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(increase(prometheus_tsdb_head_series_created_total{instance="$instance"}[$ag... | TS metrics-prometheus-* \| WHERE prometheus_tsdb_head_series_created_total IS NO... |
| Prometheus errors | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(increase(prometheus_target_scrapes_exceeded_sample_limit_total{instance="$in... | TS metrics-prometheus-* \| WHERE prometheus_target_scrapes_exceeded_sample_limit... |
| Scrape delay (counts with 1m scrape interval) | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | prometheus_target_interval_length_seconds{instance="$instance",quantile="0.99"} ... | TS metrics-prometheus-* \| WHERE quantile == "0.99" \| WHERE prometheus_target_i... |
| Rule evaulation duration | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(prometheus_evaluator_duration_seconds{instance="$instance"}) by (instance, q... | TS metrics-prometheus-* \| WHERE prometheus_evaluator_duration_seconds IS NOT NU... |
| Request count | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(increase(http_requests_total{instance="$instance"}[$aggregation_interval])) ... | TS metrics-prometheus-* \| WHERE http_requests_total IS NOT NULL \| STATS http_r... |
| Request duration per handler | `graph` → `line` | migrated | **MINOR_ISSUE** | max(sum(http_request_duration_microseconds{instance="$instance"}) by (instance, ... | FROM metrics-prometheus-* \| STATS inner_val = SUM(http_request_duration_microse... |
| Request size by handler | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(increase(http_request_size_bytes{instance="$instance", quantile="0.99"}[$agg... | TS metrics-prometheus-* \| WHERE quantile == "0.99" \| WHERE http_request_size_b... |
| Cont of concurent queries | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(prometheus_engine_queries{instance="$instance"}) by (instance, handler) \|\|... | TS metrics-prometheus-* \| WHERE prometheus_engine_queries IS NOT NULL OR promet... |
| Alert queue size | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(prometheus_notifications_queue_capacity{instance="$instance"})by (instance) ... | TS metrics-prometheus-* \| WHERE prometheus_notifications_queue_capacity IS NOT ... |
| Count of discovered alertmanagers | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(prometheus_notifications_alertmanagers_discovered{instance="$instance"}) by ... | TS metrics-prometheus-* \| WHERE prometheus_notifications_alertmanagers_discover... |
| Alerting errors | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(increase(prometheus_notifications_dropped_total{instance="$instance"}[$aggre... | TS metrics-prometheus-* \| WHERE prometheus_notifications_dropped_total IS NOT N... |
| Consul SD sync count | `graph` → `line` | migrated | **MINOR_ISSUE** | increase(prometheus_target_sync_length_seconds_count{scrape_job="consul", instan... | TS metrics-prometheus-* \| WHERE scrape_job == "consul" \| WHERE prometheus_targ... |
| Marathon SD sync count | `graph` → `line` | migrated | **MINOR_ISSUE** | increase(prometheus_target_sync_length_seconds_count{scrape_job="marathon", inst... | TS metrics-prometheus-* \| WHERE scrape_job == "marathon" \| WHERE prometheus_ta... |
| Kubernetes SD sync count | `graph` → `line` | migrated | **CORRECT** | increase(prometheus_target_sync_length_seconds_count{scrape_job="kubernetes"}[$a... | TS metrics-prometheus-* \| WHERE scrape_job == "kubernetes" \| WHERE prometheus_... |
| Service discovery errors | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(increase(prometheus_target_scrapes_exceeded_sample_limit_total{instance="$in... | TS metrics-prometheus-* \| WHERE prometheus_target_scrapes_exceeded_sample_limit... |
| Reloaded block from disk | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(increase(prometheus_tsdb_reloads_total{instance="$instance"}[30m])) by (inst... | TS metrics-prometheus-* \| WHERE prometheus_tsdb_reloads_total IS NOT NULL \| ST... |
| Loaded data blocks | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(prometheus_tsdb_blocks_loaded{instance="$instance"}) by (instance) | TS metrics-prometheus-* \| WHERE prometheus_tsdb_blocks_loaded IS NOT NULL \| ST... |
| Time series total count | `graph` → `line` | migrated | **MINOR_ISSUE** | prometheus_tsdb_head_series{instance="$instance"} | TS metrics-prometheus-* \| WHERE prometheus_tsdb_head_series IS NOT NULL \| STAT... |
| Samples Appended per second | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(rate(prometheus_tsdb_head_samples_appended_total{instance="$instance"}[$aggr... | TS metrics-prometheus-* \| WHERE prometheus_tsdb_head_samples_appended_total IS ... |
| Head chunks count | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(prometheus_tsdb_head_chunks{instance="$instance"}) by (instance) | TS metrics-prometheus-* \| WHERE prometheus_tsdb_head_chunks IS NOT NULL \| STAT... |
| Length of head block | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | max(prometheus_tsdb_head_max_time{instance="$instance"}) by (instance) - min(pro... | TS metrics-prometheus-* \| WHERE prometheus_tsdb_head_max_time IS NOT NULL OR pr... |
| Head Chunks Created/Deleted per second | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(rate(prometheus_tsdb_head_chunks_created_total{instance="$instance"}[$aggreg... | TS metrics-prometheus-* \| WHERE prometheus_tsdb_head_chunks_created_total IS NO... |
| Compaction duration | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(increase(prometheus_tsdb_compaction_duration_sum{instance="$instance"}[30m])... | TS metrics-prometheus-* \| WHERE prometheus_tsdb_compaction_duration_sum IS NOT ... |
| Go Garbage collection duration | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(prometheus_tsdb_head_gc_duration_seconds{instance="$instance"}) by (instance... | TS metrics-prometheus-* \| WHERE prometheus_tsdb_head_gc_duration_seconds IS NOT... |
| WAL truncate duration seconds | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(prometheus_tsdb_wal_truncate_duration_seconds{instance="$instance"}) by (ins... | TS metrics-prometheus-* \| WHERE prometheus_tsdb_wal_truncate_duration_seconds I... |
| WAL fsync duration seconds | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(tsdb_wal_fsync_duration_seconds{instance="$instance"}) by (instance, quantil... | TS metrics-prometheus-* \| WHERE tsdb_wal_fsync_duration_seconds IS NOT NULL \| ... |
| Memory | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(process_resident_memory_bytes{instance="$instance"}) by (instance) \|\|\| su... | TS metrics-prometheus-* \| WHERE process_resident_memory_bytes IS NOT NULL OR go... |
| Allocations per second | `graph` → `line` | migrated | **MINOR_ISSUE** | rate(go_memstats_alloc_bytes_total{instance="$instance"}[$aggregation_interval]) | TS metrics-prometheus-* \| WHERE go_memstats_alloc_bytes_total IS NOT NULL \| ST... |
| CPU per second | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(rate(process_cpu_seconds_total{instance="$instance"}[$aggregation_interval])... | TS metrics-prometheus-* \| WHERE process_cpu_seconds_total IS NOT NULL \| STATS ... |
| Heapster rows | `text` → `markdown` | migrated | **EXPECTED_LIMITATION** | — | — |
| CPU usage/s | `graph` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Memory usage | `graph` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Network rx[IN] / tx[OUT] in bytes/s | `graph` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Disk usage | `graph` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Number of free INODES | `graph` → `markdown` | requires_manual | **EXPECTED_LIMITATION** | — | — |
| Net errors | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(increase(net_conntrack_dialer_conn_failed_total{instance="$instance"}[$aggre... | TS metrics-prometheus-* \| WHERE net_conntrack_dialer_conn_failed_total IS NOT N... |
| Dashboard Links | `dashboard_links` → `links` | migrated | **EXPECTED_LIMITATION** | — | — |

<details>
<summary>Detailed traces (36 panels)</summary>

#### Uptime

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
time() - process_start_time_seconds{instance="$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=uptime backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family uptime bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family` → translated uptime expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
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

**Warnings:** Approximated time() - metric as uptime from metric timestamp

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated time() - metric as uptime from metric timestamp

**Verdict:** MINOR_ISSUE

#### Total count of time series

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
prometheus_tsdb_head_series{instance="$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE prometheus_tsdb_head_series IS NOT NULL
| STATS prometheus_tsdb_head_series = MAX(LAST_OVER_TIME(prometheus_tsdb_head_series)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| STATS prometheus_tsdb_head_series = LAST(prometheus_tsdb_head_series, time_bucket)
| KEEP prometheus_tsdb_head_series
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

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Version

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
prometheus_build_info{instance="$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE prometheus_build_info IS NOT NULL
| STATS prometheus_build_info = MAX(LAST_OVER_TIME(prometheus_build_info)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| STATS prometheus_build_info = LAST(prometheus_build_info, time_bucket)
| KEEP prometheus_build_info
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

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Actual head block length

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
prometheus_tsdb_head_max_time{instance="$instance"} - prometheus_tsdb_head_min_time{instance="$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE prometheus_tsdb_head_max_time IS NOT NULL OR prometheus_tsdb_head_min_time IS NOT NULL
| STATS prometheus_tsdb_head_max_time_instance = AVG(LAST_OVER_TIME(prometheus_tsdb_head_max_time)), prometheus_tsdb_head_min_time_instance = AVG(LAST_OVER_TIME(prometheus_tsdb_head_min_time)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| EVAL computed_value = (prometheus_tsdb_head_max_time_instance - prometheus_tsdb_head_min_time_instance)
| STATS computed_value = LAST(computed_value, time_bucket)
| KEEP computed_value
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `-`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Collapsed all series of `prometheus_tsdb_head_max_time` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Collapsed all series of `prometheus_tsdb_head_min_time` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Visual IR:**

- Kibana type: `metric`
- Layout: x=32, y=0, w=8, h=8
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `prometheus_tsdb_head_max_time` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `prometheus_tsdb_head_min_time` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `prometheus_tsdb_head_max_time` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `prometheus_tsdb_head_min_time` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Verdict:** MINOR_ISSUE

#### 2

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
2
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros`
- `query_preprocessors` / `parse_fragment` → parsed fragment family=scalar backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family scalar bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family` → translated scalar constant
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
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

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE prometheus_engine_query_duration_seconds IS NOT NULL
| STATS prometheus_engine_query_duration_seconds = MAX(prometheus_engine_query_duration_seconds) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance, slice
| EVAL series_group = CONCAT(COALESCE(TO_STRING(instance), ""), " / ", COALESCE(TO_STRING(slice), ""))
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
- Output groups: `time_bucket, instance, slice`
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

**Warnings:** Composited multi-label grouping (instance, slice) into a single XY breakdown column

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Head series created/deleted

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(increase(prometheus_tsdb_head_series_created_total{instance="$instance"}[$aggregation_interval])) by (instance) ||| sum(increase(prometheus_tsdb_head_series_removed_total{instance="$instance"}[$aggregation_interval])) by (instance) * -1
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE prometheus_tsdb_head_series_created_total IS NOT NULL OR prometheus_tsdb_head_series_removed_total IS NOT NULL
| STATS created_on = SUM(INCREASE(prometheus_tsdb_head_series_created_total)), prometheus_tsdb_head_series_removed_total_B = SUM(INCREASE(prometheus_tsdb_head_series_removed_total)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| EVAL prometheus_tsdb_head_series_removed_total_B_calc = prometheus_tsdb_head_series_removed_total_B * -1
| EVAL removed_on = prometheus_tsdb_head_series_removed_total_B_calc
| KEEP time_bucket, instance, created_on, removed_on
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
- Output metric: `created_on`
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

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Prometheus errors

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(increase(prometheus_target_scrapes_exceeded_sample_limit_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0 ||| sum(increase(prometheus_target_scrapes_sample_duplicate_timestamp_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0 ||| sum(increase(prometheus_target_scrapes_sample_out_of_bounds_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0 ||| sum(increase(prometheus_target_scrapes_sample_out_of_order_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0 ||| sum(increase(prometheus_rule_evaluation_failures_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0 ||| sum(increase(prometheus_tsdb_compactions_failed_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0 ||| sum(increase(prometheus_tsdb_reloads_failures_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0 ||| sum(increase(prometheus_tsdb_head_series_not_found{instance="$instance"}[$aggregation_interval])) by (instance) > 0 ||| sum(increase(prometheus_evaluator_iterations_missed_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0 ||| sum(increase(prometheus_evaluator_iterations_skipped_total{instance="$instance"}[$aggregation_interval])) by (instance) > 0
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter` → applied post-aggregation filter > 0
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE prometheus_target_scrapes_exceeded_sample_limit_total IS NOT NULL OR prometheus_target_scrapes_sample_duplicate_timestamp_total IS NOT NULL OR prometheus_target_scrapes_sample_out_of_bounds_total IS NOT NULL OR prometheus_target_scrapes_sample_out_of_order_total IS NOT NULL OR prometheus_rule_evaluation_failures_total IS NOT NULL OR prometheus_tsdb_compactions_failed_total IS NOT NULL OR prometheus_tsdb_reloads_failures_total IS NOT NULL OR prometheus_tsdb_head_series_not_found IS NOT NULL OR prometheus_evaluator_iterations_missed_total IS NOT NULL OR prometheus_evaluator_iterations_skipped_total IS NOT NULL
| STATS prometheus_target_scrapes_exceeded_sample_limit_total_A = SUM(INCREASE(prometheus_target_scrapes_exceeded_sample_limit_total)), prometheus_target_scrapes_sample_duplicate_timestamp_total_B = SUM(INCREASE(prometheus_target_scrapes_sample_duplicate_timestamp_total)), prometheus_target_scrapes_sample_out_of_bounds_total_C = SUM(INCREASE(prometheus_target_scrapes_sample_out_of_bounds_total)), prometheus_target_scrapes_sample_out_of_order_total_D = SUM(INCREASE(prometheus_target_scrapes_sample_out_of_order_total)), prometheus_rule_evaluation_failures_total_G = SUM(INCREASE(prometheus_rule_evaluation_failures_total)), prometheus_tsdb_compactions_failed_total_K = SUM(INCREASE(prometheus_tsdb_compactions_failed_total)), prometheus_tsdb_reloads_failures_total_L = SUM(INCREASE(prometheus_tsdb_reloads_failures_total)), prometheus_tsdb_head_series_not_found_N = SUM(MAX_OVER_TIME(TO_DOUBLE(prometheus_tsdb_head_series_not_found), 5m)), prometheus_evaluator_iterations_missed_total_O = SUM(INCREASE(prometheus_evaluator_iterations_missed_total)), prometheus_evaluator_iterations_skipped_total_P = SUM(INCREASE(prometheus_evaluator_iterations_skipped_total)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| EVAL exceeded_sample_limit_on = CASE(prometheus_target_scrapes_exceeded_sample_limit_total_A > 0, prometheus_target_scrapes_exceeded_sample_limit_total_A, NULL)
| EVAL duplicate_timestamp_on = CASE(prometheus_target_scrapes_sample_duplicate_timestamp_total_B > 0, prometheus_target_scrapes_sample_duplicate_timestamp_total_B, NULL)
| EVAL out_of_bounds_on = CASE(prometheus_target_scrapes_sample_out_of_bounds_total_C > 0, prometheus_target_scrapes_sample_out_of_bounds_total_C, NULL)
| EVAL out_of_order_on = CASE(prometheus_target_scrapes_sample_out_of_order_total_D > 0, prometheus_target_scrapes_sample_out_of_order_total_D, NULL)
| EVAL rule_evaluation_failure_on = CASE(prometheus_rule_evaluation_failures_total_G > 0, prometheus_rule_evaluation_failures_total_G, NULL)
| EVAL tsdb_compactions_failed_on = CASE(prometheus_tsdb_compactions_failed_total_K > 0, prometheus_tsdb_compactions_failed_total_K, NULL)
| EVAL tsdb_reloads_failures_on = CASE(prometheus_tsdb_reloads_failures_total_L > 0, prometheus_tsdb_reloads_failures_total_L, NULL)
| EVAL head_series_not_found_on = CASE(prometheus_tsdb_head_series_not_found_N > 0, prometheus_tsdb_head_series_not_found_N, NULL)
| EVAL evaluator_iterations_missed_on = CASE(prometheus_evaluator_iterations_missed_total_O > 0, prometheus_evaluator_iterations_missed_total_O, NULL)
| EVAL evaluator_iterations_skipped_on = CASE(prometheus_evaluator_iterations_skipped_total_P > 0, prometheus_evaluator_iterations_skipped_total_P, NULL)
| KEEP time_bucket, instance, exceeded_sample_limit_on, duplicate_timestamp_on, out_of_bounds_on, out_of_order_on, rule_evaluation_failure_on, tsdb_compactions_failed_on, tsdb_reloads_failures_on, head_series_not_found_on, evaluator_iterations_missed_on, evaluator_iterations_skipped_on
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
- Output metric: `exceeded_sample_limit_on`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration, Source PromQL used increase() but prometheus_tsdb_head_series_not_found is typed as gauge in the target index; rendered as MAX_OVER_TIME (cumulative ceiling) instead. Fix the ingest mapping to mark this field as a counter to recover the true increase over the window.

**Visual IR:**

- Kibana type: `line`
- Layout: x=32, y=0, w=16, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 10

**Warnings:** Source PromQL used increase() but prometheus_tsdb_head_series_not_found is typed as gauge in the target index; rendered as MAX_OVER_TIME (cumulative ceiling) instead. Fix the ingest mapping to mark this field as a counter to recover the true increase over the window.

**Semantic losses:** Dropped variable-driven label filters during migration; Source PromQL used increase() but prometheus_tsdb_head_series_not_found is typed as gauge in the target index; rendered as MAX_OVER_TIME (cumulative ceiling) instead. Fix the ingest mapping to mark this field as a counter to recover the true increase over the window.

**Verdict:** MINOR_ISSUE

#### Scrape delay (counts with 1m scrape interval)

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
prometheus_target_interval_length_seconds{instance="$instance",quantile="0.99"} - $scrape_interval
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE quantile == "0.99"
| WHERE prometheus_target_interval_length_seconds IS NOT NULL
| STATS prometheus_target_interval_length_seconds_quantile_0_99 = MAX(LAST_OVER_TIME(prometheus_target_interval_length_seconds)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| EVAL computed_value = (prometheus_target_interval_length_seconds_quantile_0_99 - 0)
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Grafana variable $scrape_interval used as scalar arithmetic value was replaced with literal 0; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Rule evaulation duration

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(prometheus_evaluator_duration_seconds{instance="$instance"}) by (instance, quantile)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE prometheus_evaluator_duration_seconds IS NOT NULL
| STATS prometheus_evaluator_duration_seconds = SUM(prometheus_evaluator_duration_seconds) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance, quantile
| EVAL series_group = CONCAT(COALESCE(TO_STRING(instance), ""), " / ", COALESCE(TO_STRING(quantile), ""))
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

**Warnings:** Composited multi-label grouping (instance, quantile) into a single XY breakdown column

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Request count

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(increase(http_requests_total{instance="$instance"}[$aggregation_interval])) by (instance, handler) > 0
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter` → applied post-aggregation filter > 0
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE http_requests_total IS NOT NULL
| STATS http_requests_total = SUM(INCREASE(http_requests_total)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance, handler
| WHERE http_requests_total > 0
| EVAL legend = CONCAT(COALESCE(TO_STRING(handler), ""), " on ", COALESCE(TO_STRING(instance), ""))
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
- Output groups: `time_bucket, instance, handler`
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

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Request duration per handler

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
max(sum(http_request_duration_microseconds{instance="$instance"}) by (instance, handler, quantile)) by (instance, handler) > 0
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=nested_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family nested_agg bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family` → translated nested max expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter` → applied post-aggregation filter > 0
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
FROM metrics-prometheus-*
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

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Request size by handler

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(increase(http_request_size_bytes{instance="$instance", quantile="0.99"}[$aggregation_interval])) by (instance, handler) > 0
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter` → applied post-aggregation filter > 0
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE quantile == "0.99"
| WHERE http_request_size_bytes IS NOT NULL
| STATS http_request_size_bytes = SUM(MAX_OVER_TIME(TO_DOUBLE(http_request_size_bytes), 5m)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance, handler
| WHERE http_request_size_bytes > 0
| EVAL legend = CONCAT(COALESCE(TO_STRING(handler), ""), " in ", COALESCE(TO_STRING(instance), ""))
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
- Output groups: `time_bucket, instance, handler`
- Semantic losses: Dropped variable-driven label filters during migration, Source PromQL used increase() but http_request_size_bytes is typed as gauge in the target index; rendered as MAX_OVER_TIME (cumulative ceiling) instead. Fix the ingest mapping to mark this field as a counter to recover the true increase over the window.

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=0, w=12, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Source PromQL used increase() but http_request_size_bytes is typed as gauge in the target index; rendered as MAX_OVER_TIME (cumulative ceiling) instead. Fix the ingest mapping to mark this field as a counter to recover the true increase over the window.

**Semantic losses:** Dropped variable-driven label filters during migration; Source PromQL used increase() but http_request_size_bytes is typed as gauge in the target index; rendered as MAX_OVER_TIME (cumulative ceiling) instead. Fix the ingest mapping to mark this field as a counter to recover the true increase over the window.

**Verdict:** MINOR_ISSUE

#### Cont of concurent queries

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(prometheus_engine_queries{instance="$instance"}) by (instance, handler) ||| sum(prometheus_engine_queries_concurrent_max{instance="$instance"}) by (instance, handler)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE prometheus_engine_queries IS NOT NULL OR prometheus_engine_queries_concurrent_max IS NOT NULL
| STATS Current_count = SUM(prometheus_engine_queries), Max_count = SUM(prometheus_engine_queries_concurrent_max) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance, handler
| EVAL series_group = CONCAT(COALESCE(TO_STRING(instance), ""), " / ", COALESCE(TO_STRING(handler), ""))
| KEEP time_bucket, instance, handler, Current_count, Max_count, series_group
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

**Warnings:** Composited multi-label grouping (instance, handler) into a single XY breakdown column

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Alert queue size

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(prometheus_notifications_queue_capacity{instance="$instance"})by (instance) ||| sum(prometheus_notifications_queue_length{instance="$instance"})by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE prometheus_notifications_queue_capacity IS NOT NULL OR prometheus_notifications_queue_length IS NOT NULL
| STATS Alert_queue_capacity = SUM(prometheus_notifications_queue_capacity), Alert_queue_size_on = SUM(prometheus_notifications_queue_length) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
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

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Controls / Variables (1)</summary>

- `Instance` (type: `esql`)

</details>

---

### Grafana: Redis Dashboard for Prometheus Redis Exporter (helm stable/redis-ha)

**File:** `redis-11835.json` — **Panels:** 12

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Uptime | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | max(max_over_time(redis_uptime_in_seconds{instance=~"$instance"}[$__interval])) | TS metrics-prometheus-* \| WHERE redis_uptime_in_seconds IS NOT NULL \| STATS re... |
| Clients | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | redis_connected_clients{instance=~"$instance"} | TS metrics-prometheus-* \| WHERE redis_connected_clients IS NOT NULL \| STATS re... |
| Memory Usage | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | 100 * (redis_memory_used_bytes{instance=~"$instance"}  / redis_memory_max_bytes{... | TS metrics-prometheus-* \| WHERE redis_memory_used_bytes IS NOT NULL OR redis_me... |
| Commands Executed / sec | `graph` → `line` | migrated | **MINOR_ISSUE** | rate(redis_commands_processed_total{instance=~"$instance"}[1m]) | TS metrics-prometheus-* \| WHERE redis_commands_processed_total IS NOT NULL \| S... |
| Hits / Misses per Sec | `graph` → `line` | migrated | **MINOR_ISSUE** | irate(redis_keyspace_hits_total{instance=~"$instance"}[5m]) \|\|\| irate(redis_k... | TS metrics-prometheus-* \| WHERE redis_keyspace_hits_total IS NOT NULL OR redis_... |
| Total Memory Usage | `graph` → `line` | migrated | **MINOR_ISSUE** | redis_memory_used_bytes{instance=~"$instance"}  \|\|\| redis_memory_max_bytes{in... | TS metrics-prometheus-* \| WHERE redis_memory_used_bytes IS NOT NULL OR redis_me... |
| Network I/O | `graph` → `line` | migrated | **MINOR_ISSUE** | rate(redis_net_input_bytes_total{instance=~"$instance"}[5m]) \|\|\| rate(redis_n... | TS metrics-prometheus-* \| WHERE redis_net_input_bytes_total IS NOT NULL OR redi... |
| Total Items per DB | `graph` → `area` | migrated | **MINOR_ISSUE** | sum (redis_db_keys{instance=~"$instance"}) by (db) | TS metrics-prometheus-* \| WHERE redis_db_keys IS NOT NULL \| STATS redis_db_key... |
| Expiring vs Not-Expiring Keys | `graph` → `area` | migrated | **MINOR_ISSUE** | sum (redis_db_keys{instance=~"$instance"}) - sum (redis_db_keys_expiring{instanc... | TS metrics-prometheus-* \| WHERE redis_db_keys IS NOT NULL OR redis_db_keys_expi... |
| Expired / Evicted | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(redis_expired_keys_total{instance=~"$instance"}[5m])) by (instance) \|\... | TS metrics-prometheus-* \| WHERE redis_expired_keys_total IS NOT NULL OR redis_e... |
| Command Calls / sec | `graph` → `area` | migrated_with_warnings | **MINOR_ISSUE** | topk(5, irate(redis_commands_total{instance=~"$instance"} [1m])) | TS metrics-prometheus-* \| WHERE redis_commands_total IS NOT NULL \| STATS value... |
| Redis connected clients | `graph` → `line` | migrated | **MINOR_ISSUE** | redis_connected_clients{instance="$instance"} | TS metrics-prometheus-* \| WHERE redis_connected_clients IS NOT NULL \| STATS re... |

<details>
<summary>Detailed traces (12 panels)</summary>

#### Uptime

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
max(max_over_time(redis_uptime_in_seconds{instance=~"$instance"}[$__interval]))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE redis_uptime_in_seconds IS NOT NULL
| STATS redis_uptime_in_seconds = MAX(MAX_OVER_TIME(redis_uptime_in_seconds, 5m)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| STATS redis_uptime_in_seconds = LAST(redis_uptime_in_seconds, time_bucket)
| KEEP redis_uptime_in_seconds
```

**Query IR:**

- Family: `range_agg`
- Metric: `redis_uptime_in_seconds`
- Range func: `max_over_time`
- Range window: `5m`
- Outer agg: `max`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_uptime_in_seconds`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=0, y=0, w=4, h=11
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Clients

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
redis_connected_clients{instance=~"$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE redis_connected_clients IS NOT NULL
| STATS redis_connected_clients = MAX(LAST_OVER_TIME(redis_connected_clients)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| STATS redis_connected_clients = LAST(redis_connected_clients, time_bucket)
| KEEP redis_connected_clients
```

**Query IR:**

- Family: `simple_metric`
- Metric: `redis_connected_clients`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_connected_clients`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=4, y=0, w=4, h=11
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Memory Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
100 * (redis_memory_used_bytes{instance=~"$instance"}  / redis_memory_max_bytes{instance=~"$instance"} )
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE redis_memory_used_bytes IS NOT NULL OR redis_memory_max_bytes IS NOT NULL
| STATS redis_memory_used_bytes_instance = AVG(LAST_OVER_TIME(redis_memory_used_bytes)), redis_memory_max_bytes_instance = AVG(LAST_OVER_TIME(redis_memory_max_bytes)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| EVAL computed_value = (100 * (redis_memory_used_bytes_instance / redis_memory_max_bytes_instance))
| STATS computed_value = LAST(computed_value, time_bucket)
| KEEP computed_value
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `*`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Collapsed all series of `redis_memory_used_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Collapsed all series of `redis_memory_max_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Visual IR:**

- Kibana type: `metric`
- Layout: x=8, y=0, w=8, h=11
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `redis_memory_used_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `redis_memory_max_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `redis_memory_used_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `redis_memory_max_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Verdict:** MINOR_ISSUE

#### Commands Executed / sec

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
rate(redis_commands_processed_total{instance=~"$instance"}[1m])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_commands_processed_total IS NOT NULL
| STATS redis_commands_processed_total = AVG(RATE(redis_commands_processed_total)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `redis_commands_processed_total`
- Range func: `rate`
- Range window: `1m`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_commands_processed_total`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=16, y=0, w=16, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Hits / Misses per Sec

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
irate(redis_keyspace_hits_total{instance=~"$instance"}[5m]) ||| irate(redis_keyspace_misses_total{instance=~"$instance"}[5m])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_keyspace_hits_total IS NOT NULL OR redis_keyspace_misses_total IS NOT NULL
| STATS hits = AVG(IRATE(redis_keyspace_hits_total)), misses = AVG(IRATE(redis_keyspace_misses_total)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| KEEP time_bucket, instance, hits, misses
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `redis_keyspace_hits_total`
- Range func: `irate`
- Range window: `5m`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `hits`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=32, y=0, w=16, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Total Memory Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
redis_memory_used_bytes{instance=~"$instance"}  ||| redis_memory_max_bytes{instance=~"$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_memory_used_bytes IS NOT NULL OR redis_memory_max_bytes IS NOT NULL
| STATS used = AVG(LAST_OVER_TIME(redis_memory_used_bytes)), max = AVG(LAST_OVER_TIME(redis_memory_max_bytes)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| KEEP time_bucket, instance, used, max
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `redis_memory_used_bytes`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `used`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=11, w=24, h=10
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Network I/O

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
rate(redis_net_input_bytes_total{instance=~"$instance"}[5m]) ||| rate(redis_net_output_bytes_total{instance=~"$instance"}[5m])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_net_input_bytes_total IS NOT NULL OR redis_net_output_bytes_total IS NOT NULL
| STATS input = RATE(redis_net_input_bytes_total), output = RATE(redis_net_output_bytes_total) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| KEEP time_bucket, input, output
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `redis_net_input_bytes_total`
- Range func: `rate`
- Range window: `5m`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `input`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=11, w=24, h=10
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Total Items per DB

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum (redis_db_keys{instance=~"$instance"}) by (db)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE redis_db_keys IS NOT NULL
| STATS redis_db_keys = SUM(redis_db_keys) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), db
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `redis_db_keys`
- Outer agg: `sum`
- Group labels: `db`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_db_keys`
- Output groups: `time_bucket, db`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `area`
- Layout: x=0, y=21, w=24, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Expiring vs Not-Expiring Keys

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum (redis_db_keys{instance=~"$instance"}) - sum (redis_db_keys_expiring{instance=~"$instance"})  ||| sum (redis_db_keys_expiring{instance=~"$instance"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=binary_expr backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → translated arithmetic expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE redis_db_keys IS NOT NULL OR redis_db_keys_expiring IS NOT NULL
| STATS redis_db_keys_A_lhs = SUM(redis_db_keys), expiring = SUM(redis_db_keys_expiring) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)
| EVAL not_expiring = (redis_db_keys_A_lhs - expiring)
| KEEP time_bucket, not_expiring, expiring
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `-`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `not_expiring`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `area`
- Layout: x=24, y=21, w=24, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Expired / Evicted

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(rate(redis_expired_keys_total{instance=~"$instance"}[5m])) by (instance) ||| sum(rate(redis_evicted_keys_total{instance=~"$instance"}[5m])) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=range_agg backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_expired_keys_total IS NOT NULL OR redis_evicted_keys_total IS NOT NULL
| STATS expired = SUM(RATE(redis_expired_keys_total)), evicted = SUM(RATE(redis_evicted_keys_total)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| KEEP time_bucket, instance, expired, evicted
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `redis_expired_keys_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `instance`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `expired`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration, Dropped Grafana secondary y-axis assignment for unmatched series override "reclaims"

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=32, w=24, h=10
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Dropped Grafana secondary y-axis assignment for unmatched series override "reclaims"

**Semantic losses:** Dropped variable-driven label filters during migration; Dropped Grafana secondary y-axis assignment for unmatched series override "reclaims"

**Verdict:** MINOR_ISSUE

#### Command Calls / sec

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
topk(5, irate(redis_commands_total{instance=~"$instance"} [1m]))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=topk backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family topk bypasses unsupported-pattern check
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family` → translated grouped topk as time-series breakdown for XY panel
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE redis_commands_total IS NOT NULL
| STATS value = AVG(IRATE(redis_commands_total)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), cmd
| SORT time_bucket ASC
```

**Query IR:**

- Family: `topk`
- Metric: `redis_commands_total`
- Range func: `irate`
- Range window: `1m`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `value`
- Output groups: `time_bucket, cmd`
- Semantic losses: Dropped variable-driven label filters during migration, Translated topk() as time-series breakdown by cmd; ES|QL has no subquery support so all series are shown (top-5 filtering approximated)

**Visual IR:**

- Kibana type: `area`
- Layout: x=24, y=32, w=24, h=10
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Translated topk() as time-series breakdown by cmd; ES|QL has no subquery support so all series are shown (top-5 filtering approximated)

**Semantic losses:** Dropped variable-driven label filters during migration; Translated topk() as time-series breakdown by cmd; ES|QL has no subquery support so all series are shown (top-5 filtering approximated)

**Verdict:** MINOR_ISSUE

#### Redis connected clients

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
redis_connected_clients{instance="$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=simple_metric backend=ast
- `query_classifiers` / `colocated_binary_agg_unblock`
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier`
- `query_classifiers` / `join_label_enrichment_check`
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `colocated_binary_agg_family`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `label_join_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `counter_range_window`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `approx_agg_over_summary_ratio_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `query_validators` / `late_bound_group_control`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_connected_clients IS NOT NULL
| STATS redis_connected_clients = AVG(LAST_OVER_TIME(redis_connected_clients)) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `redis_connected_clients`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_connected_clients`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=42, w=24, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Controls / Variables (3)</summary>

- `Namespace` (type: `esql`)
- `Pod Name` (type: `esql`)
- `instance` (type: `esql`)

</details>

---

<!-- /GENERATED:PER_DASHBOARD_TRACES -->

---

## Appendix: Panel Status Summary

<!-- GENERATED:APPENDIX_STATS -->
From the latest trace run:

```
Elements:            289 total (266 panels + 23 rows)
Renderable panels:   266
  Migrated:             114 (42.9%)
  With warnings:        145 (54.5%)
  Requires manual:        5 (1.9%)
  Not feasible:           1 (0.4%)
  Skipped:                1 (0.4%)
```

Verdict breakdown:

```
  CORRECT:                   11
  MINOR_ISSUE:              238
  EXPECTED_LIMITATION:       40
```
<!-- /GENERATED:APPENDIX_STATS -->

---

## Appendix: Not-Feasible Panel Breakdown

<!-- GENERATED:NOT_FEASIBLE_BREAKDOWN -->
Every panel marked `not_feasible` in the trace run (1 total):

| Panel Title | Dashboard | Source | Reason |
|-------------|-----------|--------|--------|
| Top Metrics by Series Count | Home - Migration Test Lab | grafana | PromQL metric-name introspection via __name__ requires manual redesign |

**Pattern analysis:**

- **1×** PromQL metric-name introspection via __name__ requires manua
<!-- /GENERATED:NOT_FEASIBLE_BREAKDOWN -->

---

*Last generated: 2026-08-04 18:38 UTC*
