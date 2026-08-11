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
| `detect_space_id_from_kibana_url`, `kibana_url_for_space`, `sync_result_queries_to_ir`, `carry_over_non_yaml_ir_fields`, `YAML_ROUND_TRIPPED_IR_FIELDS`, `IR_FIELDS_CARRIED_ACROSS_YAML_REBUILD` | `observability_migration.targets.kibana.compile` |
| `native_dashboard_from_ir`, `native_dashboard_from_yaml`, `upload_native_dashboard`, `upload_native_artifact`, `iter_payload_leaf_panels`, `payload_panel_queries` | `observability_migration.targets.kibana.dashboards_api` |
| `build_native_artifact`, `build_ir_artifact`, `write_native_artifact`, `write_ir_artifact`, `write_native_artifact_index` | `observability_migration.targets.kibana.native_artifacts` |
| `DashboardIR` | `observability_migration.core.assets.dashboard` |
| `enrich_yaml_panel_display` | `observability_migration.targets.kibana.emit.display` |
| `ESQLShape`, `extract_esql_columns` | `observability_migration.targets.kibana.emit.esql_utils` |

**Removed with the dashboard-YAML path.** These helpers no longer exist at the
import path shown, so importing them from there raises `ImportError`.

| Removed symbol | Was in | Replacement |
|---|---|---|
| `compile_yaml`, `compile_all` | `targets.kibana.compile` | None. Nothing consumes NDJSON. |
| `upload_yaml` | `targets.kibana.compile` | `dashboards_api.upload_native_dashboard` / `upload_native_artifact` |
| `dashboard_yaml_text`, `write_dashboard_yaml` | `targets.kibana.compile` | `DashboardIR.to_yaml_dict()` for the in-memory dict; nothing renders a file |
| `lint_dashboard_yaml`, `validate_compiled_layout` wrappers | `targets.kibana.compile` | Still importable from `targets.kibana.lint` / `targets.kibana.layout` as library code, but no command calls them |
| `upload_yaml_files` | `targets.kibana.dashboards_api` | `upload_native_artifact` |
| `emit_dashboard`, `compile`, `compile_dashboard`, `validate_queries` | `targets.kibana.adapter.KibanaTargetAdapter` | `upload(artifact_dir, **kwargs)` / `upload_dashboard(*, native_dashboard=…)` |
| `emit_dashboard`, `compile`, `validate_queries` | `core.interfaces.TargetAdapter` (ABC) | The contract is now `upload(artifact_dir, **kwargs)` + `smoke(**kwargs)` |

The `*_yaml_*` names that remain (`native_dashboard_from_yaml`,
`build_payload_from_yaml`, `map_yaml_panel`, `map_yaml_control`,
`map_yaml_filters`, `carry_over_non_yaml_ir_fields`,
`YAML_ROUND_TRIPPED_IR_FIELDS`) operate on the internal in-memory dict shape
from `DashboardIR.to_yaml_dict()`. None of them reads a file.

`translate_dashboard` (Grafana) also changed shape: it lost its `output_dir`
parameter and now returns just the `MigrationResult` instead of
`(result, yaml_path)`.

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
