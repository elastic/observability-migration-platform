# Curated Dashboard Packs — Design Spec

**Date:** 2026-07-29  
**Status:** Approved  
**Scope:** Per-dashboard curated rule packs, auto-loaded by gnetId; Redis 12776 as the first pack; curation playbook for scaling to Top 100 Grafana community dashboards.

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
2. `dashboard.get("title")` + `dashboard.get("tags", [])` → fuzzy fallback for instances that strip `gnetId`
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

### At migration time — drift detection

When `resolve_pack_for_dashboard` loads a curated pack:
1. It compares the incoming dashboard JSON's revision (if present in `_grafana_meta` or dashboard JSON) against `gnet_revision`.
2. If they match → apply the pack normally, no warning.
3. If the incoming revision is newer → emit a structured warning:
   ```
   WARNING: Curated pack grafana_12776_redis was validated against revision 6;
   this dashboard is revision 9. The pack will still be applied — metric_kinds
   and label_candidates remain valid across minor revisions — but new or changed
   panels may not benefit from fidelity overrides. Run `obs-migrate curated-pack check`
   to see what changed.
   ```
4. The pack is still applied — most of it (metric_kinds, label_candidates, layout for existing panels) remains correct across minor revisions. Only net-new panels fall back to the general pipeline.

### Pack update workflow

When a significant new revision of a community dashboard is published:
1. `obs-migrate curated-pack diff --gnet-id 12776` — shows which panels changed vs. the pinned revision.
2. Author updates the `fidelity_manifest.yaml` for changed/added panels.
3. Update `pack.yaml` / `plugin.py` if new metrics or panel types appear.
4. Update `gnet_revision` and `dashboard_sha256` in `registry.yaml`.
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
| `observability_migration/adapters/source/grafana/rules.py` | `resolve_pack_for_dashboard()`, `_load_curated_pack_for()`, drift warning |
| `observability_migration/adapters/source/grafana/cli.py` | Call `resolve_pack_for_dashboard` per dashboard; `--no-curated-packs` flag |
| `observability_migration/app/cli.py` | Forward `--no-curated-packs` in unified CLI |
| `tests/test_curated_packs.py` | Registry lookup, merge correctness, drift detection, no-curated-packs opt-out |
| `tests/curated/test_grafana_12776_redis.py` | Redis pack fixture tests (offline) |

---

## Open Questions Resolved

- **Bundled vs external**: Bundled in-package. Zero operator setup for known dashboards.
- **Translation mode**: `auto` (PROMQL-first, ES|QL fallback). Pack does not override.
- **Multi-dashboard runs**: Per-dashboard resolution; no cross-contamination.
- **User override**: User `--rules-file` always wins over curated pack on key collision.
- **No panel abandoned**: All panels get best-effort treatment. BEST_EFFORT replaces NOT_FEASIBLE — find the closest Kibana alternative, document the delta.
- **Layout curation**: Curated packs control Kibana grid layout, not just queries/visuals.
- **Dashboard version drift**: Packs pin `gnet_revision`; migration warns (not errors) on mismatch and still applies the pack.
