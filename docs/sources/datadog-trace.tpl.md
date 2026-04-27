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
