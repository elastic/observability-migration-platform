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
`DashboardIR.from_yaml_dict()` *before* the native mapping. From that point on,
the dict is no longer the source of truth: the native Dashboards API payload and
the kb-dashboard YAML *document* are both produced from the `DashboardIR`, so
they cannot drift from each other (see
`tests/test_grafana_native_dashboard_emission.py` and
`tests/test_datadog_native_dashboard_emission.py`).

**A migration writes no dashboard YAML.** The two persisted representations are
`dashboards/native/<stem>.native.json` and `dashboards/ir/<stem>.ir.json`; the
YAML document stays an in-memory derivation (`DashboardIR.to_yaml_dict()`) used
by the structural equivalence guards, and is materialized as a *file* only into a
scratch directory that `--compile` / `--legacy-import` delete again, because
`kb-dashboard-cli` takes a YAML path. `obs-migrate compile` and
`upload --artifact-format yaml` / `--yaml-dir` still accept an externally
supplied (hand-written or archived) YAML directory.

Grafana mutators that run after emission --
`targets/kibana/compile.py::sync_result_queries_to_ir` (post-validation
ES|QL fixes) and `adapters/source/grafana/polish.py::apply_metadata_polish`
(title/label polish) -- follow the same pattern: take the document derived from
`result.dashboard_ir`, mutate it, rebuild `DashboardIR.from_yaml_dict()`, then
re-derive the native payload from that rebuilt IR. Neither touches the disk.

That rebuild is **lossy by construction**: the YAML document shape is validated
against `docs/dashboards/schema.json` (`additionalProperties: false`), so
`to_yaml_dict()`/`from_yaml_dict()` only round-trip `title`, `description`,
`minimum_kibana_version`, `settings`, `panels`, `filters` and `controls`. Every
other `DashboardIR` field -- `uid`, `folder`, `tags`, `source_file`, `metadata`,
`source_extension`, `alerts`, `annotations`, `links`, `transforms`, `version`,
`source_adapter` -- has to be carried across the rebuild explicitly, or it
reverts to its dataclass default. `native_dashboard_from_ir` reads dashboard
`tags` straight off the IR precisely because the YAML shape cannot express them,
so dropping them on the rebuild uploaded the dashboard to Kibana with its tags
stripped. `targets/kibana/compile.py` owns the classification
(`YAML_ROUND_TRIPPED_IR_FIELDS` vs `IR_FIELDS_CARRIED_ACROSS_YAML_REBUILD`) and
carries over everything outside the round-tripped set by iterating
`dataclasses.fields(DashboardIR)`; a new IR field is therefore preserved
automatically, and an exhaustiveness test fails until it is classified.
Datadog's post-validation rewrite
(`adapters/source/datadog/cli.py::_rewrite_dashboard_artifacts`) regenerates
artifacts through `generate_dashboard_artifacts`, which rebuilds the IR the
same way.

### PanelIR

A leaf panel (`kind="panel"`) embeds a `VisualIR` and carries layout/title
through `VisualIR.layout`/`VisualIR.title`. `VisualIR.presentation.kind`
identifies the kb-dashboard-core block (`esql`, `lens`, `markdown`, `links`,
or `image`), while `presentation.config` preserves that block's configuration
(including the ES|QL query string for `esql`). A section/row
(`kind="section"`) carries `children: list[PanelIR]` instead.
`PanelIR.to_yaml_panel_entry()` / `PanelIR.from_yaml_panel_entry()` round-trip
one kb-dashboard-core `panels[]` entry (leaf or nested `section`).

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

Treat YAML, `--compile`, `--legacy-import`, and `--compiled-dir` as
compatibility surfaces. They remain supported for existing scripts and legacy
review/import workflows, but new dashboard migration behavior should be
specified in terms of `DashboardIR`, `NativeDashboard`, and the persisted
`native/*.native.json` review artifact.

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

### Reading the IR artifact back

`DashboardIR.from_dict()` (with `PanelIR.from_dict()`,
`VisualIR.from_dict()`, `ControlIR.from_dict()`) is the inverse of
`to_dict()` and therefore the import direction of
`dashboards/ir/<stem>.ir.json`. Every in-repo tool that used to read the
dashboard YAML off disk reads that artifact and rebuilds the IR through
these classmethods, so the readers and the migration share one definition of
what a dashboard is:

| Reader | Reads | Uses |
|---|---|---|
| `core/telemetry_contract.py` | `ir/*.ir.json` | panel queries, controls, dashboard filters for the seed contract and schema-change report |
| `targets/kibana/interaction_audit_local.py` | `ir/*.ir.json` | stable panel identities plus the `?param` binding gate |
| `parity-rig/verifier/collectors.py` | `ir/*.ir.json` | the verifier's T2 tier |
| `parity-rig/verifier/visual_regression.py` | `ir/*.ir.json` | Kibana-side panel discovery in canonical order |
| `parity-rig/verifier/classifier.py` | `native/*.native.json` + `ir/*.ir.json` | artifact mtime for the `kibana_cache_stale` rule |
| `core/telemetry_contract.py::count_declared_controls` | `native/*.native.json` | `mapping.controls` cross-check |

`from_dict()` restores dashboard identity, `panels` (layout + presentation),
`controls`, `filters` and `settings` -- everything `to_yaml_dict()` and those
readers consume. It deliberately does not rehydrate the referenced asset
collections (`alerts`/`annotations`/`links`/`transforms`) or a panel's
embedded `QueryIR`: no artifact reader consumes them, and they are exported
through `to_dict()` rather than through this path.

Prefer `ir/` for semantic content (queries, titles, controls, filters) and
`native/` for the typed API shape (`payload.panels[].grid`) and `mapping.*`
counters.

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

### Grafana `required_target_contract.json`

Grafana preflight (`--preflight`) writes this artifact under
`<output-dir>/dashboards/required_target_contract.json`. Top-level keys include:

| Key | Meaning |
|---|---|
| `field_profile` | CLI `--field-profile` plan (`otel`, `prometheus_remote_write`, `prometheus_metrics`, `prometheus_native`, `passthrough`, `auto`) |
| `planned_schema_profile` | Effective emit layout derived from the plan (`prometheus_remote_write`, `prometheus_metrics`, `prometheus_native`, or `null` for otel/passthrough/auto→otel) |
| `detected_schema_profile` | Named layout inferred from live `_field_caps` when `--es-url` was used |
| `profile_mismatch` | `true` when `planned_schema_profile` and `detected_schema_profile` are both named layouts and differ |
| `schema_profile` | Backward-compatible alias of `detected_schema_profile` |
| `field_capabilities_index` | Index pattern probed for `_field_caps` |
| `field_capabilities_discovery` | Discovery status object (`status`, `error`, `field_count`) |
| `required_fields` | Per resolved target field: `status` (`confirmed`/`missing`/`unknown`), `type`, `target_field`, `source_fields`, `roles`, `panels` |
| `counter_expectations` | Per metric: `source_field`, `target_field`, `expected_counter`, `confirmed_counter`, `panels` |
| `totals` | Aggregate counts (`fields`, `fields_confirmed`, `fields_missing`, `fields_unknown`, …) |

When `field_profile=auto` and caps are ambiguous, `planned_schema_profile` is
`null`, emit follows otel rules, and preflight may record `auto_fallback=otel`
via the resolver summary (surfaced on the contract when wired through preflight).

## Source Extensions

Every shared contract has a `source_extension: dict` field for
source-specific metadata that does not belong in the common fields.
This prevents source-specific details from leaking into shared contracts
while preserving them for debugging and reporting.
