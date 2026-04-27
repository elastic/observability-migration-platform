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
*Run `python scripts/audit_pipeline.py --update-docs` to populate.*
<!-- /GENERATED:DASHBOARD_SUMMARY -->

<!-- GENERATED:VERDICT_SUMMARY -->
<!-- /GENERATED:VERDICT_SUMMARY -->

<!-- GENERATED:WARNING_PATTERNS -->
<!-- /GENERATED:WARNING_PATTERNS -->

---

## Per-Dashboard Traces

<!-- GENERATED:PER_DASHBOARD_TRACES -->
*Run `python scripts/audit_pipeline.py --update-docs` to populate.*
<!-- /GENERATED:PER_DASHBOARD_TRACES -->

---

## Appendix: Panel Status Summary

<!-- GENERATED:APPENDIX_STATS -->
*Run `python scripts/audit_pipeline.py --update-docs` to populate.*
<!-- /GENERATED:APPENDIX_STATS -->

---

## Appendix: Not-Feasible Panel Breakdown

<!-- GENERATED:NOT_FEASIBLE_BREAKDOWN -->
*Run `python scripts/audit_pipeline.py --update-docs` to populate.*
<!-- /GENERATED:NOT_FEASIBLE_BREAKDOWN -->
