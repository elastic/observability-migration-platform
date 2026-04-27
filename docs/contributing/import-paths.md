# Import Path Guide

All code lives in the `observability_migration/` package.

## Shared core

| Symbol | Import path |
|--------|------------|
| `VisualIR`, `refresh_visual_ir` | `observability_migration.core.assets.visual` |
| `OperationalIR`, `build_operational_ir` | `observability_migration.core.assets.operational` |
| `QueryIR`, `build_query_ir` | `observability_migration.core.assets.query` |
| `AssetStatus` | `observability_migration.core.assets.status` |
| `ComparisonResult`, `ComparisonWindow` | `observability_migration.core.verification.comparators` |
| `MigrationResult`, `PanelResult` | `observability_migration.core.reporting.report` |
| `SourceAdapter`, `TargetAdapter` | `observability_migration.core.interfaces` |

## Kibana target

| Symbol | Import path |
|--------|------------|
| `compile_yaml`, `upload_yaml`, `compile_all` | `observability_migration.targets.kibana.compile` |
| `enrich_yaml_panel_display` | `observability_migration.targets.kibana.emit.display` |
| `ESQLShape`, `extract_esql_columns` | `observability_migration.targets.kibana.emit.esql_utils` |

## Grafana adapter

| Symbol | Import path |
|--------|------------|
| `translate_panel`, `translate_dashboard` | `observability_migration.adapters.source.grafana.panels` |
| `translate_promql_to_esql` | `observability_migration.adapters.source.grafana.translate` |
| `RulePackConfig`, `RuleRegistry` | `observability_migration.adapters.source.grafana.rules` |
| `SchemaResolver` | `observability_migration.adapters.source.grafana.schema` |
| `extract_dashboards_from_files` | `observability_migration.adapters.source.grafana.extract` |

## Datadog adapter

| Symbol | Import path |
|--------|------------|
| `translate_widget` | `observability_migration.adapters.source.datadog.translate` |
| `normalize_dashboard` | `observability_migration.adapters.source.datadog.normalize` |
| `parse_metric_query` | `observability_migration.adapters.source.datadog.query_parser` |
| `OTEL_PROFILE`, `FieldMapProfile` | `observability_migration.adapters.source.datadog.field_map` |
