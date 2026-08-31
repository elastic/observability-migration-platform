# Curated Dashboard Packs — Design Spec

**Date:** 2026-07-29  
**Status:** Approved  
**Scope:** Per-dashboard curated rule packs, auto-loaded by gnetId; Redis 12776 as the first pack; curation playbook for scaling to Top 100 Grafana community dashboards. Also specifies the separate Datadog curated **layout** packs — see [Datadog Curated Layout Packs](#datadog-curated-layout-packs).

---

## Two Pack Families

"Curated pack" names two independent mechanisms. Both are bundled in the
package and both auto-fire with no operator step, but they solve different
problems and share no code, schema, or registry:

| | Grafana curated rule packs | Datadog curated layout packs |
|---|---|---|
| What it overrides | Query semantics (`query.metric_kinds`, `query.label_candidates`), `panel.type_map`, hand-written `panel.query_overrides`, plus the `fidelity_manifest.yaml` contract | Panel `size` / `position` and section `collapsed` state — geometry only |
| Files | `pack.yaml` + optional `plugin.py` + `fidelity_manifest.yaml` under `adapters/source/grafana/curated_packs/<pack_dir>/` | a single `pack.yaml` under `adapters/source/datadog/curated_packs/<pack_dir>/` |
| Matched by | `registry.yaml` on `gnetId`, then exact `title_hint` (empty tags still match; tag overlap is required only when the dashboard still has tags) | `match.title_contains` declared inside the pack itself |
| Selects a panel by | `title_match` against the source panel title | the **emitted** panel title and/or presentation `kind`, plus `nth` |
| Applied | before translation (composes a `RulePackConfig`) | after translation and after `apply_style_guide_layout`, as the last word on layout |
| Operator opt-out | `--no-curated-packs` | none |
| Specified in | *Problem* → *Open Questions Resolved* below | [Datadog Curated Layout Packs](#datadog-curated-layout-packs) |

A Datadog pack never touches a query; a Grafana `panel.query_overrides` entry
never moves a panel. Do not port a construct from one family into the other —
the loaders do not understand each other's keys and will silently ignore them.

Everything from here to *Open Questions Resolved* describes the Grafana family.

---

## Problem

The general migration pipeline produces correct output for well-behaved Prometheus/Grafana setups but leaves gaps when migrating community dashboards from grafana.com:

- Counter/gauge classification is inferred, not authoritative — causes RATE vs AVG mismatches for known metrics.
- Label candidates (`instance`, `job`) are generic — the curated community standard is known.
- Some panel types and formula panels need dashboard-specific handling to reach 100% or best-possible fidelity.
- Operators today must author a `--rules-file` manually; there is no zero-friction path for known dashboards.

The goal is a bundled curated pack system that auto-fires for known community dashboards (identified by `gnetId`) and a repeatable playbook for building more packs.

---

## Non-Goals

- Dynamically downloading packs from the internet at migration time.
- Community-contributed packs in this repo (external contribution model is future work).
- Replacing the general pipeline — curated packs layer on top, not replace.
- Hardcoding target-environment decisions (e.g. `native_promql: true`) in a pack — those stay as CLI flags.

---

## Architecture

### 1. Curated Pack Directory

Bundled inside the installed package:

```
observability_migration/adapters/source/grafana/curated_packs/
├── __init__.py
├── registry.yaml                    # gnetId → pack mapping
└── grafana_12776_redis/
    ├── pack.yaml                    # declarative rules (same schema as --rules-file)
    │                                # supports: query (metric_kinds, label_candidates, ...),
    │                                #           panel (type_map, skip_types, query_overrides)
    ├── plugin.py                    # optional Python hooks (same API as --plugin)
    └── fidelity_manifest.yaml       # panel-by-panel fidelity classification
```

### 2. Registry Format

```yaml
# curated_packs/registry.yaml
packs:
  - gnet_id: 12776
    name: grafana_12776_redis
    title_hint: "Redis"          # fallback match when gnetId absent from JSON
    tags_hint: ["redis"]         # secondary fallback
    path: grafana_12776_redis
    description: "Redis (Percona, grafana.com/12776) — counter/gauge hints, label map, panel fidelity"
```

### 3. Per-Dashboard Resolution Stack

For every dashboard being migrated, a resolved `RulePackConfig` is built:

```
base defaults (RulePackConfig())
    ↓ merge
curated pack YAML   ← auto-loaded when gnetId (or title+tags) matches registry
    ↓ merge
user --rules-file   ← operator overrides anything in the curated layer
    ↓ register
user --plugin hooks
```

Dashboards with no registry match use the base pack unchanged — zero cost, zero change for existing workflows.

### 4. Pipeline Change

**New function** added to `rules.py`:

```python
def resolve_pack_for_dashboard(dashboard: dict, base_pack: RulePackConfig) -> RulePackConfig:
    """Returns a per-dashboard composed pack. Returns base_pack unchanged if no match."""
    curated = _load_curated_pack_for(dashboard)
    if curated is None:
        return base_pack
    return _merge_packs(curated, base_pack)   # base_pack wins on key collision
```

**Detection order** inside `_load_curated_pack_for`:
1. `dashboard.get("gnetId")` → exact integer lookup in registry (fast, reliable)
2. Exact `title_hint` match when `gnetId` is absent. Grafana copies and
   re-imports often strip both `gnetId` and tags; an empty tag list still
   matches. When the dashboard still has tags *and* the pack declares
   `tags_hint`, require overlap so a similarly titled unrelated dashboard
   does not pick up the pack.
3. Returns `None` if nothing matches

**Merge semantics** — same logic as `load_rule_pack_files` today:
- Lists: append-unique (curated entries added, user entries take precedence on conflict)
- Scalars: user_pack wins on key collision
- Dicts: shallow merge, user_pack wins per key

**Call site** — one change in the per-dashboard translate loop in `cli.py`:

```python
# Before
panel_results = translate_panels(dashboard, rule_pack, ...)

# After
resolved = resolve_pack_for_dashboard(dashboard, rule_pack)
panel_results = translate_panels(dashboard, resolved, ...)
```

Curated pack Python plugins are registered onto the resolved pack's registries, not the global ones — no cross-dashboard contamination in multi-dashboard runs.

### 5. CLI Surface

- No new flags for the happy path — auto-detection is zero friction for operators.
- `--no-curated-packs` flag: disables auto-loading (for operators who want full manual control).
- `--print-rule-catalog` output gains a detection line:
  ```
  Curated pack matched: grafana_12776_redis (gnetId=12776)
  ```

---

### 6. Panel-Level Query Overrides

For panels where auto-translation produces APPROXIMATE output — typically binary expressions between two gauge metrics with no outer aggregation — curated packs can supply a hand-crafted ES|QL query that bypasses auto-translation entirely:

```yaml
panel:
  query_overrides:
    - title_match: "Memory Usage"      # case-insensitive exact match against panel title
      esql_query: |                    # hand-crafted ES|QL; must produce the right shape
        TS metrics-*
        | WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend
        | STATS value = MAX(LAST_OVER_TIME(redis_memory_used_bytes)) / MAX(LAST_OVER_TIME(redis_memory_max_bytes)) * 100.0
      status_override: migrated        # migrated | migrated_with_warnings (default: migrated)
```

Optional `section_match` scopes the override to a Grafana row whose title
casefolds to (or starts with) that string, the same way layout overrides
distinguish Global vs Database duplicate titles.

**Merge semantics:** User pack overrides win by `(title_match, section_match)`.
If both the curated pack and the user `--rules-file` declare an override for
the same panel title and section, the user's query wins. Overrides with
different titles or sections are merged (both apply).

**ES|QL shape constraints:** The query must produce a shape that `_native_esql_panel_spec` can parse for the target Kibana panel type:
- `metric` / `gauge` panels: a `STATS` query with exactly one metric column and no `BY` clause. The simplest form is an inline division: `STATS value = MAX(...) / MAX(...)`.
- `line` / `area` / `bar` panels: a `STATS ... BY time_bucket [, group_label]` query with `SORT time_bucket ASC`.

**When to use:** Add a `panel_query_overrides` entry when:
1. A specific panel is APPROXIMATE due to a known translator gap (e.g. binary expression without outer aggregation or `on()` modifier)
2. A hand-crafted ES|QL query can express the same computation exactly
3. The gap cannot be fixed in the general translator without risk to other panels

**When NOT to use:** Do not use for panels that are APPROXIMATE due to data-availability gaps (`field_gap`, `data_gap`) or Kibana API limitations — those are documented deltas, not translator bugs.

**Deployed examples:** `grafana_11835_redis_exporter_helm`, `grafana_18405_redis_enterprise`, and `grafana_18406_redis_cloud` all carry `Memory Usage` overrides that compute the used/max ratio directly in ES|QL, promoting those panels from APPROXIMATE to PERFECT.

---

## Query Strategy

The Redis 12776 dashboard is entirely PromQL (Prometheus metrics from `redis_exporter`).

**Translation mode:** `auto` (already the default).
- ES 9.x with PROMQL endpoint → native `PROMQL(...)` panels, zero translation loss.
- ES without PROMQL → ES|QL fallback with correct counter/gauge treatment from the curated pack.

The curated pack does **not** hardcode `native_promql: true` — that is a target-environment decision passed via `--translation-mode`. The pack provides correctness for both paths:

- `metric_kinds` ensures rate() over counters emits `RATE`/`IRATE`, not `AVG` fallback, in ES|QL mode.
- In PROMQL mode, `metric_kinds` are no-ops — native PROMQL handles semantics correctly.

**Testing both paths is required:** CI runs `--translation-mode native` and `--translation-mode esql` for the Redis fixture and both must pass the full gate stack.

---

## Panel Fidelity Classification

Every panel in a curated dashboard is classified. **No panel is abandoned** — even the hardest cases get a curated best alternative. The classification documents confidence, not effort:

| Class | Meaning |
|---|---|
| `PERFECT` | 100% semantic translation — same query semantics, same visual type, same data shape in Kibana |
| `APPROXIMATE` | Best-effort Kibana equivalent — documented delta (visual or semantic difference is known and acceptable) |
| `BEST_EFFORT` | No direct Kibana analogue, but we found the closest meaningful substitute. Spend time here: try a different panel type, restructure the query, split into multiple panels, or use a Markdown description card that links to the Kibana equivalent workflow. Document exactly what was chosen and why. |

`BEST_EFFORT` examples:
- Grafana "Alert list" panel → Kibana rule status table (different data model, closest visual/semantic match)
- Grafana "News" panel → Kibana Markdown panel linking to the relevant docs/feed
- Grafana "Logs" panel with live tail → Kibana Discover link panel + a static Lens log panel showing recent rows
- Complex Grafana transformations (merge, sort, filter series) → restructured ES|QL query that achieves the same result differently

The fidelity manifest is the **contract** — what we committed to, reviewable per pack.

This classification is machine-readable in `fidelity_manifest.yaml`:

```yaml
# grafana_12776_redis/fidelity_manifest.yaml
dashboard:
  gnet_id: 12776
  title: "Redis"
  source: "https://grafana.com/grafana/dashboards/12776"

panels:
  - id: 2
    title: "Uptime"
    grafana_type: stat
    kibana_type: metric
    fidelity: PERFECT
    notes: "Single-value stat panel; ES|QL or PROMQL both produce correct value."

  - id: 4
    title: "Connected Clients"
    grafana_type: stat
    kibana_type: metric
    fidelity: PERFECT
    notes: "Gauge metric; AVG aggregation correct."

  - id: 6
    title: "Memory Usage"
    grafana_type: timeseries
    kibana_type: xy_chart
    fidelity: PERFECT
    notes: "Gauge metric; TS path correct."

  - id: 8
    title: "Commands / sec"
    grafana_type: timeseries
    kibana_type: xy_chart
    fidelity: PERFECT
    notes: "Counter; metric_kinds curated pack entry ensures RATE not AVG."

  - id: 10
    title: "Hit Ratio"
    grafana_type: stat
    kibana_type: metric
    fidelity: APPROXIMATE
    delta: "Grafana computes hits/(hits+misses) as a PromQL binary expression. ES|QL emits RATE separately; ratio is approximated via a formula column. Visual result matches when data is present."
    notes: "Acceptable approximation; formula panel."

  - id: 12
    title: "Slow Log"
    grafana_type: table
    kibana_type: datatable
    fidelity: APPROXIMATE
    delta: "Slow log requires redis SLOWLOG data in ES. If not ingested, panel shows empty state with a gap note."
    notes: "Data-gap, not a translator bug."
```

---

## Layout Curation

Grafana's `gridPos` (x, y, w, h on a 24-column grid) translates mechanically today — panels land at roughly the same positions. Curated packs override this with a hand-tuned Kibana layout.

### Why layout needs curation

- Kibana Lens panels have different minimum/optimal sizes than Grafana panels.
- Kibana sections (collapsible rows) are laid out differently from Grafana rows.
- Stat/metric panels look best at specific aspect ratios in Kibana's grid.
- Some Grafana panels that stacked vertically look better as a horizontal row in Kibana.

### Layout override in the pack

Each curated pack includes a `layout` section in `fidelity_manifest.yaml`:

```yaml
layout:
  grid_columns: 48        # Kibana uses 48-column grid (not 24)
  panels:
    - id: 2               # matches panel id above
      x: 0
      y: 0
      w: 8
      h: 3
    - id: 4
      x: 8
      y: 0
      w: 8
      h: 3
    - id: 6
      x: 16
      y: 0
      w: 16
      h: 6
  rows:
    - title: "Overview"
      y: 0
      collapsed: false
    - title: "Memory & CPU"
      y: 10
      collapsed: false
    - title: "Commands & Network"
      y: 20
      collapsed: false
```

### Layout curation process

For each dashboard:
1. Run mechanical migration → screenshot the result in Kibana.
2. Compare side-by-side with the Grafana original.
3. Identify panels that need resizing (too small, wrong aspect ratio) or repositioning (logical grouping broken).
4. Author the `layout` section to produce a polished Kibana-native layout — not a pixel-for-pixel copy of Grafana, but the best layout for Kibana's grid system.
5. Validate by rendering in a real Kibana instance (render audit).

The goal is a layout that looks like it was designed for Kibana, not mechanically ported from Grafana.

---

## Redis 12776 Curated Pack Content (Draft)

`pack.yaml`:

```yaml
query:
  metric_kinds:
    redis_commands_total: counter
    redis_keyspace_hits_total: counter
    redis_keyspace_misses_total: counter
    redis_net_input_bytes_total: counter
    redis_net_output_bytes_total: counter
    redis_expired_keys_total: counter
    redis_evicted_keys_total: counter
    redis_rejected_connections_total: counter
    redis_connected_clients: gauge
    redis_blocked_clients: gauge
    redis_memory_used_bytes: gauge
    redis_memory_max_bytes: gauge
    redis_uptime_in_seconds: gauge
    redis_db_keys: gauge
    redis_db_expires: gauge

  label_candidates:
    instance: [service.instance.id, host.name]
    job:      [service.name]

  metrics_dataset_filter: "prometheus"

panel:
  type_map:
    graph: timeseries   # legacy panel type used in older dashboard revisions
```

`plugin.py` — content determined during implementation after running migration and identifying any logic gaps not expressible in YAML.

---

## Dashboard Version Tracking and Drift

Grafana community dashboards are living documents — revisions get published, panels are added or removed, queries change. A curated pack is authored against a specific revision and may become stale.

### Revision pinning in the registry

```yaml
packs:
  - gnet_id: 12776
    name: grafana_12776_redis
    gnet_revision: 6               # the revision this pack was validated against
    dashboard_sha256: "abc123..."  # SHA-256 of the dashboard JSON at that revision
    path: grafana_12776_redis
```

### At migration time — no revision comparison (issue #350)

`resolve_pack_for_dashboard` matches a curated pack purely by `gnetId` (with a
title/tags fallback) and applies it unconditionally — it never reads or
compares the incoming dashboard JSON's revision against the registry's
`gnet_revision`, and emits no drift warning. This is intentional, not an
oversight: an operator's real Grafana-instance export is structurally
different from a pristine `grafana.com` download at the *same* revision
(mutated `id`/`uid`/`version`/panel-`id`/etc.), so any such comparison would
mismatch on effectively every real migration and would not be a meaningful
signal either way.

`gnet_revision` and `dashboard_sha256` are instead **maintainer-verified
provenance pins** — they record which exact `grafana.com` revision a pack's
author read when writing its `pack.yaml` overrides, re-checkable offline
against a fresh `grafana.com` download with
`python scripts/verify_curated_pack_pins.py` (network required; not part of
`make test` — see `docs/contributing/dev-commands.md`). The actual risk this
guards against — a pack silently missing dashboard content because the
upstream dashboard changed since the pack was authored — is caught at
migration time by a different, per-panel mechanism: the translator compares
each panel's source PromQL metrics against what survives into the final
emitted query and downgrades status/confidence with a warning when one is
dropped (`docs/sources/grafana.md`), independent of whether the revision pin
is current.

### Pack update workflow

When a significant new revision of a community dashboard is published:
1. Download the new revision and diff it by hand against the pinned one (no
   dedicated CLI command exists for this — `curl` the
   `grafana.com/api/dashboards/{id}/revisions/{revision}/download` endpoint
   for both revisions and compare).
2. Author updates the `fidelity_manifest.yaml` for changed/added panels.
3. Update `pack.yaml` / `plugin.py` if new metrics or panel types appear.
4. Update `gnet_revision` and `dashboard_sha256` in `registry.yaml`, then
   confirm with `python scripts/verify_curated_pack_pins.py --gnet-id <id>`.
5. Re-run the full gate stack.

### What never breaks across revisions

- `metric_kinds` — Redis exporter metric semantics don't change between dashboard revisions.
- `label_candidates` — instance/job mapping is stable.
- Layout overrides for panels that still exist — panel IDs are stable across Grafana revisions.

### What may break

- New panels added in a newer revision → they get general pipeline (no curated fidelity).
- Panels removed in a newer revision → their fidelity_manifest entries become stale (warn, not error).
- Query rewrites in a newer revision → pack's `plugin.py` hooks may need updating.

---

## Curation Playbook (scales to Top 100 dashboards)

This is the repeatable process for adding a new community dashboard pack:

### Step 1 — Fetch and Inventory
```bash
# Download dashboard JSON from grafana.com API
curl "https://grafana.com/api/dashboards/NNNN/revisions/latest/download" > dashboard.json

# Run the inventory tool (new: obs-migrate curated-pack init --gnet-id NNNN)
obs-migrate curated-pack init --gnet-id NNNN --out curated_packs/grafana_NNNN_name/
```

The init command produces a skeleton `fidelity_manifest.yaml` with all panels listed and `fidelity: UNKNOWN`.

### Step 2 — Classify Panels

For each panel, determine:
- Is the query type translatable? (PromQL → PROMQL/ES|QL, LogQL → ES|QL, ES|QL → ES|QL, other → NOT_FEASIBLE)
- Is the visual type supported? (timeseries, stat, gauge, table, text → PERFECT candidates; unsupported types → APPROXIMATE or NOT_FEASIBLE)
- Is there a formula or derived metric? (binary expressions → may need plugin.py formula)

Update `fidelity_manifest.yaml` with classifications.

### Step 3 — Build Pack

Author `pack.yaml`:
- `metric_kinds`: classify all metrics appearing in the dashboard
- `label_candidates`: map Grafana template variables to OTel field names
- `panel.type_map`: map any legacy/unusual panel types

Author `plugin.py` if needed:
- Formula panels that require post-processing
- Title/description polish specific to this dashboard
- Variable substitution edge cases

### Step 4 — Docker Test Environment

Each pack ships with a `docker-compose.yml` under `parity-rig/curated/grafana_NNNN_name/`:

```yaml
# parity-rig/curated/grafana_12776_redis/docker-compose.yml
services:
  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:9.5.0
    ...
  kibana:
    image: docker.elastic.co/kibana/kibana:9.5.0
    ...
  grafana:
    image: grafana/grafana:latest
    volumes:
      - ./grafana_provisioning:/etc/grafana/provisioning
    ...
  redis:
    image: redis:7
    ...
  redis_exporter:
    image: oliver006/redis_exporter:latest
    ...
  prometheus:
    image: prom/prometheus:latest
    # remote_write to ES
    ...
```

### Step 5 — Validate

Run the full gate stack:
```bash
# 1. Live ES|QL validation
python -m observability_migration.core.verification.verifier live_validate ...

# 2. Kibana saved-object contract
python -m observability_migration.targets.kibana.dashboards_api ...

# 3. Semantic parity
obs-migrate compare ... && python -m observability_migration.core.verification.verifier corpus_gate ...

# 4. Render audit (catches Lens accessor / invalid column)
python -m observability_migration.targets.kibana.render_audit_driver ...

# 5. Both translation modes
obs-migrate --translation-mode native ...
obs-migrate --translation-mode esql ...
```

All PERFECT panels must have zero `render_error`. APPROXIMATE panels must have zero `render_error` (data gaps are `field_gap`/`data_gap`, which are warns not fails). NOT_FEASIBLE panels produce placeholder panels — acceptable.

### Step 6 — Update fidelity_manifest and Commit

Final `fidelity_manifest.yaml` must have no `UNKNOWN` entries. PR description includes the fidelity summary table.

---

## Testing (per CI)

For the Redis 12776 pack specifically:

| Gate | Mode | Pass criterion |
|---|---|---|
| `live_validate` | esql | Zero ES|QL execution errors |
| `live_validate` | native | Zero PROMQL parse rejections |
| `dashboards_api` | both | Saved object validates against Kibana schema |
| `render_audit` | both | Zero `render_error`; `field_gap` allowed only for documented NOT_FEASIBLE/APPROXIMATE panels |
| `corpus_gate` | esql | Semantic parity score ≥ baseline |
| `interaction_audit` | esql | Control variable rewrites produce correct query rewrites |

---

## File Additions Summary

| File | Purpose |
|---|---|
| `observability_migration/adapters/source/grafana/curated_packs/__init__.py` | Package marker |
| `observability_migration/adapters/source/grafana/curated_packs/registry.yaml` | gnetId → pack lookup with revision pinning |
| `observability_migration/adapters/source/grafana/curated_packs/grafana_12776_redis/pack.yaml` | Redis declarative rules (metric_kinds, label_candidates, panel.type_map) |
| `observability_migration/adapters/source/grafana/curated_packs/grafana_12776_redis/plugin.py` | Redis Python hooks (formula panels, variable edge cases) |
| `observability_migration/adapters/source/grafana/curated_packs/grafana_12776_redis/fidelity_manifest.yaml` | Panel-by-panel fidelity + layout overrides |
| `parity-rig/curated/grafana_12776_redis/docker-compose.yml` | Docker test stack (ES + Kibana + Grafana + Redis + redis_exporter + Prometheus) |
| `parity-rig/curated/grafana_12776_redis/grafana_provisioning/` | Grafana dashboard + datasource provisioning |
| `parity-rig/curated/grafana_12776_redis/prometheus.yml` | Prometheus scrape + remote_write config |
| `observability_migration/adapters/source/grafana/rules.py` | `resolve_pack_for_dashboard()`, `_load_curated_pack_for()` |
| `observability_migration/adapters/source/grafana/cli.py` | Call `resolve_pack_for_dashboard` per dashboard; `--no-curated-packs` flag |
| `observability_migration/app/cli.py` | Forward `--no-curated-packs` in unified CLI |
| `tests/test_curated_packs.py` | Registry lookup, merge correctness, provenance-pin shape, manifest-vs-registry consistency, no-curated-packs opt-out |
| `tests/curated/test_grafana_12776_redis.py` | Redis pack fixture tests (offline) |

---

## Open Questions Resolved

- **Bundled vs external**: Bundled in-package. Zero operator setup for known dashboards.
- **Translation mode**: `auto` (PROMQL-first, ES|QL fallback). Pack does not override.
- **Multi-dashboard runs**: Per-dashboard resolution; no cross-contamination.
- **User override**: User `--rules-file` always wins over curated pack on key collision.
- **No panel abandoned**: All panels get best-effort treatment. BEST_EFFORT replaces NOT_FEASIBLE — find the closest Kibana alternative, document the delta.
- **Layout curation**: Curated packs control Kibana grid layout, not just queries/visuals.
- **Dashboard version drift**: Packs pin `gnet_revision`/`dashboard_sha256` as maintainer-verified provenance only; migration never compares them against the incoming dashboard and emits no drift warning (issue #350 — see "At migration time — no revision comparison" above). A pack that has fallen behind its upstream dashboard is caught per-panel instead, by the dropped-source-metric disclosure.

---

## Datadog Curated Layout Packs

**Status:** Implemented (`datadog_redis_overview` is the only pack today)  
**Scope:** Per-dashboard hand-tuned Kibana geometry for Datadog dashboards, auto-loaded by dashboard title. Layout only — no query, type, or field overrides.

### Why A Datadog Pack Exists

Datadog dashboards are laid out on a free-form 12-column grid with per-widget
heights that carry no Kibana equivalent. `generate.py` rescales each source row
proportionally onto Kibana's 48-column grid (`_apply_row_layout`, then
`_resolve_overlaps` and `apply_style_guide_layout`). That is structurally sound —
the layout-invariant gate proves no overlaps, no overflow, no sub-minimum widths —
but it inherits every quirk of the source: a 5/7-column split becomes a 20/28
split, single stat tiles stretch to whatever their source row implied, and tall
note widgets set the height for the charts beside them. The result reads as
mechanically ported.

For a dashboard we ship, demo, or point customers at, that is not good enough. A
curated layout pack replaces the auto-derived geometry for that one dashboard
with a hand-tuned Kibana-native layout.

**Reach for a pack when** the dashboard is high-traffic enough to hand-tune, the
panels themselves already translate correctly, and the only remaining complaint
is arrangement.

**Do not reach for a pack** to work around a translation problem. A Datadog pack
runs after translation and only sees emitted panel titles and geometry — it
cannot change a query, a panel type, a field mapping, or a title. A wrong panel
is a translator bug; fix it in the translator so every dashboard benefits.

### Directory Shape And Discovery

```
observability_migration/adapters/source/datadog/curated_packs/
├── __init__.py                     # load_curated_pack(dashboard_title)
└── datadog_redis_overview/
    └── pack.yaml                   # match + sections[].panels[] geometry
```

There is no registry file. `load_curated_pack` scans the immediate
subdirectories of `curated_packs/` for a `pack.yaml`, reads each one's
`match.title_contains`, and returns the first pack whose value is a
**case-insensitive substring** of the dashboard title:

```yaml
match:
  title_contains: "Redis - Overview"
```

Directory scan order is filesystem order, not defined, so keep
`title_contains` specific enough that no two packs can match the same
dashboard.

### Call Site

`_build_dashboard_yaml_doc` in `generate.py` calls `load_curated_pack` once per
dashboard, *after* `apply_style_guide_layout`, and applies any hit via
`_apply_curated_layout`:

```python
apply_style_guide_layout(doc)

curated_pack = load_curated_pack(dashboard.title)
if curated_pack:
    _apply_curated_layout(doc, curated_pack, results)
```

The pack therefore has the last word on layout — nothing rescales or re-rows the
panels afterwards. Because that document is then converted to `DashboardIR`, from
which both the typed Dashboards API payload and the YAML are derived, the curated
coordinates reach every emitted artifact from one place.

There is no CLI flag and no operator step. A dashboard whose title matches gets
the curated layout; every other dashboard is untouched and costs nothing.

### Selector Model

Each `sections[]` entry is matched to a generated section by exact `title`, and
each `panels[]` entry under it selects exactly one leaf panel:

```yaml
sections:
  - title: "Overview"        # exact match against the emitted section title
    collapsed: false         # optional; only set when present
    panels:
      # All three selectors are optional and shown together here; the real pack
      # uses `title` alone for named panels and `kind` + `nth` for notes.
      - title: "Hit rate"    # selector
        kind: esql           # selector
        nth: 0               # selector
        size: {w: 12, h: 6}  # applied only when present
        position: {x: 0, y: 0}
```

| Key | Meaning |
|---|---|
| `title` | Exact, case-sensitive match against the **emitted** leaf panel title. Constrains the match only when present and non-empty. |
| `kind` | The panel's presentation block key — one of `markdown`, `esql`, `lens`, `links`, `image` (`PANEL_PRESENTATION_KINDS`). Constrains the match only when present. |
| `nth` | 0-based index among the candidates that satisfied the `title`/`kind` constraints, in section order. Defaults to `0`. |
| `size` | `{w, h}` in Kibana grid columns/rows. Applied only when present; otherwise the auto-derived size stands. |
| `position` | `{x, y}` relative to the section's own coordinate space. Applied only when present. |

An omitted selector matches anything, so `kind: markdown` + `nth: 1` means
"the second markdown panel in this section, whatever it is called".

Failure modes are quiet by design and caught downstream, not at load time:

- A `sections[]` entry whose `title` matches no generated section is skipped
  silently. The coverage test asserts every declared section exists.
- A `panels[]` entry whose `nth` is out of range for its candidate list is
  skipped silently. The panel it was supposed to move then shows up in the
  coverage warning below.

### Emitted Titles, Not Datadog Titles

**This is the part that bites.** Before a pack is applied,
`_ensure_unique_leaf_panel_titles` rewrites blank and duplicate panel titles,
because the migration report and the render audit key per-panel verdicts by
title and would otherwise collapse them. So the titles a pack must use are the
*post-rewrite* ones:

| Source situation | Emitted title |
|---|---|
| Widget has a unique title | unchanged (`Hit rate`) |
| Widget has no title | `Datadog <source type> <widget id>` — e.g. `Datadog note 8013519185925578`; with no widget id, a bare `Datadog note` plus a de-duplicating ` (<ordinal>)` suffix |
| Two widgets share a title | first keeps it; later ones get ` (widget <id>)` — e.g. `Cache hit rate` and `Cache hit rate (widget 21)` |

Two consequences:

- **`title: ""` never matches anything.** An empty string is falsy, so it does
  not constrain the match at all (`_curated_spec_candidates` treats it as
  absent) — the spec silently selects the nth panel of the whole section
  instead. There are no blank emitted titles left to match anyway.
- **The second of two same-titled panels must be addressed by its real emitted
  title**, `Cache hit rate (widget 21)`, not by the source title plus an `nth`
  guess.

**Select notes with `kind: markdown` + `nth`, never by title.** A generated note
title is either widget-id-derived (opaque, and gone the moment the widget is
recreated upstream) or ordinal-derived — and the ordinal counts every leaf panel
in the dashboard, so adding or removing *any* widget renumbers it and the spec
stops matching a panel it used to move. "The nth markdown panel in this section"
survives all of that. `_panel_presentation_kind` exists precisely so packs never
have to depend on a generated title.

### Full Coverage Is Required

**Every leaf panel in a section the pack declares must be covered by some spec.**
A partially covered section is a bug, not a partial improvement: the panels no
spec matched keep their auto-derived coordinates while their neighbours move to
curated ones, so they collide.

Two safeguards make that visible instead of shipping:

1. **Coverage guard** — `_warn_uncovered_curated_panels` appends a warning naming
   the dashboard, the section, and every uncovered panel to those panels'
   `TranslationResult.warnings`, which surfaces in the operator migration
   report. The message ends with the fix: *"Add a matching panel spec (title, or
   kind + nth) to the pack."*
2. **Overlap re-resolution** — the generic `_resolve_overlaps` pass is re-run
   over that section, so a partially covered pack can never emit overlapping
   panels. The section no longer matches the curated design, but it stays
   renderable.

On a complete pack the guard is inert: it finds no uncovered panels and returns
before touching anything. Sections the pack does *not* declare are not affected
either way — they keep their auto-derived layout in full.

### Worked Example

From `datadog_redis_overview/pack.yaml` (excerpt — the shipped pack covers all
seven sections):

```yaml
match:
  title_contains: "Redis - Overview"

sections:
  - title: "Overview"
    collapsed: false
    panels:
      # Row 0: 4 stats across the full 48 columns
      - title: "Hit rate"
        size: {w: 12, h: 6}
        position: {x: 0, y: 0}
      - title: "Blocked clients"
        size: {w: 12, h: 6}
        position: {x: 12, y: 0}
      - title: "Redis keyspace"
        size: {w: 12, h: 6}
        position: {x: 24, y: 0}
      - title: "Unsaved changes"
        size: {w: 12, h: 6}
        position: {x: 36, y: 0}
      # Row 1: stat + its explanatory note, note selected by kind
      - title: "Primary link down"
        size: {w: 24, h: 8}
        position: {x: 0, y: 6}
      - kind: markdown
        nth: 0
        size: {w: 24, h: 8}
        position: {x: 24, y: 6}

  - title: "Performance Metrics"
    collapsed: false
    panels:
      # Row 0: wide chart + narrow note
      - title: "Latency by Host"
        size: {w: 36, h: 12}
        position: {x: 0, y: 0}
      - kind: markdown
        nth: 0
        size: {w: 12, h: 12}
        position: {x: 36, y: 0}
      # Row 1: the duplicate-title pair — a stat and a line chart that were both
      # called "Cache hit rate" in Datadog
      - title: "Cache hit rate"
        size: {w: 12, h: 6}
        position: {x: 0, y: 12}
      - title: "Cache hit rate (widget 21)"
        size: {w: 36, h: 12}
        position: {x: 12, y: 12}
      # ... remaining Performance Metrics panels omitted; the real section
      # covers all eight
```

Note what the `kind: markdown` + `nth: 0` entries buy: the emitted titles behind
them are `Datadog note 7896589211182748` and `Datadog note 18`, neither of which
belongs in a hand-maintained file.

### Authoring And Validating A New Pack

1. **Generate the dashboard offline.** No cluster needed:

   ```bash
   .venv/bin/datadog-migrate --source files \
     --input-dir <dir with the dashboard JSON> \
     --output-dir /tmp/dd-pack --field-profile otel --assets dashboards
   ```

2. **List the emitted titles per section** — these, not the Datadog titles, are
   the pack keys. The native review artifact is the shortest path:

   ```bash
   jq -r '.payload.panels[]
          | if .panels
            then "SECTION \(.title) (collapsed=\(.collapsed))",
                 (.panels[] | "    \(.type)  \(.config.title)  \(.grid.x),\(.grid.y) \(.grid.w)x\(.grid.h)")
            else "PANEL \(.config.title)" end' \
     /tmp/dd-pack/dashboards/native/*.native.json
   ```

   The native `type` (`vis` / `markdown`) is the API discriminator, not the pack
   `kind`; only `markdown` is spelled the same in both. It is still enough to
   spot the notes.

3. **Write `curated_packs/<pack_dir>/pack.yaml`** with a `match.title_contains`
   specific to that dashboard and a spec for **every** leaf panel of every
   section you declare. Use titles for real panels, `kind: markdown` + `nth` for
   notes, and the `(widget NN)` form for duplicates. Keep each row's widths
   summing to 48 and `y` monotonic per row.

4. **Confirm with the gates:**

   ```bash
   .venv/bin/python -m pytest tests/test_datadog_curated_layout.py tests/e2e/test_layout_invariants.py -v
   ```

   `test_shipped_redis_pack_covers_every_generated_panel` is the important one:
   it migrates the real dashboard and runs the **production matcher**
   (`_curated_spec_candidates`) against the generated document, then asserts both
   directions — no spec that matched nothing, no generated panel that no spec
   matched — and that the in-generator coverage guard stayed silent for the same
   run. A coverage gap fails CI rather than shipping a collided section.
   `tests/e2e/test_layout_invariants.py` independently re-checks the geometry of
   every shipped Datadog and Grafana dashboard: no overlap, `x + w <= 48`, no
   negative coordinates, no width below the readability floor.

5. **Look at it in Kibana** (render audit or a clean view-mode session). The
   gates prove the geometry is legal, not that it looks good — that is the whole
   point of curating it by hand.

### Packaging

A pack directory only reaches an installed operator if it is declared as package
data. `pyproject.toml` currently declares the Grafana family only:

```toml
[tool.setuptools.package-data]
"observability_migration.adapters.source.grafana.curated_packs" = ["registry.yaml", "**/*.yaml"]
```

As of 2026-08-03 there is **no equivalent entry for
`observability_migration.adapters.source.datadog.curated_packs`**, so
`datadog_redis_overview/pack.yaml` is absent from a built wheel (verified by
inspecting the wheel contents) and the curated layout applies only from a repo
checkout or editable install. Adding a Datadog pack means adding the matching
`package-data` line.

### File Map

| File | Purpose |
|---|---|
| `observability_migration/adapters/source/datadog/curated_packs/__init__.py` | `load_curated_pack(dashboard_title)` — scans pack directories, case-insensitive `match.title_contains` |
| `observability_migration/adapters/source/datadog/curated_packs/datadog_redis_overview/pack.yaml` | The only shipped pack: seven sections of hand-tuned geometry for Datadog "Redis - Overview" |
| `observability_migration/adapters/source/datadog/generate.py` | `PANEL_PRESENTATION_KINDS`, `_panel_presentation_kind`, `_curated_spec_candidates`, `_warn_uncovered_curated_panels`, `_apply_curated_layout`, and the `_build_dashboard_yaml_doc` hook |
| `tests/test_datadog_curated_layout.py` | Loader, selector semantics, per-section geometry, coverage-vs-generated-document gate, and the incomplete-pack warning/no-overlap safety net |
| `tests/e2e/test_layout_invariants.py` | Source-agnostic geometry invariants over every shipped dashboard |

### Design Decisions

- **No registry file.** One `match.title_contains` per pack keeps the match rule
  next to the layout it applies to; Datadog has no stable community-dashboard id
  like `gnetId` to key a registry on.
- **Title match, not dashboard id.** Datadog dashboard ids are per-account, so an
  id-keyed pack could never fire for a customer's copy of an integration
  dashboard.
- **Layout only.** Query and field behavior belongs in the translator or a field
  profile, where every dashboard benefits, not in a per-dashboard pack.
- **`kind` + `nth` alongside `title`.** Generated note titles are not stable pack
  keys; a positional selector over the presentation kind is.
- **Warn and self-heal rather than fail.** A stale pack degrades to the
  auto-derived layout for the panels it lost, reports why on the affected panels,
  and can never emit an overlap — a broken pack must not break a migration.
