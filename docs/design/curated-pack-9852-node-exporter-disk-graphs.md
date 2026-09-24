# Curated pack — Grafana 9852 "node-exporter disk graphs"

- **gnetId / revision**: 9852 / 1
- **Dashboard title**: node-exporter disk graphs (stians-disk-graphs)
- **Canonical sha256**: `273ed2e26432d4d3aca9f008ef589007a1fcf5d09b101528924b755bcfda65a7`
- **URL**: https://grafana.com/grafana/dashboards/9852-stians-disk-graphs/
- **Datasource**: Prometheus
- **Schema**: v16, 15 panels = 2 rows (`Memory` id 19, `Disk` id 15) + 13 leaf `graph` panels
- **Metric family**: `node_exporter` — memory gauges, disk counters, cpu counters, `node_vmstat_oom_kill`
- **Variables**: `$Node` (multi), `$CPU` (multi), `$Disk` (multi), `$RateInterval` (interval, dropped)

## Goal

Auto-fire a polished Kibana replica of this ~5M-download node-exporter dashboard on `gnetId=9852` with no `--rules-file` from the operator. Specifically:

- Pin 9 counter metrics and 8 gauges (`node_vmstat_oom_kill` among them; node_exporter leaves that metric untyped) so the engine uses correct rate semantics.
- Map `$Node` → `instance`, `$CPU` → `cpu`, `$Disk` → `device` so the populate queries and panel filters resolve to real ES fields.
- Name the Disk IO series `Weighted IO time` / `Write time` / `Read time` per device (the engine's fusion produces these names but groups by `device + instance`).
- Clean up the four rate-ratio panels (`Write size`, `Write latency`, `Read size`, `Read latency`) to produce named columns (`Write size`, `Write latency`, etc.) grouped per device.

## Baseline (no pack, --no-curated-packs)

Migration produces 6 `migrated` and 7 `migrated_with_warnings`:

| Panel | Status | Issue |
|---|---|---|
| Memory (21) | migrated, 0.85 | — |
| Memory write cache (23) | migrated, 0.85 | — |
| OOM killed procs (25) | migrated_with_warnings, 0.85 | node_exporter declares node_vmstat_oom_kill `# TYPE ... untyped` (Prometheus type "unknown"); source irate() on it is not valid against an ES|QL non-counter field |
| IO Wait per core (2) | migrated_with_warnings, 0.6 | #355 two-layer split (per-cpu + global Min/Avg/Max); composite breakdown column |
| Disk active time (17) | migrated, 0.9 | — |
| Disk IO (4) | migrated_with_warnings, 0.6 | #354 multi-target fusion; breakdown is `device / instance` CONCAT |
| IOPS (13) | migrated, 0.9 | — |
| Write bandwidth (9) | migrated, 0.9 | — |
| Write size (11) | migrated_with_warnings, 0.6 | #376 same-bucket ratio; output column `computed_value`; breakdown is `instance / device` |
| Write latency (6) | migrated_with_warnings, 0.6 | Same as Write size |
| Read bandwidth (26) | migrated, 0.9 | — |
| Read size (27) | migrated_with_warnings, 0.6 | Same as Write size |
| Read latency (28) | migrated_with_warnings, 0.6 | Same as Write latency |

## Engine vs pack split

The pack exclusively repairs pack-addressable concerns. Engine bugs found while curating go to elastic/obs-integration-team#1201.

**Engine handles (no pack action):**
- `irate` / `rate` counter evaluation (#irate/#rate)
- Multi-select `MV_COUNT + MV_CONTAINS` binds for `$Node`, `$CPU`, `$Disk`
- IO Wait per core two-layer split (#355)
- Element-wise `rate(A)/rate(B)` → per-device `STATS+EVAL` (#376/PR#438)
- Disk IO three-metric fusion with suppressed composite legend (#354)
- `$RateInterval` interval-variable disclosure (#356)

**Pack handles:**
- `metric_kinds`: 9 counter pins, plus `node_vmstat_oom_kill: gauge` (the exporter leaves it untyped, so `RATE`/`IRATE` is invalid against it in ES|QL)
- `controls.field_overrides`: Node→instance, CPU→cpu, Disk→device
- `Disk IO` override: named series + per-device only breakdown (not device+instance)
- `Write size`, `Write latency`, `Read size`, `Read latency` overrides: named output column + per-device grouping

## Fidelity

- **PERFECT** (6 panels): Memory, Memory write cache, Disk active time, IOPS, Write bandwidth, Read bandwidth
- **APPROXIMATE** (7 panels): OOM killed procs (untyped source metric degrades `irate()` to its gauge analogue), IO Wait per core (#355 two-layer), Disk IO (per-device-only grouping), Write size / Write latency / Read size / Read latency (#376 same-bucket ratio)

## Known limitation on OTel-mapped targets

On a target whose schema profile is **not** Prometheus — e.g. an OTel collector
`prometheusreceiver` → `elasticsearch` exporter stream with `mapping.mode: otel`
— four panels the engine routes to native PROMQL (Disk active time, IOPS,
Write bandwidth, Read bandwidth) render empty on the default command. OOM
killed procs stays on ES|QL: the gauge pin disagrees with the source `irate()`,
so the engine degrades it instead of emitting native PROMQL. The native PROMQL
path emits bare Prometheus label names, so `instance=~"<value>"` matches
nothing where the target exposes that dimension as `service.instance.id`. It
fails silently: HTTP 200, zero rows, no error.

This is an engine gap, not a pack gap — the ES|QL path namespaces the same
labels correctly. Until it is fixed, migrate this dashboard with
`--translation-mode esql`, which renders all 13 panels.

Verified with that flag against the `infra/` stack: all 13 panels match
Grafana's series composition, gauges agree to 0.0–0.1%, and the main rate series
agree to 0.3–1.0% at the dashboard's default 15m range. Rate panels read
progressively low at wider ranges (≈-15% at 30m) because rate bucket width is
derived from a fixed bucket count; that is tracked separately and affects every
migrated dashboard's rate panels, not just this one.

## Validation

The running `infra/` stack indexes all required metrics (`metrics.node_disk_*`, `metrics.node_memory_*`, `metrics.node_cpu_seconds_total`, `metrics.node_vmstat_oom_kill`) with labels under `attributes.{device,cpu}`. No additional docker rig is needed.

Gate stack:
1. `make test` — offline: curated pack tests + field-profile portability + layout invariants
2. `scripts/run_cross_profile_corpus.py` — zero ES|QL leakage across all 5 profiles
3. `grafana-migrate --no-curated-packs` vs with-pack, both translation modes, `--es-url http://localhost:19200`
4. `verifier.live_validate` — runtime ES|QL oracle
5. `verifier.dashboards_api` — typed Kibana UI contract
6. `scripts/run_render_audit_local.sh` with `INPUT_DIR` / `ES_URL` / `KIBANA_URL` overrides
7. Browser side-by-side vs Grafana (view mode, clean state)
8. `scripts/verify_curated_pack_pins.py --gnet-id 9852` — provenance pin
