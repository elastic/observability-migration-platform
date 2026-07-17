# Grafana ↔ Datadog field-profile alignment — design

**Date:** 2026-07-17  
**Status:** Implemented; see the canonical [Field Profile Contract](../../command-contract.md#field-profile-contract)

**Depends on:** Grafana `passthrough` (#299) — planned profiles build on that CLI surface

## Goal

Give operators one mental model for `--field-profile` on both sources:

1. Choose a **planned** profile (assets-first; works before telemetry exists).
2. Emit field names for that plan.
3. With `--es-url`, **verify** against live `_field_caps` — do not silently remap to a different layout.
4. Default both sources to **`otel`**.

## Non-goals

- Shared profile-loader package / single cross-adapter registry refactor.
- ECS field profiles.
- Adding Datadog `--field-profile auto` (Datadog stays explicit-only).
- Changing Datadog built-in profile **names** (`elastic_agent`, `prometheus`, YAML paths).
- Grafana inventing Datadog-only profile names (`elastic_agent`).

## Decisions (locked)

| Decision | Choice |
|---|---|
| Alignment style | Operator UX parity (plan → emit → verify), not shared machinery |
| Offline / no `--es-url` | Emit planned layout field names immediately |
| With `--es-url` | Verify plan; surface mismatch / missing fields; do **not** override the plan |
| Grafana `auto` when caps ambiguous | Fall back to **`otel` + warn** (not hard-fail) |
| Datadog `auto` | Does not exist; keep explicit profiles only |

## Current asymmetry

| | Grafana (today, post-passthrough) | Datadog (today) |
|---|---|---|
| CLI profiles | Effectively `otel` + `passthrough` (bool into `SchemaResolver`); docs also mention discovery layouts | `otel`/`default`, `elastic_agent`, `prometheus`, `passthrough`, YAML |
| Layout choice | Discovery-driven when not passthrough | Explicit planned profile |
| Live caps | Discovery / fallback; passthrough validates bare names | Check mapped fields → `confirmed` / `missing` / `unknown` |
| Default | `otel` | `otel` |

## Target behavior

### Shared contract (both sources)

1. `--field-profile` selects the **plan**.
2. Translation uses that plan’s metric/label mapping rules.
3. `--es-url` loads `_field_caps` for readiness and type-aware checks only.
4. Artifacts expose `field_profile` plus per-field status where contracts already exist:
   - Grafana: `required_target_contract.json` (extend/clarify planned vs detected).
   - Datadog: `target_readiness_contract.json` (already profile + statuses).
5. Docs (`command-contract`, `sources/grafana.md`, `sources/datadog.md`) and operator skills describe the same plan→verify story.

### Grafana profiles

| Profile | Offline emit | With `--es-url` |
|---|---|---|
| `otel` | Bare / OTel-candidate mapping (current non-namespaced path) | Verify; warn on missing |
| `prometheus_remote_write` | `prometheus.<metric>.{counter,value,rate}`, `prometheus.labels.*` | Verify; **`profile_mismatch`** if live layout looks like another named profile |
| `prometheus_metrics` | `prometheus.metrics.<metric>`, `prometheus.labels.*` | Same mismatch rule |
| `prometheus_native` | `metrics.<metric>`, `labels.*` | Same mismatch rule |
| `passthrough` | Source names verbatim (rule-pack overrides still apply) | Validate bare names exist when possible; no automatic remapping |
| `auto` | **Rejected** without `--es-url` | Detect clear `prometheus_remote_write` / `prometheus_metrics` / `prometheus_native`; if ambiguous → emit as **`otel`** and **warn** |

`profile_mismatch` (Grafana): planned profile ≠ detected named layout. Translation **keeps the plan**; preflight/report warn (or block only if existing Grafana preflight already treats mismatch as blocker — preserve current severity unless tests require otherwise). Do not silently switch emit rules to the detected layout.

### Datadog profiles (explicit-only; docs/skills aligned)

| Profile | Behavior |
|---|---|
| `otel` / `default` | Planned OTel-ish maps (default) |
| `elastic_agent`, `prometheus` (Metricbeat), `prometheus_native` (ES `/_prometheus`), `passthrough`, YAML | Explicit plan only |
| + `--es-url` | Field readiness against that plan (`confirmed` / `missing` / `unknown`) |

No profile auto-detection. Wrong profile → missing-field warns/blocks in preflight; emitted queries still follow the chosen profile.
Prometheus profiles use their label prefixes for metric queries only; log
queries retain ECS / OTel field mappings.

### Alignment rules (must hold)

- Same flag name, default (`otel`), and narrative: **plan then verify**.
- Grafana `auto` is the only source-specific convenience; document it as Grafana-only next to Datadog’s “always explicit”.
- Status vocabulary stays parallel: planned profile name + field existence statuses; Grafana may add `detected_schema_profile` / `profile_mismatch` for named Prometheus layouts.
- Skills (`prepare-target-telemetry`, `understand-source-schema`, `assess-migration-readiness`) updated in `.claude` / `.cursor` / `.agents` lockstep.

## Implementation sketch (for the plan)

1. **Grafana `SchemaResolver`:** accept explicit `field_profile` ∈ `{otel, prometheus_remote_write, prometheus_metrics, prometheus_native, passthrough, auto}`; planned emit without requiring discovery; discovery used for verify + `auto` detection only.
2. **Grafana CLI:** expand `_GRAFANA_FIELD_PROFILES`; `auto` requires `--es-url`; wire alert + alternate-index resolvers the same way.
3. **Preflight / contracts:** record `field_profile`, `schema_profile` / detected layout, mismatch flag; keep Datadog contract shape.
4. **Tests:** planned offline emit for each Grafana named profile; `auto` ambiguous → otel + warn; mismatch does not change emitted names; Datadog regression that profiles remain explicit.
5. **Docs + skills:** command-contract + both source docs + skills mirrors.

Reuse direction already explored in local WIP / stash (planned Grafana profiles + Datadog contract polish) — land cleanly on top of #299/`passthrough`, not as a silent remap of discovery-first behavior.

## Success criteria

- Offline Grafana migrate with `--field-profile prometheus_remote_write` emits `prometheus.*` fields without `--es-url`.
- Offline Grafana migrate with `--field-profile prometheus_metrics` emits `prometheus.metrics.*` / `prometheus.labels.*` without `--es-url`.
- Live run with wrong plan vs caps surfaces mismatch/missing without rewriting queries to the detected layout.
- `auto` without clear caps → otel mapping + explicit warning; `auto` without `--es-url` → clear error.
- Datadog still has no `auto`; default `otel`; readiness statuses unchanged in meaning.
- Docs/skills describe one shared story with Grafana-only `auto` called out.

## Open follow-ups (not this design)

- Whether Grafana `profile_mismatch` should be a hard preflight **block** vs warn-only (preserve existing severity in first land; revisit if operators need a gate).
- Optional later: Datadog convenience heuristics that *suggest* a profile in doctor/preflight text without changing emit rules.
