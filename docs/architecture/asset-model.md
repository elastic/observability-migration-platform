# Asset Model

## Shared Status Vocabulary

All migrated assets use the unified `AssetStatus` enum:

| Status | Meaning |
|---|---|
| `translated` | Successfully migrated |
| `translated_with_warnings` | Migrated with semantic approximations |
| `manual_required` | Needs human review/action |
| `not_feasible` | Cannot be automatically migrated |
| `blocked` | Blocked by dependencies |
| `skipped` | Intentionally skipped (e.g. row panels) |

### Source Mapping

| Grafana Status | Datadog Status | Shared Status |
|---|---|---|
| `migrated` | `ok` | `translated` |
| `migrated_with_warnings` | `warning` | `translated_with_warnings` |
| `requires_manual` | — | `manual_required` |
| `not_feasible` | `blocked` | `not_feasible` |

## DashboardIR

The top-level container: `title`, `description`, `filters`, `settings`,
`minimum_kibana_version`, plus `panels` (`PanelIR`), `controls` (`ControlIR`),
`alerts`, `annotations`, `links`, and `transforms`.

**Adapter status (IR-first for both sources):**

| Source | Primary artifact | Native / YAML derivation |
|---|---|---|
| Grafana | `DashboardIR` | `native_dashboard_from_ir(dashboard_ir)` and `dashboard_ir.to_yaml_dict()` are both *derived* from the same IR (`adapters/source/grafana/panels.py::translate_dashboard`) |
| Datadog | `DashboardIR` | same invert via `adapters/source/datadog/generate.py::generate_dashboard_artifacts` |

Both adapters still assemble a kb-dashboard-core dict first (the per-panel /
per-widget translators are the expensive, well-tested part of the pipeline
and stay dict-shaped), then convert that dict to a `DashboardIR` via
`DashboardIR.from_yaml_dict()` *before* the native mapping and the on-disk
YAML write. From that point on, the dict is no longer the source of truth:
the native Dashboards API payload and the exported YAML file are both
produced from the `DashboardIR`, so they cannot drift from each other (see
`tests/test_grafana_native_dashboard_emission.py` and
`tests/test_datadog_native_dashboard_emission.py`).

Grafana mutators that run after emission --
`targets/kibana/compile.py::sync_result_queries_to_yaml` (post-validation
ES|QL fixes) and `adapters/source/grafana/polish.py::apply_metadata_polish`
(title/label polish) -- follow the same pattern: mutate the dict, rebuild
`DashboardIR.from_yaml_dict()`, then re-derive both the native IR and the
on-disk YAML from that rebuilt IR. Datadog's post-validation rewrite
(`adapters/source/datadog/cli.py::_rewrite_dashboard_yaml`) regenerates
artifacts through `generate_dashboard_artifacts`, which rebuilds the IR the
same way.

### PanelIR

A leaf panel (`kind="panel"`) embeds a `VisualIR` (presentation: chart type +
config, including the ES|QL query string) and carries layout/title through
`VisualIR.layout`/`VisualIR.title`. A section/row (`kind="section"`) carries
`children: list[PanelIR]` instead. `PanelIR.to_yaml_panel_entry()` /
`PanelIR.from_yaml_panel_entry()` round-trip one kb-dashboard-core
`panels[]` entry (leaf or nested `section`).

### ControlIR

Maps onto the Dashboards API's `pinned_panels` controls (options-list,
range-slider, ES|QL `esql_control`). Because dashboard-level controls carry
many translator-specific keys, `ControlIR.from_yaml_control()` preserves the
raw source dict verbatim in `source_extension`, overlaying only fields a
mutator changed (e.g. a polished `label`); a control built purely from typed
fields (no source dict, e.g. a synthesized ES|QL binding control) is
assembled from those fields directly. `ControlIR.to_yaml_control()` is the
inverse.

### YAML as a derived export, not a source

`DashboardIR.to_yaml_dict()` / `DashboardIR.from_yaml_dict()` are the
bridge to the kb-dashboard-core YAML dict shape that
`targets/kibana/dashboards_api.py` and `kb-dashboard-cli` speak. For both
Grafana and Datadog, YAML is written *from* the IR (for `kb-dashboard-lint`
and the `--compile`/`--legacy-import` paths) rather than the IR being
derived from YAML as the long-term source of truth. Standalone
`obs-migrate upload --artifact-dir … --artifact-format yaml` (or the
`--yaml-dir` compatibility alias) still maps on-disk YAML through
`native_dashboard_from_yaml` because that path only has files; migrate
`--upload` prefers the in-memory `native_dashboard` already derived from
`DashboardIR`. See `docs/command-contract.md` for the operator-facing
contract (full YAML retirement remains out of scope).

### Native Dashboard-as-Code review artifacts, not a compile step

The pre-typed-API pipeline had a `--compile` step that turned YAML into
NDJSON so operators could inspect what would be imported before uploading it.
Native Dashboard-as-Code does not need compilation -- `NativeDashboard` is
already the typed API's own shape -- but it still needs a stable, inspectable,
uploadable artifact so that review-before-upload workflow is not lost.
`targets/kibana/native_artifacts.py` fills that gap: every dashboard
migration run persists `dashboards/native/<stem>.native.json` (exactly
`NativeDashboard.to_api_payload()`, wrapped in a small envelope) and
`dashboards/ir/<stem>.ir.json` (`DashboardIR.to_dict()`, JSON-normalized),
plus a `dashboards/native/index.json` over the run. This happens
unconditionally, after the same final IR/native regeneration that
`migrate --upload` would use (post-validation query fixes, metadata polish),
so the persisted artifact and an immediate `--upload` always match. A later
`obs-migrate upload --artifact-dir … --artifact-format native` (or the
default `auto`, which prefers native when present) deploys that exact
reviewed payload with `dashboards_api.upload_native_artifact()` -- no YAML
re-mapping, and no legacy fallback, since there is nothing to silently
re-derive a rejected native payload from.

## QueryIR vs TargetQueryContract vs TargetQueryPlan

The query representation is split into three contracts:

- **QueryIR**: Source-agnostic semantic intent (what the query means).
- **TargetQueryContract**: Conditions required for an exact target translation.
- **TargetQueryPlan**: Target-specific rendering (how the query runs on Kibana/ES).

This separation exists because the same semantic intent may render differently
on different targets (e.g. PROMQL vs ES|QL on Elastic Serverless).

## TargetQueryContract

`TargetQueryContract` records the target requirements for an exact translation.
It sits between `QueryIR` and `TargetQueryPlan` and is evaluated before the
runtime query is considered final.

## Source Extensions

Every shared contract has a `source_extension: dict` field for
source-specific metadata that does not belong in the common fields.
This prevents source-specific details from leaking into shared contracts
while preserving them for debugging and reporting.
