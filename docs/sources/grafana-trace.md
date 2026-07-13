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
| grafana | Prometheus Blackbox Exporter | 10 | 9 | 1 | 0 | 0 | 0 | 2 |
| grafana | Diverse Panel Types Test | 10 | 3 | 7 | 0 | 0 | 0 | 1 |
| grafana | Docker and system monitoring | 15 | 6 | 8 | 0 | 1 | 0 | 0 |
| grafana | Docker and system monitoring | 20 | 11 | 6 | 0 | 3 | 0 | 0 |
| grafana | Express Prometheus Middleware | 23 | 1 | 20 | 0 | 2 | 0 | 1 |
| grafana | Flagger Canary Status | 34 | 1 | 31 | 0 | 2 | 0 | 4 |
| grafana | Home - Migration Test Lab | 6 | 3 | 2 | 0 | 1 | 0 | 0 |
| grafana | Kubernetes cluster monitoring (via Prometheus) | 21 | 15 | 6 | 0 | 0 | 0 | 0 |
| grafana | Kubernetes Cluster (Prometheus) | 29 | 1 | 28 | 0 | 0 | 0 | 6 |
| grafana | Kubernetes / Views / Global | 26 | 11 | 12 | 0 | 3 | 0 | 4 |
| grafana | Kubernetes Kafka | 32 | 21 | 11 | 0 | 0 | 0 | 3 |
| grafana | Multi Pattern Coverage | 10 | 5 | 4 | 0 | 0 | 1 | 1 |
| grafana | MySQL Overview | 36 | 7 | 12 | 0 | 17 | 0 | 14 |
| grafana | Node Exporter Full | 116 | 39 | 77 | 0 | 0 | 0 | 16 |
| grafana | NodeJS Application Dashboard | 9 | 5 | 3 | 0 | 1 | 0 | 0 |
| grafana | Prometheus 2.0 Overview | 30 | 1 | 29 | 0 | 0 | 0 | 0 |
| grafana | Prometheus 2.0 (by FUSAKLA) | 44 | 28 | 10 | 5 | 1 | 0 | 0 |
| grafana | Redis Dashboard for Prometheus Redis Exporter (helm stable/redis-ha) | 12 | 7 | 5 | 0 | 0 | 0 | 0 |
| grafana | Redis Dashboard for Prometheus Redis Exporter 1.x | 13 | 9 | 3 | 0 | 1 | 0 | 0 |

**19 dashboards, 496 panels** audited from `infra/grafana/dashboards/`.
<!-- /GENERATED:DASHBOARD_SUMMARY -->

<!-- GENERATED:VERDICT_SUMMARY -->
## Verdict Summary

| Verdict | Count | Meaning |
|---------|-------|---------|
| **CORRECT** | 24 | Translation is semantically accurate |
| **MINOR_ISSUE** | 427 | Translated with approximations — review recommended |
| **EXPECTED_LIMITATION** | 97 | Known unsupported feature — placeholder or skip |
<!-- /GENERATED:VERDICT_SUMMARY -->

<!-- GENERATED:WARNING_PATTERNS -->
## Top Warning Patterns

| Count | Warning |
|------:|---------|
| 102 | Dropped variable-driven label filters during migration |
| 78 | Grafana panel description is not carried into Kibana YAML automatically |
| 66 | Approximated PromQL arithmetic using same-bucket ES\|QL math |
| 56 | XY chart shows a single breakdown; additional grouping dimension(s) ['job'] are in the query but not on the chart, so series differing only by those are visually merged |
| 42 | PromQL series labels were not retained; output is bucket-level and may collapse multiple source series |
| 32 | Grafana panel has 1 field override(s); verify visual mappings manually |
| 31 | Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value |
| 17 | PromQL 'or' between metrics that cannot be aligned in ES\|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped |
| 12 | Grafana panel has 1 link(s); verify drilldowns manually |
| 11 | Grafana panel has 2 field override(s); verify visual mappings manually |
| 9 | Panel has 2 PromQL targets but only 1 could be migrated |
| 7 | Panel has 3 PromQL targets but only 1 could be migrated |
| 6 | round(v, step) emitted as ROUND(v / step) * step |
| 6 | Grafana panel has 18 field override(s); verify visual mappings manually |
| 6 | Grafana panel has 19 field override(s); verify visual mappings manually |
<!-- /GENERATED:WARNING_PATTERNS -->

---

## Per-Dashboard Traces

<!-- GENERATED:PER_DASHBOARD_TRACES -->
### Grafana: Prometheus Blackbox Exporter

**File:** `blackbox-7587.json` — **Panels:** 12

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| $target status | `row` → `skipped` | skipped | **EXPECTED_LIMITATION** | — | — |
| $target status | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Global Probe Duration | `graph` → `line` | migrated | **MINOR_ISSUE** | probe_duration_seconds{instance=~"$target"} | TS metrics-prometheus-* \| WHERE probe_duration_seconds IS NOT NULL \| STATS pro... |
| Status | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | probe_success{instance=~"$target"} | TS metrics-prometheus-* \| WHERE probe_success IS NOT NULL \| STATS probe_succes... |
| HTTP Duration | `graph` → `line` | migrated | **MINOR_ISSUE** | probe_http_duration_seconds{instance=~"$target"} | TS metrics-prometheus-* \| WHERE probe_http_duration_seconds IS NOT NULL \| STAT... |
| Probe Duration | `graph` → `line` | migrated | **MINOR_ISSUE** | probe_duration_seconds{instance=~"$target"} | TS metrics-prometheus-* \| WHERE probe_duration_seconds IS NOT NULL \| STATS pro... |
| HTTP Status Code | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | probe_http_status_code{instance=~"$target"} | TS metrics-prometheus-* \| WHERE probe_http_status_code IS NOT NULL \| STATS pro... |
| HTTP Version | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | probe_http_version{instance=~"$target"} | TS metrics-prometheus-* \| WHERE probe_http_version IS NOT NULL \| STATS probe_h... |
| SSL | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | probe_http_ssl{instance=~"$target"} | TS metrics-prometheus-* \| WHERE probe_http_ssl IS NOT NULL \| STATS probe_http_... |
| SSL Expiry | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | probe_ssl_earliest_cert_expiry{instance=~"$target"} - time() | TS metrics-prometheus-* \| WHERE probe_ssl_earliest_cert_expiry IS NOT NULL \| S... |
| Average Probe Duration | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | avg(probe_duration_seconds{instance=~"$target"}) | TS metrics-prometheus-* \| WHERE probe_duration_seconds IS NOT NULL \| STATS pro... |
| Average DNS Lookup | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | avg(probe_dns_lookup_time_seconds{instance=~"$target"}) | TS metrics-prometheus-* \| WHERE probe_dns_lookup_time_seconds IS NOT NULL \| ST... |

<details>
<summary>Detailed traces (10 panels)</summary>

#### Global Probe Duration

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
probe_duration_seconds{instance=~"$target"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE probe_duration_seconds IS NOT NULL
| STATS probe_duration_seconds = MAX(LAST_OVER_TIME(probe_duration_seconds)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `probe_duration_seconds`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `probe_duration_seconds`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=48, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Status

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
probe_success{instance=~"$target"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE probe_success IS NOT NULL
| STATS probe_success = MAX(LAST_OVER_TIME(probe_success)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), probe_success = MAX(probe_success)
| KEEP time_bucket, probe_success
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `probe_success`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `probe_success`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=0, y=0, w=8, h=3
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### HTTP Duration

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
probe_http_duration_seconds{instance=~"$target"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE probe_http_duration_seconds IS NOT NULL
| STATS probe_http_duration_seconds = MAX(LAST_OVER_TIME(probe_http_duration_seconds)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `probe_http_duration_seconds`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `probe_http_duration_seconds`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=8, y=0, w=20, h=9
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Probe Duration

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
probe_duration_seconds{instance=~"$target"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE probe_duration_seconds IS NOT NULL
| STATS probe_duration_seconds = AVG(probe_duration_seconds) BY time_bucket = TBUCKET(5 minute), instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `probe_duration_seconds`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `probe_duration_seconds`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=28, y=0, w=20, h=9
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### HTTP Status Code

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
probe_http_status_code{instance=~"$target"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE probe_http_status_code IS NOT NULL
| STATS probe_http_status_code = MAX(LAST_OVER_TIME(probe_http_status_code)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), probe_http_status_code = MAX(probe_http_status_code)
| KEEP time_bucket, probe_http_status_code
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `probe_http_status_code`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `probe_http_status_code`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=0, y=3, w=8, h=3
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### HTTP Version

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
probe_http_version{instance=~"$target"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE probe_http_version IS NOT NULL
| STATS probe_http_version = MAX(LAST_OVER_TIME(probe_http_version)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), probe_http_version = MAX(probe_http_version)
| KEEP time_bucket, probe_http_version
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `probe_http_version`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `probe_http_version`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=0, y=6, w=8, h=3
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### SSL

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
probe_http_ssl{instance=~"$target"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE probe_http_ssl IS NOT NULL
| STATS probe_http_ssl = MAX(LAST_OVER_TIME(probe_http_ssl)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), probe_http_ssl = MAX(probe_http_ssl)
| KEEP time_bucket, probe_http_ssl
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `probe_http_ssl`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `probe_http_ssl`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=0, y=9, w=8, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### SSL Expiry

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
probe_ssl_earliest_cert_expiry{instance=~"$target"} - time()
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE probe_ssl_earliest_cert_expiry IS NOT NULL
| STATS probe_ssl_earliest_cert_expiry_instance = AVG(probe_ssl_earliest_cert_expiry) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (probe_ssl_earliest_cert_expiry_instance - DATE_DIFF("seconds", TO_DATETIME(0), NOW()))
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Collapsed all series of `probe_ssl_earliest_cert_expiry` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Visual IR:**

- Kibana type: `metric`
- Layout: x=8, y=9, w=20, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `probe_ssl_earliest_cert_expiry` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `probe_ssl_earliest_cert_expiry` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Verdict:** MINOR_ISSUE

#### Average Probe Duration

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
avg(probe_duration_seconds{instance=~"$target"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE probe_duration_seconds IS NOT NULL
| STATS probe_duration_seconds = AVG(probe_duration_seconds) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), probe_duration_seconds = MAX(probe_duration_seconds)
| KEEP time_bucket, probe_duration_seconds
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `probe_duration_seconds`
- Outer agg: `avg`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `probe_duration_seconds`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=28, y=9, w=10, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Average DNS Lookup

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
avg(probe_dns_lookup_time_seconds{instance=~"$target"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE probe_dns_lookup_time_seconds IS NOT NULL
| STATS probe_dns_lookup_time_seconds = AVG(probe_dns_lookup_time_seconds) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), probe_dns_lookup_time_seconds = MAX(probe_dns_lookup_time_seconds)
| KEEP time_bucket, probe_dns_lookup_time_seconds
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `probe_dns_lookup_time_seconds`
- Outer agg: `avg`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `probe_dns_lookup_time_seconds`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=38, y=9, w=10, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Controls / Variables (1)</summary>

- `target` (type: `options`)

</details>

---

### Grafana: Diverse Panel Types Test

**File:** `diverse-panels-test.json` — **Panels:** 11

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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
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
| STATS http_requests_total = SUM(RATE(http_requests_total, 5m)) BY time_bucket = TBUCKET(5 minute), handler
| SORT time_bucket ASC
| STATS http_requests_total = MAX(http_requests_total) BY handler
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
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family topk bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
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
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to bar panel

**Translated (bar):**

```
TS metrics-prometheus-*
| WHERE http_requests_total IS NOT NULL
| STATS _bucket_value = SUM(RATE(http_requests_total, 5m)) BY time_bucket = TBUCKET(5 minute), handler
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemAvailable_bytes IS NOT NULL OR node_memory_MemTotal_bytes IS NOT NULL
| STATS node_memory_MemAvailable_bytes = AVG(node_memory_MemAvailable_bytes), node_memory_MemTotal_bytes = AVG(node_memory_MemTotal_bytes) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = ((1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel` → approximated bargauge panel

**Translated (bar):**

```
TS metrics-prometheus-*
| WHERE node_filesystem_avail_bytes IS NOT NULL OR node_filesystem_size_bytes IS NOT NULL
| STATS node_filesystem_avail_bytes_mountpoint_pods = AVG(CASE((NOT (mountpoint RLIKE ".*pods.*")), node_filesystem_avail_bytes, NULL)), node_filesystem_size_bytes = AVG(node_filesystem_size_bytes) BY time_bucket = TBUCKET(5 minute), mountpoint
| EVAL computed_value = (100 - ((node_filesystem_avail_bytes_mountpoint_pods / node_filesystem_size_bytes) * 100))
| SORT time_bucket ASC
| STATS computed_value = MAX(computed_value) BY mountpoint
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
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
| STATS ALERTS = MAX(LAST_OVER_TIME(ALERTS)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
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
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family logql_stream bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family` → translated LogQL logs query
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
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

- `instance` (type: `options`)

</details>

---

### Grafana: Docker and system monitoring

**File:** `docker-system-4271.json` — **Panels:** 15

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Containers | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | count(rate(container_last_seen{container_label_namespace=~"$namespace",instance=... | TS metrics-prometheus-* \| WHERE container_last_seen IS NOT NULL \| STATS contai... |
| Load [1m] | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | avg(node_load1{instance=~"$server:.*"}) /  count(count(node_cpu{instance=~"$serv... | TS metrics-prometheus-* \| WHERE node_load1 IS NOT NULL OR node_cpu IS NOT NULL ... |
| Memory | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | ((node_memory_MemTotal{instance=~"$server:.*"} - node_memory_MemAvailable{instan... | TS metrics-prometheus-* \| WHERE node_memory_MemTotal IS NOT NULL OR node_memory... |
| Swap | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | (node_memory_SwapTotal{instance=~'$server:.*'} - node_memory_SwapFree{instance=~... | TS metrics-prometheus-* \| WHERE node_memory_SwapTotal IS NOT NULL OR node_memor... |
| Disk space | `singlestat` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | min((node_filesystem_size{fstype=~"xfs\|ext4",instance=~"$server:.*"} - node_fil... | — |
| Uptime | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | time() - node_boot_time{instance=~"$server:.*"} | FROM metrics-prometheus-* \| WHERE node_boot_time IS NOT NULL \| STATS start_tim... |
| CPU Usage | `graph` → `area` | migrated_with_warnings | **MINOR_ISSUE** | 100 - (avg by (instance) (irate(node_cpu{mode="idle",instance=~"$server:.*"}[5m]... | TS metrics-prometheus-* \| WHERE mode == "idle" \| WHERE node_cpu IS NOT NULL \|... |
| Disk I/O | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | -irate(node_disk_bytes_read{instance=~"$server:.*"}[5m]) \|\|\| irate(node_disk_... | TS metrics-prometheus-* \| WHERE node_disk_bytes_read IS NOT NULL OR node_disk_b... |
| Network Traffic | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(irate(node_network_receive_bytes{instance=~"$server:.*"}[5m])) \|\|\| - sum(... | TS metrics-prometheus-* \| WHERE node_network_receive_bytes IS NOT NULL OR node_... |
| Memory | `graph` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_MemTotal{instance=~"$server:.*"} \|\|\| node_memory_MemTotal{instanc... | TS metrics-prometheus-* \| WHERE node_memory_MemTotal IS NOT NULL OR node_memory... |
| Received Network Traffic per Container | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(rate(container_network_receive_bytes_total{container_label_namespace=~"$name... | TS metrics-prometheus-* \| WHERE container_network_receive_bytes_total IS NOT NU... |
| Sent Network Traffic per Container | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(rate(container_network_transmit_bytes_total{container_label_namespace=~"$nam... | TS metrics-prometheus-* \| WHERE container_network_transmit_bytes_total IS NOT N... |
| CPU Usage per Container | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(rate(container_cpu_usage_seconds_total{container_label_namespace=~"$namespac... | TS metrics-prometheus-* \| WHERE container_cpu_usage_seconds_total IS NOT NULL \... |
| Memory Usage per Container | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(container_memory_rss{container_label_namespace=~"$namespace",instance=~"$ser... | TS metrics-prometheus-* \| WHERE container_memory_rss IS NOT NULL \| STATS conta... |
| Memory Swap per Container | `graph` → `line` | migrated | **MINOR_ISSUE** | sum(container_memory_swap{container_label_namespace=~"$namespace",instance=~"$se... | TS metrics-prometheus-* \| WHERE container_memory_swap IS NOT NULL \| STATS cont... |

<details>
<summary>Detailed traces (15 panels)</summary>

#### Containers

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
count(rate(container_last_seen{container_label_namespace=~"$namespace",instance=~"$server:.*"}[3m]))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE container_last_seen IS NOT NULL
| STATS container_last_seen = COUNT(RATE(container_last_seen, 3m)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), container_last_seen = MAX(container_last_seen)
| KEEP time_bucket, container_last_seen
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `container_last_seen`
- Range func: `rate`
- Range window: `3m`
- Outer agg: `count`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_last_seen`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=0, y=0, w=8, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Load [1m]

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
avg(node_load1{instance=~"$server:.*"}) /  count(count(node_cpu{instance=~"$server:.*"}) by (cpu))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE node_load1 IS NOT NULL OR node_cpu IS NOT NULL
| STATS node_load1_instance_avg = AVG(node_load1), node_cpu_instance_count = COUNT_DISTINCT(cpu) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (node_load1_instance_avg / node_cpu_instance_count)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
| KEEP time_bucket, computed_value
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Approximated nested count(count()) as COUNT_DISTINCT(cpu)

**Visual IR:**

- Kibana type: `metric`
- Layout: x=8, y=0, w=8, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Approximated nested count(count()) as COUNT_DISTINCT(cpu); PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Approximated nested count(count()) as COUNT_DISTINCT(cpu)

**Verdict:** MINOR_ISSUE

#### Memory

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
((node_memory_MemTotal{instance=~"$server:.*"} - node_memory_MemAvailable{instance=~"$server:.*"}) / node_memory_MemTotal{instance=~"$server:.*"}) * 100
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemTotal IS NOT NULL OR node_memory_MemAvailable IS NOT NULL
| STATS node_memory_MemTotal_instance = AVG(node_memory_MemTotal), node_memory_MemAvailable_instance = AVG(node_memory_MemAvailable) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (((node_memory_MemTotal_instance - node_memory_MemAvailable_instance) / node_memory_MemTotal_instance) * 100)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
| KEEP time_bucket, computed_value
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Collapsed all series of `node_memory_MemTotal` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Collapsed all series of `node_memory_MemAvailable` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Visual IR:**

- Kibana type: `metric`
- Layout: x=16, y=0, w=8, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `node_memory_MemTotal` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_MemAvailable` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `node_memory_MemTotal` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_MemAvailable` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Verdict:** MINOR_ISSUE

#### Swap

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
(node_memory_SwapTotal{instance=~'$server:.*'} - node_memory_SwapFree{instance=~'$server:.*'})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE node_memory_SwapTotal IS NOT NULL OR node_memory_SwapFree IS NOT NULL
| STATS node_memory_SwapTotal_instance = AVG(node_memory_SwapTotal), node_memory_SwapFree_instance = AVG(node_memory_SwapFree) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (node_memory_SwapTotal_instance - node_memory_SwapFree_instance)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Collapsed all series of `node_memory_SwapTotal` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Collapsed all series of `node_memory_SwapFree` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Visual IR:**

- Kibana type: `metric`
- Layout: x=24, y=0, w=8, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `node_memory_SwapTotal` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_SwapFree` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `node_memory_SwapTotal` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_SwapFree` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Verdict:** MINOR_ISSUE

#### Disk space

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (singlestat):**

```
min((node_filesystem_size{fstype=~"xfs|ext4",instance=~"$server:.*"} - node_filesystem_free{fstype=~"xfs|ext4",instance=~"$server:.*"} )/ node_filesystem_size{fstype=~"xfs|ext4",instance=~"$server:.*"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=unknown backend=ast
- `query_classifiers` / `fragment_guardrails` → Aggregating over a per-element / between two time-series (min(A / B)) cannot be expressed accurately in ES|QL; rewrite as a ratio of aggregates if the series are label-aligned

**Query IR:**

- Family: `unknown`
- Outer agg: `min`
- Binary op: `/`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=32, y=0, w=8, h=6
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Aggregating over a per-element / between two time-series (min(A / B)) cannot be expressed accurately in ES|QL; rewrite as a ratio of aggregates if the series are label-aligned

**Verdict:** EXPECTED_LIMITATION

#### Uptime

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
time() - node_boot_time{instance=~"$server:.*"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
| WHERE node_boot_time IS NOT NULL
| STATS start_time_ms = MAX(node_boot_time * 1000)
| EVAL node_boot_time_uptime_seconds = DATE_DIFF("seconds", TO_DATETIME(start_time_ms), NOW())
| KEEP node_boot_time_uptime_seconds
```

**Query IR:**

- Family: `uptime`
- Metric: `node_boot_time`
- Binary op: `-`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `node_boot_time_uptime_seconds`
- Semantic losses: Dropped variable-driven label filters during migration, Approximated time() - metric as uptime from metric timestamp

**Visual IR:**

- Kibana type: `metric`
- Layout: x=40, y=0, w=8, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration; Approximated time() - metric as uptime from metric timestamp

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated time() - metric as uptime from metric timestamp

**Verdict:** MINOR_ISSUE

#### CPU Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
100 - (avg by (instance) (irate(node_cpu{mode="idle",instance=~"$server:.*"}[5m])) * 100)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE mode == "idle"
| WHERE node_cpu IS NOT NULL
| STATS node_cpu_mode_idle_irate_avg = AVG(IRATE(node_cpu, 5m)) BY time_bucket = TBUCKET(5 minute), instance
| EVAL node_cpu_mode_idle_irate_avg_calc = node_cpu_mode_idle_irate_avg * 100
| EVAL computed_value = (100 - node_cpu_mode_idle_irate_avg_calc)
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

- Kibana type: `area`
- Layout: x=0, y=6, w=12, h=9
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Disk I/O

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
-irate(node_disk_bytes_read{instance=~"$server:.*"}[5m]) ||| irate(node_disk_bytes_written{instance=~"$server:.*"}[5m])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE node_disk_bytes_read IS NOT NULL OR node_disk_bytes_written IS NOT NULL
| STATS node_disk_bytes_read_A_rhs = AVG(IRATE(node_disk_bytes_read, 5m)), node_disk_bytes_written_B = AVG(IRATE(node_disk_bytes_written, 5m)) BY time_bucket = TBUCKET(5 minute), device
| EVAL OUT_on = (0 - node_disk_bytes_read_A_rhs)
| EVAL IN_on = node_disk_bytes_written_B
| KEEP time_bucket, device, OUT_on, IN_on
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `-`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `OUT_on`
- Output groups: `time_bucket, device`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=12, y=6, w=12, h=9
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Network Traffic

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(irate(node_network_receive_bytes{instance=~"$server:.*"}[5m])) ||| - sum(irate(node_network_transmit_bytes{instance=~"$server:.*"}[5m]))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE node_network_receive_bytes IS NOT NULL OR node_network_transmit_bytes IS NOT NULL
| STATS node_network_receive_bytes_A = SUM(IRATE(node_network_receive_bytes, 5m)), node_network_transmit_bytes_B = SUM(IRATE(node_network_transmit_bytes, 5m)) BY time_bucket = TBUCKET(5 minute)
| EVAL RECEIVED = node_network_receive_bytes_A
| EVAL SENT = (-1 * node_network_transmit_bytes_B)
| KEEP time_bucket, RECEIVED, SENT
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `node_network_receive_bytes`
- Range func: `irate`
- Range window: `5m`
- Outer agg: `sum`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `RECEIVED`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=6, w=12, h=9
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Memory

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
node_memory_MemTotal{instance=~"$server:.*"} ||| node_memory_MemTotal{instance=~"$server:.*"} - node_memory_MemAvailable{instance=~"$server:.*"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemTotal IS NOT NULL OR node_memory_MemAvailable IS NOT NULL
| STATS node_memory_MemTotal_A = AVG(node_memory_MemTotal), node_memory_MemTotal_B_lhs = AVG(node_memory_MemTotal), node_memory_MemAvailable_B_rhs = AVG(node_memory_MemAvailable) BY time_bucket = TBUCKET(5 minute), instance
| EVAL RAM_Total = node_memory_MemTotal_A
| EVAL RAM_Used = (node_memory_MemTotal_B_lhs - node_memory_MemAvailable_B_rhs)
| KEEP time_bucket, instance, RAM_Total, RAM_Used
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `node_memory_MemTotal`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `RAM_Total`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `area`
- Layout: x=36, y=6, w=12, h=9
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Received Network Traffic per Container

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(rate(container_network_receive_bytes_total{container_label_namespace=~"$namespace",instance=~"$server:.*"}[3m])) by (name)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE container_network_receive_bytes_total IS NOT NULL
| STATS container_network_receive_bytes_total = SUM(RATE(container_network_receive_bytes_total, 3m)) BY time_bucket = TBUCKET(5 minute), name
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `container_network_receive_bytes_total`
- Range func: `rate`
- Range window: `3m`
- Outer agg: `sum`
- Group labels: `name`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_network_receive_bytes_total`
- Output groups: `time_bucket, name`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=15, w=24, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Sent Network Traffic per Container

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(rate(container_network_transmit_bytes_total{container_label_namespace=~"$namespace",instance=~"$server:.*"}[5m])) by (name)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE container_network_transmit_bytes_total IS NOT NULL
| STATS container_network_transmit_bytes_total = SUM(RATE(container_network_transmit_bytes_total, 5m)) BY time_bucket = TBUCKET(5 minute), name
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `container_network_transmit_bytes_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `name`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_network_transmit_bytes_total`
- Output groups: `time_bucket, name`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=15, w=24, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### CPU Usage per Container

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(rate(container_cpu_usage_seconds_total{container_label_namespace=~"$namespace",instance=~"$server:.*"}[5m])) by (name) * 100
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=scaled_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family scaled_agg bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family` → translated scaled aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE container_cpu_usage_seconds_total IS NOT NULL
| STATS container_cpu_usage_seconds_total = SUM(RATE(container_cpu_usage_seconds_total, 5m)) BY time_bucket = TBUCKET(5 minute), name
| EVAL container_cpu_usage_seconds_total_calc = container_cpu_usage_seconds_total * 100
| KEEP time_bucket, name, container_cpu_usage_seconds_total_calc
| SORT time_bucket ASC
```

**Query IR:**

- Family: `scaled_agg`
- Metric: `container_cpu_usage_seconds_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `name`
- Binary op: `*`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_cpu_usage_seconds_total_calc`
- Output groups: `time_bucket, name`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=26, w=16, h=10
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Memory Usage per Container

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(container_memory_rss{container_label_namespace=~"$namespace",instance=~"$server:.*"}) by (name)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE container_memory_rss IS NOT NULL
| STATS container_memory_rss = SUM(container_memory_rss) BY time_bucket = TBUCKET(5 minute), name
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `container_memory_rss`
- Outer agg: `sum`
- Group labels: `name`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_memory_rss`
- Output groups: `time_bucket, name`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=16, y=26, w=16, h=10
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Memory Swap per Container

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(container_memory_swap{container_label_namespace=~"$namespace",instance=~"$server:.*"}) by (name)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE container_memory_swap IS NOT NULL
| STATS container_memory_swap = SUM(container_memory_swap) BY time_bucket = TBUCKET(5 minute), name
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `container_memory_swap`
- Outer agg: `sum`
- Group labels: `name`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_memory_swap`
- Output groups: `time_bucket, name`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=32, y=26, w=16, h=10
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
<summary>Controls / Variables (2)</summary>

- `Node` (type: `options`)
- `Container Namespace` (type: `options`)

</details>

---

### Grafana: Docker and system monitoring

**File:** `docker-system-893.json` — **Panels:** 20

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Uptime | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | time() - node_boot_time{instance=~"$server:.*"} | FROM metrics-prometheus-* \| WHERE node_boot_time IS NOT NULL \| STATS start_tim... |
| Containers | `singlestat` → `metric` | migrated | **CORRECT** | count(rate(container_last_seen{name=~".+"}[$interval])) | TS metrics-prometheus-* \| WHERE container_last_seen IS NOT NULL \| STATS contai... |
| Disk space | `singlestat` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | min((node_filesystem_size{fstype=~"xfs\|ext4",instance=~"$server:.*"} - node_fil... | — |
| Memory | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | ((node_memory_MemTotal{instance=~"$server:.*"} - node_memory_MemAvailable{instan... | TS metrics-prometheus-* \| WHERE node_memory_MemTotal IS NOT NULL OR node_memory... |
| Swap | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | (node_memory_SwapTotal{instance=~'$server:.*'} - node_memory_SwapFree{instance=~... | TS metrics-prometheus-* \| WHERE node_memory_SwapTotal IS NOT NULL OR node_memor... |
| Load | `singlestat` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | node_load1{instance=~"$server:.*"} / count by(job, instance)(count by(job, insta... | — |
| Network Traffic | `graph` → `line` | migrated | **CORRECT** | sum(rate(container_network_receive_bytes_total{id="/"}[$interval])) by (id) \|\|... | TS metrics-prometheus-* \| WHERE id == "/" \| WHERE container_network_receive_by... |
| CPU Usage | `graph` → `area` | migrated | **CORRECT** | sum(rate(process_cpu_seconds_total[$interval])) * 100 | TS metrics-prometheus-* \| WHERE process_cpu_seconds_total IS NOT NULL \| STATS ... |
| Load | `graph` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | node_load1{instance=~"$server:.*"} / count by(job, instance)(count by(job, insta... | — |
| Used Disk Space | `graph` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_filesystem_size{fstype="aufs"} - node_filesystem_free{fstype="aufs"} | TS metrics-prometheus-* \| WHERE fstype == "aufs" \| WHERE node_filesystem_size ... |
| Available Memory | `graph` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_MemTotal - node_memory_MemAvailable | TS metrics-prometheus-* \| WHERE node_memory_MemTotal IS NOT NULL OR node_memory... |
| Disk I/O | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | -sum(rate(node_disk_bytes_read[$interval])) by (device) \|\|\| sum(rate(node_dis... | TS metrics-prometheus-* \| WHERE node_disk_bytes_read IS NOT NULL OR node_disk_b... |
| Received Network Traffic per Container | `graph` → `line` | migrated | **CORRECT** | sum(rate(container_network_receive_bytes_total{name=~".+"}[$interval])) by (name... | TS metrics-prometheus-* \| WHERE container_network_receive_bytes_total IS NOT NU... |
| Sent Network Traffic per Container | `graph` → `line` | migrated | **CORRECT** | sum(rate(container_network_transmit_bytes_total{name=~".+"}[$interval])) by (nam... | TS metrics-prometheus-* \| WHERE container_network_transmit_bytes_total IS NOT N... |
| CPU Usage per Container | `graph` → `area` | migrated | **CORRECT** | sum(rate(container_cpu_usage_seconds_total{name=~".+"}[$interval])) by (name) * ... | TS metrics-prometheus-* \| WHERE container_cpu_usage_seconds_total IS NOT NULL \... |
| Memory Usage per Container | `graph` → `area` | migrated | **CORRECT** | sum(container_memory_rss{name=~".+"}) by (name) | TS metrics-prometheus-* \| WHERE container_memory_rss IS NOT NULL \| STATS conta... |
| Memory Swap per Container | `graph` → `area` | migrated | **CORRECT** | sum(container_memory_swap{name=~".+"}) by (name) | TS metrics-prometheus-* \| WHERE container_memory_swap IS NOT NULL \| STATS cont... |
| Usage memory | `table` → `datatable` | migrated | **CORRECT** | container_memory_usage_bytes{name=~".+"} | TS metrics-prometheus-* \| WHERE container_memory_usage_bytes IS NOT NULL \| STA... |
| Remaining memory | `table` → `datatable` | migrated | **CORRECT** | sum(100 - ((container_spec_memory_limit_bytes{name=~".+"} - container_memory_usa... | FROM metrics-prometheus-* \| STATS container_spec_memory_limit_bytes = SUM(conta... |
| Limit memory | `table` → `datatable` | migrated | **CORRECT** | sum(container_spec_memory_limit_bytes{name=~".+"}) by (name)  | TS metrics-prometheus-* \| WHERE container_spec_memory_limit_bytes IS NOT NULL \... |

<details>
<summary>Detailed traces (20 panels)</summary>

#### Uptime

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
time() - node_boot_time{instance=~"$server:.*"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
FROM metrics-prometheus-*
| WHERE node_boot_time IS NOT NULL
| STATS start_time_ms = MAX(node_boot_time * 1000)
| EVAL node_boot_time_uptime_seconds = DATE_DIFF("seconds", TO_DATETIME(start_time_ms), NOW())
| KEEP node_boot_time_uptime_seconds
```

**Query IR:**

- Family: `uptime`
- Metric: `node_boot_time`
- Binary op: `-`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `node_boot_time_uptime_seconds`
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

**Warnings:** Dropped variable-driven label filters during migration; Approximated time() - metric as uptime from metric timestamp

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated time() - metric as uptime from metric timestamp

**Verdict:** MINOR_ISSUE

#### Containers

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
count(rate(container_last_seen{name=~".+"}[$interval]))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE container_last_seen IS NOT NULL
| STATS container_last_seen = COUNT(RATE(container_last_seen, 5m)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), container_last_seen = MAX(container_last_seen)
| KEEP time_bucket, container_last_seen
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `container_last_seen`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `count`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_last_seen`

**Visual IR:**

- Kibana type: `metric`
- Layout: x=8, y=0, w=8, h=8
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Verdict:** CORRECT

#### Disk space

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (singlestat):**

```
min((node_filesystem_size{fstype=~"xfs|ext4",instance=~"$server:.*"} - node_filesystem_free{fstype=~"xfs|ext4",instance=~"$server:.*"} )/ node_filesystem_size{fstype=~"xfs|ext4",instance=~"$server:.*"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=unknown backend=ast
- `query_classifiers` / `fragment_guardrails` → Aggregating over a per-element / between two time-series (min(A / B)) cannot be expressed accurately in ES|QL; rewrite as a ratio of aggregates if the series are label-aligned

**Query IR:**

- Family: `unknown`
- Outer agg: `min`
- Binary op: `/`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=16, y=0, w=8, h=8
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Aggregating over a per-element / between two time-series (min(A / B)) cannot be expressed accurately in ES|QL; rewrite as a ratio of aggregates if the series are label-aligned

**Verdict:** EXPECTED_LIMITATION

#### Memory

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
((node_memory_MemTotal{instance=~"$server:.*"} - node_memory_MemAvailable{instance=~"$server:.*"}) / node_memory_MemTotal{instance=~"$server:.*"}) * 100
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemTotal IS NOT NULL OR node_memory_MemAvailable IS NOT NULL
| STATS node_memory_MemTotal_instance = AVG(node_memory_MemTotal), node_memory_MemAvailable_instance = AVG(node_memory_MemAvailable) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (((node_memory_MemTotal_instance - node_memory_MemAvailable_instance) / node_memory_MemTotal_instance) * 100)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
| KEEP time_bucket, computed_value
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Collapsed all series of `node_memory_MemTotal` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Collapsed all series of `node_memory_MemAvailable` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Visual IR:**

- Kibana type: `metric`
- Layout: x=24, y=0, w=8, h=8
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `node_memory_MemTotal` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_MemAvailable` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `node_memory_MemTotal` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_MemAvailable` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Verdict:** MINOR_ISSUE

#### Swap

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
(node_memory_SwapTotal{instance=~'$server:.*'} - node_memory_SwapFree{instance=~'$server:.*'})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE node_memory_SwapTotal IS NOT NULL OR node_memory_SwapFree IS NOT NULL
| STATS node_memory_SwapTotal_instance = AVG(node_memory_SwapTotal), node_memory_SwapFree_instance = AVG(node_memory_SwapFree) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (node_memory_SwapTotal_instance - node_memory_SwapFree_instance)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Collapsed all series of `node_memory_SwapTotal` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Collapsed all series of `node_memory_SwapFree` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Visual IR:**

- Kibana type: `metric`
- Layout: x=32, y=0, w=8, h=8
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `node_memory_SwapTotal` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_SwapFree` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `node_memory_SwapTotal` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_SwapFree` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Verdict:** MINOR_ISSUE

#### Load

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (singlestat):**

```
node_load1{instance=~"$server:.*"} / count by(job, instance)(count by(job, instance, cpu)(node_cpu{instance=~"$server:.*"}))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`

**Query IR:**

- Family: `binary_expr`
- Binary op: `/`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=40, y=0, w=8, h=8
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** PromQL arithmetic with divergent filters/groupings cannot be translated safely yet

**Verdict:** EXPECTED_LIMITATION

#### Network Traffic

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(rate(container_network_receive_bytes_total{id="/"}[$interval])) by (id) ||| - sum(rate(container_network_transmit_bytes_total{id="/"}[$interval])) by (id)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE id == "/"
| WHERE container_network_receive_bytes_total IS NOT NULL OR container_network_transmit_bytes_total IS NOT NULL
| STATS container_network_receive_bytes_total_A = SUM(RATE(container_network_receive_bytes_total, 5m)), container_network_transmit_bytes_total_B = SUM(RATE(container_network_transmit_bytes_total, 5m)) BY time_bucket = TBUCKET(5 minute), id
| EVAL RECEIVED = container_network_receive_bytes_total_A
| EVAL SENT = (-1 * container_network_transmit_bytes_total_B)
| KEEP time_bucket, id, RECEIVED, SENT
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `container_network_receive_bytes_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `id`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `RECEIVED`
- Output groups: `time_bucket, id`

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=8, h=9
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Verdict:** CORRECT

#### CPU Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(rate(process_cpu_seconds_total[$interval])) * 100
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=scaled_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family scaled_agg bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family` → translated scaled aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE process_cpu_seconds_total IS NOT NULL
| STATS process_cpu_seconds_total = SUM(RATE(process_cpu_seconds_total, 5m)) BY time_bucket = TBUCKET(5 minute)
| EVAL process_cpu_seconds_total_calc = process_cpu_seconds_total * 100
| KEEP time_bucket, process_cpu_seconds_total_calc
| SORT time_bucket ASC
```

**Query IR:**

- Family: `scaled_agg`
- Metric: `process_cpu_seconds_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `sum`
- Binary op: `*`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `process_cpu_seconds_total_calc`
- Output groups: `time_bucket`

**Visual IR:**

- Kibana type: `area`
- Layout: x=8, y=0, w=8, h=9
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 5

**Verdict:** CORRECT

#### Load

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (graph):**

```
node_load1{instance=~"$server:.*"} / count by(job, instance)(count by(job, instance, cpu)(node_cpu{instance=~"$server:.*"}))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
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
- Layout: x=16, y=0, w=8, h=9
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** PromQL arithmetic with divergent filters/groupings cannot be translated safely yet

**Verdict:** EXPECTED_LIMITATION

#### Used Disk Space

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
node_filesystem_size{fstype="aufs"} - node_filesystem_free{fstype="aufs"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE fstype == "aufs"
| WHERE node_filesystem_size IS NOT NULL OR node_filesystem_free IS NOT NULL
| STATS node_filesystem_size_fstype_aufs = AVG(node_filesystem_size), node_filesystem_free_fstype_aufs = AVG(node_filesystem_free) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (node_filesystem_size_fstype_aufs - node_filesystem_free_fstype_aufs)
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Collapsed all series of `node_filesystem_size` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Collapsed all series of `node_filesystem_free` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Visual IR:**

- Kibana type: `area`
- Layout: x=24, y=0, w=8, h=9
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `node_filesystem_size` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_filesystem_free` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `node_filesystem_size` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_filesystem_free` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Verdict:** MINOR_ISSUE

#### Available Memory

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
node_memory_MemTotal - node_memory_MemAvailable
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemTotal IS NOT NULL OR node_memory_MemAvailable IS NOT NULL
| STATS node_memory_MemTotal = AVG(node_memory_MemTotal), node_memory_MemAvailable = AVG(node_memory_MemAvailable) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (node_memory_MemTotal - node_memory_MemAvailable)
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Collapsed all series of `node_memory_MemTotal` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Collapsed all series of `node_memory_MemAvailable` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Visual IR:**

- Kibana type: `area`
- Layout: x=32, y=0, w=8, h=9
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 16

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `node_memory_MemTotal` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_MemAvailable` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `node_memory_MemTotal` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_MemAvailable` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Verdict:** MINOR_ISSUE

#### Disk I/O

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
-sum(rate(node_disk_bytes_read[$interval])) by (device) ||| sum(rate(node_disk_bytes_written[$interval])) by (device)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE node_disk_bytes_read IS NOT NULL OR node_disk_bytes_written IS NOT NULL
| STATS node_disk_bytes_read_A_rhs = SUM(RATE(node_disk_bytes_read, 5m)), node_disk_bytes_written_B = SUM(RATE(node_disk_bytes_written, 5m)) BY time_bucket = TBUCKET(5 minute), device
| EVAL OUT_on = (0 - node_disk_bytes_read_A_rhs)
| EVAL IN_on = node_disk_bytes_written_B
| KEEP time_bucket, device, OUT_on, IN_on
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `-`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `OUT_on`
- Output groups: `time_bucket, device`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math

**Visual IR:**

- Kibana type: `line`
- Layout: x=40, y=0, w=8, h=9
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math

**Verdict:** MINOR_ISSUE

#### Received Network Traffic per Container

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(rate(container_network_receive_bytes_total{name=~".+"}[$interval])) by (name)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE container_network_receive_bytes_total IS NOT NULL
| STATS container_network_receive_bytes_total = SUM(RATE(container_network_receive_bytes_total, 5m)) BY time_bucket = TBUCKET(5 minute), name
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `container_network_receive_bytes_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `name`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_network_receive_bytes_total`
- Output groups: `time_bucket, name`

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Verdict:** CORRECT

#### Sent Network Traffic per Container

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(rate(container_network_transmit_bytes_total{name=~".+"}[$interval])) by (name)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE container_network_transmit_bytes_total IS NOT NULL
| STATS container_network_transmit_bytes_total = SUM(RATE(container_network_transmit_bytes_total, 5m)) BY time_bucket = TBUCKET(5 minute), name
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `container_network_transmit_bytes_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `name`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_network_transmit_bytes_total`
- Output groups: `time_bucket, name`

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=0, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Verdict:** CORRECT

#### CPU Usage per Container

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(rate(container_cpu_usage_seconds_total{name=~".+"}[$interval])) by (name) * 100
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=scaled_agg backend=ast
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family scaled_agg bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family`
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family` → translated scaled aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE container_cpu_usage_seconds_total IS NOT NULL
| STATS container_cpu_usage_seconds_total = SUM(RATE(container_cpu_usage_seconds_total, 5m)) BY time_bucket = TBUCKET(5 minute), name
| EVAL container_cpu_usage_seconds_total_calc = container_cpu_usage_seconds_total * 100
| KEEP time_bucket, name, container_cpu_usage_seconds_total_calc
| SORT time_bucket ASC
```

**Query IR:**

- Family: `scaled_agg`
- Metric: `container_cpu_usage_seconds_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `name`
- Binary op: `*`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_cpu_usage_seconds_total_calc`
- Output groups: `time_bucket, name`

**Visual IR:**

- Kibana type: `area`
- Layout: x=0, y=0, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Verdict:** CORRECT

</details>

<details>
<summary>Controls / Variables (2)</summary>

- `Container Group` (type: `options`)
- `Node` (type: `options`)

</details>

---

### Grafana: Express Prometheus Middleware

**File:** `express-14565.json` — **Panels:** 24

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| HTTP Requests | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Count by class | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | sum(  http_requests_total{instance="$instance",status=~".{1,2}"} or  on() label_... | TS metrics-prometheus-* \| WHERE status RLIKE ".{1,2}" \| WHERE http_requests_to... |
| Request duration average by request | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | http_request_duration_seconds_sum{instance="$instance"} / http_request_duration_... | TS metrics-prometheus-* \| WHERE http_request_duration_seconds_sum IS NOT NULL O... |
| Request count by request | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | http_requests_total{instance="$instance"} | TS metrics-prometheus-* \| WHERE http_requests_total IS NOT NULL \| STATS http_r... |
| Request duration 95th percentile | `timeseries` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | histogram_quantile(0.95, sum by (job, le) (rate(http_request_duration_seconds_bu... | — |
| Request duration 99th percentile | `timeseries` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | histogram_quantile(0.99, sum by (job, le) (rate(http_request_duration_seconds_bu... | — |
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note` → noted or-vector zero-fill approximation
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
TS metrics-prometheus-*
| WHERE status RLIKE ".{1,2}"
| WHERE http_requests_total IS NOT NULL
| STATS http_requests_total = SUM(LAST_OVER_TIME(http_requests_total)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), http_requests_total = MAX(http_requests_total)
| KEEP time_bucket, http_requests_total
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `http_requests_total`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `http_requests_total`
- Semantic losses: Dropped variable-driven label filters during migration, Approximated PromQL 'or vector(N)' zero-fill fallback by dropping the constant operand; time ranges with no data appear as gaps instead of the fallback value, Panel has 7 PromQL targets but only 1 could be migrated

**Visual IR:**

- Kibana type: `gauge`
- Layout: x=0, y=0, w=48, h=12
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 7

**Warnings:** Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value; Approximated PromQL 'or vector(N)' zero-fill fallback by dropping the constant operand; time ranges with no data appear as gaps instead of the fallback value; Panel has 7 PromQL targets but only 1 could be migrated

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated PromQL 'or vector(N)' zero-fill fallback by dropping the constant operand; time ranges with no data appear as gaps instead of the fallback value; Panel has 7 PromQL targets but only 1 could be migrated

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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE http_request_duration_seconds_sum IS NOT NULL OR http_request_duration_seconds_count IS NOT NULL
| STATS http_request_duration_seconds_sum_instance = MAX(LAST_OVER_TIME(http_request_duration_seconds_sum)), http_request_duration_seconds_count_instance = MAX(LAST_OVER_TIME(http_request_duration_seconds_count)) BY time_bucket = TBUCKET(5 minute), method, path, status
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE http_requests_total IS NOT NULL
| STATS http_requests_total = MAX(LAST_OVER_TIME(http_requests_total)) BY time_bucket = TBUCKET(5 minute), method, path, status
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

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (timeseries):**

```
histogram_quantile(0.95, sum by (job, le) (rate(http_request_duration_seconds_bucket{instance="$instance"}[$__rate_interval])))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=histogram_quantile backend=ast
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family` → histogram_quantile field type unsupported
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`

**Query IR:**

- Family: `histogram_quantile`
- Metric: `http_request_duration_seconds`
- Range window: `5m`
- Group labels: `job`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=0, y=24, w=24, h=12
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** histogram_quantile target field type could not be determined; cannot safely translate to ES|QL PERCENTILE() (verify the base metric is a histogram or exponential_histogram field on the target index)

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** EXPECTED_LIMITATION

#### Request duration 99th percentile

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (timeseries):**

```
histogram_quantile(0.99, sum by (job, le) (rate(http_request_duration_seconds_bucket{instance="$instance"}[$__rate_interval])))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=histogram_quantile backend=ast
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family` → histogram_quantile field type unsupported
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`

**Query IR:**

- Family: `histogram_quantile`
- Metric: `http_request_duration_seconds`
- Range window: `5m`
- Group labels: `job`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=24, y=24, w=24, h=12
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** histogram_quantile target field type could not be determined; cannot safely translate to ES|QL PERCENTILE() (verify the base metric is a histogram or exponential_histogram field on the target index)

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** EXPECTED_LIMITATION

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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE le == "0.005"
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(5 minute), method, path, status
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE le == "0.01"
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(5 minute), method, path, status
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE le == "0.025"
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(5 minute), method, path, status
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE le == "0.05"
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(5 minute), method, path, status
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE le == "0.1"
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(5 minute), method, path, status
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE le == "0.25"
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(5 minute), method, path, status
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE le == "0.5"
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(5 minute), method, path, status
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE (le == "1" OR le == "1.0")
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(5 minute), method, path, status
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE le == "2.5"
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(5 minute), method, path, status
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE (le == "5" OR le == "5.0")
| WHERE http_request_duration_seconds_bucket IS NOT NULL
| STATS http_request_duration_seconds_bucket = MAX(LAST_OVER_TIME(http_request_duration_seconds_bucket)) BY time_bucket = TBUCKET(5 minute), method, path, status
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

- `Instance:` (type: `options`)
- `Node Exporter:` (type: `options`)

</details>

---

### Grafana: Flagger Canary Status

**File:** `flagger-canary-15158.json` — **Panels:** 38

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Flagger Rollout Status: $canary.$namespace | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Requests, Error, Duration (RED) - Only usable for Istio-enabled Services | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Utilization, Saturation, Errors (USE) | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Infrastructure Resources and Events | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Instructions | `text` → `markdown` | migrated | **EXPECTED_LIMITATION** | — | — |
| Primary: Image Tag | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | kube_pod_container_info{namespace="$namespace",pod=~"$primary-.*",container!~"PO... | TS metrics-prometheus-* \| WHERE NOT (container RLIKE "POD\|istio-proxy") \| WHE... |
| Flagger Canary: Last Result | `stat` → `datatable` | migrated_with_warnings | **MINOR_ISSUE** | sum by (cluster) (flagger_canary_status{namespace="$namespace",name=~"$canary",a... | TS metrics-prometheus-* \| WHERE flagger_canary_status IS NOT NULL \| STATS flag... |
| Canary: Image Tag | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | kube_pod_container_info{namespace="$namespace",pod=~"$canary-.*",pod!~".primary.... | TS metrics-prometheus-* \| WHERE NOT (pod RLIKE ".primary.*") \| WHERE NOT (cont... |
| Primary: Healthy POD Replicas | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | kube_deployment_status_replicas_available{namespace=~"$namespace", deployment=~"... | TS metrics-prometheus-* \| WHERE kube_deployment_status_replicas_available IS NO... |
| Primary: Flagger Weighting | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | flagger_canary_weight{namespace=~"$namespace",workload=~"$primary",account=~"$aw... | TS metrics-prometheus-* \| WHERE flagger_canary_weight IS NOT NULL \| STATS flag... |
| Canary: Flagger Weighting | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | flagger_canary_weight{namespace=~"$namespace",workload=~"$canary",account=~"$aws... | TS metrics-prometheus-* \| WHERE flagger_canary_weight IS NOT NULL \| STATS flag... |
| Canary: Healthy POD Replicas | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | kube_deployment_status_replicas_available{namespace=~"$namespace", deployment=~"... | TS metrics-prometheus-* \| WHERE kube_deployment_status_replicas_available IS NO... |
| Primary: Incoming Request Volume | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | round(sum by (cluster)(rate(istio_requests_total{reporter="destination",destinat... | TS metrics-prometheus-* \| WHERE reporter == "destination" \| WHERE istio_reques... |
| Primary: Incoming Success Rate | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum by (cluster)(irate(istio_requests_total{reporter="destination",destination_w... | TS metrics-prometheus-* \| WHERE reporter == "destination" \| WHERE istio_reques... |
| Canary: Incoming Success Rate | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum by (cluster)(irate(istio_requests_total{reporter="destination",destination_w... | TS metrics-prometheus-* \| WHERE reporter == "destination" \| WHERE istio_reques... |
| Canary: Incoming Request Volume | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | round(sum by (cluster)(rate(istio_requests_total{reporter="destination",destinat... | TS metrics-prometheus-* \| WHERE reporter == "destination" \| WHERE istio_reques... |
| Primary: Request Duration | `timeseries` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | histogram_quantile(0.50, sum(irate(istio_request_duration_milliseconds_bucket{re... | — |
| Canary: Request Duration | `timeseries` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | histogram_quantile(0.50, sum(irate(istio_request_duration_milliseconds_bucket{re... | — |
| Primary: Incoming Requests by Source And Response Code | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | round(sum(irate(istio_requests_total{connection_security_policy="mutual_tls", de... | TS metrics-prometheus-* \| WHERE reporter == "destination" \| WHERE istio_reques... |
| Canary: Incoming Requests by Source And Response Code | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | round(sum(irate(istio_requests_total{connection_security_policy="mutual_tls", de... | TS metrics-prometheus-* \| WHERE reporter == "destination" \| WHERE istio_reques... |
| Primary: Outgoing Requests by Destination And Response Code | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | round(sum(irate(istio_requests_total{connection_security_policy="mutual_tls", so... | TS metrics-prometheus-* \| WHERE reporter == "source" \| WHERE istio_requests_to... |
| Canary: Outgoing Requests by Destination And Response Code | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | round(sum(irate(istio_requests_total{connection_security_policy="mutual_tls", so... | TS metrics-prometheus-* \| WHERE reporter == "source" \| WHERE istio_requests_to... |
| Primary: App CPU Usage (Avg all PODs, excludes Istio proxy) | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | 100 - (avg by (cluster)(rate(container_cpu_usage_seconds_total{cpu="total",names... | TS metrics-prometheus-* \| WHERE container_cpu_usage_seconds_total IS NOT NULL O... |
| Canary: App CPU Usage (Avg all PODs, excludes Istio proxy) | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | 100 - (avg by (cluster)(rate(container_cpu_usage_seconds_total{cpu="total",names... | TS metrics-prometheus-* \| WHERE container_cpu_usage_seconds_total IS NOT NULL O... |
| Primary: Max POD Memory Usage | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | max by (cluster)(sum by (cluster,pod) (container_memory_working_set_bytes{namesp... | FROM metrics-prometheus-* \| WHERE NOT (container RLIKE "POD\|istio-proxy") \| S... |
| Canary: Max POD Memory Usage | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | max by (cluster)(sum by (cluster,pod) (container_memory_working_set_bytes{namesp... | FROM metrics-prometheus-* \| WHERE NOT (container RLIKE "POD\|istio-proxy") \| S... |
| Primary: Network I/O | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum by (cluster)(rate (container_network_receive_bytes_total{namespace="$namespa... | TS metrics-prometheus-* \| WHERE container_network_receive_bytes_total IS NOT NU... |
| Canary: Network I/O | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum by (cluster)(rate (container_network_receive_bytes_total{namespace="$namespa... | TS metrics-prometheus-* \| WHERE container_network_receive_bytes_total IS NOT NU... |
| Primary: Disk I/O (IOPS) | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum by (cluster)(rate (container_fs_reads_total{namespace="$namespace",pod=~"$pr... | TS metrics-prometheus-* \| WHERE container_fs_reads_total IS NOT NULL OR contain... |
| Canary: Disk I/O (IOPS) | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum by (cluster)(rate (container_fs_reads_total{namespace="$namespace",pod=~"$ca... | TS metrics-prometheus-* \| WHERE container_fs_reads_total IS NOT NULL OR contain... |
| Primary: Node & AZ Distribution | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | sum(count(count(container_cpu_usage_seconds_total{namespace="$namespace",pod=~"$... | FROM metrics-prometheus-* \| WHERE NOT (container RLIKE "POD\|istio-proxy") \| S... |
| Canary: Node & AZ Distribution | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | sum(count(count(container_cpu_usage_seconds_total{namespace="$namespace",pod=~"$... | FROM metrics-prometheus-* \| WHERE NOT (pod RLIKE ".*primary.*") \| WHERE NOT (c... |
| Primary: Workload Replicas (PODs) | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | kube_horizontalpodautoscaler_status_current_replicas{namespace="$namespace",hori... | TS metrics-prometheus-* \| WHERE kube_horizontalpodautoscaler_status_current_rep... |
| Canary: Workload Replicas (PODs) | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | kube_horizontalpodautoscaler_status_current_replicas{namespace="$namespace",hori... | TS metrics-prometheus-* \| WHERE kube_horizontalpodautoscaler_status_current_rep... |
| Primary: PODs by replicaSet | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | avg(kube_replicaset_status_ready_replicas{namespace="$namespace",replicaset=~"$p... | TS metrics-prometheus-* \| WHERE kube_replicaset_status_ready_replicas IS NOT NU... |
| Canary: PODs by replicaSet | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | avg(kube_replicaset_status_ready_replicas{namespace="$namespace",replicaset=~"$c... | TS metrics-prometheus-* \| WHERE NOT (replicaset RLIKE ".*primary.*") \| WHERE k... |
| Primary: CPU Throttling | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | avg by (cluster)(rate(container_cpu_cfs_throttled_seconds_total{namespace="$name... | TS metrics-prometheus-* \| WHERE container_cpu_cfs_throttled_seconds_total IS NO... |
| Canary: CPU Throttling | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | avg by (cluster)(rate(container_cpu_cfs_throttled_seconds_total{namespace="$name... | TS metrics-prometheus-* \| WHERE NOT (pod RLIKE ".*primary.*") \| WHERE containe... |

<details>
<summary>Detailed traces (33 panels)</summary>

#### Primary: Image Tag

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
kube_pod_container_info{namespace="$namespace",pod=~"$primary-.*",container!~"POD|istio-proxy",account=~"$awsaccount",cluster=~"$cluster"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE NOT (container RLIKE "POD|istio-proxy")
| WHERE kube_pod_container_info IS NOT NULL
| STATS kube_pod_container_info = MAX(LAST_OVER_TIME(kube_pod_container_info)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kube_pod_container_info = MAX(kube_pod_container_info)
| KEEP time_bucket, kube_pod_container_info
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `kube_pod_container_info`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kube_pod_container_info`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=0, y=0, w=20, h=3
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Flagger Canary: Last Result

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
sum by (cluster) (flagger_canary_status{namespace="$namespace",name=~"$canary",account=~"$awsaccount",cluster=~"$cluster"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → approximated grouped stat as datatable

**Translated (datatable):**

```
TS metrics-prometheus-*
| WHERE flagger_canary_status IS NOT NULL
| STATS flagger_canary_status = SUM(flagger_canary_status) BY time_bucket = TBUCKET(5 minute), cluster
| SORT time_bucket ASC
| STATS flagger_canary_status = MAX(flagger_canary_status) BY cluster
| KEEP cluster, flagger_canary_status
```

**Query IR:**

- Family: `simple_agg`
- Metric: `flagger_canary_status`
- Outer agg: `sum`
- Group labels: `cluster`
- Output shape: `table`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `flagger_canary_status`
- Output groups: `cluster`
- Semantic losses: Dropped variable-driven label filters during migration, Approximated grouped stat panel as summary table

**Visual IR:**

- Kibana type: `datatable`
- Layout: x=20, y=0, w=8, h=8
- Presentation kind: `esql`
- Config keys: type, query, metrics, breakdowns

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration; Approximated grouped stat panel as summary table

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated grouped stat panel as summary table

**Verdict:** MINOR_ISSUE

#### Canary: Image Tag

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
kube_pod_container_info{namespace="$namespace",pod=~"$canary-.*",pod!~".primary.*",container!~"POD|istio-proxy",account=~"$awsaccount",cluster=~"$cluster"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE NOT (pod RLIKE ".primary.*")
| WHERE NOT (container RLIKE "POD|istio-proxy")
| WHERE kube_pod_container_info IS NOT NULL
| STATS kube_pod_container_info = MAX(LAST_OVER_TIME(kube_pod_container_info)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kube_pod_container_info = MAX(kube_pod_container_info)
| KEEP time_bucket, kube_pod_container_info
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `kube_pod_container_info`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kube_pod_container_info`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=28, y=0, w=20, h=3
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Primary: Healthy POD Replicas

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
kube_deployment_status_replicas_available{namespace=~"$namespace", deployment=~"$primary",account=~"$awsaccount",cluster=~"$cluster"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE kube_deployment_status_replicas_available IS NOT NULL
| STATS kube_deployment_status_replicas_available = MAX(LAST_OVER_TIME(kube_deployment_status_replicas_available)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `kube_deployment_status_replicas_available`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kube_deployment_status_replicas_available`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=3, w=10, h=6
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Primary: Flagger Weighting

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
flagger_canary_weight{namespace=~"$namespace",workload=~"$primary",account=~"$awsaccount",cluster=~"$cluster"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE flagger_canary_weight IS NOT NULL
| STATS flagger_canary_weight = MAX(LAST_OVER_TIME(flagger_canary_weight)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `flagger_canary_weight`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `flagger_canary_weight`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=10, y=3, w=10, h=6
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Canary: Flagger Weighting

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
flagger_canary_weight{namespace=~"$namespace",workload=~"$canary",account=~"$awsaccount",cluster=~"$cluster"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE flagger_canary_weight IS NOT NULL
| STATS flagger_canary_weight = MAX(LAST_OVER_TIME(flagger_canary_weight)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `flagger_canary_weight`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `flagger_canary_weight`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=28, y=3, w=10, h=6
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Canary: Healthy POD Replicas

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
kube_deployment_status_replicas_available{namespace=~"$namespace", deployment=~"$canary",account=~"$awsaccount",cluster=~"$cluster"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE kube_deployment_status_replicas_available IS NOT NULL
| STATS kube_deployment_status_replicas_available = MAX(LAST_OVER_TIME(kube_deployment_status_replicas_available)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `kube_deployment_status_replicas_available`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kube_deployment_status_replicas_available`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=38, y=3, w=10, h=6
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Primary: Incoming Request Volume

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
round(sum by (cluster)(rate(istio_requests_total{reporter="destination",destination_workload_namespace=~"$namespace",destination_workload=~"$primary",account=~"$awsaccount",cluster=~"$cluster"}[$__rate_interval])), 0.001)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms` → applied value wrapper transforms: round
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE reporter == "destination"
| WHERE istio_requests_total IS NOT NULL
| STATS istio_requests_total = SUM(RATE(istio_requests_total, 5m)) BY time_bucket = TBUCKET(5 minute), cluster
| EVAL istio_requests_total = ROUND(istio_requests_total / 0.001) * 0.001
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `istio_requests_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `cluster`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `istio_requests_total`
- Output groups: `time_bucket, cluster`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=12, h=8
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration; round(v, step) emitted as ROUND(v / step) * step

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Primary: Incoming Success Rate

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum by (cluster)(irate(istio_requests_total{reporter="destination",destination_workload_namespace=~"$namespace",destination_workload=~"$primary",response_code!~"5.*",account=~"$awsaccount",cluster=~"$cluster"}[$__rate_interval])) / sum by (cluster)(irate(istio_requests_total{reporter="destination",destination_workload_namespace=~"$namespace",destination_workload=~"$primary",account=~"$awsaccount",cluster=~"$cluster"}[$__rate_interval]))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE reporter == "destination"
| WHERE istio_requests_total IS NOT NULL
| STATS istio_requests_total_reporter_destination_response_code_5_irate_sum = SUM(IRATE(CASE((NOT (response_code RLIKE "5.*")), istio_requests_total, NULL), 5m)), istio_requests_total_reporter_destination_irate_sum = SUM(IRATE(istio_requests_total, 5m)) BY time_bucket = TBUCKET(5 minute), cluster
| EVAL computed_value = (istio_requests_total_reporter_destination_response_code_5_irate_sum / istio_requests_total_reporter_destination_irate_sum)
| KEEP time_bucket, cluster, computed_value
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
- Output groups: `time_bucket, cluster`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=12, y=0, w=12, h=8
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Canary: Incoming Success Rate

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum by (cluster)(irate(istio_requests_total{reporter="destination",destination_workload_namespace=~"$namespace",destination_workload=~"$canary",response_code!~"5.*",account=~"$awsaccount",cluster=~"$cluster"}[$__rate_interval])) / sum by (cluster)(irate(istio_requests_total{reporter="destination",destination_workload_namespace=~"$namespace",destination_workload=~"$canary",account=~"$awsaccount",cluster=~"$cluster"}[$__rate_interval]))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE reporter == "destination"
| WHERE istio_requests_total IS NOT NULL
| STATS istio_requests_total_reporter_destination_response_code_5_irate_sum = SUM(IRATE(CASE((NOT (response_code RLIKE "5.*")), istio_requests_total, NULL), 5m)), istio_requests_total_reporter_destination_irate_sum = SUM(IRATE(istio_requests_total, 5m)) BY time_bucket = TBUCKET(5 minute), cluster
| EVAL computed_value = (istio_requests_total_reporter_destination_response_code_5_irate_sum / istio_requests_total_reporter_destination_irate_sum)
| KEEP time_bucket, cluster, computed_value
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
- Output groups: `time_bucket, cluster`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=0, w=12, h=8
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Canary: Incoming Request Volume

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
round(sum by (cluster)(rate(istio_requests_total{reporter="destination",destination_workload_namespace=~"$namespace",destination_workload=~"$canary",account=~"$awsaccount",cluster=~"$cluster"}[$__rate_interval])), 0.001)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms` → applied value wrapper transforms: round
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE reporter == "destination"
| WHERE istio_requests_total IS NOT NULL
| STATS istio_requests_total = SUM(RATE(istio_requests_total, 5m)) BY time_bucket = TBUCKET(5 minute), cluster
| EVAL istio_requests_total = ROUND(istio_requests_total / 0.001) * 0.001
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `istio_requests_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `cluster`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `istio_requests_total`
- Output groups: `time_bucket, cluster`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=36, y=0, w=12, h=8
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration; round(v, step) emitted as ROUND(v / step) * step

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Primary: Request Duration

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (timeseries):**

```
histogram_quantile(0.50, sum(irate(istio_request_duration_milliseconds_bucket{reporter="destination",destination_workload=~"$primary", destination_workload_namespace=~"$namespace",account=~"$awsaccount",cluster=~"$cluster"}[$__rate_interval])) by (cluster,le)) / 1000
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `binary_expr_family` → arithmetic operand unsupported; marked not_feasible
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`

**Query IR:**

- Family: `binary_expr`
- Binary op: `/`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Semantic losses: PromQL '/' arithmetic where an operand contains a nested set operator or vector-matching join that cannot be safely combined; marked for manual review so data is not silently dropped

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=0, y=8, w=24, h=7
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 4

**Warnings:** PromQL '/' arithmetic where an operand contains a nested set operator or vector-matching join that cannot be safely combined; marked for manual review so data is not silently dropped

**Semantic losses:** PromQL '/' arithmetic where an operand contains a nested set operator or vector-matching join that cannot be safely combined; marked for manual review so data is not silently dropped

**Verdict:** EXPECTED_LIMITATION

#### Canary: Request Duration

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (timeseries):**

```
histogram_quantile(0.50, sum(irate(istio_request_duration_milliseconds_bucket{reporter="destination",destination_workload=~"$canary", destination_workload_namespace=~"$namespace",account=~"$awsaccount",cluster=~"$cluster"}[$__rate_interval])) by (cluster,le)) / 1000
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `binary_expr_family` → arithmetic operand unsupported; marked not_feasible
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`

**Query IR:**

- Family: `binary_expr`
- Binary op: `/`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Semantic losses: PromQL '/' arithmetic where an operand contains a nested set operator or vector-matching join that cannot be safely combined; marked for manual review so data is not silently dropped

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=24, y=8, w=24, h=7
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 4

**Warnings:** PromQL '/' arithmetic where an operand contains a nested set operator or vector-matching join that cannot be safely combined; marked for manual review so data is not silently dropped

**Semantic losses:** PromQL '/' arithmetic where an operand contains a nested set operator or vector-matching join that cannot be safely combined; marked for manual review so data is not silently dropped

**Verdict:** EXPECTED_LIMITATION

#### Primary: Incoming Requests by Source And Response Code

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
round(sum(irate(istio_requests_total{connection_security_policy="mutual_tls", destination_workload_namespace=~"$namespace", destination_workload=~"$primary", reporter="destination",account=~"$awsaccount",cluster=~"$cluster"}[$__rate_interval])) by (cluster,source_workload, source_workload_namespace, response_code), 0.001) ||| round(sum(irate(istio_requests_total{connection_security_policy!="mutual_tls", destination_workload_namespace=~"$namespace", destination_workload=~"$primary", reporter="destination",account=~"$awsaccount",cluster=~"$cluster"}[$__rate_interval])) by (cluster,source_workload, source_workload_namespace, response_code), 0.001)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms` → applied value wrapper transforms: round
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE reporter == "destination"
| WHERE istio_requests_total IS NOT NULL
| STATS istio_requests_total_B = SUM(IRATE(CASE((connection_security_policy == "mutual_tls"), istio_requests_total, NULL), 5m)), istio_requests_total_A = SUM(IRATE(CASE((connection_security_policy != "mutual_tls"), istio_requests_total, NULL), 5m)) BY time_bucket = TBUCKET(5 minute), cluster, source_workload, source_workload_namespace, response_code
| EVAL mTLS = istio_requests_total_B
| EVAL value = istio_requests_total_A
| EVAL legend = CONCAT(COALESCE(TO_STRING(source_workload), ""), ".", COALESCE(TO_STRING(source_workload_namespace), ""), " : ", COALESCE(TO_STRING(response_code), ""), " (?mTLS) (", COALESCE(TO_STRING(cluster), ""), ")")
| KEEP time_bucket, cluster, source_workload, source_workload_namespace, response_code, mTLS, value, legend
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `istio_requests_total`
- Range func: `irate`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `cluster, source_workload, source_workload_namespace, response_code`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `mTLS`
- Output groups: `time_bucket, cluster, source_workload, source_workload_namespace, response_code`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=15, w=24, h=9
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Dropped variable-driven label filters during migration; round(v, step) emitted as ROUND(v / step) * step

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Canary: Incoming Requests by Source And Response Code

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
round(sum(irate(istio_requests_total{connection_security_policy="mutual_tls", destination_workload_namespace=~"$namespace", destination_workload=~"$canary", reporter="destination",account=~"$awsaccount",cluster=~"$cluster"}[$__rate_interval])) by (cluster,source_workload, source_workload_namespace, response_code), 0.001) ||| round(sum(irate(istio_requests_total{connection_security_policy!="mutual_tls", destination_workload_namespace=~"$namespace", destination_workload=~"$canary", reporter="destination",account=~"$awsaccount",cluster=~"$cluster"}[$__rate_interval])) by (cluster,source_workload, source_workload_namespace, response_code), 0.001)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms` → applied value wrapper transforms: round
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE reporter == "destination"
| WHERE istio_requests_total IS NOT NULL
| STATS istio_requests_total_B = SUM(IRATE(CASE((connection_security_policy == "mutual_tls"), istio_requests_total, NULL), 5m)), istio_requests_total_A = SUM(IRATE(CASE((connection_security_policy != "mutual_tls"), istio_requests_total, NULL), 5m)) BY time_bucket = TBUCKET(5 minute), cluster, source_workload, source_workload_namespace, response_code
| EVAL mTLS = istio_requests_total_B
| EVAL value = istio_requests_total_A
| EVAL legend = CONCAT(COALESCE(TO_STRING(source_workload), ""), ".", COALESCE(TO_STRING(source_workload_namespace), ""), " : ", COALESCE(TO_STRING(response_code), ""), " (?mTLS) (", COALESCE(TO_STRING(cluster), ""), ")")
| KEEP time_bucket, cluster, source_workload, source_workload_namespace, response_code, mTLS, value, legend
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `istio_requests_total`
- Range func: `irate`
- Range window: `5m`
- Outer agg: `sum`
- Group labels: `cluster, source_workload, source_workload_namespace, response_code`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `mTLS`
- Output groups: `time_bucket, cluster, source_workload, source_workload_namespace, response_code`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=15, w=24, h=9
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Dropped variable-driven label filters during migration; round(v, step) emitted as ROUND(v / step) * step

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated count() over a comparison by distinct series
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE scrape_duration_seconds IS NOT NULL
| STATS scrape_duration_seconds = MAX(LAST_OVER_TIME(scrape_duration_seconds)) BY time_bucket = TBUCKET(5 minute)
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemAvailable_bytes IS NOT NULL OR node_memory_MemTotal_bytes IS NOT NULL
| STATS node_memory_MemAvailable_bytes = AVG(node_memory_MemAvailable_bytes), node_memory_MemTotal_bytes = AVG(node_memory_MemTotal_bytes) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = ((1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel`
- `panel_translators` / `datatable_panel` → mapped to datatable panel

**Translated (datatable):**

```
TS metrics-prometheus-*
| WHERE up IS NOT NULL
| STATS up = MAX(LAST_OVER_TIME(up)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
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

### Grafana: Kubernetes cluster monitoring (via Prometheus)

**File:** `k8s-cluster-monitoring-315.json` — **Panels:** 21

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Network I/O pressure | `graph` → `line` | migrated | **MINOR_ISSUE** | sum (rate (container_network_receive_bytes_total{kubernetes_io_hostname=~"^$Node... | TS metrics-prometheus-* \| WHERE container_network_receive_bytes_total IS NOT NU... |
| Cluster memory usage | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum (container_memory_working_set_bytes{id="/",kubernetes_io_hostname=~"^$Node$"... | TS metrics-prometheus-* \| WHERE container_memory_working_set_bytes IS NOT NULL ... |
| Used | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | sum (container_memory_working_set_bytes{id="/",kubernetes_io_hostname=~"^$Node$"... | TS metrics-prometheus-* \| WHERE id == "/" \| WHERE container_memory_working_set... |
| Total | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | sum (machine_memory_bytes{kubernetes_io_hostname=~"^$Node$"}) | TS metrics-prometheus-* \| WHERE machine_memory_bytes IS NOT NULL \| STATS machi... |
| Cluster CPU usage (1m avg) | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum (rate (container_cpu_usage_seconds_total{id="/",kubernetes_io_hostname=~"^$N... | TS metrics-prometheus-* \| WHERE container_cpu_usage_seconds_total IS NOT NULL O... |
| Used | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | sum (rate (container_cpu_usage_seconds_total{id="/",kubernetes_io_hostname=~"^$N... | TS metrics-prometheus-* \| WHERE id == "/" \| WHERE container_cpu_usage_seconds_... |
| Total | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | sum (machine_cpu_cores{kubernetes_io_hostname=~"^$Node$"}) | TS metrics-prometheus-* \| WHERE machine_cpu_cores IS NOT NULL \| STATS machine_... |
| Cluster filesystem usage | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum (container_fs_usage_bytes{device=~"^/dev/[sv]d[a-z][1-9]$",id="/",kubernetes... | TS metrics-prometheus-* \| WHERE device RLIKE "/dev/[sv]d[a-z][1-9]" \| WHERE id... |
| Used | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | sum (container_fs_usage_bytes{device=~"^/dev/[sv]d[a-z][1-9]$",id="/",kubernetes... | TS metrics-prometheus-* \| WHERE device RLIKE "/dev/[sv]d[a-z][1-9]" \| WHERE id... |
| Total | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | sum (container_fs_limit_bytes{device=~"^/dev/[sv]d[a-z][1-9]$",id="/",kubernetes... | TS metrics-prometheus-* \| WHERE device RLIKE "/dev/[sv]d[a-z][1-9]" \| WHERE id... |
| Pods CPU usage (1m avg) | `graph` → `line` | migrated | **MINOR_ISSUE** | sum (rate (container_cpu_usage_seconds_total{image!="",name=~"^k8s_.*",kubernete... | TS metrics-prometheus-* \| WHERE image != "" \| WHERE name RLIKE "k8s_.*" \| WHE... |
| System services CPU usage (1m avg) | `graph` → `line` | migrated | **MINOR_ISSUE** | sum (rate (container_cpu_usage_seconds_total{systemd_service_name!="",kubernetes... | TS metrics-prometheus-* \| WHERE systemd_service_name != "" \| WHERE container_c... |
| Containers CPU usage (1m avg) | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum (rate (container_cpu_usage_seconds_total{image!="",name=~"^k8s_.*",container... | TS metrics-prometheus-* \| WHERE image != "" \| WHERE name RLIKE "k8s_.*" \| WHE... |
| All processes CPU usage (1m avg) | `graph` → `line` | migrated | **MINOR_ISSUE** | sum (rate (container_cpu_usage_seconds_total{id!="/",kubernetes_io_hostname=~"^$... | TS metrics-prometheus-* \| WHERE id != "/" \| WHERE container_cpu_usage_seconds_... |
| Pods memory usage | `graph` → `line` | migrated | **MINOR_ISSUE** | sum (container_memory_working_set_bytes{image!="",name=~"^k8s_.*",kubernetes_io_... | TS metrics-prometheus-* \| WHERE image != "" \| WHERE name RLIKE "k8s_.*" \| WHE... |
| System services memory usage | `graph` → `line` | migrated | **MINOR_ISSUE** | sum (container_memory_working_set_bytes{systemd_service_name!="",kubernetes_io_h... | TS metrics-prometheus-* \| WHERE systemd_service_name != "" \| WHERE container_m... |
| Containers memory usage | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum (container_memory_working_set_bytes{image!="",name=~"^k8s_.*",container_name... | TS metrics-prometheus-* \| WHERE image != "" \| WHERE name RLIKE "k8s_.*" \| WHE... |
| All processes memory usage | `graph` → `line` | migrated | **MINOR_ISSUE** | sum (container_memory_working_set_bytes{id!="/",kubernetes_io_hostname=~"^$Node$... | TS metrics-prometheus-* \| WHERE id != "/" \| WHERE container_memory_working_set... |
| Pods network I/O (1m avg) | `graph` → `line` | migrated | **MINOR_ISSUE** | sum (rate (container_network_receive_bytes_total{image!="",name=~"^k8s_.*",kuber... | TS metrics-prometheus-* \| WHERE image != "" \| WHERE name RLIKE "k8s_.*" \| WHE... |
| Containers network I/O (1m avg) | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum (rate (container_network_receive_bytes_total{image!="",name=~"^k8s_.*",kuber... | TS metrics-prometheus-* \| WHERE image != "" \| WHERE name RLIKE "k8s_.*" \| WHE... |
| All processes network I/O (1m avg) | `graph` → `line` | migrated | **MINOR_ISSUE** | sum (rate (container_network_receive_bytes_total{id!="/",kubernetes_io_hostname=... | TS metrics-prometheus-* \| WHERE id != "/" \| WHERE container_network_receive_by... |

<details>
<summary>Detailed traces (21 panels)</summary>

#### Network I/O pressure

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum (rate (container_network_receive_bytes_total{kubernetes_io_hostname=~"^$Node$"}[1m])) ||| - sum (rate (container_network_transmit_bytes_total{kubernetes_io_hostname=~"^$Node$"}[1m]))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE container_network_receive_bytes_total IS NOT NULL OR container_network_transmit_bytes_total IS NOT NULL
| STATS container_network_receive_bytes_total_A = SUM(RATE(container_network_receive_bytes_total, 1m)), container_network_transmit_bytes_total_B = SUM(RATE(container_network_transmit_bytes_total, 1m)) BY time_bucket = TBUCKET(5 minute)
| EVAL Received = container_network_receive_bytes_total_A
| EVAL Sent = (-1 * container_network_transmit_bytes_total_B)
| KEEP time_bucket, Received, Sent
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `container_network_receive_bytes_total`
- Range func: `rate`
- Range window: `1m`
- Outer agg: `sum`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `Received`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=48, h=9
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Cluster memory usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum (container_memory_working_set_bytes{id="/",kubernetes_io_hostname=~"^$Node$"}) / sum (machine_memory_bytes{kubernetes_io_hostname=~"^$Node$"}) * 100
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE container_memory_working_set_bytes IS NOT NULL OR machine_memory_bytes IS NOT NULL
| STATS container_memory_working_set_bytes_id_sum = SUM(CASE((id == "/"), container_memory_working_set_bytes, NULL)), machine_memory_bytes_kubernetes_io_hostname_sum = SUM(machine_memory_bytes) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = ((container_memory_working_set_bytes_id_sum / machine_memory_bytes_kubernetes_io_hostname_sum) * 100)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
| KEEP time_bucket, computed_value
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

- Kibana type: `metric`
- Layout: x=0, y=0, w=16, h=12
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Used

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum (container_memory_working_set_bytes{id="/",kubernetes_io_hostname=~"^$Node$"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE id == "/"
| WHERE container_memory_working_set_bytes IS NOT NULL
| STATS container_memory_working_set_bytes = SUM(container_memory_working_set_bytes) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), container_memory_working_set_bytes = MAX(container_memory_working_set_bytes)
| KEEP time_bucket, container_memory_working_set_bytes
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `container_memory_working_set_bytes`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_memory_working_set_bytes`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=0, y=0, w=8, h=12
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Total

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum (machine_memory_bytes{kubernetes_io_hostname=~"^$Node$"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE machine_memory_bytes IS NOT NULL
| STATS machine_memory_bytes = SUM(machine_memory_bytes) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), machine_memory_bytes = MAX(machine_memory_bytes)
| KEEP time_bucket, machine_memory_bytes
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `machine_memory_bytes`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `machine_memory_bytes`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=8, y=0, w=8, h=12
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Cluster CPU usage (1m avg)

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum (rate (container_cpu_usage_seconds_total{id="/",kubernetes_io_hostname=~"^$Node$"}[1m])) / sum (machine_cpu_cores{kubernetes_io_hostname=~"^$Node$"}) * 100
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE container_cpu_usage_seconds_total IS NOT NULL OR machine_cpu_cores IS NOT NULL
| STATS container_cpu_usage_seconds_total_id_rate_sum = SUM(RATE(CASE((id == "/"), container_cpu_usage_seconds_total, NULL), 1m)), machine_cpu_cores_kubernetes_io_hostname_sum = SUM(SUM_OVER_TIME(machine_cpu_cores, 5m)) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = ((container_cpu_usage_seconds_total_id_rate_sum / machine_cpu_cores_kubernetes_io_hostname_sum) * 100)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
| KEEP time_bucket, computed_value
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

- Kibana type: `metric`
- Layout: x=16, y=0, w=16, h=12
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Used

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum (rate (container_cpu_usage_seconds_total{id="/",kubernetes_io_hostname=~"^$Node$"}[1m]))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE id == "/"
| WHERE container_cpu_usage_seconds_total IS NOT NULL
| STATS container_cpu_usage_seconds_total = SUM(RATE(container_cpu_usage_seconds_total, 1m)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), container_cpu_usage_seconds_total = MAX(container_cpu_usage_seconds_total)
| KEEP time_bucket, container_cpu_usage_seconds_total
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `container_cpu_usage_seconds_total`
- Range func: `rate`
- Range window: `1m`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_cpu_usage_seconds_total`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=16, y=0, w=8, h=12
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Total

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum (machine_cpu_cores{kubernetes_io_hostname=~"^$Node$"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE machine_cpu_cores IS NOT NULL
| STATS machine_cpu_cores = SUM(machine_cpu_cores) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), machine_cpu_cores = MAX(machine_cpu_cores)
| KEEP time_bucket, machine_cpu_cores
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `machine_cpu_cores`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `machine_cpu_cores`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=24, y=0, w=8, h=12
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Cluster filesystem usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum (container_fs_usage_bytes{device=~"^/dev/[sv]d[a-z][1-9]$",id="/",kubernetes_io_hostname=~"^$Node$"}) / sum (container_fs_limit_bytes{device=~"^/dev/[sv]d[a-z][1-9]$",id="/",kubernetes_io_hostname=~"^$Node$"}) * 100
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE device RLIKE "/dev/[sv]d[a-z][1-9]"
| WHERE id == "/"
| WHERE container_fs_usage_bytes IS NOT NULL OR container_fs_limit_bytes IS NOT NULL
| STATS container_fs_usage_bytes_device_dev_sv_d_a_id_sum = SUM(container_fs_usage_bytes), container_fs_limit_bytes_device_dev_sv_d_a_id_sum = SUM(container_fs_limit_bytes) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = ((container_fs_usage_bytes_device_dev_sv_d_a_id_sum / container_fs_limit_bytes_device_dev_sv_d_a_id_sum) * 100)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
| KEEP time_bucket, computed_value
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

- Kibana type: `metric`
- Layout: x=32, y=0, w=16, h=12
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Used

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum (container_fs_usage_bytes{device=~"^/dev/[sv]d[a-z][1-9]$",id="/",kubernetes_io_hostname=~"^$Node$"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE device RLIKE "/dev/[sv]d[a-z][1-9]"
| WHERE id == "/"
| WHERE container_fs_usage_bytes IS NOT NULL
| STATS container_fs_usage_bytes = SUM(container_fs_usage_bytes) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), container_fs_usage_bytes = MAX(container_fs_usage_bytes)
| KEEP time_bucket, container_fs_usage_bytes
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `container_fs_usage_bytes`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_fs_usage_bytes`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=32, y=0, w=8, h=12
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Total

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum (container_fs_limit_bytes{device=~"^/dev/[sv]d[a-z][1-9]$",id="/",kubernetes_io_hostname=~"^$Node$"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE device RLIKE "/dev/[sv]d[a-z][1-9]"
| WHERE id == "/"
| WHERE container_fs_limit_bytes IS NOT NULL
| STATS container_fs_limit_bytes = SUM(container_fs_limit_bytes) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), container_fs_limit_bytes = MAX(container_fs_limit_bytes)
| KEEP time_bucket, container_fs_limit_bytes
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `container_fs_limit_bytes`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_fs_limit_bytes`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=40, y=0, w=8, h=12
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Pods CPU usage (1m avg)

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum (rate (container_cpu_usage_seconds_total{image!="",name=~"^k8s_.*",kubernetes_io_hostname=~"^$Node$"}[1m])) by (pod_name)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE image != ""
| WHERE name RLIKE "k8s_.*"
| WHERE container_cpu_usage_seconds_total IS NOT NULL
| STATS container_cpu_usage_seconds_total = SUM(RATE(container_cpu_usage_seconds_total, 1m)) BY time_bucket = TBUCKET(5 minute), pod_name
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `container_cpu_usage_seconds_total`
- Range func: `rate`
- Range window: `1m`
- Outer agg: `sum`
- Group labels: `pod_name`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_cpu_usage_seconds_total`
- Output groups: `time_bucket, pod_name`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=48, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### System services CPU usage (1m avg)

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum (rate (container_cpu_usage_seconds_total{systemd_service_name!="",kubernetes_io_hostname=~"^$Node$"}[1m])) by (systemd_service_name)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE systemd_service_name != ""
| WHERE container_cpu_usage_seconds_total IS NOT NULL
| STATS container_cpu_usage_seconds_total = SUM(RATE(container_cpu_usage_seconds_total, 1m)) BY time_bucket = TBUCKET(5 minute), systemd_service_name
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `container_cpu_usage_seconds_total`
- Range func: `rate`
- Range window: `1m`
- Outer agg: `sum`
- Group labels: `systemd_service_name`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_cpu_usage_seconds_total`
- Output groups: `time_bucket, systemd_service_name`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=48, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Containers CPU usage (1m avg)

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum (rate (container_cpu_usage_seconds_total{image!="",name=~"^k8s_.*",container_name!="POD",kubernetes_io_hostname=~"^$Node$"}[1m])) by (container_name, pod_name) ||| sum (rate (container_cpu_usage_seconds_total{image!="",name!~"^k8s_.*",kubernetes_io_hostname=~"^$Node$"}[1m])) by (kubernetes_io_hostname, name, image) ||| sum (rate (container_cpu_usage_seconds_total{rkt_container_name!="",kubernetes_io_hostname=~"^$Node$"}[1m])) by (kubernetes_io_hostname, rkt_container_name)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE image != ""
| WHERE name RLIKE "k8s_.*"
| WHERE container_name != "POD"
| WHERE container_cpu_usage_seconds_total IS NOT NULL
| STATS container_cpu_usage_seconds_total = SUM(RATE(container_cpu_usage_seconds_total, 1m)) BY time_bucket = TBUCKET(5 minute), container_name, pod_name
| EVAL legend = CONCAT("pod: ", COALESCE(TO_STRING(pod_name), ""), " | ", COALESCE(TO_STRING(container_name), ""))
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `container_cpu_usage_seconds_total`
- Range func: `rate`
- Range window: `1m`
- Outer agg: `sum`
- Group labels: `container_name, pod_name`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_cpu_usage_seconds_total`
- Output groups: `time_bucket, container_name, pod_name`
- Semantic losses: Dropped variable-driven label filters during migration, Panel has 3 PromQL targets but only 1 could be migrated

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=48, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 3

**Warnings:** Panel has 3 PromQL targets but only 1 could be migrated

**Semantic losses:** Dropped variable-driven label filters during migration; Panel has 3 PromQL targets but only 1 could be migrated

**Verdict:** MINOR_ISSUE

#### All processes CPU usage (1m avg)

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum (rate (container_cpu_usage_seconds_total{id!="/",kubernetes_io_hostname=~"^$Node$"}[1m])) by (id)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE id != "/"
| WHERE container_cpu_usage_seconds_total IS NOT NULL
| STATS container_cpu_usage_seconds_total = SUM(RATE(container_cpu_usage_seconds_total, 1m)) BY time_bucket = TBUCKET(5 minute), id
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `container_cpu_usage_seconds_total`
- Range func: `rate`
- Range window: `1m`
- Outer agg: `sum`
- Group labels: `id`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_cpu_usage_seconds_total`
- Output groups: `time_bucket, id`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=48, h=24
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Pods memory usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum (container_memory_working_set_bytes{image!="",name=~"^k8s_.*",kubernetes_io_hostname=~"^$Node$"}) by (pod_name)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE image != ""
| WHERE name RLIKE "k8s_.*"
| WHERE container_memory_working_set_bytes IS NOT NULL
| STATS container_memory_working_set_bytes = SUM(container_memory_working_set_bytes) BY time_bucket = TBUCKET(5 minute), pod_name
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `container_memory_working_set_bytes`
- Outer agg: `sum`
- Group labels: `pod_name`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `container_memory_working_set_bytes`
- Output groups: `time_bucket, pod_name`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=48, h=12
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
<summary>Controls / Variables (1)</summary>

- `Node` (type: `options`)

</details>

---

### Grafana: Kubernetes Cluster (Prometheus)

**File:** `k8s-cluster-prometheus-6417.json` — **Panels:** 35

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Cluster Health | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Deployments | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Node | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Pods | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Containers | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Jobs | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Cluster Pod Usage | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_pod_info{node=~"$node"}) / sum(kube_node_status_allocatable_pods{node=~... | TS metrics-prometheus-* \| WHERE kube_pod_info IS NOT NULL OR kube_node_status_a... |
| Cluster CPU Usage | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_pod_container_resource_requests_cpu_cores{node=~"$node"}) / sum(kube_no... | TS metrics-prometheus-* \| WHERE kube_pod_container_resource_requests_cpu_cores ... |
| Cluster Memory Usage | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_pod_container_resource_requests_memory_bytes{node=~"$node"}) / sum(kube... | TS metrics-prometheus-* \| WHERE kube_pod_container_resource_requests_memory_byt... |
| Cluster Disk Usage | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | (sum (node_filesystem_size{nodename=~"$node"}) - sum (node_filesystem_free{noden... | TS metrics-prometheus-* \| WHERE node_filesystem_size IS NOT NULL OR node_filesy... |
| Cluster Pod Capacity | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_node_status_allocatable_pods{node=~"$node"}) \|\|\| sum(kube_node_statu... | TS metrics-prometheus-* \| WHERE kube_node_status_allocatable_pods IS NOT NULL O... |
| Cluster CPU Capacity | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_node_status_capacity_cpu_cores{node=~"$node"}) \|\|\| sum(kube_node_sta... | TS metrics-prometheus-* \| WHERE kube_node_status_capacity_cpu_cores IS NOT NULL... |
| Cluster Mem Capacity | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_node_status_allocatable_memory_bytes{node=~"$node"}) \|\|\| sum(kube_no... | TS metrics-prometheus-* \| WHERE kube_node_status_allocatable_memory_bytes IS NO... |
| Cluster Disk Capacity | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(node_filesystem_size{nodename=~"$node"}) - sum(node_filesystem_free{nodename... | TS metrics-prometheus-* \| WHERE node_filesystem_size IS NOT NULL OR node_filesy... |
| Deployment Replicas - Up To Date | `table` → `datatable` | migrated_with_warnings | **MINOR_ISSUE** | kube_deployment_status_replicas{namespace=~"$namespace"} | TS metrics-prometheus-* \| WHERE kube_deployment_status_replicas IS NOT NULL \| ... |
| Deployment Replicas | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_deployment_status_replicas{namespace=~"$namespace"}) | TS metrics-prometheus-* \| WHERE kube_deployment_status_replicas IS NOT NULL \| ... |
| Deployment Replicas - Updated | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_deployment_status_replicas_updated{namespace=~"$namespace"}) | TS metrics-prometheus-* \| WHERE kube_deployment_status_replicas_updated IS NOT ... |
| Deployment Replicas - Unavailable | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_deployment_status_replicas_unavailable{namespace=~"$namespace"}) | TS metrics-prometheus-* \| WHERE kube_deployment_status_replicas_unavailable IS ... |
| Number Of Nodes | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_node_info{node=~"$node"}) | TS metrics-prometheus-* \| WHERE kube_node_info IS NOT NULL \| STATS kube_node_i... |
| Nodes Out of Disk | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_node_status_condition{condition="OutOfDisk", node=~"$node", status="tru... | TS metrics-prometheus-* \| WHERE condition == "OutOfDisk" \| WHERE status == "tr... |
| Nodes Unavailable | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_node_spec_unschedulable{node=~"$node"}) | TS metrics-prometheus-* \| WHERE kube_node_spec_unschedulable IS NOT NULL \| STA... |
| Pods Running | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_pod_status_phase{namespace=~"$namespace", phase="Running"}) | TS metrics-prometheus-* \| WHERE phase == "Running" \| WHERE kube_pod_status_pha... |
| Pods Pending | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_pod_status_phase{namespace=~"$namespace", phase="Pending"}) | TS metrics-prometheus-* \| WHERE phase == "Pending" \| WHERE kube_pod_status_pha... |
| Pods Failed | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_pod_status_phase{namespace=~"$namespace", phase="Failed"}) | TS metrics-prometheus-* \| WHERE phase == "Failed" \| WHERE kube_pod_status_phas... |
| Pods Succeeded | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_pod_status_phase{namespace=~"$namespace", phase="Succeeded"}) | TS metrics-prometheus-* \| WHERE phase == "Succeeded" \| WHERE kube_pod_status_p... |
| Pods Unknown | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_pod_status_phase{namespace=~"$namespace", phase="Unknown"}) | TS metrics-prometheus-* \| WHERE phase == "Unknown" \| WHERE kube_pod_status_pha... |
| Containers Running | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_pod_container_status_running{namespace=~"$namespace"}) | TS metrics-prometheus-* \| WHERE kube_pod_container_status_running IS NOT NULL \... |
| Containers Waiting | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_pod_container_status_waiting{namespace=~"$namespace"}) | TS metrics-prometheus-* \| WHERE kube_pod_container_status_waiting IS NOT NULL \... |
| Containers Terminated | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_pod_container_status_terminated{namespace=~"$namespace"}) | TS metrics-prometheus-* \| WHERE kube_pod_container_status_terminated IS NOT NUL... |
| Containers Restarts (Last 30 Minutes) | `singlestat` → `metric` | migrated | **CORRECT** | sum(delta(kube_pod_container_status_restarts{namespace="kube-system"}[30m])) | TS metrics-prometheus-* \| WHERE namespace == "kube-system" \| WHERE kube_pod_co... |
| CPU Cores Requested by Containers | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_pod_container_resource_requests_cpu_cores{namespace=~"$namespace", node... | TS metrics-prometheus-* \| WHERE kube_pod_container_resource_requests_cpu_cores ... |
| Memory Requested By Containers | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_pod_container_resource_requests_memory_bytes{namespace=~"$namespace", n... | TS metrics-prometheus-* \| WHERE kube_pod_container_resource_requests_memory_byt... |
| Jobs Succeeded | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_job_status_succeeded{namespace=~"$namespace"}) | TS metrics-prometheus-* \| WHERE kube_job_status_succeeded IS NOT NULL \| STATS ... |
| Jobs Succeeded | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_job_status_active{namespace=~"$namespace"}) | TS metrics-prometheus-* \| WHERE kube_job_status_active IS NOT NULL \| STATS kub... |
| Jobs Failed | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_job_status_failed{namespace=~"$namespace"}) | TS metrics-prometheus-* \| WHERE kube_job_status_failed IS NOT NULL \| STATS kub... |

<details>
<summary>Detailed traces (29 panels)</summary>

#### Cluster Pod Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(kube_pod_info{node=~"$node"}) / sum(kube_node_status_allocatable_pods{node=~".*"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE kube_pod_info IS NOT NULL OR kube_node_status_allocatable_pods IS NOT NULL
| STATS kube_pod_info_node_sum = SUM(kube_pod_info), kube_node_status_allocatable_pods_node_sum = SUM(kube_node_status_allocatable_pods) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (kube_pod_info_node_sum / kube_node_status_allocatable_pods_node_sum)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
| KEEP time_bucket, computed_value
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=0, y=0, w=12, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Cluster CPU Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(kube_pod_container_resource_requests_cpu_cores{node=~"$node"}) / sum(kube_node_status_allocatable_cpu_cores{node=~"$node"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE kube_pod_container_resource_requests_cpu_cores IS NOT NULL OR kube_node_status_allocatable_cpu_cores IS NOT NULL
| STATS kube_pod_container_resource_requests_cpu_cores_node_sum = SUM(kube_pod_container_resource_requests_cpu_cores), kube_node_status_allocatable_cpu_cores_node_sum = SUM(kube_node_status_allocatable_cpu_cores) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (kube_pod_container_resource_requests_cpu_cores_node_sum / kube_node_status_allocatable_cpu_cores_node_sum)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
| KEEP time_bucket, computed_value
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=12, y=0, w=12, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Cluster Memory Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(kube_pod_container_resource_requests_memory_bytes{node=~"$node"}) / sum(kube_node_status_allocatable_memory_bytes{node=~"$node"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE kube_pod_container_resource_requests_memory_bytes IS NOT NULL OR kube_node_status_allocatable_memory_bytes IS NOT NULL
| STATS kube_pod_container_resource_requests_memory_bytes_node_sum = SUM(kube_pod_container_resource_requests_memory_bytes), kube_node_status_allocatable_memory_bytes_node_sum = SUM(kube_node_status_allocatable_memory_bytes) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (kube_pod_container_resource_requests_memory_bytes_node_sum / kube_node_status_allocatable_memory_bytes_node_sum)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
| KEEP time_bucket, computed_value
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=24, y=0, w=12, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Cluster Disk Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
(sum (node_filesystem_size{nodename=~"$node"}) - sum (node_filesystem_free{nodename=~"$node"})) / sum (node_filesystem_size{nodename=~"$node"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE node_filesystem_size IS NOT NULL OR node_filesystem_free IS NOT NULL
| STATS node_filesystem_size_nodename_sum = SUM(node_filesystem_size), node_filesystem_free_nodename_sum = SUM(node_filesystem_free) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = ((node_filesystem_size_nodename_sum - node_filesystem_free_nodename_sum) / node_filesystem_size_nodename_sum)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
| KEEP time_bucket, computed_value
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=36, y=0, w=12, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Cluster Pod Capacity

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(kube_node_status_allocatable_pods{node=~"$node"}) ||| sum(kube_node_status_capacity_pods{node=~"$node"}) ||| sum(kube_pod_info{node=~"$node"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE kube_node_status_allocatable_pods IS NOT NULL OR kube_node_status_capacity_pods IS NOT NULL OR kube_pod_info IS NOT NULL
| STATS kube_node_status_allocatable_pods_A = SUM(kube_node_status_allocatable_pods), kube_node_status_capacity_pods_B = SUM(kube_node_status_capacity_pods), kube_pod_info_C = SUM(kube_pod_info) BY time_bucket = TBUCKET(5 minute)
| EVAL allocatable = kube_node_status_allocatable_pods_A
| EVAL capacity = kube_node_status_capacity_pods_B
| EVAL requested = kube_pod_info_C
| KEEP time_bucket, allocatable, capacity, requested
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `kube_node_status_allocatable_pods`
- Outer agg: `sum`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `allocatable`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=6, w=12, h=7
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 3

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Cluster CPU Capacity

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(kube_node_status_capacity_cpu_cores{node=~"$node"}) ||| sum(kube_node_status_allocatable_cpu_cores{node=~"$node"}) ||| sum(kube_pod_container_resource_requests_cpu_cores{node=~"$node"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE kube_node_status_capacity_cpu_cores IS NOT NULL OR kube_node_status_allocatable_cpu_cores IS NOT NULL OR kube_pod_container_resource_requests_cpu_cores IS NOT NULL
| STATS kube_node_status_capacity_cpu_cores_A = SUM(kube_node_status_capacity_cpu_cores), kube_node_status_allocatable_cpu_cores_B = SUM(kube_node_status_allocatable_cpu_cores), kube_pod_container_resource_requests_cpu_cores_C = SUM(kube_pod_container_resource_requests_cpu_cores) BY time_bucket = TBUCKET(5 minute)
| EVAL allocatable = kube_node_status_capacity_cpu_cores_A
| EVAL capacity = kube_node_status_allocatable_cpu_cores_B
| EVAL requested = kube_pod_container_resource_requests_cpu_cores_C
| KEEP time_bucket, allocatable, capacity, requested
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `kube_node_status_capacity_cpu_cores`
- Outer agg: `sum`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `allocatable`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=12, y=6, w=12, h=7
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 3

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Cluster Mem Capacity

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(kube_node_status_allocatable_memory_bytes{node=~"$node"}) ||| sum(kube_node_status_capacity_memory_bytes{node=~"$node"}) ||| sum(kube_pod_container_resource_requests_memory_bytes{node=~"$node"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE kube_node_status_allocatable_memory_bytes IS NOT NULL OR kube_node_status_capacity_memory_bytes IS NOT NULL OR kube_pod_container_resource_requests_memory_bytes IS NOT NULL
| STATS kube_node_status_allocatable_memory_bytes_A = SUM(kube_node_status_allocatable_memory_bytes), kube_node_status_capacity_memory_bytes_B = SUM(kube_node_status_capacity_memory_bytes), kube_pod_container_resource_requests_memory_bytes_C = SUM(kube_pod_container_resource_requests_memory_bytes) BY time_bucket = TBUCKET(5 minute)
| EVAL allocatable = kube_node_status_allocatable_memory_bytes_A
| EVAL capacity = kube_node_status_capacity_memory_bytes_B
| EVAL requested = kube_pod_container_resource_requests_memory_bytes_C
| KEEP time_bucket, allocatable, capacity, requested
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `kube_node_status_allocatable_memory_bytes`
- Outer agg: `sum`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `allocatable`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=6, w=12, h=7
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 3

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Cluster Disk Capacity

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(node_filesystem_size{nodename=~"$node"}) - sum(node_filesystem_free{nodename=~"$node"}) ||| sum(node_filesystem_size{nodename=~"$node"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE node_filesystem_size IS NOT NULL OR node_filesystem_free IS NOT NULL
| STATS node_filesystem_size_A_lhs = SUM(node_filesystem_size), node_filesystem_free_A_rhs = SUM(node_filesystem_free), node_filesystem_size_B = SUM(node_filesystem_size) BY time_bucket = TBUCKET(5 minute)
| EVAL usage = (node_filesystem_size_A_lhs - node_filesystem_free_A_rhs)
| EVAL `limit` = node_filesystem_size_B
| KEEP time_bucket, usage, `limit`
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `-`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `usage`
- Output groups: `time_bucket`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=36, y=6, w=12, h=7
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Deployment Replicas - Up To Date

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (table):**

```
kube_deployment_status_replicas{namespace=~"$namespace"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel`
- `panel_translators` / `datatable_panel` → mapped to datatable panel

**Translated (datatable):**

```
TS metrics-prometheus-*
| WHERE kube_deployment_status_replicas IS NOT NULL
| STATS kube_deployment_status_replicas = MAX(LAST_OVER_TIME(kube_deployment_status_replicas)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kube_deployment_status_replicas = MAX(kube_deployment_status_replicas)
| KEEP time_bucket, kube_deployment_status_replicas
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `kube_deployment_status_replicas`
- Output shape: `table`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kube_deployment_status_replicas`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `datatable`
- Layout: x=0, y=0, w=12, h=8
- Presentation kind: `esql`
- Config keys: type, query, metrics, breakdowns

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Deployment Replicas

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(kube_deployment_status_replicas{namespace=~"$namespace"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE kube_deployment_status_replicas IS NOT NULL
| STATS kube_deployment_status_replicas = SUM(kube_deployment_status_replicas) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kube_deployment_status_replicas = MAX(kube_deployment_status_replicas)
| KEEP time_bucket, kube_deployment_status_replicas
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `kube_deployment_status_replicas`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kube_deployment_status_replicas`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=12, y=0, w=12, h=8
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Deployment Replicas - Updated

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(kube_deployment_status_replicas_updated{namespace=~"$namespace"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE kube_deployment_status_replicas_updated IS NOT NULL
| STATS kube_deployment_status_replicas_updated = SUM(kube_deployment_status_replicas_updated) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kube_deployment_status_replicas_updated = MAX(kube_deployment_status_replicas_updated)
| KEEP time_bucket, kube_deployment_status_replicas_updated
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `kube_deployment_status_replicas_updated`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kube_deployment_status_replicas_updated`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=24, y=0, w=12, h=8
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Deployment Replicas - Unavailable

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(kube_deployment_status_replicas_unavailable{namespace=~"$namespace"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE kube_deployment_status_replicas_unavailable IS NOT NULL
| STATS kube_deployment_status_replicas_unavailable = SUM(kube_deployment_status_replicas_unavailable) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kube_deployment_status_replicas_unavailable = MAX(kube_deployment_status_replicas_unavailable)
| KEEP time_bucket, kube_deployment_status_replicas_unavailable
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `kube_deployment_status_replicas_unavailable`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kube_deployment_status_replicas_unavailable`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=36, y=0, w=12, h=8
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Number Of Nodes

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(kube_node_info{node=~"$node"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE kube_node_info IS NOT NULL
| STATS kube_node_info = SUM(kube_node_info) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kube_node_info = MAX(kube_node_info)
| KEEP time_bucket, kube_node_info
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `kube_node_info`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kube_node_info`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=0, y=0, w=16, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Nodes Out of Disk

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(kube_node_status_condition{condition="OutOfDisk", node=~"$node", status="true"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE condition == "OutOfDisk"
| WHERE status == "true"
| WHERE kube_node_status_condition IS NOT NULL
| STATS kube_node_status_condition = SUM(kube_node_status_condition) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kube_node_status_condition = MAX(kube_node_status_condition)
| KEEP time_bucket, kube_node_status_condition
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `kube_node_status_condition`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kube_node_status_condition`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=16, y=0, w=16, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Nodes Unavailable

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(kube_node_spec_unschedulable{node=~"$node"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE kube_node_spec_unschedulable IS NOT NULL
| STATS kube_node_spec_unschedulable = SUM(kube_node_spec_unschedulable) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kube_node_spec_unschedulable = MAX(kube_node_spec_unschedulable)
| KEEP time_bucket, kube_node_spec_unschedulable
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `kube_node_spec_unschedulable`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kube_node_spec_unschedulable`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=32, y=0, w=16, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

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
| CPU Usage | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(node_cpu_seconds_total{mode!~"idle\|iowait\|steal", cluster="$cluster",... | TS metrics-prometheus-* \| WHERE NOT (mode RLIKE "idle\|iowait\|steal") \| WHERE... |
| RAM Usage | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"} - node_memory_Mem... | TS metrics-prometheus-* \| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_... |
| Running Pods | `stat` → `metric` | migrated | **MINOR_ISSUE** | sum(kube_pod_status_phase{phase="Running", cluster="$cluster"}) | TS metrics-prometheus-* \| WHERE phase == "Running" \| WHERE kube_pod_status_pha... |
| Cluster CPU Utilization | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | avg(sum by (instance, cpu) (rate(node_cpu_seconds_total{mode!~"idle\|iowait\|ste... | TS metrics-prometheus-* \| WHERE NOT (mode RLIKE "idle\|iowait\|steal") \| WHERE... |
| Cluster Memory Utilization | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"} - node_memory_Mem... | TS metrics-prometheus-* \| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_... |
| CPU Utilization by namespace | `timeseries` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | sum(rate(container_cpu_usage_seconds_total{image!="", cluster="$cluster"}[$__rat... | — |
| Memory Utilization by namespace | `timeseries` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | sum(container_memory_working_set_bytes{image!="", cluster="$cluster"}) by (names... | — |
| CPU Utilization by instance | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | avg(sum by (instance, cpu) (rate(node_cpu_seconds_total{mode!~"idle\|iowait\|ste... | TS metrics-prometheus-* \| WHERE NOT (mode RLIKE "idle\|iowait\|steal") \| WHERE... |
| Memory Utilization by instance | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(node_memory_MemTotal_bytes{cluster="$cluster", job="$job"} - node_memory_Mem... | TS metrics-prometheus-* \| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_... |
| CPU Throttled seconds by namespace | `timeseries` → `line` | migrated | **MINOR_ISSUE** | sum(rate(container_cpu_cfs_throttled_seconds_total{image!="", cluster="$cluster"... | TS metrics-prometheus-* \| WHERE image != "" \| WHERE container_cpu_cfs_throttle... |
| CPU Core Throttled by instance | `timeseries` → `line` | migrated | **MINOR_ISSUE** | sum(rate(node_cpu_core_throttles_total{cluster="$cluster", job="$job"}[$__rate_i... | TS metrics-prometheus-* \| WHERE node_cpu_core_throttles_total IS NOT NULL \| ST... |
| Kubernetes Pods QoS classes | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(kube_pod_status_qos_class{cluster="$cluster"}) by (qos_class) \|\|\| sum(kub... | TS metrics-prometheus-* \| WHERE kube_pod_status_qos_class IS NOT NULL \| STATS ... |
| Kubernetes Pods Status Reason | `timeseries` → `line` | migrated | **MINOR_ISSUE** | sum(kube_pod_status_reason{cluster="$cluster"}) by (reason) | TS metrics-prometheus-* \| WHERE kube_pod_status_reason IS NOT NULL \| STATS kub... |
| OOM Events by namespace | `timeseries` → `line` | migrated | **MINOR_ISSUE** | sum(increase(container_oom_events_total{cluster="$cluster"}[$__rate_interval])) ... | TS metrics-prometheus-* \| WHERE container_oom_events_total IS NOT NULL \| STATS... |
| Container Restarts by namespace | `timeseries` → `line` | migrated | **MINOR_ISSUE** | sum(increase(kube_pod_container_status_restarts_total{cluster="$cluster"}[$__rat... | TS metrics-prometheus-* \| WHERE kube_pod_container_status_restarts_total IS NOT... |
| Global Network Utilization by device | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(node_network_receive_bytes_total{device!~"(veth\|azv\|lxc).*", cluster=... | TS metrics-prometheus-* \| WHERE NOT (device RLIKE "(veth\|azv\|lxc).*") \| WHER... |
| Network Saturation - Packets dropped | `timeseries` → `line` | migrated | **MINOR_ISSUE** | sum(rate(node_network_receive_drop_total{cluster="$cluster", job="$job"}[$__rate... | TS metrics-prometheus-* \| WHERE node_network_receive_drop_total IS NOT NULL OR ... |
| Network Received by namespace | `timeseries` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | sum(rate(container_network_receive_bytes_total{cluster="$cluster"}[$__rate_inter... | — |
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel` → approximated bargauge panel

**Translated (bar):**

```
TS metrics-prometheus-*
| WHERE kube_pod_container_resource_requests IS NOT NULL OR machine_cpu_cores IS NOT NULL OR kube_pod_container_resource_limits IS NOT NULL
| STATS kube_pod_container_resource_requests_Requests_lhs = SUM(CASE((resource == "cpu"), kube_pod_container_resource_requests, NULL)), machine_cpu_cores_Requests_rhs = SUM(machine_cpu_cores), kube_pod_container_resource_limits_Limits_lhs = SUM(CASE((resource == "cpu"), kube_pod_container_resource_limits, NULL)), machine_cpu_cores_Limits_rhs = SUM(machine_cpu_cores) BY time_bucket = TBUCKET(5 minute)
| EVAL Requests = (kube_pod_container_resource_requests_Requests_lhs / machine_cpu_cores_Requests_rhs)
| EVAL Limits = (kube_pod_container_resource_limits_Limits_lhs / machine_cpu_cores_Limits_rhs)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), Requests = MAX(Requests), Limits = MAX(Limits)
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Dropped 2 incompatible target(s); showing 2 mergeable targets (1 of the dropped targets are Windows-specific), Approximated bargauge as bar chart

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

**Warnings:** Grafana panel has 2 transformation(s); manual review recommended; Approximated PromQL arithmetic using same-bucket ES|QL math; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series; Dropped 2 incompatible target(s); showing 2 mergeable targets (1 of the dropped targets are Windows-specific); Approximated bargauge as bar chart

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Dropped 2 incompatible target(s); showing 2 mergeable targets (1 of the dropped targets are Windows-specific); Approximated bargauge as bar chart

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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel` → approximated bargauge panel

**Translated (bar):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_memory_MemAvailable_bytes IS NOT NULL OR windows_memory_available_bytes IS NOT NULL OR windows_memory_cache_bytes IS NOT NULL OR windows_os_visible_memory_bytes IS NOT NULL OR kube_pod_container_resource_requests IS NOT NULL OR machine_memory_bytes IS NOT NULL OR kube_pod_container_resource_limits IS NOT NULL
| STATS node_memory_MemTotal_bytes_Real_Linux_lhs_lhs = SUM(node_memory_MemTotal_bytes), node_memory_MemAvailable_bytes_Real_Linux_lhs_rhs = SUM(node_memory_MemAvailable_bytes), node_memory_MemTotal_bytes_Real_Linux_rhs = SUM(node_memory_MemTotal_bytes), windows_memory_available_bytes_Real_Windows_lhs_lhs = SUM(windows_memory_available_bytes), windows_memory_cache_bytes_Real_Windows_lhs_rhs = SUM(windows_memory_cache_bytes), windows_os_visible_memory_bytes_Real_Windows_rhs = SUM(windows_os_visible_memory_bytes), kube_pod_container_resource_requests_Requests_lhs = SUM(CASE((resource == "memory"), kube_pod_container_resource_requests, NULL)), machine_memory_bytes_Requests_rhs = SUM(machine_memory_bytes), kube_pod_container_resource_limits_Limits_lhs = SUM(CASE((resource == "memory"), kube_pod_container_resource_limits, NULL)), machine_memory_bytes_Limits_rhs = SUM(machine_memory_bytes) BY time_bucket = TBUCKET(5 minute)
| EVAL Real_Linux = ((node_memory_MemTotal_bytes_Real_Linux_lhs_lhs - node_memory_MemAvailable_bytes_Real_Linux_lhs_rhs) / node_memory_MemTotal_bytes_Real_Linux_rhs)
| EVAL Real_Windows = ((windows_memory_available_bytes_Real_Windows_lhs_lhs + windows_memory_cache_bytes_Real_Windows_lhs_rhs) / windows_os_visible_memory_bytes_Real_Windows_rhs)
| EVAL Requests = (kube_pod_container_resource_requests_Requests_lhs / machine_memory_bytes_Requests_rhs)
| EVAL Limits = (kube_pod_container_resource_limits_Limits_lhs / machine_memory_bytes_Limits_rhs)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), Real_Linux = MAX(Real_Linux), Real_Windows = MAX(Real_Windows), Requests = MAX(Requests), Limits = MAX(Limits)
| KEEP time_bucket, Real_Linux, Real_Windows, Requests, Limits
| EVAL __labels = MV_APPEND(MV_APPEND(MV_APPEND("Real Linux", "Real Windows"), "Requests"), "Limits"), __values = MV_APPEND(MV_APPEND(MV_APPEND(TO_STRING(Real_Linux), TO_STRING(Real_Windows)), TO_STRING(Requests)), TO_STRING(Limits))
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
- Output metric: `Real_Linux`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Approximated bargauge as bar chart

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

**Warnings:** Grafana panel has 2 transformation(s); manual review recommended; Approximated PromQL arithmetic using same-bucket ES|QL math; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series; Approximated bargauge as bar chart

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Approximated bargauge as bar chart

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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family` → translated nested count(count()) expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
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
- Layout: x=24, y=0, w=4, h=6
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE kube_namespace_labels IS NOT NULL OR kube_pod_container_status_running IS NOT NULL OR kube_pod_status_phase IS NOT NULL OR kube_service_info IS NOT NULL OR kube_endpoint_info IS NOT NULL OR kube_ingress_info IS NOT NULL OR kube_deployment_labels IS NOT NULL OR kube_statefulset_labels IS NOT NULL OR kube_daemonset_labels IS NOT NULL OR kube_persistentvolumeclaim_info IS NOT NULL OR kube_hpa_labels IS NOT NULL OR kube_configmap_info IS NOT NULL OR kube_secret_info IS NOT NULL OR kube_networkpolicy_labels IS NOT NULL OR kube_node_info IS NOT NULL
| STATS kube_namespace_labels_A = SUM(kube_namespace_labels), kube_pod_container_status_running_B = SUM(kube_pod_container_status_running), kube_pod_status_phase_O = SUM(CASE((phase == "Running"), kube_pod_status_phase, NULL)), kube_service_info_C = SUM(kube_service_info), kube_endpoint_info_D = SUM(kube_endpoint_info), kube_ingress_info_E = SUM(kube_ingress_info), kube_deployment_labels_F = SUM(kube_deployment_labels), kube_statefulset_labels_G = SUM(kube_statefulset_labels), kube_daemonset_labels_H = SUM(kube_daemonset_labels), kube_persistentvolumeclaim_info_I = SUM(kube_persistentvolumeclaim_info), kube_hpa_labels_J = SUM(kube_hpa_labels), kube_configmap_info_K = SUM(kube_configmap_info), kube_secret_info_L = SUM(kube_secret_info), kube_networkpolicy_labels_M = SUM(kube_networkpolicy_labels), kube_node_info_N = COUNT_DISTINCT(node) BY time_bucket = TBUCKET(5 minute)
| EVAL Namespaces = kube_namespace_labels_A
| EVAL Running_Containers = kube_pod_container_status_running_B
| EVAL Running_Pods = kube_pod_status_phase_O
| EVAL Services = kube_service_info_C
| EVAL Endpoints = kube_endpoint_info_D
| EVAL Ingresses = kube_ingress_info_E
| EVAL Deployments = kube_deployment_labels_F
| EVAL Statefulsets = kube_statefulset_labels_G
| EVAL Daemonsets = kube_daemonset_labels_H
| EVAL Persistent_Volume_Claims = kube_persistentvolumeclaim_info_I
| EVAL Horizontal_Pod_Autoscalers = kube_hpa_labels_J
| EVAL Configmaps = kube_configmap_info_K
| EVAL Secrets = kube_secret_info_L
| EVAL Network_Policies = kube_networkpolicy_labels_M
| EVAL Nodes = kube_node_info_N
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated count of counter metric
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
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
- Layout: x=24, y=6, w=4, h=6
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE NOT (mode RLIKE "idle|iowait|steal")
| WHERE node_cpu_seconds_total IS NOT NULL
| STATS node_cpu_seconds_total = SUM(RATE(node_cpu_seconds_total, 5m)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), node_cpu_seconds_total = MAX(node_cpu_seconds_total)
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
- Semantic losses: Dropped variable-driven label filters during migration, Panel has 5 PromQL targets but only 1 could be migrated (1 of the dropped targets are Windows-specific)

**Visual IR:**

- Kibana type: `metric`
- Layout: x=0, y=12, w=12, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 5
- transformations: 2

**Warnings:** Grafana panel has 2 transformation(s); manual review recommended; Panel has 5 PromQL targets but only 1 could be migrated (1 of the dropped targets are Windows-specific)

**Semantic losses:** Dropped variable-driven label filters during migration; Panel has 5 PromQL targets but only 1 could be migrated (1 of the dropped targets are Windows-specific)

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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_memory_MemAvailable_bytes IS NOT NULL
| STATS node_memory_MemTotal_bytes_cluster_job_sum = SUM(node_memory_MemTotal_bytes), node_memory_MemAvailable_bytes_cluster_job_sum = SUM(node_memory_MemAvailable_bytes) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (node_memory_MemTotal_bytes_cluster_job_sum - node_memory_MemAvailable_bytes_cluster_job_sum)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Panel has 5 PromQL targets but only 1 could be migrated (1 of the dropped targets are Windows-specific)

**Visual IR:**

- Kibana type: `metric`
- Layout: x=12, y=12, w=12, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 5
- transformations: 2

**Warnings:** Grafana panel has 2 transformation(s); manual review recommended; Approximated PromQL arithmetic using same-bucket ES|QL math; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series; Panel has 5 PromQL targets but only 1 could be migrated (1 of the dropped targets are Windows-specific)

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Panel has 5 PromQL targets but only 1 could be migrated (1 of the dropped targets are Windows-specific)

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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE phase == "Running"
| WHERE kube_pod_status_phase IS NOT NULL
| STATS kube_pod_status_phase = SUM(kube_pod_status_phase) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kube_pod_status_phase = MAX(kube_pod_status_phase)
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
- Layout: x=24, y=12, w=4, h=6
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family` → translated nested avg over rate expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE NOT (mode RLIKE "idle|iowait|steal")
| WHERE node_cpu_seconds_total IS NOT NULL
| STATS inner_val = SUM(RATE(node_cpu_seconds_total, 5m)) BY time_bucket = TBUCKET(5 minute), instance, cpu
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_memory_MemAvailable_bytes IS NOT NULL OR windows_os_visible_memory_bytes IS NOT NULL OR windows_memory_available_bytes IS NOT NULL
| STATS node_memory_MemTotal_bytes_Linux_lhs_lhs = SUM(node_memory_MemTotal_bytes), node_memory_MemAvailable_bytes_Linux_lhs_rhs = SUM(node_memory_MemAvailable_bytes), node_memory_MemTotal_bytes_Linux_rhs = SUM(node_memory_MemTotal_bytes), windows_os_visible_memory_bytes_Windows_lhs_lhs = SUM(windows_os_visible_memory_bytes), windows_memory_available_bytes_Windows_lhs_rhs = SUM(windows_memory_available_bytes), windows_os_visible_memory_bytes_Windows_rhs = SUM(windows_os_visible_memory_bytes) BY time_bucket = TBUCKET(5 minute)
| EVAL Linux = ((node_memory_MemTotal_bytes_Linux_lhs_lhs - node_memory_MemAvailable_bytes_Linux_lhs_rhs) / node_memory_MemTotal_bytes_Linux_rhs)
| EVAL Windows = ((windows_os_visible_memory_bytes_Windows_lhs_lhs - windows_memory_available_bytes_Windows_lhs_rhs) / windows_os_visible_memory_bytes_Windows_rhs)
| KEEP time_bucket, Linux, Windows
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `/`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `Linux`
- Output groups: `time_bucket`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

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

**Warnings:** Grafana panel has 1 transformation(s); manual review recommended; Approximated PromQL arithmetic using same-bucket ES|QL math; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Notes:** Grafana panel has 1 transformation(s); manual review recommended

**Verdict:** MINOR_ISSUE

#### CPU Utilization by namespace

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

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
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → arithmetic operand unsupported; marked not_feasible
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`

**Query IR:**

- Family: `binary_expr`
- Binary op: `+`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Semantic losses: PromQL '+' arithmetic where an operand contains a nested set operator or vector-matching join that cannot be safely combined; marked for manual review so data is not silently dropped

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=0, y=12, w=24, h=12
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** PromQL '+' arithmetic where an operand contains a nested set operator or vector-matching join that cannot be safely combined; marked for manual review so data is not silently dropped

**Semantic losses:** PromQL '+' arithmetic where an operand contains a nested set operator or vector-matching join that cannot be safely combined; marked for manual review so data is not silently dropped

**Verdict:** EXPECTED_LIMITATION

#### Memory Utilization by namespace

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

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
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family binary_expr bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family`
- `query_translators` / `logql_count_family`
- `query_translators` / `uptime_family`
- `query_translators` / `join_family`
- `query_translators` / `binary_expr_family` → arithmetic operand unsupported; marked not_feasible
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`

**Query IR:**

- Family: `binary_expr`
- Binary op: `+`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Semantic losses: PromQL '+' arithmetic where an operand contains a nested set operator or vector-matching join that cannot be safely combined; marked for manual review so data is not silently dropped

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=24, y=12, w=24, h=12
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** PromQL '+' arithmetic where an operand contains a nested set operator or vector-matching join that cannot be safely combined; marked for manual review so data is not silently dropped

**Semantic losses:** PromQL '+' arithmetic where an operand contains a nested set operator or vector-matching join that cannot be safely combined; marked for manual review so data is not silently dropped

**Verdict:** EXPECTED_LIMITATION

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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family` → translated nested avg over rate expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE NOT (mode RLIKE "idle|iowait|steal")
| WHERE node_cpu_seconds_total IS NOT NULL
| STATS inner_val = SUM(RATE(node_cpu_seconds_total, 5m)) BY time_bucket = TBUCKET(5 minute), instance, cpu
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_memory_MemAvailable_bytes IS NOT NULL OR windows_os_visible_memory_bytes IS NOT NULL OR windows_memory_available_bytes IS NOT NULL
| STATS node_memory_MemTotal_bytes_Linux_lhs = SUM(node_memory_MemTotal_bytes), node_memory_MemAvailable_bytes_Linux_rhs = SUM(node_memory_MemAvailable_bytes), windows_os_visible_memory_bytes_Windows_lhs = SUM(windows_os_visible_memory_bytes), windows_memory_available_bytes_Windows_rhs = SUM(windows_memory_available_bytes) BY time_bucket = TBUCKET(5 minute), instance
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=24, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter` → applied post-aggregation filter > 0
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
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

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Controls / Variables (2)</summary>

- `cluster` (type: `options`)
- `job` (type: `options`)

</details>

---

### Grafana: Kubernetes Kafka

**File:** `kafka-12483.json` — **Panels:** 35

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| General | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| JVM | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Details | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Cluster status | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(up{namespace="$namespace", app_kubernetes_io_component="kafka"}) / 3 | TS metrics-prometheus-* \| WHERE app_kubernetes_io_component == "kafka" \| WHERE... |
| Number of brokers | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | kafka_brokers{namespace="$namespace"} | TS metrics-prometheus-* \| WHERE kafka_brokers IS NOT NULL \| STATS kafka_broker... |
| Active controller status | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | sum(kafka_controller_kafkacontroller_activecontrollercount_value{namespace="$nam... | TS metrics-prometheus-* \| WHERE kafka_controller_kafkacontroller_activecontroll... |
| In-sync replicas shrinks | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | - sum(kafka_server_replicamanager_total_isrshrinkspersec_count{namespace="$names... | TS metrics-prometheus-* \| WHERE kafka_server_replicamanager_total_isrshrinksper... |
| Under replicated partitions | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | sum(kafka_topic_partition_under_replicated_partition{namespace="$namespace"}) | TS metrics-prometheus-* \| WHERE kafka_topic_partition_under_replicated_partitio... |
| Partition count | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | kafka_server_replicamanager_total_partitioncount_value{namespace="$namespace", i... | TS metrics-prometheus-* \| WHERE kafka_server_replicamanager_total_partitioncoun... |
| Broker status | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | up{namespace="$namespace", instance=~"$broker"} | TS metrics-prometheus-* \| WHERE up IS NOT NULL \| STATS up = MAX(LAST_OVER_TIME... |
| Unclean leader election | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(kafka_controller_controllerstats_uncleanleaderelectionspersec_count{namespac... | TS metrics-prometheus-* \| WHERE kafka_controller_controllerstats_uncleanleadere... |
| Active controller | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | kafka_controller_kafkacontroller_activecontrollercount_value{namespace="$namespa... | TS metrics-prometheus-* \| WHERE kafka_controller_kafkacontroller_activecontroll... |
| In-sync replicas expands | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | - sum(kafka_server_replicamanager_total_isrexpandspersec_count{namespace="$names... | TS metrics-prometheus-* \| WHERE kafka_server_replicamanager_total_isrexpandsper... |
| Offline partitions count | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | sum(kafka_controller_kafkacontroller_offlinepartitionscount_value{namespace="$na... | TS metrics-prometheus-* \| WHERE kafka_controller_kafkacontroller_offlinepartiti... |
| Bytes rate | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | kafka_server_brokertopicmetrics_total_bytesinpersec_count{namespace="$namespace"... | TS metrics-prometheus-* \| WHERE kafka_server_brokertopicmetrics_total_bytesinpe... |
| Messages IN | `graph` → `line` | migrated | **MINOR_ISSUE** | kafka_server_brokertopicmetrics_total_messagesinpersec_oneminuterate{namespace="... | TS metrics-prometheus-* \| WHERE kafka_server_brokertopicmetrics_total_messagesi... |
| Number of partitions | `graph` → `bar` | migrated | **MINOR_ISSUE** | kafka_server_replicamanager_total_partitioncount_value{namespace="$namespace"} | TS metrics-prometheus-* \| WHERE kafka_server_replicamanager_total_partitioncoun... |
| Bytes IN | `graph` → `line` | migrated | **MINOR_ISSUE** | rate(kafka_server_brokertopicmetrics_total_bytesinpersec_count{namespace="$names... | TS metrics-prometheus-* \| WHERE kafka_server_brokertopicmetrics_total_bytesinpe... |
| Bytes OUT | `graph` → `line` | migrated | **MINOR_ISSUE** | rate(kafka_server_brokertopicmetrics_total_bytesoutpersec_count{namespace="$name... | TS metrics-prometheus-* \| WHERE kafka_server_brokertopicmetrics_total_bytesoutp... |
| In-sync replicas | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | - kafka_server_replicamanager_total_isrshrinkspersec_count{namespace="$namespace... | TS metrics-prometheus-* \| WHERE kafka_server_replicamanager_total_isrshrinksper... |
| JVM memory | `graph` → `line` | migrated | **MINOR_ISSUE** | java_lang_memory_heapmemoryusage_used{namespace="$namespace", instance=~"$broker... | TS metrics-prometheus-* \| WHERE java_lang_memory_heapmemoryusage_used IS NOT NU... |
| CPU load | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | java_lang_operatingsystem_systemcpuload{namespace="$namespace", instance=~"$brok... | TS metrics-prometheus-* \| WHERE java_lang_operatingsystem_systemcpuload IS NOT ... |
| System memory | `graph` → `line` | migrated | **MINOR_ISSUE** | java_lang_operatingsystem_totalphysicalmemorysize{namespace="$namespace", instan... | TS metrics-prometheus-* \| WHERE java_lang_operatingsystem_totalphysicalmemorysi... |
| System load | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | java_lang_operatingsystem_systemloadaverage{namespace="$namespace", instance=~"$... | TS metrics-prometheus-* \| WHERE java_lang_operatingsystem_systemloadaverage IS ... |
| Open file descriptor | `graph` → `line` | migrated | **MINOR_ISSUE** | java_lang_operatingsystem_openfiledescriptorcount{namespace="$namespace", instan... | TS metrics-prometheus-* \| WHERE java_lang_operatingsystem_openfiledescriptorcou... |
| Total memory | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | sum(java_lang_operatingsystem_totalphysicalmemorysize{namespace="$namespace", in... | TS metrics-prometheus-* \| WHERE java_lang_operatingsystem_totalphysicalmemorysi... |
| Class loading | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | java_lang_classloading_loadedclasscount{namespace="$namespace", instance=~"$brok... | TS metrics-prometheus-* \| WHERE java_lang_classloading_loadedclasscount IS NOT ... |
| Threads | `graph` → `line` | migrated | **MINOR_ISSUE** | java_lang_threading_threadcount{namespace="$namespace", instance=~"$broker"} \|\... | TS metrics-prometheus-* \| WHERE java_lang_threading_threadcount IS NOT NULL OR ... |
| Total CPUs | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | sum(java_lang_operatingsystem_availableprocessors{namespace="$namespace", instan... | TS metrics-prometheus-* \| WHERE java_lang_operatingsystem_availableprocessors I... |
| Number of requests | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | kafka_network_requestmetrics_requestspersec_count{namespace="$namespace", instan... | TS metrics-prometheus-* \| WHERE request RLIKE "Produce, version=.*" \| WHERE ka... |
| Purgatory delayed fetch operations | `graph` → `line` | migrated | **MINOR_ISSUE** | kafka_server_delayedoperationpurgatory_numdelayedoperations_fetch_value{namespac... | TS metrics-prometheus-* \| WHERE kafka_server_delayedoperationpurgatory_numdelay... |
| Log size by broker | `graph` → `line` | migrated | **MINOR_ISSUE** | sum (kafka_log_log_size{namespace="$namespace"}) by(instance) | TS metrics-prometheus-* \| WHERE kafka_log_log_size IS NOT NULL \| STATS kafka_l... |
| All number of requests | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(kafka_network_requestmetrics_requestspersec_count{namespace="$namespace", in... | TS metrics-prometheus-* \| WHERE kafka_network_requestmetrics_requestspersec_cou... |
| Idle network processors | `graph` → `line` | migrated | **MINOR_ISSUE** | kafka_network_processor_idlepercent_value{namespace="$namespace", instance=~"$br... | TS metrics-prometheus-* \| WHERE kafka_network_processor_idlepercent_value IS NO... |
| Idle request handler threads | `graph` → `line` | migrated | **MINOR_ISSUE** | kafka_server_kafkarequesthandlerpool_total_requesthandleravgidlepercent_oneminut... | TS metrics-prometheus-* \| WHERE kafka_server_kafkarequesthandlerpool_total_requ... |

<details>
<summary>Detailed traces (32 panels)</summary>

#### Cluster status

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(up{namespace="$namespace", app_kubernetes_io_component="kafka"}) / 3
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE app_kubernetes_io_component == "kafka"
| WHERE up IS NOT NULL
| STATS up_app_kubernetes_io_component_kafka_sum = SUM(up) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (up_app_kubernetes_io_component_kafka_sum / 3)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
| KEEP time_bucket, computed_value
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=0, y=0, w=8, h=4
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Number of brokers

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
kafka_brokers{namespace="$namespace"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE kafka_brokers IS NOT NULL
| STATS kafka_brokers = MAX(LAST_OVER_TIME(kafka_brokers)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kafka_brokers = MAX(kafka_brokers)
| KEEP time_bucket, kafka_brokers
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `kafka_brokers`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kafka_brokers`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=8, y=0, w=8, h=4
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Active controller status

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(kafka_controller_kafkacontroller_activecontrollercount_value{namespace="$namespace"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE kafka_controller_kafkacontroller_activecontrollercount_value IS NOT NULL
| STATS kafka_controller_kafkacontroller_activecontrollercount_value = SUM(kafka_controller_kafkacontroller_activecontrollercount_value) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kafka_controller_kafkacontroller_activecontrollercount_value = MAX(kafka_controller_kafkacontroller_activecontrollercount_value)
| KEEP time_bucket, kafka_controller_kafkacontroller_activecontrollercount_value
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `kafka_controller_kafkacontroller_activecontrollercount_value`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kafka_controller_kafkacontroller_activecontrollercount_value`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=16, y=0, w=8, h=4
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### In-sync replicas shrinks

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
- sum(kafka_server_replicamanager_total_isrshrinkspersec_count{namespace="$namespace", instance=~"$broker"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE kafka_server_replicamanager_total_isrshrinkspersec_count IS NOT NULL
| STATS kafka_server_replicamanager_total_isrshrinkspersec_count = SUM(LAST_OVER_TIME(kafka_server_replicamanager_total_isrshrinkspersec_count)) BY time_bucket = TBUCKET(5 minute)
| EVAL kafka_server_replicamanager_total_isrshrinkspersec_count = -1 * kafka_server_replicamanager_total_isrshrinkspersec_count
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kafka_server_replicamanager_total_isrshrinkspersec_count = MAX(kafka_server_replicamanager_total_isrshrinkspersec_count)
| KEEP time_bucket, kafka_server_replicamanager_total_isrshrinkspersec_count
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `kafka_server_replicamanager_total_isrshrinkspersec_count`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kafka_server_replicamanager_total_isrshrinkspersec_count`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=24, y=0, w=8, h=4
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value; Applied negation to match leading minus in original expression

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Under replicated partitions

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(kafka_topic_partition_under_replicated_partition{namespace="$namespace"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE kafka_topic_partition_under_replicated_partition IS NOT NULL
| STATS kafka_topic_partition_under_replicated_partition = SUM(kafka_topic_partition_under_replicated_partition) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kafka_topic_partition_under_replicated_partition = MAX(kafka_topic_partition_under_replicated_partition)
| KEEP time_bucket, kafka_topic_partition_under_replicated_partition
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `kafka_topic_partition_under_replicated_partition`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kafka_topic_partition_under_replicated_partition`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=32, y=0, w=8, h=4
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Partition count

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
kafka_server_replicamanager_total_partitioncount_value{namespace="$namespace", instance=~"$broker"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE kafka_server_replicamanager_total_partitioncount_value IS NOT NULL
| STATS kafka_server_replicamanager_total_partitioncount_value = MAX(LAST_OVER_TIME(kafka_server_replicamanager_total_partitioncount_value)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kafka_server_replicamanager_total_partitioncount_value = MAX(kafka_server_replicamanager_total_partitioncount_value)
| KEEP time_bucket, kafka_server_replicamanager_total_partitioncount_value
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `kafka_server_replicamanager_total_partitioncount_value`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kafka_server_replicamanager_total_partitioncount_value`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=40, y=0, w=8, h=9
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Broker status

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
up{namespace="$namespace", instance=~"$broker"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE up IS NOT NULL
| STATS up = MAX(LAST_OVER_TIME(up)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), up = MAX(up)
| KEEP time_bucket, up
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `up`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `up`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=0, y=4, w=8, h=5
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Unclean leader election

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(kafka_controller_controllerstats_uncleanleaderelectionspersec_count{namespace="$namespace", instance=~"$broker"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE kafka_controller_controllerstats_uncleanleaderelectionspersec_count IS NOT NULL
| STATS kafka_controller_controllerstats_uncleanleaderelectionspersec_count = SUM(LAST_OVER_TIME(kafka_controller_controllerstats_uncleanleaderelectionspersec_count)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kafka_controller_controllerstats_uncleanleaderelectionspersec_count = MAX(kafka_controller_controllerstats_uncleanleaderelectionspersec_count)
| KEEP time_bucket, kafka_controller_controllerstats_uncleanleaderelectionspersec_count
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `kafka_controller_controllerstats_uncleanleaderelectionspersec_count`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kafka_controller_controllerstats_uncleanleaderelectionspersec_count`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=8, y=4, w=8, h=5
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Active controller

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
kafka_controller_kafkacontroller_activecontrollercount_value{namespace="$namespace"} == 1
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter` → applied post-aggregation filter == 1
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE kafka_controller_kafkacontroller_activecontrollercount_value IS NOT NULL
| STATS kafka_controller_kafkacontroller_activecontrollercount_value = MAX(LAST_OVER_TIME(kafka_controller_kafkacontroller_activecontrollercount_value)) BY time_bucket = TBUCKET(5 minute)
| WHERE kafka_controller_kafkacontroller_activecontrollercount_value == 1
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kafka_controller_kafkacontroller_activecontrollercount_value = MAX(kafka_controller_kafkacontroller_activecontrollercount_value)
| KEEP time_bucket, kafka_controller_kafkacontroller_activecontrollercount_value
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `kafka_controller_kafkacontroller_activecontrollercount_value`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kafka_controller_kafkacontroller_activecontrollercount_value`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=16, y=4, w=8, h=5
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### In-sync replicas expands

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
- sum(kafka_server_replicamanager_total_isrexpandspersec_count{namespace="$namespace", instance=~"$broker"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE kafka_server_replicamanager_total_isrexpandspersec_count IS NOT NULL
| STATS kafka_server_replicamanager_total_isrexpandspersec_count = SUM(LAST_OVER_TIME(kafka_server_replicamanager_total_isrexpandspersec_count)) BY time_bucket = TBUCKET(5 minute)
| EVAL kafka_server_replicamanager_total_isrexpandspersec_count = -1 * kafka_server_replicamanager_total_isrexpandspersec_count
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kafka_server_replicamanager_total_isrexpandspersec_count = MAX(kafka_server_replicamanager_total_isrexpandspersec_count)
| KEEP time_bucket, kafka_server_replicamanager_total_isrexpandspersec_count
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `kafka_server_replicamanager_total_isrexpandspersec_count`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kafka_server_replicamanager_total_isrexpandspersec_count`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=24, y=4, w=8, h=5
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value; Applied negation to match leading minus in original expression

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Offline partitions count

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(kafka_controller_kafkacontroller_offlinepartitionscount_value{namespace="$namespace", instance=~"$broker"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE kafka_controller_kafkacontroller_offlinepartitionscount_value IS NOT NULL
| STATS kafka_controller_kafkacontroller_offlinepartitionscount_value = SUM(kafka_controller_kafkacontroller_offlinepartitionscount_value) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), kafka_controller_kafkacontroller_offlinepartitionscount_value = MAX(kafka_controller_kafkacontroller_offlinepartitionscount_value)
| KEEP time_bucket, kafka_controller_kafkacontroller_offlinepartitionscount_value
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `kafka_controller_kafkacontroller_offlinepartitionscount_value`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kafka_controller_kafkacontroller_offlinepartitionscount_value`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=32, y=4, w=8, h=5
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Bytes rate

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
kafka_server_brokertopicmetrics_total_bytesinpersec_count{namespace="$namespace", instance=~"$broker"} / kafka_server_brokertopicmetrics_total_bytesoutpersec_count{namespace="$namespace", instance=~"$broker"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE kafka_server_brokertopicmetrics_total_bytesinpersec_count IS NOT NULL OR kafka_server_brokertopicmetrics_total_bytesoutpersec_count IS NOT NULL
| STATS kafka_server_brokertopicmetrics_total_bytesinpersec_count_namespace_instance = MAX(LAST_OVER_TIME(kafka_server_brokertopicmetrics_total_bytesinpersec_count)), kafka_server_brokertopicmetrics_total_bytesoutpersec_count_namespace_instance = MAX(LAST_OVER_TIME(kafka_server_brokertopicmetrics_total_bytesoutpersec_count)) BY time_bucket = TBUCKET(5 minute), instance
| EVAL computed_value = (kafka_server_brokertopicmetrics_total_bytesinpersec_count_namespace_instance / kafka_server_brokertopicmetrics_total_bytesoutpersec_count_namespace_instance)
| KEEP time_bucket, instance, computed_value
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
- Output groups: `time_bucket, instance`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=9, w=16, h=10
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Messages IN

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
kafka_server_brokertopicmetrics_total_messagesinpersec_oneminuterate{namespace="$namespace", instance=~"$broker"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE kafka_server_brokertopicmetrics_total_messagesinpersec_oneminuterate IS NOT NULL
| STATS kafka_server_brokertopicmetrics_total_messagesinpersec_oneminuterate = MAX(LAST_OVER_TIME(kafka_server_brokertopicmetrics_total_messagesinpersec_oneminuterate)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `kafka_server_brokertopicmetrics_total_messagesinpersec_oneminuterate`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kafka_server_brokertopicmetrics_total_messagesinpersec_oneminuterate`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=16, y=9, w=16, h=10
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Number of partitions

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
kafka_server_replicamanager_total_partitioncount_value{namespace="$namespace"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to bar panel

**Translated (bar):**

```
TS metrics-prometheus-*
| WHERE kafka_server_replicamanager_total_partitioncount_value IS NOT NULL
| STATS kafka_server_replicamanager_total_partitioncount_value = MAX(LAST_OVER_TIME(kafka_server_replicamanager_total_partitioncount_value)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `kafka_server_replicamanager_total_partitioncount_value`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kafka_server_replicamanager_total_partitioncount_value`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `bar`
- Layout: x=32, y=9, w=16, h=10
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Bytes IN

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
rate(kafka_server_brokertopicmetrics_total_bytesinpersec_count{namespace="$namespace", instance=~"$broker"}[5m])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE kafka_server_brokertopicmetrics_total_bytesinpersec_count IS NOT NULL
| STATS kafka_server_brokertopicmetrics_total_bytesinpersec_count = AVG(RATE(kafka_server_brokertopicmetrics_total_bytesinpersec_count, 5m)) BY time_bucket = TBUCKET(5 minute), instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `kafka_server_brokertopicmetrics_total_bytesinpersec_count`
- Range func: `rate`
- Range window: `5m`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `kafka_server_brokertopicmetrics_total_bytesinpersec_count`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=19, w=16, h=11
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
<summary>Controls / Variables (2)</summary>

- `namespace` (type: `options`)
- `broker` (type: `options`)

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
| Partial Target Drop | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | rate(api_requests_total[5m]) \|\|\| avg(node_load1) \|\|\| histogram_quantile(0.... | TS metrics-prometheus-* \| WHERE api_requests_total IS NOT NULL OR node_load1 IS... |
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE status RLIKE "2.."
| WHERE http_requests_total IS NOT NULL
| STATS http_requests_total = SUM(RATE(http_requests_total, 5m)) BY time_bucket = TBUCKET(5 minute), service, route
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel` → approximated bargauge panel

**Translated (gauge):**

```
TS metrics-prometheus-*
| WHERE queue_depth IS NOT NULL
| STATS queue_depth = AVG(queue_depth) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), queue_depth = MAX(queue_depth)
| KEEP time_bucket, queue_depth
| EVAL _gauge_min = 0, _gauge_max = 500, _gauge_goal = 300
| SORT time_bucket ASC
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE frontend_requests_total IS NOT NULL OR worker_jobs_total IS NOT NULL
| STATS frontend_requests_total_A = RATE(frontend_requests_total, 5m), worker_jobs_total_B = RATE(worker_jobs_total, 5m) BY time_bucket = TBUCKET(5 minute)
| EVAL frontend = frontend_requests_total_A
| EVAL worker = worker_jobs_total_B
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE api_requests_total IS NOT NULL OR node_load1 IS NOT NULL
| STATS api_requests_total_A = RATE(api_requests_total, 5m), node_load1_B = AVG_OVER_TIME(node_load1, 5m) BY time_bucket = TBUCKET(5 minute)
| EVAL api = api_requests_total_A
| EVAL load = node_load1_B
| KEEP time_bucket, api, load
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
- Semantic losses: Dropped 1 incompatible target(s); showing 2 mergeable targets

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=0, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 3

**Warnings:** Dropped 1 incompatible target(s); showing 2 mergeable targets

**Semantic losses:** Dropped 1 incompatible target(s); showing 2 mergeable targets

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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → approximated grouped stat as datatable

**Translated (datatable):**

```
TS metrics-prometheus-*
| WHERE kube_pod_info IS NOT NULL
| STATS kube_pod_info = SUM(kube_pod_info) BY time_bucket = TBUCKET(5 minute), pod
| SORT time_bucket ASC
| STATS kube_pod_info = MAX(kube_pod_info) BY pod
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
TS metrics-prometheus-*
| WHERE slo_burn_rate IS NOT NULL
| STATS slo_burn_rate = AVG(slo_burn_rate) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), slo_burn_rate = MAX(slo_burn_rate)
| KEEP time_bucket, slo_burn_rate
| EVAL _gauge_min = 0, _gauge_max = 100, _gauge_goal = 2
| SORT time_bucket ASC
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
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family logql_stream bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family`
- `query_translators` / `logql_stream_family` → translated LogQL logs query
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
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

- `service` (type: `options`)
- `namespace` (type: `options`)

</details>

---

### Grafana: MySQL Overview

**File:** `mysql-overview-7362.json` — **Panels:** 50

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| row | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Connections | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Table Locks | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Temporary Objects | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Sorts | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Aborted | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Network | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Memory | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Command, Handlers, Processes | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Query Cache | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Files and Tables | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| Table Openings | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| MySQL Table Definition Cache | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| System Charts | `row` → `section` | skipped | **EXPECTED_LIMITATION** | — | — |
| MySQL Uptime | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | mysql_global_status_uptime{instance="$host"} | TS metrics-prometheus-* \| WHERE mysql_global_status_uptime IS NOT NULL \| STATS... |
| Current QPS | `singlestat` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | rate(mysql_global_status_queries{instance="$host"}[$interval]) or irate(mysql_gl... | — |
| InnoDB Buffer Pool Size | `singlestat` → `metric` | migrated | **MINOR_ISSUE** | mysql_global_variables_innodb_buffer_pool_size{instance="$host"} | TS metrics-prometheus-* \| WHERE mysql_global_variables_innodb_buffer_pool_size ... |
| Buffer Pool Size of Total RAM | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | (mysql_global_variables_innodb_buffer_pool_size{instance="$host"} * 100) / on (i... | TS metrics-prometheus-* \| WHERE mysql_global_variables_innodb_buffer_pool_size ... |
| MySQL Connections | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | max(max_over_time(mysql_global_status_threads_connected{instance="$host"}[$inter... | TS metrics-prometheus-* \| STATS mysql_global_status_threads_connected = MAX(MAX... |
| MySQL Client Thread Activity | `graph` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | max_over_time(mysql_global_status_threads_connected{instance="$host"}[$interval]... | — |
| MySQL Questions | `graph` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | rate(mysql_global_status_questions{instance="$host"}[$interval]) or irate(mysql_... | — |
| MySQL Thread Cache | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | mysql_global_variables_thread_cache_size{instance="$host"} \|\|\| mysql_global_s... | TS metrics-prometheus-* \| WHERE mysql_global_variables_thread_cache_size IS NOT... |
| MySQL Temporary Objects | `graph` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | rate(mysql_global_status_created_tmp_tables{instance="$host"}[$interval]) or ira... | — |
| MySQL Select Types | `graph` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | rate(mysql_global_status_select_full_join{instance="$host"}[$interval]) or irate... | — |
| MySQL Sorts | `graph` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | rate(mysql_global_status_sort_rows{instance="$host"}[$interval]) or irate(mysql_... | — |
| MySQL Slow Queries | `graph` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | rate(mysql_global_status_slow_queries{instance="$host"}[$interval]) or irate(mys... | — |
| MySQL Aborted Connections | `graph` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | rate(mysql_global_status_aborted_connects{instance="$host"}[$interval]) or irate... | — |
| MySQL Table Locks | `graph` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | rate(mysql_global_status_table_locks_immediate{instance="$host"}[$interval]) or ... | — |
| MySQL Network Traffic | `graph` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | rate(mysql_global_status_bytes_received{instance="$host"}[$interval]) or irate(m... | — |
| MySQL Network Usage Hourly | `graph` → `bar` | migrated_with_warnings | **MINOR_ISSUE** | increase(mysql_global_status_bytes_received{instance="$host"}[1h]) \|\|\| increa... | TS metrics-prometheus-* \| WHERE mysql_global_status_bytes_received IS NOT NULL ... |
| MySQL Internal Memory Overview | `graph` → `area` | migrated | **MINOR_ISSUE** | node_memory_MemTotal_bytes{instance="$host"} \|\|\| mysql_global_status_innodb_p... | TS metrics-prometheus-* \| WHERE node_memory_MemTotal_bytes IS NOT NULL OR mysql... |
| Top Command Counters | `graph` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | topk(5, rate(mysql_global_status_commands_total{instance="$host"}[$interval])>0)... | — |
| Top Command Counters Hourly | `graph` → `bar` | migrated_with_warnings | **MINOR_ISSUE** | topk(5, increase(mysql_global_status_commands_total{instance="$host"}[1h])>0) | TS metrics-prometheus-* \| WHERE mysql_global_status_commands_total IS NOT NULL ... |
| MySQL Handlers | `graph` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | rate(mysql_global_status_handlers_total{instance="$host", handler!~"commit\|roll... | — |
| MySQL Transaction Handlers | `graph` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | rate(mysql_global_status_handlers_total{instance="$host", handler=~"commit\|roll... | — |
| Process States | `graph` → `line` | migrated | **MINOR_ISSUE** | mysql_info_schema_threads{instance="$host"} | TS metrics-prometheus-* \| WHERE mysql_info_schema_threads IS NOT NULL \| STATS ... |
| Top Process States Hourly | `graph` → `bar` | migrated_with_warnings | **MINOR_ISSUE** | topk(5, avg_over_time(mysql_info_schema_threads{instance="$host"}[1h])) | TS metrics-prometheus-* \| WHERE mysql_info_schema_threads IS NOT NULL \| STATS ... |
| MySQL Query Cache Memory | `graph` → `line` | migrated | **MINOR_ISSUE** | mysql_global_status_qcache_free_memory{instance="$host"} \|\|\| mysql_global_var... | TS metrics-prometheus-* \| WHERE mysql_global_status_qcache_free_memory IS NOT N... |
| MySQL Query Cache Activity | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | rate(mysql_global_status_qcache_hits{instance="$host"}[$interval]) or irate(mysq... | TS metrics-prometheus-* \| WHERE mysql_global_status_qcache_queries_in_cache IS ... |
| MySQL File Openings | `graph` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | rate(mysql_global_status_opened_files{instance="$host"}[$interval]) or irate(mys... | — |
| MySQL Open Files | `graph` → `line` | migrated | **MINOR_ISSUE** | mysql_global_status_open_files{instance="$host"} \|\|\| mysql_global_variables_o... | TS metrics-prometheus-* \| WHERE mysql_global_status_open_files IS NOT NULL OR m... |
| MySQL Table Open Cache Status | `graph` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | rate(mysql_global_status_opened_tables{instance="$host"}[$interval]) or irate(my... | — |
| MySQL Open Tables | `graph` → `line` | migrated | **MINOR_ISSUE** | mysql_global_status_open_tables{instance="$host"} \|\|\| mysql_global_variables_... | TS metrics-prometheus-* \| WHERE mysql_global_status_open_tables IS NOT NULL OR ... |
| MySQL Table Definition Cache | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | mysql_global_status_open_table_definitions{instance="$host"} \|\|\| mysql_global... | TS metrics-prometheus-* \| WHERE mysql_global_status_open_table_definitions IS N... |
| I/O Activity | `graph` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | rate(node_vmstat_pgpgin{instance="$host"}[$interval]) * 1024 or irate(node_vmsta... | — |
| Memory Distribution | `graph` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_MemTotal_bytes{instance="$host"} - (node_memory_MemFree_bytes{instan... | TS metrics-prometheus-* \| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_... |
| CPU Usage / Load | `graph` → `area` | migrated_with_warnings | **MINOR_ISSUE** | clamp_max(((avg by (mode) ( (clamp_max(rate(node_cpu_seconds_total{instance="$ho... | TS metrics-prometheus-* \| WHERE node_load1 IS NOT NULL \| STATS node_load1 = AV... |
| Disk Latency | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum((rate(node_disk_read_time_seconds_total{device!~"dm-.+", instance="$host"}[$... | TS metrics-prometheus-* \| STATS node_disk_read_time_seconds_total = SUM(RATE(no... |
| Network Traffic | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(node_network_receive_bytes_total{instance="$host", device!="lo"}[$inter... | TS metrics-prometheus-* \| WHERE node_network_receive_bytes_total IS NOT NULL OR... |
| Swap Activity | `graph` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | rate(node_vmstat_pswpin{instance="$host"}[$interval]) * 4096 or irate(node_vmsta... | — |

<details>
<summary>Detailed traces (36 panels)</summary>

#### MySQL Uptime

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
mysql_global_status_uptime{instance="$host"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE mysql_global_status_uptime IS NOT NULL
| STATS mysql_global_status_uptime = MAX(LAST_OVER_TIME(mysql_global_status_uptime)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), mysql_global_status_uptime = MAX(mysql_global_status_uptime)
| KEEP time_bucket, mysql_global_status_uptime
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `mysql_global_status_uptime`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `mysql_global_status_uptime`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=0, y=0, w=12, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Current QPS

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (singlestat):**

```
rate(mysql_global_status_queries{instance="$host"}[$interval]) or irate(mysql_global_status_queries{instance="$host"}[5m])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `binary_expr_family` → or union not alignable; marked not_feasible
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`

**Query IR:**

- Family: `binary_expr`
- Binary op: `or`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Semantic losses: PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=12, y=0, w=12, h=6
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- links: 1
- has_description: True

**Warnings:** Grafana panel has 1 link(s); verify drilldowns manually; Grafana panel description is not carried into Kibana YAML automatically; PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Semantic losses:** PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Notes:** Grafana panel has 1 link(s); verify drilldowns manually; Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** EXPECTED_LIMITATION

#### InnoDB Buffer Pool Size

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
mysql_global_variables_innodb_buffer_pool_size{instance="$host"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE mysql_global_variables_innodb_buffer_pool_size IS NOT NULL
| STATS mysql_global_variables_innodb_buffer_pool_size = MAX(LAST_OVER_TIME(mysql_global_variables_innodb_buffer_pool_size)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), mysql_global_variables_innodb_buffer_pool_size = MAX(mysql_global_variables_innodb_buffer_pool_size)
| KEEP time_bucket, mysql_global_variables_innodb_buffer_pool_size
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `mysql_global_variables_innodb_buffer_pool_size`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `mysql_global_variables_innodb_buffer_pool_size`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=24, y=0, w=12, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- links: 1
- has_description: True

**Warnings:** Grafana panel has 1 link(s); verify drilldowns manually; Grafana panel description is not carried into Kibana YAML automatically

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Buffer Pool Size of Total RAM

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
(mysql_global_variables_innodb_buffer_pool_size{instance="$host"} * 100) / on (instance) node_memory_MemTotal_bytes{instance="$host"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE mysql_global_variables_innodb_buffer_pool_size IS NOT NULL OR node_memory_MemTotal_bytes IS NOT NULL
| STATS mysql_global_variables_innodb_buffer_pool_size_instance = AVG(mysql_global_variables_innodb_buffer_pool_size), node_memory_MemTotal_bytes_instance = AVG(node_memory_MemTotal_bytes) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = ((mysql_global_variables_innodb_buffer_pool_size_instance * 100) / node_memory_MemTotal_bytes_instance)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
| KEEP time_bucket, computed_value
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Collapsed all series of `mysql_global_variables_innodb_buffer_pool_size` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Collapsed all series of `node_memory_MemTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Visual IR:**

- Kibana type: `metric`
- Layout: x=36, y=0, w=12, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- links: 1
- has_description: True

**Warnings:** Grafana panel has 1 link(s); verify drilldowns manually; Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `mysql_global_variables_innodb_buffer_pool_size` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_MemTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `mysql_global_variables_innodb_buffer_pool_size` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_MemTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Notes:** Grafana panel has 1 link(s); verify drilldowns manually; Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### MySQL Connections

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
max(max_over_time(mysql_global_status_threads_connected{instance="$host"}[$interval])  or mysql_global_status_threads_connected{instance="$host"} ) ||| mysql_global_status_max_used_connections{instance="$host"} ||| mysql_global_variables_max_connections{instance="$host"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family`
- `query_translators` / `fragment_extract` → extracted fragment fields via ast
- `query_translators` / `extract_label_filters`
- `query_translators` / `scalar_outer_agg`
- `query_translators` / `resolve_labels`
- `query_translators` / `counter_detection`
- `query_translators` / `source_type` → selected TS source
- `query_translators` / `time_filter` → applied time filter @timestamp >= ?_tstart AND @timestamp <= ?_tend
- `query_translators` / `bucket` → applied bucket time_bucket = TBUCKET(5 minute)
- `query_translators` / `stats_expression` → built stats expression MAX(MAX_OVER_TIME(mysql_global_status_threads_connected, 5m))
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql` → rendered ES|QL query
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| STATS mysql_global_status_threads_connected = MAX(MAX_OVER_TIME(mysql_global_status_threads_connected, 5m)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
```

**Query IR:**

- Family: `unknown`
- Metric: `mysql_global_status_threads_connected`
- Range func: `max_over_time`
- Range window: `5m`
- Outer agg: `max`
- Binary op: `or`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `mysql_global_status_threads_connected`
- Semantic losses: Panel has 3 PromQL targets but only 1 could be migrated

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=24, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 3
- links: 1
- has_description: True

**Warnings:** Grafana panel has 1 link(s); verify drilldowns manually; Grafana panel description is not carried into Kibana YAML automatically; Panel has 3 PromQL targets but only 1 could be migrated

**Semantic losses:** Panel has 3 PromQL targets but only 1 could be migrated

**Notes:** Grafana panel has 1 link(s); verify drilldowns manually; Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### MySQL Client Thread Activity

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (graph):**

```
max_over_time(mysql_global_status_threads_connected{instance="$host"}[$interval]) or
max_over_time(mysql_global_status_threads_connected{instance="$host"}[5m])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `binary_expr_family` → or union not alignable; marked not_feasible
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`

**Query IR:**

- Family: `binary_expr`
- Binary op: `or`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Semantic losses: PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=24, y=0, w=24, h=11
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 3
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Semantic losses:** PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** EXPECTED_LIMITATION

#### MySQL Questions

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (graph):**

```
rate(mysql_global_status_questions{instance="$host"}[$interval]) or irate(mysql_global_status_questions{instance="$host"}[5m])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `binary_expr_family` → or union not alignable; marked not_feasible
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`

**Query IR:**

- Family: `binary_expr`
- Binary op: `or`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Semantic losses: PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=0, y=0, w=24, h=11
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- links: 1
- has_description: True

**Warnings:** Grafana panel has 1 link(s); verify drilldowns manually; Grafana panel description is not carried into Kibana YAML automatically; PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Semantic losses:** PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Notes:** Grafana panel has 1 link(s); verify drilldowns manually; Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** EXPECTED_LIMITATION

#### MySQL Thread Cache

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
mysql_global_variables_thread_cache_size{instance="$host"} ||| mysql_global_status_threads_cached{instance="$host"} ||| rate(mysql_global_status_threads_created{instance="$host"}[$interval]) or irate(mysql_global_status_threads_created{instance="$host"}[5m])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE mysql_global_variables_thread_cache_size IS NOT NULL OR mysql_global_status_threads_cached IS NOT NULL
| STATS mysql_global_variables_thread_cache_size_B = AVG(mysql_global_variables_thread_cache_size), mysql_global_status_threads_cached_C = AVG(mysql_global_status_threads_cached) BY time_bucket = TBUCKET(5 minute), instance
| EVAL Thread_Cache_Size = mysql_global_variables_thread_cache_size_B
| EVAL Threads_Cached = mysql_global_status_threads_cached_C
| KEEP time_bucket, instance, Thread_Cache_Size, Threads_Cached
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `mysql_global_variables_thread_cache_size`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `Thread_Cache_Size`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration, Dropped 1 incompatible target(s); showing 2 mergeable targets

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=0, w=24, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 3
- links: 1
- has_description: True

**Warnings:** Grafana panel has 1 link(s); verify drilldowns manually; Grafana panel description is not carried into Kibana YAML automatically; Dropped 1 incompatible target(s); showing 2 mergeable targets

**Semantic losses:** Dropped variable-driven label filters during migration; Dropped 1 incompatible target(s); showing 2 mergeable targets

**Notes:** Grafana panel has 1 link(s); verify drilldowns manually; Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### MySQL Temporary Objects

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (graph):**

```
rate(mysql_global_status_created_tmp_tables{instance="$host"}[$interval]) or irate(mysql_global_status_created_tmp_tables{instance="$host"}[5m])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `binary_expr_family` → or union not alignable; marked not_feasible
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`

**Query IR:**

- Family: `binary_expr`
- Binary op: `or`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Semantic losses: PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=0, y=0, w=24, h=11
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 3

**Warnings:** PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Semantic losses:** PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Verdict:** EXPECTED_LIMITATION

#### MySQL Select Types

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (graph):**

```
rate(mysql_global_status_select_full_join{instance="$host"}[$interval]) or irate(mysql_global_status_select_full_join{instance="$host"}[5m])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `binary_expr_family` → or union not alignable; marked not_feasible
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`

**Query IR:**

- Family: `binary_expr`
- Binary op: `or`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Semantic losses: PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=24, y=0, w=24, h=11
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 5
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Semantic losses:** PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** EXPECTED_LIMITATION

#### MySQL Sorts

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (graph):**

```
rate(mysql_global_status_sort_rows{instance="$host"}[$interval]) or irate(mysql_global_status_sort_rows{instance="$host"}[5m])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `binary_expr_family` → or union not alignable; marked not_feasible
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`

**Query IR:**

- Family: `binary_expr`
- Binary op: `or`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Semantic losses: PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=0, y=0, w=24, h=11
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 4
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Semantic losses:** PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** EXPECTED_LIMITATION

#### MySQL Slow Queries

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (graph):**

```
rate(mysql_global_status_slow_queries{instance="$host"}[$interval]) or irate(mysql_global_status_slow_queries{instance="$host"}[5m])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `binary_expr_family` → or union not alignable; marked not_feasible
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`

**Query IR:**

- Family: `binary_expr`
- Binary op: `or`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Semantic losses: PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=24, y=0, w=24, h=11
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Semantic losses:** PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** EXPECTED_LIMITATION

#### MySQL Aborted Connections

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (graph):**

```
rate(mysql_global_status_aborted_connects{instance="$host"}[$interval]) or irate(mysql_global_status_aborted_connects{instance="$host"}[5m])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `binary_expr_family` → or union not alignable; marked not_feasible
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`

**Query IR:**

- Family: `binary_expr`
- Binary op: `or`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Semantic losses: PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=0, y=0, w=24, h=11
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Semantic losses:** PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** EXPECTED_LIMITATION

#### MySQL Table Locks

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (graph):**

```
rate(mysql_global_status_table_locks_immediate{instance="$host"}[$interval]) or irate(mysql_global_status_table_locks_immediate{instance="$host"}[5m])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `binary_expr_family` → or union not alignable; marked not_feasible
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`

**Query IR:**

- Family: `binary_expr`
- Binary op: `or`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Semantic losses: PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=24, y=0, w=24, h=11
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Semantic losses:** PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** EXPECTED_LIMITATION

#### MySQL Network Traffic

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (graph):**

```
rate(mysql_global_status_bytes_received{instance="$host"}[$interval]) or irate(mysql_global_status_bytes_received{instance="$host"}[5m])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `binary_expr_family` → or union not alignable; marked not_feasible
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`

**Query IR:**

- Family: `binary_expr`
- Binary op: `or`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Semantic losses: PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=0, y=0, w=24, h=11
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Semantic losses:** PromQL 'or' between metrics that cannot be aligned in ES|QL (differing grouping dimensions or source shapes); marked for manual review so no series are silently dropped

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** EXPECTED_LIMITATION

</details>

<details>
<summary>Controls / Variables (1)</summary>

- `Host` (type: `options`)

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
| Pressure | `bargauge` → `bar` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_pressure_cpu_waiting_seconds_total{instance="$node",job="$job"}[$__ra... | TS metrics-prometheus-* \| WHERE node_pressure_cpu_waiting_seconds_total IS NOT ... |
| CPU Busy | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | 100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle", instance="$node"}[$__rat... | TS metrics-prometheus-* \| WHERE mode == "idle" \| WHERE node_cpu_seconds_total ... |
| Sys Load | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | scalar(node_load1{instance="$node",job="$job"}) * 100 / count(count(node_cpu_sec... | FROM metrics-prometheus-* \| WHERE node_load1 IS NOT NULL OR node_cpu_seconds_to... |
| RAM Used | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | (1 - (node_memory_MemAvailable_bytes{instance="$node", job="$job"} / node_memory... | TS metrics-prometheus-* \| WHERE node_memory_MemAvailable_bytes IS NOT NULL OR n... |
| SWAP Used | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | ((node_memory_SwapTotal_bytes{instance="$node",job="$job"} - node_memory_SwapFre... | TS metrics-prometheus-* \| WHERE node_memory_SwapTotal_bytes IS NOT NULL OR node... |
| Root FS Used | `gauge` → `gauge` | migrated_with_warnings | **MINOR_ISSUE** | 100 - ((node_filesystem_avail_bytes{instance="$node",job="$job",mountpoint="/",f... | TS metrics-prometheus-* \| WHERE mountpoint == "/" \| WHERE fstype != "rootfs" \... |
| CPU Cores | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | count(count(node_cpu_seconds_total{instance="$node",job="$job"}) by (cpu)) | FROM metrics-prometheus-* \| WHERE node_cpu_seconds_total IS NOT NULL \| STATS n... |
| Uptime | `stat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | node_time_seconds{instance="$node",job="$job"} - node_boot_time_seconds{instance... | TS metrics-prometheus-* \| WHERE node_time_seconds IS NOT NULL OR node_boot_time... |
| RootFS Total | `stat` → `metric` | migrated | **MINOR_ISSUE** | node_filesystem_size_bytes{instance="$node",job="$job",mountpoint="/",fstype!="r... | TS metrics-prometheus-* \| WHERE mountpoint == "/" \| WHERE fstype != "rootfs" \... |
| RAM Total | `stat` → `metric` | migrated | **MINOR_ISSUE** | node_memory_MemTotal_bytes{instance="$node",job="$job"} | TS metrics-prometheus-* \| WHERE node_memory_MemTotal_bytes IS NOT NULL \| STATS... |
| SWAP Total | `stat` → `metric` | migrated | **MINOR_ISSUE** | node_memory_SwapTotal_bytes{instance="$node",job="$job"} | TS metrics-prometheus-* \| WHERE node_memory_SwapTotal_bytes IS NOT NULL \| STAT... |
| CPU Basic | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | sum(irate(node_cpu_seconds_total{instance="$node",job="$job", mode="system"}[$__... | TS metrics-prometheus-* \| WHERE node_cpu_seconds_total IS NOT NULL \| STATS nod... |
| Memory Basic | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_MemTotal_bytes{instance="$node",job="$job"} \|\|\| node_memory_MemTo... | TS metrics-prometheus-* \| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_... |
| Network Traffic Basic | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_network_receive_bytes_total{instance="$node",job="$job"}[$__rate_inte... | TS metrics-prometheus-* \| WHERE node_network_receive_bytes_total IS NOT NULL OR... |
| Disk Space Used Basic | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | 100 - ((node_filesystem_avail_bytes{instance="$node",job="$job",device!~'rootfs'... | TS metrics-prometheus-* \| WHERE NOT (device RLIKE "rootfs") \| WHERE node_files... |
| CPU | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | sum(irate(node_cpu_seconds_total{instance="$node",job="$job", mode="system"}[$__... | TS metrics-prometheus-* \| WHERE node_cpu_seconds_total IS NOT NULL \| STATS nod... |
| Memory Stack | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_memory_MemTotal_bytes{instance="$node",job="$job"} - node_memory_MemFree_by... | TS metrics-prometheus-* \| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_... |
| Network Traffic | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_network_receive_bytes_total{instance="$node",job="$job"}[$__rate_inte... | TS metrics-prometheus-* \| WHERE node_network_receive_bytes_total IS NOT NULL OR... |
| Disk Space Used | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_filesystem_size_bytes{instance="$node",job="$job",device!~'rootfs'} - node_... | TS metrics-prometheus-* \| WHERE NOT (device RLIKE "rootfs") \| WHERE node_files... |
| Disk IOps | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_disk_reads_completed_total{instance="$node",job="$job",device=~"$disk... | TS metrics-prometheus-* \| WHERE node_disk_reads_completed_total IS NOT NULL OR ... |
| I/O Usage Read / Write | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_disk_read_bytes_total{instance="$node",job="$job",device=~"$diskdevic... | TS metrics-prometheus-* \| WHERE node_disk_read_bytes_total IS NOT NULL OR node_... |
| I/O Utilization | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_disk_io_time_seconds_total{instance="$node",job="$job",device=~"$disk... | TS metrics-prometheus-* \| WHERE node_disk_io_time_seconds_total IS NOT NULL \| ... |
| CPU spent seconds in guests (VMs) | `timeseries` → `bar` | migrated_with_warnings | **MINOR_ISSUE** | sum by(instance) (irate(node_cpu_guest_seconds_total{instance="$node",job="$job"... | TS metrics-prometheus-* \| STATS numerator = SUM(IRATE(CASE((mode == "user"), no... |
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
| CPU Frequency Scaling | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_cpu_scaling_frequency_hertz{instance="$node",job="$job"} \|\|\| avg(node_cp... | TS metrics-prometheus-* \| WHERE node_cpu_scaling_frequency_hertz IS NOT NULL \|... |
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
| Systemd Units State | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | node_systemd_units{instance="$node",job="$job",state="activating"} \|\|\| node_s... | TS metrics-prometheus-* \| WHERE node_systemd_units IS NOT NULL \| STATS node_sy... |
| Disk IOps Completed | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_disk_reads_completed_total{instance="$node",job="$job"}[$__rate_inter... | TS metrics-prometheus-* \| WHERE node_disk_reads_completed_total IS NOT NULL OR ... |
| Disk R/W Data | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_disk_read_bytes_total{instance="$node",job="$job"}[$__rate_interval])... | TS metrics-prometheus-* \| WHERE node_disk_read_bytes_total IS NOT NULL OR node_... |
| Disk Average Wait Time | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(node_disk_read_time_seconds_total{instance="$node",job="$job"}[$__rate_int... | TS metrics-prometheus-* \| WHERE node_disk_read_time_seconds_total IS NOT NULL O... |
| Average Queue Size | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_disk_io_time_weighted_seconds_total{instance="$node",job="$job"}[$__r... | TS metrics-prometheus-* \| WHERE node_disk_io_time_weighted_seconds_total IS NOT... |
| Disk R/W Merged | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_disk_reads_merged_total{instance="$node",job="$job"}[$__rate_interval... | TS metrics-prometheus-* \| WHERE node_disk_reads_merged_total IS NOT NULL OR nod... |
| Time Spent Doing I/Os | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_disk_io_time_seconds_total{instance="$node",job="$job"}[$__rate_inter... | TS metrics-prometheus-* \| WHERE node_disk_io_time_seconds_total IS NOT NULL OR ... |
| Instantaneous Queue Size | `timeseries` → `line` | migrated | **MINOR_ISSUE** | node_disk_io_now{instance="$node",job="$job"} | TS metrics-prometheus-* \| WHERE node_disk_io_now IS NOT NULL \| STATS node_disk... |
| Disk IOps Discards completed / merged | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(node_disk_discards_completed_total{instance="$node",job="$job"}[$__rate_in... | TS metrics-prometheus-* \| WHERE node_disk_discards_completed_total IS NOT NULL ... |
| Filesystem space available | `timeseries` → `line` | migrated | **MINOR_ISSUE** | node_filesystem_avail_bytes{instance="$node",job="$job",device!~'rootfs'} | TS metrics-prometheus-* \| WHERE NOT (device RLIKE "rootfs") \| WHERE node_files... |
| File Nodes Free | `timeseries` → `line` | migrated | **MINOR_ISSUE** | node_filesystem_files_free{instance="$node",job="$job",device!~'rootfs'} | TS metrics-prometheus-* \| WHERE NOT (device RLIKE "rootfs") \| WHERE node_files... |
| File Descriptor | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_filefd_maximum{instance="$node",job="$job"} \|\|\| node_filefd_allocated{in... | TS metrics-prometheus-* \| WHERE node_filefd_maximum IS NOT NULL OR node_filefd_... |
| File Nodes Size | `timeseries` → `line` | migrated | **MINOR_ISSUE** | node_filesystem_files{instance="$node",job="$job",device!~'rootfs'} | TS metrics-prometheus-* \| WHERE NOT (device RLIKE "rootfs") \| WHERE node_files... |
| Filesystem in ReadOnly / Error | `timeseries` → `area` | migrated | **MINOR_ISSUE** | node_filesystem_readonly{instance="$node",job="$job",device!~'rootfs'} \|\|\| no... | TS metrics-prometheus-* \| WHERE NOT (device RLIKE "rootfs") \| WHERE node_files... |
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
| Network Operational Status | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | node_network_up{operstate="up",instance="$node",job="$job"} \|\|\| node_network_... | TS metrics-prometheus-* \| WHERE operstate == "up" \| WHERE node_network_up IS N... |
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel` → approximated bargauge panel

**Translated (bar):**

```
TS metrics-prometheus-*
| WHERE node_pressure_cpu_waiting_seconds_total IS NOT NULL OR node_pressure_memory_waiting_seconds_total IS NOT NULL OR node_pressure_io_waiting_seconds_total IS NOT NULL
| STATS node_pressure_cpu_waiting_seconds_total_CPU_some = IRATE(node_pressure_cpu_waiting_seconds_total, 5m), node_pressure_memory_waiting_seconds_total_Memory_some = IRATE(node_pressure_memory_waiting_seconds_total, 5m), node_pressure_io_waiting_seconds_total_I_O_some = IRATE(node_pressure_io_waiting_seconds_total, 5m) BY time_bucket = TBUCKET(5 minute)
| EVAL CPU = node_pressure_cpu_waiting_seconds_total_CPU_some
| EVAL Mem = node_pressure_memory_waiting_seconds_total_Memory_some
| EVAL I_O = node_pressure_io_waiting_seconds_total_I_O_some
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), CPU = MAX(CPU), Mem = MAX(Mem), I_O = MAX(I_O)
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
- Semantic losses: Dropped variable-driven label filters during migration, Approximated bargauge as bar chart

**Visual IR:**

- Kibana type: `bar`
- Layout: x=0, y=0, w=6, h=8
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 3
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Approximated bargauge as bar chart

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated bargauge as bar chart

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
TS metrics-prometheus-*
| WHERE mode == "idle"
| WHERE node_cpu_seconds_total IS NOT NULL
| STATS node_cpu_seconds_total_mode_idle_rate_avg = AVG(RATE(node_cpu_seconds_total, 5m)) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (100 * (1 - node_cpu_seconds_total_mode_idle_rate_avg))
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
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
- Layout: x=6, y=0, w=6, h=8
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

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

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
FROM metrics-prometheus-*
| WHERE node_load1 IS NOT NULL OR node_cpu_seconds_total IS NOT NULL
| STATS node_load1_instance_job = AVG(node_load1), node_cpu_seconds_total_instance_job_count = COUNT_DISTINCT(cpu) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)
| EVAL computed_value = ((node_load1_instance_job * 100) / node_cpu_seconds_total_instance_job_count)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
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
- Layout: x=12, y=0, w=6, h=8
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Approximated scalar() as a direct metric value; Approximated nested count(count()) as COUNT_DISTINCT(cpu); PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

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

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemAvailable_bytes IS NOT NULL OR node_memory_MemTotal_bytes IS NOT NULL
| STATS node_memory_MemAvailable_bytes_instance_job = AVG(node_memory_MemAvailable_bytes), node_memory_MemTotal_bytes_instance_job = AVG(node_memory_MemTotal_bytes) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = ((1 - (node_memory_MemAvailable_bytes_instance_job / node_memory_MemTotal_bytes_instance_job)) * 100)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Collapsed all series of `node_memory_MemAvailable_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Collapsed all series of `node_memory_MemTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Visual IR:**

- Kibana type: `gauge`
- Layout: x=18, y=0, w=6, h=8
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `node_memory_MemAvailable_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_MemTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `node_memory_MemAvailable_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_MemTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
TS metrics-prometheus-*
| WHERE node_memory_SwapTotal_bytes IS NOT NULL OR node_memory_SwapFree_bytes IS NOT NULL
| STATS node_memory_SwapTotal_bytes_instance_job = AVG(node_memory_SwapTotal_bytes), node_memory_SwapFree_bytes_instance_job = AVG(node_memory_SwapFree_bytes) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (((node_memory_SwapTotal_bytes_instance_job - node_memory_SwapFree_bytes_instance_job) / node_memory_SwapTotal_bytes_instance_job) * 100)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Collapsed all series of `node_memory_SwapTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Collapsed all series of `node_memory_SwapFree_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Visual IR:**

- Kibana type: `gauge`
- Layout: x=24, y=0, w=6, h=8
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `node_memory_SwapTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_SwapFree_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `node_memory_SwapTotal_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_memory_SwapFree_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel` → mapped to gauge panel

**Translated (gauge):**

```
TS metrics-prometheus-*
| WHERE mountpoint == "/"
| WHERE fstype != "rootfs"
| WHERE node_filesystem_avail_bytes IS NOT NULL OR node_filesystem_size_bytes IS NOT NULL
| STATS node_filesystem_avail_bytes_mountpoint_fstype_rootfs = AVG(node_filesystem_avail_bytes), node_filesystem_size_bytes_mountpoint_fstype_rootfs = AVG(node_filesystem_size_bytes) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (100 - ((node_filesystem_avail_bytes_mountpoint_fstype_rootfs * 100) / node_filesystem_size_bytes_mountpoint_fstype_rootfs))
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Collapsed all series of `node_filesystem_avail_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Collapsed all series of `node_filesystem_size_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Visual IR:**

- Kibana type: `gauge`
- Layout: x=30, y=0, w=6, h=8
- Presentation kind: `esql`
- Config keys: type, query, metric, appearance, minimum

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `node_filesystem_avail_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_filesystem_size_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `node_filesystem_avail_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_filesystem_size_bytes` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family` → translated nested count(count()) expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
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
- Layout: x=36, y=0, w=4, h=3
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Approximated nested count(count()) as COUNT_DISTINCT(cpu)

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

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE node_time_seconds IS NOT NULL OR node_boot_time_seconds IS NOT NULL
| STATS node_time_seconds_instance_job = AVG(node_time_seconds), node_boot_time_seconds_instance_job = AVG(node_boot_time_seconds) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (node_time_seconds_instance_job - node_boot_time_seconds_instance_job)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Collapsed all series of `node_time_seconds` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity., Collapsed all series of `node_boot_time_seconds` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

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

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Collapsed all series of `node_time_seconds` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_boot_time_seconds` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Collapsed all series of `node_time_seconds` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.; Collapsed all series of `node_boot_time_seconds` into a single AVG line; the source selector has no series labels (no legend, by(), or dashboard reference), so per-series detail is dropped. Add a legend/by() or migrate with target access to recover per-series fidelity.

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE mountpoint == "/"
| WHERE fstype != "rootfs"
| WHERE node_filesystem_size_bytes IS NOT NULL
| STATS node_filesystem_size_bytes = MAX(LAST_OVER_TIME(node_filesystem_size_bytes)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), node_filesystem_size_bytes = MAX(node_filesystem_size_bytes)
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
- Layout: x=36, y=3, w=4, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically

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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemTotal_bytes IS NOT NULL
| STATS node_memory_MemTotal_bytes = MAX(LAST_OVER_TIME(node_memory_MemTotal_bytes)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), node_memory_MemTotal_bytes = MAX(node_memory_MemTotal_bytes)
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
- Layout: x=40, y=3, w=4, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically

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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE node_memory_SwapTotal_bytes IS NOT NULL
| STATS node_memory_SwapTotal_bytes = MAX(LAST_OVER_TIME(node_memory_SwapTotal_bytes)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), node_memory_SwapTotal_bytes = MAX(node_memory_SwapTotal_bytes)
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
- Layout: x=44, y=3, w=4, h=6
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically

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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE node_cpu_seconds_total IS NOT NULL
| STATS node_cpu_seconds_total_A_lhs = SUM(IRATE(CASE((mode == "system"), node_cpu_seconds_total, NULL), 5m)), node_cpu_seconds_total_A_rhs = COUNT_DISTINCT(cpu), node_cpu_seconds_total_B_lhs = SUM(IRATE(CASE((mode == "user"), node_cpu_seconds_total, NULL), 5m)), node_cpu_seconds_total_B_rhs = COUNT_DISTINCT(cpu), node_cpu_seconds_total_C_lhs = SUM(IRATE(CASE((mode == "iowait"), node_cpu_seconds_total, NULL), 5m)), node_cpu_seconds_total_C_rhs = COUNT_DISTINCT(cpu), node_cpu_seconds_total_D_lhs = SUM(IRATE(CASE((mode RLIKE ".*irq"), node_cpu_seconds_total, NULL), 5m)), node_cpu_seconds_total_D_rhs = COUNT_DISTINCT(cpu), node_cpu_seconds_total_E_lhs = SUM(IRATE(CASE((mode != "idle") and (mode != "user") and (mode != "system") and (mode != "iowait") and (mode != "irq") and (mode != "softirq"), node_cpu_seconds_total, NULL), 5m)), node_cpu_seconds_total_E_rhs = COUNT_DISTINCT(cpu), node_cpu_seconds_total_F_lhs = SUM(IRATE(CASE((mode == "idle"), node_cpu_seconds_total, NULL), 5m)), node_cpu_seconds_total_F_rhs = COUNT_DISTINCT(cpu) BY time_bucket = TBUCKET(5 minute)
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration, Approximated nested count(count()) as COUNT_DISTINCT(cpu)

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

**Warnings:** Grafana panel has 7 field override(s); verify visual mappings manually; Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Approximated nested count(count()) as COUNT_DISTINCT(cpu); PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; Approximated nested count(count()) as COUNT_DISTINCT(cpu)

**Notes:** Grafana panel has 7 field override(s); verify visual mappings manually; Grafana panel description is not carried into Kibana YAML automatically

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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE node_memory_MemTotal_bytes IS NOT NULL OR node_memory_MemFree_bytes IS NOT NULL OR node_memory_Cached_bytes IS NOT NULL OR node_memory_Buffers_bytes IS NOT NULL OR node_memory_SReclaimable_bytes IS NOT NULL OR node_memory_SwapTotal_bytes IS NOT NULL OR node_memory_SwapFree_bytes IS NOT NULL
| STATS node_memory_MemTotal_bytes_A = AVG(node_memory_MemTotal_bytes), node_memory_MemTotal_bytes_B_lhs_lhs = AVG(node_memory_MemTotal_bytes), node_memory_MemFree_bytes_B_lhs_rhs = AVG(node_memory_MemFree_bytes), node_memory_Cached_bytes_B_rhs_lhs_lhs = AVG(node_memory_Cached_bytes), node_memory_Buffers_bytes_B_rhs_lhs_rhs = AVG(node_memory_Buffers_bytes), node_memory_SReclaimable_bytes_B_rhs_rhs = AVG(node_memory_SReclaimable_bytes), node_memory_Cached_bytes_C_lhs_lhs = AVG(node_memory_Cached_bytes), node_memory_Buffers_bytes_C_lhs_rhs = AVG(node_memory_Buffers_bytes), node_memory_SReclaimable_bytes_C_rhs = AVG(node_memory_SReclaimable_bytes), node_memory_MemFree_bytes_D = AVG(node_memory_MemFree_bytes), node_memory_SwapTotal_bytes_E_lhs = AVG(node_memory_SwapTotal_bytes), node_memory_SwapFree_bytes_E_rhs = AVG(node_memory_SwapFree_bytes) BY time_bucket = TBUCKET(5 minute), instance, job
| EVAL RAM_Total = node_memory_MemTotal_bytes_A
| EVAL RAM_Used = ((node_memory_MemTotal_bytes_B_lhs_lhs - node_memory_MemFree_bytes_B_lhs_rhs) - ((node_memory_Cached_bytes_B_rhs_lhs_lhs + node_memory_Buffers_bytes_B_rhs_lhs_rhs) + node_memory_SReclaimable_bytes_B_rhs_rhs))
| EVAL RAM_Cache_Buffer = ((node_memory_Cached_bytes_C_lhs_lhs + node_memory_Buffers_bytes_C_lhs_rhs) + node_memory_SReclaimable_bytes_C_rhs)
| EVAL RAM_Free = node_memory_MemFree_bytes_D
| EVAL SWAP_Used = (node_memory_SwapTotal_bytes_E_lhs - node_memory_SwapFree_bytes_E_rhs)
| KEEP time_bucket, instance, job, RAM_Total, RAM_Used, RAM_Cache_Buffer, RAM_Free, SWAP_Used
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

**Warnings:** Grafana panel has 23 field override(s); verify visual mappings manually; Grafana panel description is not carried into Kibana YAML automatically; XY chart shows a single breakdown; additional grouping dimension(s) ['job'] are in the query but not on the chart, so series differing only by those are visually merged

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

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE node_network_receive_bytes_total IS NOT NULL OR node_network_transmit_bytes_total IS NOT NULL
| STATS node_network_receive_bytes_total_A_lhs = AVG(IRATE(node_network_receive_bytes_total, 5m)), node_network_transmit_bytes_total_B_lhs = AVG(IRATE(node_network_transmit_bytes_total, 5m)) BY time_bucket = TBUCKET(5 minute), device
| EVAL recv = (node_network_receive_bytes_total_A_lhs * 8)
| EVAL trans = (node_network_transmit_bytes_total_B_lhs * 8)
| KEEP time_bucket, device, recv, trans
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
- Output groups: `time_bucket, device`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=11, w=24, h=10
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2
- field_overrides: 24
- has_description: True

**Warnings:** Grafana panel has 24 field override(s); verify visual mappings manually; Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math

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

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE NOT (device RLIKE "rootfs")
| WHERE node_filesystem_avail_bytes IS NOT NULL OR node_filesystem_size_bytes IS NOT NULL
| STATS node_filesystem_avail_bytes_device_rootfs = AVG(node_filesystem_avail_bytes), node_filesystem_size_bytes_device_rootfs = AVG(node_filesystem_size_bytes) BY time_bucket = TBUCKET(5 minute), mountpoint
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

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math

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

### Grafana: NodeJS Application Dashboard

**File:** `nodejs-11159.json` — **Panels:** 9

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Process CPU Usage | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | irate(process_cpu_user_seconds_total{instance=~"$instance"}[2m]) * 100 \|\|\| ir... | TS metrics-prometheus-* \| WHERE process_cpu_user_seconds_total IS NOT NULL OR p... |
| Event Loop Lag | `graph` → `line` | migrated | **MINOR_ISSUE** | nodejs_eventloop_lag_seconds{instance=~"$instance"} | TS metrics-prometheus-* \| WHERE nodejs_eventloop_lag_seconds IS NOT NULL \| STA... |
| Node.js Version | `singlestat` → `datatable` | migrated_with_warnings | **MINOR_ISSUE** | sum(nodejs_version_info{instance=~"$instance"}) by (version) | TS metrics-prometheus-* \| WHERE nodejs_version_info IS NOT NULL \| STATS nodejs... |
| Process Restart Times | `singlestat` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | sum(changes(process_start_time_seconds{instance=~"$instance"}[1m])) | — |
| Process Memory Usage | `graph` → `line` | migrated | **MINOR_ISSUE** | process_resident_memory_bytes{instance=~"$instance"} \|\|\| nodejs_heap_size_tot... | TS metrics-prometheus-* \| WHERE process_resident_memory_bytes IS NOT NULL OR no... |
| Active Handlers/Requests Total | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | nodejs_active_handles_total{instance=~"$instance"} \|\|\| nodejs_active_requests... | TS metrics-prometheus-* \| WHERE nodejs_active_handles_total IS NOT NULL OR node... |
| Heap Total Detail | `graph` → `line` | migrated | **MINOR_ISSUE** | nodejs_heap_space_size_total_bytes{instance=~"$instance"} | TS metrics-prometheus-* \| WHERE nodejs_heap_space_size_total_bytes IS NOT NULL ... |
| Heap Used Detail | `graph` → `line` | migrated | **MINOR_ISSUE** | nodejs_heap_space_size_used_bytes{instance=~"$instance"} | TS metrics-prometheus-* \| WHERE nodejs_heap_space_size_used_bytes IS NOT NULL \... |
| Heap Available Detail | `graph` → `line` | migrated | **MINOR_ISSUE** | nodejs_heap_space_size_available_bytes{instance=~"$instance"} | TS metrics-prometheus-* \| WHERE nodejs_heap_space_size_available_bytes IS NOT N... |

<details>
<summary>Detailed traces (9 panels)</summary>

#### Process CPU Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
irate(process_cpu_user_seconds_total{instance=~"$instance"}[2m]) * 100 ||| irate(process_cpu_system_seconds_total{instance=~"$instance"}[2m]) * 100
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE process_cpu_user_seconds_total IS NOT NULL OR process_cpu_system_seconds_total IS NOT NULL
| STATS process_cpu_user_seconds_total_A_lhs = AVG(IRATE(process_cpu_user_seconds_total, 2m)), process_cpu_system_seconds_total_B_lhs = AVG(IRATE(process_cpu_system_seconds_total, 2m)) BY time_bucket = TBUCKET(5 minute), instance
| EVAL User_CPU = (process_cpu_user_seconds_total_A_lhs * 100)
| EVAL Sys_CPU = (process_cpu_system_seconds_total_B_lhs * 100)
| KEEP time_bucket, instance, User_CPU, Sys_CPU
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `*`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `User_CPU`
- Output groups: `time_bucket, instance`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=20, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Event Loop Lag

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
nodejs_eventloop_lag_seconds{instance=~"$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE nodejs_eventloop_lag_seconds IS NOT NULL
| STATS nodejs_eventloop_lag_seconds = MAX(LAST_OVER_TIME(nodejs_eventloop_lag_seconds)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `nodejs_eventloop_lag_seconds`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `nodejs_eventloop_lag_seconds`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=20, y=0, w=18, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Node.js Version

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(nodejs_version_info{instance=~"$instance"}) by (version)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → approximated grouped stat as datatable

**Translated (datatable):**

```
TS metrics-prometheus-*
| WHERE nodejs_version_info IS NOT NULL
| STATS nodejs_version_info = SUM(nodejs_version_info) BY time_bucket = TBUCKET(5 minute), version
| SORT time_bucket ASC
| STATS nodejs_version_info = MAX(nodejs_version_info) BY version
| KEEP version, nodejs_version_info
```

**Query IR:**

- Family: `simple_agg`
- Metric: `nodejs_version_info`
- Outer agg: `sum`
- Group labels: `version`
- Output shape: `table`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `nodejs_version_info`
- Output groups: `version`
- Semantic losses: Dropped variable-driven label filters during migration, Approximated grouped stat panel as summary table

**Visual IR:**

- Kibana type: `datatable`
- Layout: x=36, y=0, w=12, h=5
- Presentation kind: `esql`
- Config keys: type, query, metrics, breakdowns

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Approximated grouped stat panel as summary table

**Semantic losses:** Dropped variable-driven label filters during migration; Approximated grouped stat panel as summary table

**Verdict:** MINOR_ISSUE

#### Process Restart Times

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (singlestat):**

```
sum(changes(process_start_time_seconds{instance=~"$instance"}[1m]))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=unknown backend=ast
- `query_classifiers` / `fragment_guardrails` → changes() counts value transitions and has no ES|QL equivalent

**Query IR:**

- Family: `unknown`
- Metric: `process_start_time_seconds`
- Range window: `1m`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=38, y=5, w=10, h=6
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** changes() counts value transitions and has no ES|QL equivalent

**Verdict:** EXPECTED_LIMITATION

#### Process Memory Usage

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
process_resident_memory_bytes{instance=~"$instance"} ||| nodejs_heap_size_total_bytes{instance=~"$instance"} ||| nodejs_heap_size_used_bytes{instance=~"$instance"} ||| nodejs_external_memory_bytes{instance=~"$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE process_resident_memory_bytes IS NOT NULL OR nodejs_heap_size_total_bytes IS NOT NULL OR nodejs_heap_size_used_bytes IS NOT NULL OR nodejs_external_memory_bytes IS NOT NULL
| STATS process_resident_memory_bytes_A = AVG(process_resident_memory_bytes), nodejs_heap_size_total_bytes_B = AVG(nodejs_heap_size_total_bytes), nodejs_heap_size_used_bytes_C = AVG(nodejs_heap_size_used_bytes), nodejs_external_memory_bytes_D = AVG(nodejs_external_memory_bytes) BY time_bucket = TBUCKET(5 minute), instance
| EVAL Process_Memory = process_resident_memory_bytes_A
| EVAL Heap_Total = nodejs_heap_size_total_bytes_B
| EVAL Heap_Used = nodejs_heap_size_used_bytes_C
| EVAL External_Memory = nodejs_external_memory_bytes_D
| KEEP time_bucket, instance, Process_Memory, Heap_Total, Heap_Used, External_Memory
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `process_resident_memory_bytes`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `Process_Memory`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=11, w=32, h=10
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 4

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Active Handlers/Requests Total

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
nodejs_active_handles_total{instance=~"$instance"} ||| nodejs_active_requests_total{instance=~"$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE nodejs_active_handles_total IS NOT NULL OR nodejs_active_requests_total IS NOT NULL
| STATS nodejs_active_handles_total_A = MAX(LAST_OVER_TIME(nodejs_active_handles_total)), nodejs_active_requests_total_B = MAX(LAST_OVER_TIME(nodejs_active_requests_total)) BY time_bucket = TBUCKET(5 minute), instance
| EVAL Active_Handler = nodejs_active_handles_total_A
| EVAL Active_Request = nodejs_active_requests_total_B
| KEEP time_bucket, instance, Active_Handler, Active_Request
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `nodejs_active_handles_total`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `Active_Handler`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=32, y=11, w=16, h=10
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Counter referenced without rate(); using LAST_OVER_TIME to preserve raw cumulative value

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Heap Total Detail

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
nodejs_heap_space_size_total_bytes{instance=~"$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE nodejs_heap_space_size_total_bytes IS NOT NULL
| STATS nodejs_heap_space_size_total_bytes = MAX(LAST_OVER_TIME(nodejs_heap_space_size_total_bytes)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `nodejs_heap_space_size_total_bytes`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `nodejs_heap_space_size_total_bytes`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=21, w=16, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Heap Used Detail

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
nodejs_heap_space_size_used_bytes{instance=~"$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE nodejs_heap_space_size_used_bytes IS NOT NULL
| STATS nodejs_heap_space_size_used_bytes = MAX(LAST_OVER_TIME(nodejs_heap_space_size_used_bytes)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `nodejs_heap_space_size_used_bytes`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `nodejs_heap_space_size_used_bytes`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=16, y=21, w=16, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Heap Available Detail

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
nodejs_heap_space_size_available_bytes{instance=~"$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE nodejs_heap_space_size_available_bytes IS NOT NULL
| STATS nodejs_heap_space_size_available_bytes = MAX(LAST_OVER_TIME(nodejs_heap_space_size_available_bytes)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `nodejs_heap_space_size_available_bytes`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `nodejs_heap_space_size_available_bytes`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=32, y=21, w=16, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Controls / Variables (1)</summary>

- `instance` (type: `options`)

</details>

---

### Grafana: Prometheus 2.0 Overview

**File:** `prometheus-2-overview-3662.json` — **Panels:** 30

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Uptime | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | avg(avg_over_time(up{instance=~"$instance",job=~"$job"}[$interval]) * 100) | TS metrics-prometheus-* \| WHERE up IS NOT NULL \| STATS up_instance_job_avg_ove... |
| Currently Down | `table` → `datatable` | migrated_with_warnings | **MINOR_ISSUE** | up{instance=~"$instance",job=~"$job"} < 1 | TS metrics-prometheus-* \| WHERE up IS NOT NULL \| STATS up = AVG(up) BY time_bu... |
| Total Series | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(prometheus_tsdb_head_series{job=~"$job",instance=~"$instance"}) | TS metrics-prometheus-* \| WHERE prometheus_tsdb_head_series IS NOT NULL \| STAT... |
| Memory Chunks | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(prometheus_tsdb_head_chunks{job=~"$job",instance=~"$instance"}) | TS metrics-prometheus-* \| WHERE prometheus_tsdb_head_chunks IS NOT NULL \| STAT... |
| Missed Iterations | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(sum_over_time(prometheus_evaluator_iterations_missed_total{job=~"$job",insta... | TS metrics-prometheus-* \| WHERE prometheus_evaluator_iterations_missed_total IS... |
| Skipped Iterations | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(sum_over_time(prometheus_evaluator_iterations_skipped_total{job=~"$job",inst... | TS metrics-prometheus-* \| WHERE prometheus_evaluator_iterations_skipped_total I... |
| Tardy Scrapes | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(sum_over_time(prometheus_target_scrapes_exceeded_sample_limit_total{job=~"$j... | TS metrics-prometheus-* \| WHERE prometheus_target_scrapes_exceeded_sample_limit... |
| Reload Failures | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(sum_over_time(prometheus_tsdb_reloads_failures_total{job=~"$job",instance=~"... | TS metrics-prometheus-* \| WHERE prometheus_tsdb_reloads_failures_total IS NOT N... |
| Skipped Scrapes | `singlestat` → `metric` | migrated_with_warnings | **MINOR_ISSUE** | sum(sum_over_time(prometheus_target_scrapes_exceeded_sample_limit_total{job=~"$j... | TS metrics-prometheus-* \| WHERE prometheus_target_scrapes_exceeded_sample_limit... |
| Failures and Errors | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(increase(net_conntrack_dialer_conn_failed_total{instance=~"$instance"}[5m]))... | TS metrics-prometheus-* \| WHERE net_conntrack_dialer_conn_failed_total IS NOT N... |
| Upness (stacked) | `graph` → `area` | migrated_with_warnings | **MINOR_ISSUE** | up{instance=~"$instance",job=~"$job"} | TS metrics-prometheus-* \| WHERE up IS NOT NULL \| STATS up = MAX(LAST_OVER_TIME... |
| Storage Memory Chunks | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | prometheus_tsdb_head_chunks{job=~"$job",instance=~"$instance"} | TS metrics-prometheus-* \| WHERE prometheus_tsdb_head_chunks IS NOT NULL \| STAT... |
| Series Count | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | prometheus_tsdb_head_series{job=~"$job",instance=~"$instance"} | TS metrics-prometheus-* \| WHERE prometheus_tsdb_head_series IS NOT NULL \| STAT... |
| Series Created / Removed | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum( increase(prometheus_tsdb_head_series_created_total{instance=~"$instance"}[5... | TS metrics-prometheus-* \| WHERE prometheus_tsdb_head_series_created_total IS NO... |
| Appended Samples per Second | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | rate(prometheus_tsdb_head_samples_appended_total{job=~"$job",instance=~"$instanc... | TS metrics-prometheus-* \| WHERE prometheus_tsdb_head_samples_appended_total IS ... |
| Scrape Sync Total | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(prometheus_target_scrape_pool_sync_total{job=~"$job",instance=~"$instance"})... | TS metrics-prometheus-* \| WHERE prometheus_target_scrape_pool_sync_total IS NOT... |
| Target Sync | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(prometheus_target_sync_length_seconds_sum{job=~"$job",instance=~"$insta... | TS metrics-prometheus-* \| WHERE prometheus_target_sync_length_seconds_sum IS NO... |
| Scrape Duration | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | scrape_duration_seconds{instance=~"$instance"} | TS metrics-prometheus-* \| WHERE scrape_duration_seconds IS NOT NULL \| STATS sc... |
| Rejected Scrapes | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(prometheus_target_scrapes_exceeded_sample_limit_total{job=~"$job",instance=~... | TS metrics-prometheus-* \| WHERE prometheus_target_scrapes_exceeded_sample_limit... |
| Average Rule Evaluation Duration | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | 1000 * rate(prometheus_evaluator_duration_seconds_sum{job=~"$job", instance=~"$i... | TS metrics-prometheus-* \| WHERE prometheus_evaluator_duration_seconds_sum IS NO... |
| Prometheus Engine Query Duration Seconds | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(prometheus_engine_query_duration_seconds_sum{job=~"$job",instance=~"$instanc... | TS metrics-prometheus-* \| WHERE prometheus_engine_query_duration_seconds_sum IS... |
| HTTP Request Duration | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(http_request_duration_microseconds_count{job=~"$job",instance=~"$instan... | TS metrics-prometheus-* \| WHERE http_request_duration_microseconds_count IS NOT... |
| Rule Evaluator Iterations | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(prometheus_evaluator_iterations_total{job=~"$job", instance=~"$instance... | TS metrics-prometheus-* \| WHERE prometheus_evaluator_iterations_total IS NOT NU... |
| Notifications Sent | `graph` → `line` | migrated | **CORRECT** | rate(prometheus_notifications_sent_total[5m]) | TS metrics-prometheus-* \| WHERE prometheus_notifications_sent_total IS NOT NULL... |
| Minutes Since Successful Config Reload | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | (time() - prometheus_config_last_reload_success_timestamp_seconds{job=~"$job",in... | FROM metrics-prometheus-* \| WHERE prometheus_config_last_reload_success_timesta... |
| Successful Config Reload | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | prometheus_config_last_reload_successful{job=~"$job",instance=~"$instance"} | TS metrics-prometheus-* \| WHERE prometheus_config_last_reload_successful IS NOT... |
| GC Rate / 2m | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(go_gc_duration_seconds_sum{instance=~"$instance",job=~"$job"}[2m])) by ... | TS metrics-prometheus-* \| WHERE go_gc_duration_seconds_sum IS NOT NULL \| STATS... |
| Go Memory Usage (FIXME) | `graph` → `area` | migrated_with_warnings | **MINOR_ISSUE** | sum(go_memstats_alloc_bytes{job=~"$job", instance=~"$instance"}) \|\|\| sum(go_m... | TS metrics-prometheus-* \| WHERE go_memstats_alloc_bytes IS NOT NULL OR go_memst... |
| Scrape Duration | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | prometheus_target_interval_length_seconds{instance=~"$instance", job=~"$job"} | TS metrics-prometheus-* \| WHERE prometheus_target_interval_length_seconds IS NO... |
| Target Scrapes / 5m | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(prometheus_target_interval_length_seconds_count{job=~"$job",instance=~"... | TS metrics-prometheus-* \| WHERE prometheus_target_interval_length_seconds_count... |

<details>
<summary>Detailed traces (30 panels)</summary>

#### Uptime

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
avg(avg_over_time(up{instance=~"$instance",job=~"$job"}[$interval]) * 100)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE up IS NOT NULL
| STATS up_instance_job_avg_over_time_avg = AVG(AVG_OVER_TIME(up, 5m)) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (up_instance_job_avg_over_time_avg * 100)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
| KEEP time_bucket, computed_value
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

- Kibana type: `metric`
- Layout: x=0, y=0, w=12, h=12
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### Currently Down

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (table):**

```
up{instance=~"$instance",job=~"$job"} < 1
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter` → applied post-aggregation filter < 1
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel`
- `panel_translators` / `gauge_panel`
- `panel_translators` / `datatable_panel` → mapped to datatable panel

**Translated (datatable):**

```
TS metrics-prometheus-*
| WHERE up IS NOT NULL
| STATS up = AVG(up) BY time_bucket = TBUCKET(5 minute), instance
| WHERE up < 1
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `up`
- Output shape: `table`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `up`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `datatable`
- Layout: x=12, y=0, w=12, h=12
- Presentation kind: `esql`
- Config keys: type, query, metrics, breakdowns

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### Total Series

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(prometheus_tsdb_head_series{job=~"$job",instance=~"$instance"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE prometheus_tsdb_head_series IS NOT NULL
| STATS prometheus_tsdb_head_series = SUM(prometheus_tsdb_head_series) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), prometheus_tsdb_head_series = MAX(prometheus_tsdb_head_series)
| KEEP time_bucket, prometheus_tsdb_head_series
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `prometheus_tsdb_head_series`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `prometheus_tsdb_head_series`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=24, y=0, w=12, h=12
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### Memory Chunks

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(prometheus_tsdb_head_chunks{job=~"$job",instance=~"$instance"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE prometheus_tsdb_head_chunks IS NOT NULL
| STATS prometheus_tsdb_head_chunks = SUM(prometheus_tsdb_head_chunks) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), prometheus_tsdb_head_chunks = MAX(prometheus_tsdb_head_chunks)
| KEEP time_bucket, prometheus_tsdb_head_chunks
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `prometheus_tsdb_head_chunks`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `prometheus_tsdb_head_chunks`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=36, y=0, w=12, h=12
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Missed Iterations

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(sum_over_time(prometheus_evaluator_iterations_missed_total{job=~"$job",instance=~"$instance"}[$interval]))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE prometheus_evaluator_iterations_missed_total IS NOT NULL
| STATS prometheus_evaluator_iterations_missed_total = SUM(SUM_OVER_TIME(TO_DOUBLE(prometheus_evaluator_iterations_missed_total), 5m)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), prometheus_evaluator_iterations_missed_total = MAX(prometheus_evaluator_iterations_missed_total)
| KEEP time_bucket, prometheus_evaluator_iterations_missed_total
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `prometheus_evaluator_iterations_missed_total`
- Range func: `sum_over_time`
- Range window: `5m`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `prometheus_evaluator_iterations_missed_total`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=0, y=0, w=8, h=11
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### Skipped Iterations

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(sum_over_time(prometheus_evaluator_iterations_skipped_total{job=~"$job",instance=~"$instance"}[$interval]))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE prometheus_evaluator_iterations_skipped_total IS NOT NULL
| STATS prometheus_evaluator_iterations_skipped_total = SUM(SUM_OVER_TIME(TO_DOUBLE(prometheus_evaluator_iterations_skipped_total), 5m)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), prometheus_evaluator_iterations_skipped_total = MAX(prometheus_evaluator_iterations_skipped_total)
| KEEP time_bucket, prometheus_evaluator_iterations_skipped_total
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `prometheus_evaluator_iterations_skipped_total`
- Range func: `sum_over_time`
- Range window: `5m`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `prometheus_evaluator_iterations_skipped_total`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=8, y=0, w=8, h=11
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### Tardy Scrapes

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(sum_over_time(prometheus_target_scrapes_exceeded_sample_limit_total{job=~"$job",instance=~"$instance"}[$interval]))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE prometheus_target_scrapes_exceeded_sample_limit_total IS NOT NULL
| STATS prometheus_target_scrapes_exceeded_sample_limit_total = SUM(SUM_OVER_TIME(TO_DOUBLE(prometheus_target_scrapes_exceeded_sample_limit_total), 5m)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), prometheus_target_scrapes_exceeded_sample_limit_total = MAX(prometheus_target_scrapes_exceeded_sample_limit_total)
| KEEP time_bucket, prometheus_target_scrapes_exceeded_sample_limit_total
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `prometheus_target_scrapes_exceeded_sample_limit_total`
- Range func: `sum_over_time`
- Range window: `5m`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `prometheus_target_scrapes_exceeded_sample_limit_total`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=16, y=0, w=8, h=11
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### Reload Failures

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(sum_over_time(prometheus_tsdb_reloads_failures_total{job=~"$job",instance=~"$instance"}[$interval]))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE prometheus_tsdb_reloads_failures_total IS NOT NULL
| STATS prometheus_tsdb_reloads_failures_total = SUM(SUM_OVER_TIME(TO_DOUBLE(prometheus_tsdb_reloads_failures_total), 5m)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), prometheus_tsdb_reloads_failures_total = MAX(prometheus_tsdb_reloads_failures_total)
| KEEP time_bucket, prometheus_tsdb_reloads_failures_total
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `prometheus_tsdb_reloads_failures_total`
- Range func: `sum_over_time`
- Range window: `5m`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `prometheus_tsdb_reloads_failures_total`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=24, y=0, w=8, h=11
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### Skipped Scrapes

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (singlestat):**

```
sum(sum_over_time(prometheus_target_scrapes_exceeded_sample_limit_total{job=~"$job",instance=~"$instance"}[$interval])) +
sum(sum_over_time(prometheus_target_scrapes_sample_duplicate_timestamp_total{job=~"$job",instance=~"$instance"}[$interval])) +
sum(sum_over_time(prometheus_target_scrapes_sample_out_of_bounds_total{job=~"$job",instance=~"$instance"}[$interval])) +
sum(sum_over_time(prometheus_target_scrapes_sample_out_of_order_total{job=~"$job",instance=~"$instance"}[$interval]))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE prometheus_target_scrapes_exceeded_sample_limit_total IS NOT NULL OR prometheus_target_scrapes_sample_duplicate_timestamp_total IS NOT NULL OR prometheus_target_scrapes_sample_out_of_bounds_total IS NOT NULL OR prometheus_target_scrapes_sample_out_of_order_total IS NOT NULL
| STATS prometheus_target_scrapes_exceeded_sample_limit_total_job_instance_sum_over_time_sum = SUM(SUM_OVER_TIME(TO_DOUBLE(prometheus_target_scrapes_exceeded_sample_limit_total), 5m)), prometheus_target_scrapes_sample_duplicate_timestamp_total_job_instance_sum_over_time_sum = SUM(SUM_OVER_TIME(TO_DOUBLE(prometheus_target_scrapes_sample_duplicate_timestamp_total), 5m)), prometheus_target_scrapes_sample_out_of_bounds_total_job_instance_sum_over_time_sum = SUM(SUM_OVER_TIME(TO_DOUBLE(prometheus_target_scrapes_sample_out_of_bounds_total), 5m)), prometheus_target_scrapes_sample_out_of_order_total_job_instance_sum_over_time_sum = SUM(SUM_OVER_TIME(TO_DOUBLE(prometheus_target_scrapes_sample_out_of_order_total), 5m)) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (((prometheus_target_scrapes_exceeded_sample_limit_total_job_instance_sum_over_time_sum + prometheus_target_scrapes_sample_duplicate_timestamp_total_job_instance_sum_over_time_sum) + prometheus_target_scrapes_sample_out_of_bounds_total_job_instance_sum_over_time_sum) + prometheus_target_scrapes_sample_out_of_order_total_job_instance_sum_over_time_sum)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
| KEEP time_bucket, computed_value
| SORT time_bucket ASC
```

**Query IR:**

- Family: `binary_expr`
- Metric: `computed_value`
- Binary op: `+`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `computed_value`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=32, y=0, w=16, h=11
- Presentation kind: `esql`
- Config keys: type, query, primary, titles_and_text

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### Failures and Errors

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum(increase(net_conntrack_dialer_conn_failed_total{instance=~"$instance"}[5m])) > 0 ||| sum(increase(prometheus_evaluator_iterations_missed_total{instance=~"$instance"}[5m])) > 0 ||| sum(increase(prometheus_evaluator_iterations_skipped_total{instance=~"$instance"}[5m])) > 0 ||| sum(increase(prometheus_rule_evaluation_failures_total{instance=~"$instance"}[5m])) > 0 ||| sum(increase(prometheus_sd_azure_refresh_failures_total{instance=~"$instance"}[5m])) > 0 ||| sum(increase(prometheus_sd_consul_rpc_failures_total{instance=~"$instance"}[5m])) > 0 ||| sum(increase(prometheus_sd_dns_lookup_failures_total{instance=~"$instance"}[5m])) > 0 ||| sum(increase(prometheus_sd_ec2_refresh_failures_total{instance=~"$instance"}[5m])) > 0 ||| sum(increase(prometheus_sd_gce_refresh_failures_total{instance=~"$instance"}[5m])) > 0 ||| sum(increase(prometheus_sd_marathon_refresh_failures_total{instance=~"$instance"}[5m])) > 0 ||| sum(increase(prometheus_sd_openstack_refresh_failures_total{instance=~"$instance"}[5m])) > 0 ||| sum(increase(prometheus_sd_triton_refresh_failures_total{instance=~"$instance"}[5m])) > 0 ||| sum(increase(prometheus_target_scrapes_exceeded_sample_limit_total{instance=~"$instance"}[5m])) > 0 ||| sum(increase(prometheus_target_scrapes_sample_duplicate_timestamp_total{instance=~"$instance"}[5m])) > 0 ||| sum(increase(prometheus_target_scrapes_sample_out_of_bounds_total{instance=~"$instance"}[5m])) > 0 ||| sum(increase(prometheus_target_scrapes_sample_out_of_order_total{instance=~"$instance"}[5m])) > 0 ||| sum(increase(prometheus_treecache_zookeeper_failures_total{instance=~"$instance"}[5m])) > 0 ||| sum(increase(prometheus_tsdb_compactions_failed_total{instance=~"$instance"}[5m])) > 0 ||| sum(increase(prometheus_tsdb_head_series_not_found{instance=~"$instance"}[5m])) > 0 ||| sum(increase(prometheus_tsdb_reloads_failures_total{instance=~"$instance"}[5m])) > 0
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter` → applied post-aggregation filter > 0
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE net_conntrack_dialer_conn_failed_total IS NOT NULL OR prometheus_evaluator_iterations_missed_total IS NOT NULL OR prometheus_evaluator_iterations_skipped_total IS NOT NULL OR prometheus_rule_evaluation_failures_total IS NOT NULL OR prometheus_sd_azure_refresh_failures_total IS NOT NULL OR prometheus_sd_consul_rpc_failures_total IS NOT NULL OR prometheus_sd_dns_lookup_failures_total IS NOT NULL OR prometheus_sd_ec2_refresh_failures_total IS NOT NULL OR prometheus_sd_gce_refresh_failures_total IS NOT NULL OR prometheus_sd_marathon_refresh_failures_total IS NOT NULL OR prometheus_sd_openstack_refresh_failures_total IS NOT NULL OR prometheus_sd_triton_refresh_failures_total IS NOT NULL OR prometheus_target_scrapes_exceeded_sample_limit_total IS NOT NULL OR prometheus_target_scrapes_sample_duplicate_timestamp_total IS NOT NULL OR prometheus_target_scrapes_sample_out_of_bounds_total IS NOT NULL OR prometheus_target_scrapes_sample_out_of_order_total IS NOT NULL OR prometheus_treecache_zookeeper_failures_total IS NOT NULL OR prometheus_tsdb_compactions_failed_total IS NOT NULL OR prometheus_tsdb_head_series_not_found IS NOT NULL OR prometheus_tsdb_reloads_failures_total IS NOT NULL
| STATS net_conntrack_dialer_conn_failed_total_A = SUM(INCREASE(net_conntrack_dialer_conn_failed_total, 5m)), prometheus_evaluator_iterations_missed_total_B = SUM(INCREASE(prometheus_evaluator_iterations_missed_total, 5m)), prometheus_evaluator_iterations_skipped_total_C = SUM(INCREASE(prometheus_evaluator_iterations_skipped_total, 5m)), prometheus_rule_evaluation_failures_total_D = SUM(INCREASE(prometheus_rule_evaluation_failures_total, 5m)), prometheus_sd_azure_refresh_failures_total_E = SUM(INCREASE(prometheus_sd_azure_refresh_failures_total, 5m)), prometheus_sd_consul_rpc_failures_total_F = SUM(INCREASE(prometheus_sd_consul_rpc_failures_total, 5m)), prometheus_sd_dns_lookup_failures_total_G = SUM(INCREASE(prometheus_sd_dns_lookup_failures_total, 5m)), prometheus_sd_ec2_refresh_failures_total_H = SUM(INCREASE(prometheus_sd_ec2_refresh_failures_total, 5m)), prometheus_sd_gce_refresh_failures_total_I = SUM(INCREASE(prometheus_sd_gce_refresh_failures_total, 5m)), prometheus_sd_marathon_refresh_failures_total_J = SUM(INCREASE(prometheus_sd_marathon_refresh_failures_total, 5m)), prometheus_sd_openstack_refresh_failures_total_K = SUM(INCREASE(prometheus_sd_openstack_refresh_failures_total, 5m)), prometheus_sd_triton_refresh_failures_total_L = SUM(INCREASE(prometheus_sd_triton_refresh_failures_total, 5m)), prometheus_target_scrapes_exceeded_sample_limit_total_M = SUM(INCREASE(prometheus_target_scrapes_exceeded_sample_limit_total, 5m)), prometheus_target_scrapes_sample_duplicate_timestamp_total_N = SUM(INCREASE(prometheus_target_scrapes_sample_duplicate_timestamp_total, 5m)), prometheus_target_scrapes_sample_out_of_bounds_total_O = SUM(INCREASE(prometheus_target_scrapes_sample_out_of_bounds_total, 5m)), prometheus_target_scrapes_sample_out_of_order_total_P = SUM(INCREASE(prometheus_target_scrapes_sample_out_of_order_total, 5m)), prometheus_treecache_zookeeper_failures_total_Q = SUM(INCREASE(prometheus_treecache_zookeeper_failures_total, 5m)), prometheus_tsdb_compactions_failed_total_R = SUM(INCREASE(prometheus_tsdb_compactions_failed_total, 5m)), prometheus_tsdb_head_series_not_found_S = SUM(MAX_OVER_TIME(TO_DOUBLE(prometheus_tsdb_head_series_not_found), 5m)), prometheus_tsdb_reloads_failures_total_T = SUM(INCREASE(prometheus_tsdb_reloads_failures_total, 5m)) BY time_bucket = TBUCKET(5 minute)
| EVAL Failed_Connections = CASE(net_conntrack_dialer_conn_failed_total_A > 0, net_conntrack_dialer_conn_failed_total_A, NULL)
| EVAL Missed_Iterations = CASE(prometheus_evaluator_iterations_missed_total_B > 0, prometheus_evaluator_iterations_missed_total_B, NULL)
| EVAL Skipped_Iterations = CASE(prometheus_evaluator_iterations_skipped_total_C > 0, prometheus_evaluator_iterations_skipped_total_C, NULL)
| EVAL Evaluation = CASE(prometheus_rule_evaluation_failures_total_D > 0, prometheus_rule_evaluation_failures_total_D, NULL)
| EVAL Azure_Refresh = CASE(prometheus_sd_azure_refresh_failures_total_E > 0, prometheus_sd_azure_refresh_failures_total_E, NULL)
| EVAL Consul_RPC = CASE(prometheus_sd_consul_rpc_failures_total_F > 0, prometheus_sd_consul_rpc_failures_total_F, NULL)
| EVAL DNS_Lookup = CASE(prometheus_sd_dns_lookup_failures_total_G > 0, prometheus_sd_dns_lookup_failures_total_G, NULL)
| EVAL EC2_Refresh = CASE(prometheus_sd_ec2_refresh_failures_total_H > 0, prometheus_sd_ec2_refresh_failures_total_H, NULL)
| EVAL GCE_Refresh = CASE(prometheus_sd_gce_refresh_failures_total_I > 0, prometheus_sd_gce_refresh_failures_total_I, NULL)
| EVAL Marathon_Refresh = CASE(prometheus_sd_marathon_refresh_failures_total_J > 0, prometheus_sd_marathon_refresh_failures_total_J, NULL)
| EVAL Openstack_Refresh = CASE(prometheus_sd_openstack_refresh_failures_total_K > 0, prometheus_sd_openstack_refresh_failures_total_K, NULL)
| EVAL Triton_Refresh = CASE(prometheus_sd_triton_refresh_failures_total_L > 0, prometheus_sd_triton_refresh_failures_total_L, NULL)
| EVAL Sample_Limit = CASE(prometheus_target_scrapes_exceeded_sample_limit_total_M > 0, prometheus_target_scrapes_exceeded_sample_limit_total_M, NULL)
| EVAL Duplicate_Timestamp = CASE(prometheus_target_scrapes_sample_duplicate_timestamp_total_N > 0, prometheus_target_scrapes_sample_duplicate_timestamp_total_N, NULL)
| EVAL Timestamp_Out_of_Bounds = CASE(prometheus_target_scrapes_sample_out_of_bounds_total_O > 0, prometheus_target_scrapes_sample_out_of_bounds_total_O, NULL)
| EVAL Sample_Out_of_Order = CASE(prometheus_target_scrapes_sample_out_of_order_total_P > 0, prometheus_target_scrapes_sample_out_of_order_total_P, NULL)
| EVAL Zookeeper = CASE(prometheus_treecache_zookeeper_failures_total_Q > 0, prometheus_treecache_zookeeper_failures_total_Q, NULL)
| EVAL TSDB_Compactions = CASE(prometheus_tsdb_compactions_failed_total_R > 0, prometheus_tsdb_compactions_failed_total_R, NULL)
| EVAL Series_Not_Found = CASE(prometheus_tsdb_head_series_not_found_S > 0, prometheus_tsdb_head_series_not_found_S, NULL)
| EVAL Reload = CASE(prometheus_tsdb_reloads_failures_total_T > 0, prometheus_tsdb_reloads_failures_total_T, NULL)
| KEEP time_bucket, Failed_Connections, Missed_Iterations, Skipped_Iterations, Evaluation, Azure_Refresh, Consul_RPC, DNS_Lookup, EC2_Refresh, GCE_Refresh, Marathon_Refresh, Openstack_Refresh, Triton_Refresh, Sample_Limit, Duplicate_Timestamp, Timestamp_Out_of_Bounds, Sample_Out_of_Order, Zookeeper, TSDB_Compactions, Series_Not_Found, Reload
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `net_conntrack_dialer_conn_failed_total`
- Range func: `increase`
- Range window: `5m`
- Outer agg: `sum`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `Failed_Connections`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration, Source PromQL used increase() but prometheus_tsdb_head_series_not_found is typed as gauge in the target index; rendered as MAX_OVER_TIME (cumulative ceiling) instead. Fix the ingest mapping to mark this field as a counter to recover the true increase over the window.

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=48, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 20
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Dropped variable-driven label filters during migration; Source PromQL used increase() but prometheus_tsdb_head_series_not_found is typed as gauge in the target index; rendered as MAX_OVER_TIME (cumulative ceiling) instead. Fix the ingest mapping to mark this field as a counter to recover the true increase over the window.

**Semantic losses:** Dropped variable-driven label filters during migration; Source PromQL used increase() but prometheus_tsdb_head_series_not_found is typed as gauge in the target index; rendered as MAX_OVER_TIME (cumulative ceiling) instead. Fix the ingest mapping to mark this field as a counter to recover the true increase over the window.

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

#### Upness (stacked)

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
up{instance=~"$instance",job=~"$job"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE up IS NOT NULL
| STATS up = MAX(LAST_OVER_TIME(up)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `up`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `up`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `area`
- Layout: x=0, y=0, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Storage Memory Chunks

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
prometheus_tsdb_head_chunks{job=~"$job",instance=~"$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE prometheus_tsdb_head_chunks IS NOT NULL
| STATS prometheus_tsdb_head_chunks = MAX(LAST_OVER_TIME(prometheus_tsdb_head_chunks)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `prometheus_tsdb_head_chunks`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `prometheus_tsdb_head_chunks`
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

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Series Count

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
prometheus_tsdb_head_series{job=~"$job",instance=~"$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE prometheus_tsdb_head_series IS NOT NULL
| STATS prometheus_tsdb_head_series = MAX(LAST_OVER_TIME(prometheus_tsdb_head_series)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_metric`
- Metric: `prometheus_tsdb_head_series`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `prometheus_tsdb_head_series`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=24, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Series Created / Removed

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
sum( increase(prometheus_tsdb_head_series_created_total{instance=~"$instance"}[5m]) ) ||| sum( increase(prometheus_tsdb_head_series_removed_total{instance=~"$instance"}[5m]) )
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE prometheus_tsdb_head_series_created_total IS NOT NULL OR prometheus_tsdb_head_series_removed_total IS NOT NULL
| STATS prometheus_tsdb_head_series_created_total_A = SUM(INCREASE(prometheus_tsdb_head_series_created_total, 5m)), prometheus_tsdb_head_series_removed_total_B = SUM(INCREASE(prometheus_tsdb_head_series_removed_total, 5m)) BY time_bucket = TBUCKET(5 minute)
| EVAL created = prometheus_tsdb_head_series_created_total_A
| EVAL removed = prometheus_tsdb_head_series_removed_total_B
| KEEP time_bucket, created, removed
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `prometheus_tsdb_head_series_created_total`
- Range func: `increase`
- Range window: `5m`
- Outer agg: `sum`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `created`
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

**Warnings:** Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Appended Samples per Second

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (graph):**

```
rate(prometheus_tsdb_head_samples_appended_total{job=~"$job",instance=~"$instance"}[1m])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE prometheus_tsdb_head_samples_appended_total IS NOT NULL
| STATS prometheus_tsdb_head_samples_appended_total = AVG(RATE(prometheus_tsdb_head_samples_appended_total, 1m)) BY time_bucket = TBUCKET(5 minute), instance
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `prometheus_tsdb_head_samples_appended_total`
- Range func: `rate`
- Range window: `1m`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `prometheus_tsdb_head_samples_appended_total`
- Output groups: `time_bucket, instance`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=0, w=48, h=12
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- has_description: True

**Warnings:** Grafana panel description is not carried into Kibana YAML automatically; Dropped variable-driven label filters during migration

**Semantic losses:** Dropped variable-driven label filters during migration

**Notes:** Grafana panel description is not carried into Kibana YAML automatically

**Verdict:** MINOR_ISSUE

</details>

---

### Grafana: Prometheus 2.0 (by FUSAKLA)

**File:** `prometheus-all.json` — **Panels:** 44

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
| Compaction duration | `graph` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | sum(increase(prometheus_tsdb_compaction_duration_sum{instance="$instance"}[30m])... | — |
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE prometheus_tsdb_head_series IS NOT NULL
| STATS prometheus_tsdb_head_series = MAX(LAST_OVER_TIME(prometheus_tsdb_head_series)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), prometheus_tsdb_head_series = MAX(prometheus_tsdb_head_series)
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE prometheus_build_info IS NOT NULL
| STATS prometheus_build_info = MAX(LAST_OVER_TIME(prometheus_build_info)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), prometheus_build_info = MAX(prometheus_build_info)
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE prometheus_tsdb_head_max_time IS NOT NULL OR prometheus_tsdb_head_min_time IS NOT NULL
| STATS prometheus_tsdb_head_max_time_instance = AVG(prometheus_tsdb_head_max_time), prometheus_tsdb_head_min_time_instance = AVG(prometheus_tsdb_head_min_time) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (prometheus_tsdb_head_max_time_instance - prometheus_tsdb_head_min_time_instance)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
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
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family scalar bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
- `query_translators` / `scalar_family` → translated scalar constant
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
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

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE prometheus_engine_query_duration_seconds IS NOT NULL
| STATS prometheus_engine_query_duration_seconds = MAX(prometheus_engine_query_duration_seconds) BY time_bucket = TBUCKET(5 minute), instance, slice
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

**Warnings:** XY chart shows a single breakdown; additional grouping dimension(s) ['slice'] are in the query but not on the chart, so series differing only by those are visually merged

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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE prometheus_tsdb_head_series_created_total IS NOT NULL OR prometheus_tsdb_head_series_removed_total IS NOT NULL
| STATS prometheus_tsdb_head_series_created_total_A = SUM(INCREASE(prometheus_tsdb_head_series_created_total, 5m)), prometheus_tsdb_head_series_removed_total_B = SUM(INCREASE(prometheus_tsdb_head_series_removed_total, 5m)) BY time_bucket = TBUCKET(5 minute), instance
| EVAL prometheus_tsdb_head_series_removed_total_B_calc = prometheus_tsdb_head_series_removed_total_B * -1
| EVAL created_on = prometheus_tsdb_head_series_created_total_A
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter` → applied post-aggregation filter > 0
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE prometheus_target_scrapes_exceeded_sample_limit_total IS NOT NULL OR prometheus_target_scrapes_sample_duplicate_timestamp_total IS NOT NULL OR prometheus_target_scrapes_sample_out_of_bounds_total IS NOT NULL OR prometheus_target_scrapes_sample_out_of_order_total IS NOT NULL OR prometheus_rule_evaluation_failures_total IS NOT NULL OR prometheus_tsdb_compactions_failed_total IS NOT NULL OR prometheus_tsdb_reloads_failures_total IS NOT NULL OR prometheus_tsdb_head_series_not_found IS NOT NULL OR prometheus_evaluator_iterations_missed_total IS NOT NULL OR prometheus_evaluator_iterations_skipped_total IS NOT NULL
| STATS prometheus_target_scrapes_exceeded_sample_limit_total_A = SUM(INCREASE(prometheus_target_scrapes_exceeded_sample_limit_total, 5m)), prometheus_target_scrapes_sample_duplicate_timestamp_total_B = SUM(INCREASE(prometheus_target_scrapes_sample_duplicate_timestamp_total, 5m)), prometheus_target_scrapes_sample_out_of_bounds_total_C = SUM(INCREASE(prometheus_target_scrapes_sample_out_of_bounds_total, 5m)), prometheus_target_scrapes_sample_out_of_order_total_D = SUM(INCREASE(prometheus_target_scrapes_sample_out_of_order_total, 5m)), prometheus_rule_evaluation_failures_total_G = SUM(INCREASE(prometheus_rule_evaluation_failures_total, 5m)), prometheus_tsdb_compactions_failed_total_K = SUM(INCREASE(prometheus_tsdb_compactions_failed_total, 5m)), prometheus_tsdb_reloads_failures_total_L = SUM(INCREASE(prometheus_tsdb_reloads_failures_total, 5m)), prometheus_tsdb_head_series_not_found_N = SUM(MAX_OVER_TIME(TO_DOUBLE(prometheus_tsdb_head_series_not_found), 5m)), prometheus_evaluator_iterations_missed_total_O = SUM(INCREASE(prometheus_evaluator_iterations_missed_total, 5m)), prometheus_evaluator_iterations_skipped_total_P = SUM(INCREASE(prometheus_evaluator_iterations_skipped_total, 5m)) BY time_bucket = TBUCKET(5 minute), instance
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE quantile == "0.99"
| WHERE prometheus_target_interval_length_seconds IS NOT NULL
| STATS prometheus_target_interval_length_seconds_quantile_0_99 = MAX(LAST_OVER_TIME(prometheus_target_interval_length_seconds)) BY time_bucket = TBUCKET(5 minute)
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE prometheus_evaluator_duration_seconds IS NOT NULL
| STATS prometheus_evaluator_duration_seconds = SUM(prometheus_evaluator_duration_seconds) BY time_bucket = TBUCKET(5 minute), instance, quantile
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

**Warnings:** XY chart shows a single breakdown; additional grouping dimension(s) ['quantile'] are in the query but not on the chart, so series differing only by those are visually merged

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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter` → applied post-aggregation filter > 0
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE http_requests_total IS NOT NULL
| STATS http_requests_total = SUM(INCREASE(http_requests_total, 5m)) BY time_bucket = TBUCKET(5 minute), instance, handler
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family` → translated nested max expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter` → applied post-aggregation filter > 0
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter` → applied post-aggregation filter > 0
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE quantile == "0.99"
| WHERE http_request_size_bytes IS NOT NULL
| STATS http_request_size_bytes = SUM(MAX_OVER_TIME(TO_DOUBLE(http_request_size_bytes), 5m)) BY time_bucket = TBUCKET(5 minute), instance, handler
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE prometheus_engine_queries IS NOT NULL OR prometheus_engine_queries_concurrent_max IS NOT NULL
| STATS prometheus_engine_queries_A = SUM(prometheus_engine_queries), prometheus_engine_queries_concurrent_max_B = SUM(prometheus_engine_queries_concurrent_max) BY time_bucket = TBUCKET(5 minute), instance, handler
| EVAL Current_count = prometheus_engine_queries_A
| EVAL Max_count = prometheus_engine_queries_concurrent_max_B
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

**Warnings:** XY chart shows a single breakdown; additional grouping dimension(s) ['handler'] are in the query but not on the chart, so series differing only by those are visually merged

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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE prometheus_notifications_queue_capacity IS NOT NULL OR prometheus_notifications_queue_length IS NOT NULL
| STATS prometheus_notifications_queue_capacity_A = SUM(prometheus_notifications_queue_capacity), prometheus_notifications_queue_length_B = SUM(prometheus_notifications_queue_length) BY time_bucket = TBUCKET(5 minute), instance
| EVAL Alert_queue_capacity = prometheus_notifications_queue_capacity_A
| EVAL Alert_queue_size_on = prometheus_notifications_queue_length_B
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

- `Instance` (type: `options`)

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
| Network I/O | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | rate(redis_net_input_bytes_total{instance=~"$instance"}[5m]) \|\|\| rate(redis_n... | TS metrics-prometheus-* \| WHERE redis_net_input_bytes_total IS NOT NULL \| STAT... |
| Total Items per DB | `graph` → `area` | migrated | **MINOR_ISSUE** | sum (redis_db_keys{instance=~"$instance"}) by (db) | TS metrics-prometheus-* \| WHERE redis_db_keys IS NOT NULL \| STATS redis_db_key... |
| Expiring vs Not-Expiring Keys | `graph` → `area` | migrated_with_warnings | **MINOR_ISSUE** | sum (redis_db_keys{instance=~"$instance"}) - sum (redis_db_keys_expiring{instanc... | TS metrics-prometheus-* \| WHERE redis_db_keys IS NOT NULL OR redis_db_keys_expi... |
| Expired / Evicted | `graph` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(redis_expired_keys_total{instance=~"$instance"}[5m])) by (instance) \|\... | TS metrics-prometheus-* \| WHERE redis_expired_keys_total IS NOT NULL OR redis_e... |
| Command Calls / sec | `graph` → `area` | migrated_with_warnings | **MINOR_ISSUE** | topk(5, irate(redis_commands_total{instance=~"$instance"} [1m])) | TS metrics-prometheus-* \| WHERE redis_commands_total IS NOT NULL \| STATS _buck... |
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE redis_uptime_in_seconds IS NOT NULL
| STATS redis_uptime_in_seconds = MAX(MAX_OVER_TIME(redis_uptime_in_seconds, 5m)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), redis_uptime_in_seconds = MAX(redis_uptime_in_seconds)
| KEEP time_bucket, redis_uptime_in_seconds
| SORT time_bucket ASC
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE redis_connected_clients IS NOT NULL
| STATS redis_connected_clients = MAX(LAST_OVER_TIME(redis_connected_clients)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), redis_connected_clients = MAX(redis_connected_clients)
| KEEP time_bucket, redis_connected_clients
| SORT time_bucket ASC
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE redis_memory_used_bytes IS NOT NULL OR redis_memory_max_bytes IS NOT NULL
| STATS redis_memory_used_bytes_instance = AVG(redis_memory_used_bytes), redis_memory_max_bytes_instance = AVG(redis_memory_max_bytes) BY time_bucket = TBUCKET(5 minute)
| EVAL computed_value = (100 * (redis_memory_used_bytes_instance / redis_memory_max_bytes_instance))
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), computed_value = MAX(computed_value)
| KEEP time_bucket, computed_value
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_commands_processed_total IS NOT NULL
| STATS redis_commands_processed_total = AVG(RATE(redis_commands_processed_total, 1m)) BY time_bucket = TBUCKET(5 minute), instance
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_keyspace_hits_total IS NOT NULL OR redis_keyspace_misses_total IS NOT NULL
| STATS redis_keyspace_hits_total_A = AVG(IRATE(redis_keyspace_hits_total, 5m)), redis_keyspace_misses_total_B = AVG(IRATE(redis_keyspace_misses_total, 5m)) BY time_bucket = TBUCKET(5 minute), instance
| EVAL hits = redis_keyspace_hits_total_A
| EVAL misses = redis_keyspace_misses_total_B
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_memory_used_bytes IS NOT NULL OR redis_memory_max_bytes IS NOT NULL
| STATS redis_memory_used_bytes_A = AVG(redis_memory_used_bytes), redis_memory_max_bytes_B = AVG(redis_memory_max_bytes) BY time_bucket = TBUCKET(5 minute), instance
| EVAL used = redis_memory_used_bytes_A
| EVAL max = redis_memory_max_bytes_B
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_net_input_bytes_total IS NOT NULL
| STATS redis_net_input_bytes_total = AVG(RATE(redis_net_input_bytes_total, 5m)) BY time_bucket = TBUCKET(5 minute), input
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
- Output metric: `redis_net_input_bytes_total`
- Output groups: `time_bucket, input`
- Semantic losses: Dropped variable-driven label filters during migration, Panel has 2 PromQL targets but only 1 could be migrated

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=11, w=24, h=10
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Panel has 2 PromQL targets but only 1 could be migrated

**Semantic losses:** Dropped variable-driven label filters during migration; Panel has 2 PromQL targets but only 1 could be migrated

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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE redis_db_keys IS NOT NULL
| STATS redis_db_keys = SUM(redis_db_keys) BY time_bucket = TBUCKET(5 minute), db
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE redis_db_keys IS NOT NULL OR redis_db_keys_expiring IS NOT NULL
| STATS redis_db_keys_A_lhs = SUM(redis_db_keys), redis_db_keys_expiring_A_rhs = SUM(redis_db_keys_expiring), redis_db_keys_expiring_B = SUM(redis_db_keys_expiring) BY time_bucket = TBUCKET(5 minute)
| EVAL not_expiring = (redis_db_keys_A_lhs - redis_db_keys_expiring_A_rhs)
| EVAL expiring = redis_db_keys_expiring_B
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
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `area`
- Layout: x=24, y=21, w=24, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math; PromQL series labels were not retained; output is bucket-level and may collapse multiple source series

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_expired_keys_total IS NOT NULL OR redis_evicted_keys_total IS NOT NULL
| STATS redis_expired_keys_total_A = SUM(RATE(redis_expired_keys_total, 5m)), redis_evicted_keys_total_B = SUM(RATE(redis_evicted_keys_total, 5m)) BY time_bucket = TBUCKET(5 minute), instance
| EVAL expired = redis_expired_keys_total_A
| EVAL evicted = redis_evicted_keys_total_B
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
- `query_classifiers` / `fragment_guardrails`
- `query_classifiers` / `family_classifier` → fragment family topk bypasses unsupported-pattern check
- `query_classifiers` / `unsupported_patterns`
- `query_classifiers` / `warning_patterns`
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
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE redis_commands_total IS NOT NULL
| STATS _bucket_value = AVG(IRATE(redis_commands_total, 1m)) BY time_bucket = TBUCKET(5 minute), cmd
| SORT time_bucket ASC
| STATS value = LAST(_bucket_value, time_bucket) BY cmd
| KEEP cmd, value
| SORT value DESC
| LIMIT 5
```

**Query IR:**

- Family: `topk`
- Metric: `redis_commands_total`
- Range func: `irate`
- Range window: `1m`
- Output shape: `table`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `value`
- Output groups: `cmd`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `area`
- Layout: x=24, y=32, w=24, h=10
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Translated grouped topk() as latest-bucket ES|QL top N

**Semantic losses:** Dropped variable-driven label filters during migration

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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_connected_clients IS NOT NULL
| STATS redis_connected_clients = AVG(redis_connected_clients) BY time_bucket = TBUCKET(5 minute), instance
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

- `Namespace` (type: `options`)
- `Pod Name` (type: `options`)
- `instance` (type: `options`)

</details>

---

### Grafana: Redis Dashboard for Prometheus Redis Exporter 1.x

**File:** `redis-exporter-763.json` — **Panels:** 13

| Panel | Source Type → Kibana | Status | Verdict | Source Query | Translated Query |
|-------|---------------------|--------|---------|-------------|-----------------|
| Max Uptime | `stat` → `metric` | migrated | **MINOR_ISSUE** | max(max_over_time(redis_uptime_in_seconds{instance=~"$instance"}[$__interval])) | TS metrics-prometheus-* \| WHERE redis_uptime_in_seconds IS NOT NULL \| STATS re... |
| Clients | `stat` → `metric` | migrated | **MINOR_ISSUE** | sum(redis_connected_clients{instance=~"$instance"}) | TS metrics-prometheus-* \| WHERE redis_connected_clients IS NOT NULL \| STATS re... |
| Memory Usage | `gauge` → `markdown` | not_feasible | **EXPECTED_LIMITATION** | sum(100 * (redis_memory_used_bytes{instance=~"$instance"}  / redis_memory_max_by... | — |
| Total Commands / sec | `timeseries` → `area` | migrated | **MINOR_ISSUE** | sum(rate(redis_commands_total{instance=~"$instance"} [1m])) by (cmd) | TS metrics-prometheus-* \| WHERE redis_commands_total IS NOT NULL \| STATS redis... |
| Hits / Misses per Sec | `timeseries` → `line` | migrated | **MINOR_ISSUE** | irate(redis_keyspace_hits_total{instance=~"$instance"}[5m]) \|\|\| irate(redis_k... | TS metrics-prometheus-* \| WHERE redis_keyspace_hits_total IS NOT NULL OR redis_... |
| Total Memory Usage | `timeseries` → `line` | migrated | **MINOR_ISSUE** | redis_memory_used_bytes{instance=~"$instance"} \|\|\| redis_memory_max_bytes{ins... | TS metrics-prometheus-* \| WHERE redis_memory_used_bytes IS NOT NULL OR redis_me... |
| Network I/O | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(rate(redis_net_input_bytes_total{instance=~"$instance"}[5m])) \|\|\| sum(rat... | TS metrics-prometheus-* \| WHERE redis_net_input_bytes_total IS NOT NULL \| STAT... |
| Total Items per DB | `timeseries` → `area` | migrated | **MINOR_ISSUE** | sum (redis_db_keys{instance=~"$instance"}) by (db, instance) | TS metrics-prometheus-* \| WHERE redis_db_keys IS NOT NULL \| STATS redis_db_key... |
| Expiring vs Not-Expiring Keys | `timeseries` → `area` | migrated_with_warnings | **MINOR_ISSUE** | sum (redis_db_keys{instance=~"$instance"}) by (instance) - sum (redis_db_keys_ex... | TS metrics-prometheus-* \| WHERE redis_db_keys IS NOT NULL OR redis_db_keys_expi... |
| Expired/Evicted Keys | `timeseries` → `line` | migrated | **MINOR_ISSUE** | sum(rate(redis_expired_keys_total{instance=~"$instance"}[5m])) by (instance) \|\... | TS metrics-prometheus-* \| WHERE redis_expired_keys_total IS NOT NULL OR redis_e... |
| Connected/Blocked Clients | `timeseries` → `line` | migrated | **MINOR_ISSUE** | sum(redis_connected_clients{instance=~"$instance"}) \|\|\| sum(redis_blocked_cli... | TS metrics-prometheus-* \| WHERE redis_connected_clients IS NOT NULL OR redis_bl... |
| Average Time Spent by Command / sec | `timeseries` → `line` | migrated_with_warnings | **MINOR_ISSUE** | sum(irate(redis_commands_duration_seconds_total{instance =~ "$instance"}[1m])) b... | TS metrics-prometheus-* \| WHERE redis_commands_duration_seconds_total IS NOT NU... |
| Total Time Spent by Command / sec | `timeseries` → `area` | migrated | **MINOR_ISSUE** | sum(irate(redis_commands_duration_seconds_total{instance=~"$instance"}[1m])) by ... | TS metrics-prometheus-* \| WHERE redis_commands_duration_seconds_total IS NOT NU... |

<details>
<summary>Detailed traces (13 panels)</summary>

#### Max Uptime

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (stat):**

```
max(max_over_time(redis_uptime_in_seconds{instance=~"$instance"}[$__interval]))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE redis_uptime_in_seconds IS NOT NULL
| STATS redis_uptime_in_seconds = MAX(MAX_OVER_TIME(redis_uptime_in_seconds, 5m)) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), redis_uptime_in_seconds = MAX(redis_uptime_in_seconds)
| KEEP time_bucket, redis_uptime_in_seconds
| SORT time_bucket ASC
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
- Layout: x=0, y=0, w=6, h=11
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

**Source (stat):**

```
sum(redis_connected_clients{instance=~"$instance"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel` → mapped to metric panel

**Translated (metric):**

```
TS metrics-prometheus-*
| WHERE redis_connected_clients IS NOT NULL
| STATS redis_connected_clients = SUM(redis_connected_clients) BY time_bucket = TBUCKET(5 minute)
| SORT time_bucket ASC
| STATS time_bucket = MAX(time_bucket), redis_connected_clients = MAX(redis_connected_clients)
| KEEP time_bucket, redis_connected_clients
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `redis_connected_clients`
- Outer agg: `sum`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_connected_clients`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `metric`
- Layout: x=6, y=0, w=4, h=11
- Presentation kind: `esql`
- Config keys: type, query, primary

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Memory Usage

**Translation path:** `not_feasible` · **Query language:** `promql` · **Readiness:** `manual_only`

**Source (gauge):**

```
sum(100 * (redis_memory_used_bytes{instance=~"$instance"}  / redis_memory_max_bytes{instance=~"$instance"}))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
- `query_preprocessors` / `grafana_macros` → expanded Grafana macros
- `query_preprocessors` / `parse_fragment` → parsed fragment family=unknown backend=ast
- `query_classifiers` / `fragment_guardrails` → Aggregating over a per-element * between two time-series (sum(A * B)) cannot be expressed accurately in ES|QL; rewrite as a ratio of aggregates if the series are label-aligned

**Query IR:**

- Family: `unknown`
- Outer agg: `sum`
- Binary op: `*`
- Output shape: `single_value`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`

**Visual IR:**

- Kibana type: `markdown`
- Layout: x=10, y=0, w=6, h=11
- Presentation kind: `markdown`
- Config keys: content

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Warnings:** Aggregating over a per-element * between two time-series (sum(A * B)) cannot be expressed accurately in ES|QL; rewrite as a ratio of aggregates if the series are label-aligned

**Verdict:** EXPECTED_LIMITATION

#### Total Commands / sec

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(rate(redis_commands_total{instance=~"$instance"} [1m])) by (cmd)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE redis_commands_total IS NOT NULL
| STATS redis_commands_total = SUM(RATE(redis_commands_total, 1m)) BY time_bucket = TBUCKET(5 minute), cmd
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `redis_commands_total`
- Range func: `rate`
- Range window: `1m`
- Outer agg: `sum`
- Group labels: `cmd`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_commands_total`
- Output groups: `time_bucket, cmd`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `area`
- Layout: x=16, y=0, w=16, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Hits / Misses per Sec

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
irate(redis_keyspace_hits_total{instance=~"$instance"}[5m]) ||| irate(redis_keyspace_misses_total{instance=~"$instance"}[5m])
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_keyspace_hits_total IS NOT NULL OR redis_keyspace_misses_total IS NOT NULL
| STATS redis_keyspace_hits_total_A = AVG(IRATE(redis_keyspace_hits_total, 5m)), redis_keyspace_misses_total_B = AVG(IRATE(redis_keyspace_misses_total, 5m)) BY time_bucket = TBUCKET(5 minute), instance
| EVAL hits = redis_keyspace_hits_total_A
| EVAL misses = redis_keyspace_misses_total_B
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

**Source (timeseries):**

```
redis_memory_used_bytes{instance=~"$instance"} ||| redis_memory_max_bytes{instance=~"$instance"}
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family`
- `query_translators` / `simple_metric_family` → translated simple metric expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_memory_used_bytes IS NOT NULL OR redis_memory_max_bytes IS NOT NULL
| STATS redis_memory_used_bytes_A = AVG(redis_memory_used_bytes), redis_memory_max_bytes_B = AVG(redis_memory_max_bytes) BY time_bucket = TBUCKET(5 minute), instance
| EVAL used = redis_memory_used_bytes_A
| EVAL max = redis_memory_max_bytes_B
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
- field_overrides: 1

**Warnings:** Grafana panel has 1 field override(s); verify visual mappings manually

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Network I/O

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(rate(redis_net_input_bytes_total{instance=~"$instance"}[5m])) ||| sum(rate(redis_net_output_bytes_total{instance=~"$instance"}[5m]))
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_net_input_bytes_total IS NOT NULL
| STATS redis_net_input_bytes_total = SUM(RATE(redis_net_input_bytes_total, 5m)) BY time_bucket = TBUCKET(5 minute), input
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `redis_net_input_bytes_total`
- Range func: `rate`
- Range window: `5m`
- Outer agg: `sum`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_net_input_bytes_total`
- Output groups: `time_bucket, input`
- Semantic losses: Dropped variable-driven label filters during migration, Panel has 2 PromQL targets but only 1 could be migrated

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=11, w=24, h=10
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Panel has 2 PromQL targets but only 1 could be migrated

**Semantic losses:** Dropped variable-driven label filters during migration; Panel has 2 PromQL targets but only 1 could be migrated

**Verdict:** MINOR_ISSUE

#### Total Items per DB

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum (redis_db_keys{instance=~"$instance"}) by (db, instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE redis_db_keys IS NOT NULL
| STATS redis_db_keys = SUM(redis_db_keys) BY time_bucket = TBUCKET(5 minute), db, instance
| EVAL legend = CONCAT(COALESCE(TO_STRING(db), ""), ", ", COALESCE(TO_STRING(instance), ""))
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `redis_db_keys`
- Outer agg: `sum`
- Group labels: `db, instance`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_db_keys`
- Output groups: `time_bucket, db, instance`
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
- field_overrides: 1

**Warnings:** Grafana panel has 1 field override(s); verify visual mappings manually

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Expiring vs Not-Expiring Keys

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum (redis_db_keys{instance=~"$instance"}) by (instance) - sum (redis_db_keys_expiring{instance=~"$instance"}) by (instance) ||| sum (redis_db_keys_expiring{instance=~"$instance"}) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE redis_db_keys IS NOT NULL OR redis_db_keys_expiring IS NOT NULL
| STATS redis_db_keys_A_lhs = SUM(redis_db_keys), redis_db_keys_expiring_A_rhs = SUM(redis_db_keys_expiring), redis_db_keys_expiring_B = SUM(redis_db_keys_expiring) BY time_bucket = TBUCKET(5 minute), instance
| EVAL not_expiring = (redis_db_keys_A_lhs - redis_db_keys_expiring_A_rhs)
| EVAL expiring = redis_db_keys_expiring_B
| KEEP time_bucket, instance, not_expiring, expiring
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
- Output groups: `time_bucket, instance`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `area`
- Layout: x=24, y=21, w=24, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Warnings:** Approximated PromQL arithmetic using same-bucket ES|QL math

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Expired/Evicted Keys

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(rate(redis_expired_keys_total{instance=~"$instance"}[5m])) by (instance) ||| sum(rate(redis_evicted_keys_total{instance=~"$instance"}[5m])) by (instance)
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_expired_keys_total IS NOT NULL OR redis_evicted_keys_total IS NOT NULL
| STATS redis_expired_keys_total_A = SUM(RATE(redis_expired_keys_total, 5m)), redis_evicted_keys_total_B = SUM(RATE(redis_evicted_keys_total, 5m)) BY time_bucket = TBUCKET(5 minute), instance
| EVAL expired = redis_expired_keys_total_A
| EVAL evicted = redis_evicted_keys_total_B
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
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=32, w=24, h=10
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2
- field_overrides: 3

**Warnings:** Grafana panel has 3 field override(s); verify visual mappings manually

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Connected/Blocked Clients

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(redis_connected_clients{instance=~"$instance"}) ||| sum(redis_blocked_clients{instance=~"$instance"})
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family`
- `query_translators` / `simple_agg_family` → translated simple aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_connected_clients IS NOT NULL OR redis_blocked_clients IS NOT NULL
| STATS redis_connected_clients_A = SUM(redis_connected_clients), redis_blocked_clients_B = SUM(redis_blocked_clients) BY time_bucket = TBUCKET(5 minute)
| EVAL connected = redis_connected_clients_A
| EVAL blocked = redis_blocked_clients_B
| KEEP time_bucket, connected, blocked
| SORT time_bucket ASC
```

**Query IR:**

- Family: `simple_agg`
- Metric: `redis_connected_clients`
- Outer agg: `sum`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `connected`
- Output groups: `time_bucket`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=24, y=32, w=24, h=10
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, legend

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 2

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

#### Average Time Spent by Command / sec

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(irate(redis_commands_duration_seconds_total{instance =~ "$instance"}[1m])) by (cmd)
  /
sum(irate(redis_commands_total{instance =~ "$instance"}[1m])) by (cmd)

```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter`
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to line panel

**Translated (line):**

```
TS metrics-prometheus-*
| WHERE redis_commands_duration_seconds_total IS NOT NULL OR redis_commands_total IS NOT NULL
| STATS redis_commands_duration_seconds_total_instance_irate_sum = SUM(IRATE(redis_commands_duration_seconds_total, 1m)), redis_commands_total_instance_irate_sum = SUM(IRATE(redis_commands_total, 1m)) BY time_bucket = TBUCKET(5 minute), cmd
| EVAL computed_value = (redis_commands_duration_seconds_total_instance_irate_sum / redis_commands_total_instance_irate_sum)
| KEEP time_bucket, cmd, computed_value
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
- Output groups: `time_bucket, cmd`
- Semantic losses: Approximated PromQL arithmetic using same-bucket ES|QL math, Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `line`
- Layout: x=0, y=42, w=24, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, breakdown

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1
- field_overrides: 1

**Warnings:** Grafana panel has 1 field override(s); verify visual mappings manually; Approximated PromQL arithmetic using same-bucket ES|QL math

**Semantic losses:** Approximated PromQL arithmetic using same-bucket ES|QL math; Dropped variable-driven label filters during migration

**Notes:** Grafana panel has 1 field override(s); verify visual mappings manually

**Verdict:** MINOR_ISSUE

#### Total Time Spent by Command / sec

**Translation path:** `rule_engine` · **Query language:** `promql` · **Readiness:** `metrics_mapping_needed`

**Source (timeseries):**

```
sum(irate(redis_commands_duration_seconds_total{instance=~"$instance"}[1m])) by (cmd) != 0
```

**Pipeline trace:**

- `query_preprocessors` / `template_variable_guardrails`
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
- `query_translators` / `topk_family`
- `query_translators` / `label_replace_family`
- `query_translators` / `scaled_agg_family`
- `query_translators` / `histogram_quantile_family`
- `query_translators` / `nested_agg_family`
- `query_translators` / `range_agg_family` → translated range aggregation expression
- `query_postprocessors` / `index_rewrite`
- `query_postprocessors` / `render_esql`
- `query_postprocessors` / `value_wrapper_transforms`
- `query_postprocessors` / `or_vector_fallback_note`
- `query_postprocessors` / `post_filter` → applied post-aggregation filter != 0
- `query_validators` / `metric_name_required`
- `query_validators` / `dynamic_metric_name`
- `query_validators` / `time_filter_source_alignment`
- `query_validators` / `live_metric_fields_exist`
- `query_validators` / `rendered_query_required`
- `panel_translators` / `metric_panel`
- `panel_translators` / `bargauge_panel`
- `panel_translators` / `xy_panel` → mapped to area panel

**Translated (area):**

```
TS metrics-prometheus-*
| WHERE redis_commands_duration_seconds_total IS NOT NULL
| STATS redis_commands_duration_seconds_total = SUM(IRATE(redis_commands_duration_seconds_total, 1m)) BY time_bucket = TBUCKET(5 minute), cmd
| WHERE redis_commands_duration_seconds_total != 0
| SORT time_bucket ASC
```

**Query IR:**

- Family: `range_agg`
- Metric: `redis_commands_duration_seconds_total`
- Range func: `irate`
- Range window: `1m`
- Outer agg: `sum`
- Group labels: `cmd`
- Output shape: `time_series`
- Source lang: `promql`
- Target index: `metrics-prometheus-*`
- Output metric: `redis_commands_duration_seconds_total`
- Output groups: `time_bucket, cmd`
- Semantic losses: Dropped variable-driven label filters during migration

**Visual IR:**

- Kibana type: `area`
- Layout: x=24, y=42, w=24, h=11
- Presentation kind: `esql`
- Config keys: type, query, dimension, metrics, mode

**Operational IR:**

- Query language: `promql`

**Inventory:**

- targets: 1

**Semantic losses:** Dropped variable-driven label filters during migration

**Verdict:** MINOR_ISSUE

</details>

<details>
<summary>Controls / Variables (2)</summary>

- `namespace` (type: `options`)
- `instance` (type: `options`)

</details>

---

<!-- /GENERATED:PER_DASHBOARD_TRACES -->

---

## Appendix: Panel Status Summary

<!-- GENERATED:APPENDIX_STATS -->
From the latest trace run:

```
Elements:            548 total (496 panels + 52 rows)
Renderable panels:   496
  Migrated:             183 (36.9%)
  With warnings:        275 (55.4%)
  Requires manual:        5 (1.0%)
  Not feasible:          32 (6.5%)
  Skipped:                1 (0.2%)
```

Verdict breakdown:

```
  CORRECT:                   24
  MINOR_ISSUE:              427
  EXPECTED_LIMITATION:       97
```
<!-- /GENERATED:APPENDIX_STATS -->

---

## Appendix: Not-Feasible Panel Breakdown

<!-- GENERATED:NOT_FEASIBLE_BREAKDOWN -->
Every panel marked `not_feasible` in the trace run (32 total):

| Panel Title | Dashboard | Source | Reason |
|-------------|-----------|--------|--------|
| Disk space | Docker and system monitoring | grafana | Aggregating over a per-element / between two time-series (min(A / B)) cannot be expressed accurately... |
| Disk space | Docker and system monitoring | grafana | Aggregating over a per-element / between two time-series (min(A / B)) cannot be expressed accurately... |
| Load | Docker and system monitoring | grafana | PromQL arithmetic with divergent filters/groupings cannot be translated safely yet |
| Load | Docker and system monitoring | grafana | PromQL arithmetic with divergent filters/groupings cannot be translated safely yet |
| Request duration 95th percentile | Express Prometheus Middleware | grafana | histogram_quantile target field type could not be determined; cannot safely translate to ES\|QL PERCE... |
| Request duration 99th percentile | Express Prometheus Middleware | grafana | histogram_quantile target field type could not be determined; cannot safely translate to ES\|QL PERCE... |
| Primary: Request Duration | Flagger Canary Status | grafana | PromQL '/' arithmetic where an operand contains a nested set operator or vector-matching join that c... |
| Canary: Request Duration | Flagger Canary Status | grafana | PromQL '/' arithmetic where an operand contains a nested set operator or vector-matching join that c... |
| Top Metrics by Series Count | Home - Migration Test Lab | grafana | PromQL metric-name introspection via __name__ requires manual redesign |
| CPU Utilization by namespace | Kubernetes / Views / Global | grafana | PromQL '+' arithmetic where an operand contains a nested set operator or vector-matching join that c... |
| Memory Utilization by namespace | Kubernetes / Views / Global | grafana | PromQL '+' arithmetic where an operand contains a nested set operator or vector-matching join that c... |
| Network Received by namespace | Kubernetes / Views / Global | grafana | PromQL '+' arithmetic where an operand contains a nested set operator or vector-matching join that c... |
| Current QPS | MySQL Overview | grafana | Grafana panel has 1 link(s); verify drilldowns manually; Grafana panel description is not carried in... |
| MySQL Client Thread Activity | MySQL Overview | grafana | Grafana panel description is not carried into Kibana YAML automatically; PromQL 'or' between metrics... |
| MySQL Questions | MySQL Overview | grafana | Grafana panel has 1 link(s); verify drilldowns manually; Grafana panel description is not carried in... |
| MySQL Temporary Objects | MySQL Overview | grafana | PromQL 'or' between metrics that cannot be aligned in ES\|QL (differing grouping dimensions or source... |
| MySQL Select Types | MySQL Overview | grafana | Grafana panel description is not carried into Kibana YAML automatically; PromQL 'or' between metrics... |
| MySQL Sorts | MySQL Overview | grafana | Grafana panel description is not carried into Kibana YAML automatically; PromQL 'or' between metrics... |
| MySQL Slow Queries | MySQL Overview | grafana | Grafana panel description is not carried into Kibana YAML automatically; PromQL 'or' between metrics... |
| MySQL Aborted Connections | MySQL Overview | grafana | Grafana panel description is not carried into Kibana YAML automatically; PromQL 'or' between metrics... |
| MySQL Table Locks | MySQL Overview | grafana | Grafana panel description is not carried into Kibana YAML automatically; PromQL 'or' between metrics... |
| MySQL Network Traffic | MySQL Overview | grafana | Grafana panel description is not carried into Kibana YAML automatically; PromQL 'or' between metrics... |
| Top Command Counters | MySQL Overview | grafana | Grafana panel has 1 link(s); verify drilldowns manually; Grafana panel description is not carried in... |
| MySQL Handlers | MySQL Overview | grafana | Grafana panel description is not carried into Kibana YAML automatically; PromQL 'or' between metrics... |
| MySQL Transaction Handlers | MySQL Overview | grafana | PromQL 'or' between metrics that cannot be aligned in ES\|QL (differing grouping dimensions or source... |
| MySQL File Openings | MySQL Overview | grafana | PromQL 'or' between metrics that cannot be aligned in ES\|QL (differing grouping dimensions or source... |
| MySQL Table Open Cache Status | MySQL Overview | grafana | Grafana panel has 1 link(s); verify drilldowns manually; Grafana panel description is not carried in... |
| I/O Activity | MySQL Overview | grafana | PromQL 'or' between metrics that cannot be aligned in ES\|QL (differing grouping dimensions or source... |
| Swap Activity | MySQL Overview | grafana | PromQL 'or' between metrics that cannot be aligned in ES\|QL (differing grouping dimensions or source... |
| Process Restart Times | NodeJS Application Dashboard | grafana | changes() counts value transitions and has no ES\|QL equivalent |
| Compaction duration | Prometheus 2.0 (by FUSAKLA) | grafana | Aggregating over a per-element / between two time-series (sum(A / B)) cannot be expressed accurately... |
| Memory Usage | Redis Dashboard for Prometheus Redis Exporter 1.x | grafana | Aggregating over a per-element * between two time-series (sum(A * B)) cannot be expressed accurately... |

**Pattern analysis:**

- **17×** PromQL 'or' between metrics that cannot be aligned in ES|QL
- **12×** Grafana panel description is not carried into Kibana YAML au
- **4×** Grafana panel has 1 link(s); verify drilldowns manually
- **3×** PromQL '+' arithmetic where an operand contains a nested set
- **2×** Aggregating over a per-element / between two time-series (mi
- **2×** PromQL arithmetic with divergent filters/groupings cannot be
- **2×** histogram_quantile target field type could not be determined
- **2×** PromQL '/' arithmetic where an operand contains a nested set
- **1×** PromQL metric-name introspection via __name__ requires manua
- **1×** changes() counts value transitions and has no ES|QL equivale
- **1×** Aggregating over a per-element / between two time-series (su
- **1×** Aggregating over a per-element * between two time-series (su
<!-- /GENERATED:NOT_FEASIBLE_BREAKDOWN -->

---

*Last generated: 2026-07-09 10:11 UTC*
