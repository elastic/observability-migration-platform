# Curated Pack — Grafana 12485 "PostgreSQL Exporter"

> Design + living discoveries for the 12485 curated pack. Follows the general
> Curation Playbook in `curated-dashboard-packs-plan.md`. 14114 (PostgreSQL
> Exporter Quickstart) already ships a validated pack; this doc is the net-new
> 12485 work plus a live re-validation pass of 14114.

- Source: Grafana Labs / community **"PostgreSQL Exporter"**,
  <https://grafana.com/grafana/dashboards/12485-postgresql-exporter/>
- gnetId **12485**, only revision is **1** (2020-06-17).
- canonical sha256 (rev 1) = `e14a35eac532db4f79837a293411edb20d04164ca59de17d73557d08637a4700`
  (matches `parity-rig/benchmark/community_corpus.json`).
- Datasource: Prometheus (`__inputs[0].pluginId == prometheus`) — curated-pack eligible.
- Panels: ~37 across two sections — **Global Statistics** and **Database: $Database**
  (the DB section repeats the same metric families scoped by `datname`).
- Controls: `Instance`, `Database`, `Interval` (Grafana interval var).

## Goal

Ship a curated pack so 12485 renders in Kibana with the same information as
Grafana (or better) against a real `postgres_exporter` scrape ingested in the
Elastic `prometheus_native` layout (`metrics.* + labels.*`), then prove it with
render + interaction audits and a side-by-side.

## Engine vs pack split

The general pipeline already handles rate/gauge translation, five-target
fusion, and control synthesis. The pack only carries what 12485 needs beyond
that, verified against the **real** exporter (not guessed):

### Empirical exporter findings (prometheuscommunity/postgres-exporter v0.15.0)

Read directly from the live rig exporter (`curl :9187/metrics`). The dashboard
was authored against an older exporter lineage, so several names differ from
what v0.15.0 actually emits:

| Dashboard PromQL name | Real exporter field | Kind | Note |
|---|---|---|---|
| `pg_database_size` | `pg_database_size_bytes` | gauge | rename |
| `pg_replication_lag` | `pg_replication_lag_seconds` | gauge | rename; 0 on a standalone primary |
| `pg_stat_statements_calls` | `pg_stat_statements_calls_total` | counter | needs `--collector.stat_statements` + extension |
| `pg_stat_statements_total_time_seconds` | `pg_stat_statements_seconds_total` | counter | same |
| `pg_postmaster_start_time_seconds` | *(same)* | gauge | needs `--collector.postmaster` (off by default) |
| `pg_stat_activity_count` | *(same)* | **gauge** | `_count` suffix would mislead offline heuristic → force gauge |
| `pg_locks_count` | *(same)* | **gauge** | same |
| `pg_settings_shared_buffers_bytes` | *(same)* | gauge | present as-is |
| `pg_stat_database_*` (xact/tup/blk_time/deadlocks/temp_files/blks_*) | *(same)* | counter | exporter `# TYPE` = counter |

`stat_statements` and `postmaster` collectors are **off by default** in v0.15.0,
and `pg_stat_statements` requires `shared_preload_libraries` + `CREATE EXTENSION`.
The rig (`parity-rig/curated/grafana_763_redis_exporter/`) was extended to enable
all three so Query rate / Average query runtime / Uptime render on real data.
On a target cluster that does not run these, those panels are an honest
`field_gap`/`data_gap`, not a translator bug.

### Pack rules

- **`metric_kinds`** — force `pg_stat_activity_count` + `pg_locks_count` +
  `pg_stat_database_numbackends` to `gauge`; assert counters for the rated
  `pg_stat_database_*` and mapped `pg_stat_statements_*` targets; gauges for the
  `pg_settings_*` / size / lag / start-time series.
- **`metric_map`** — the four renames above (targets emitted verbatim under `metrics.`).
- **`label_rewrites` / `label_candidates`** — `instance`→`labels.instance`,
  `datname`/`db`→`labels.datname`, `job`→`labels.job`, `state`→`labels.state`,
  `mode`→`labels.mode`.
- **`controls.field_overrides`** — `Instance`/`instance`→`labels.instance`,
  `Database`/`database`/`datname`→`labels.datname` (both cases, since
  `resolve_control_field` matches the variable name exactly).
- **`plugin.py`** — rewrite `Instance` populate
  `label_values({job="postgres-exporter"}, instance)` → `label_values(pg_up, instance)`
  (the `postgres-exporter` job filter never matches Elastic labels); rewrite
  `Database` populate `label_values(datname)` →
  `label_values(pg_stat_database_numbackends, datname)` (needs a per-db metric
  anchor); drop the `Interval` control if it lands as an inert control.

### Fidelity

- **PERFECT**: rate panels (Transactions, Tuples, Deadlocks, Temp files, I/O
  time, Transaction rate, Query rate), gauge stats (Version, Max/Shared
  buffers, Active clients, Connections by state/db, Locks by state, DB size,
  Replication lag, Numbackends).
- **APPROXIMATE** (PERFECT under native PROMQL, documented delta in ES|QL):
  Shared Buffer Hits, Commit Ratio, Connections used, Average query runtime
  (ratios), PostgreSQL Uptime (`time() - start_time`). Add per-panel ES|QL
  `query_overrides` only where a panel would otherwise `render_error`.

## Validation gates (UI testing)

1. Migrate + upload to Kibana (`prometheus_native`, `--esql-index` = data view).
2. Render audit — 0 `render_error`; any `field_gap`/`data_gap` documented in
   `fidelity_manifest.yaml`.
3. Interaction audit — `Instance` + `Database` controls rewrite panel queries.
4. Side-by-side vs the provisioned Grafana 12485 in a clean Kibana view session.

## Task checklist

- [x] registry.yaml entry (12485, rev 1, sha above)
- [x] pack.yaml + plugin.py + fidelity_manifest.yaml
- [x] offline fixture tests; `typecheck` green (own files `ruff`-clean)
- [x] rig: enable stat_statements + postmaster + extension
- [x] live: migrate + upload + render/interaction audit
- [x] re-validate 14114 on the same rig
- [x] docs: discoveries here + `docs/sources/grafana.md`

## Live validation results (2026-08-31, rig ES 9.5 + Kibana, real postgres_exporter)

**Migration** (`obs-migrate migrate --field-profile prometheus_native --es-url … --upload`):
35 panels — 18 migrated, 17 migrated_with_warnings, **0 requires-manual, 0
not-feasible; verification gate 18 Green / 17 Yellow / 0 Red; 35/35 ES|QL
queries validated; uploaded**. 23 panels native-PROMQL (oracle-verifiable), 12
ES|QL. Curated pack auto-fired on gnetId 12485.

**Render audit** (`render_audit_driver --elements`, headless Chrome vs the live
upload): **all 32 panel elements rendered; 0 render_error, 0 error markers, 0
console/server errors.** Emitted viz types are correct (graphs → `xy`,
singlestats → `metric`, ratios → `gauge`). The `warn` status is benign: the
element audit's per-element chart-kind heuristic (XY panels expose a secondary
metric element) and three duplicate panels inside the collapsed `Database:`
row (they render on expand).

**Interaction**: both controls populate from live ES —
`Instance` → `['.*', 'postgres:5432']`, `Database` → `['.*', 'postgres',
'rigdb', 'template0', 'template1']` — and panels bind the params (`?Instance`
×57, `?Database` ×23), e.g. `max(metrics.pg_replication_lag_seconds{instance=~?Instance})`.

**Visual**: every panel renders real data (Uptime 16.57 min via the postmaster
collector; Query rate 25.5 & Avg runtime 2.45 ms via pg_stat_statements; Total
DB size 62.80 MB via the `_bytes` rename; gauges + xy time-series all correct).
UI polish (2026-09-01): I/O legends are Read/Write; ratio gauges keep chrome
titles; Global KPI strip fills 48 cols; Database section is a hole-free 3+2
KPI grid plus 24+24 graph pairs; Locks by state is a stacked bar; Replication
lag spans the full row.

**14114 re-validation** (same rig, single input): curated pack fired, **6/6
migrated, 6 Green / 0 Red, render audit PASS (6/6 rendered, 0 errors)** — no
regression, no changes needed.

## Discoveries

- The curated Redis rig (`parity-rig/curated/grafana_763_redis_exporter/`) is a
  **shared multi-exporter rig** that already runs a real
  `prometheuscommunity/postgres-exporter:v0.15.0` + a load generator, ingested
  via `redis_scraper.py` into `metrics-postgres.prometheus-default`
  (`prometheus_native`). New postgres packs validate here rather than in a
  throwaway rig; the scraper honours each exporter's `# TYPE`, so counters land
  counter-typed in ES.
- The rig exporter's default collectors do NOT emit
  `pg_postmaster_start_time_seconds` or any `pg_stat_statements_*`. Enabling
  `--collector.postmaster` + `--collector.stat_statements` (with
  `shared_preload_libraries=pg_stat_statements` + `CREATE EXTENSION`) surfaced
  `pg_postmaster_start_time_seconds` (gauge), `pg_stat_statements_calls_total`
  and `pg_stat_statements_seconds_total` (counters, labelled by `datname`,
  `queryid`, `user`).
- `metric_map` targets are emitted **verbatim** (the field-profile prefix is
  not prepended), so map targets must include the `metrics.` prefix; non-mapped
  gauges are emitted bare offline (`pg_stat_activity_count`, not
  `metrics.pg_stat_activity_count`).
