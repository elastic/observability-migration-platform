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
| `check_esql_structure`, `StructuralFinding` | `observability_migration.core.verification.translation_oracle` |
| `MigrationResult`, `PanelResult` | `observability_migration.core.reporting.report` |
| `SourceAdapter`, `TargetAdapter` | `observability_migration.core.interfaces` |

## Kibana target

| Symbol | Import path |
|--------|------------|
| `compile_yaml`, `upload_yaml`, `compile_all`, `dashboard_yaml_text`, `write_dashboard_yaml`, `sync_result_queries_to_ir`, `YAML_ROUND_TRIPPED_IR_FIELDS`, `IR_FIELDS_CARRIED_ACROSS_YAML_REBUILD` | `observability_migration.targets.kibana.compile` |
| `native_dashboard_from_ir`, `native_dashboard_from_yaml`, `upload_native_dashboard`, `upload_native_artifact`, `upload_yaml_files` | `observability_migration.targets.kibana.dashboards_api` |
| `build_native_artifact`, `build_ir_artifact`, `write_native_artifact`, `write_ir_artifact`, `write_native_artifact_index` | `observability_migration.targets.kibana.native_artifacts` |
| `DashboardIR` | `observability_migration.core.assets.dashboard` |
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
