# Section audit 04 — Memory Meminfo

**Dashboard:** Grafana community 1860 *Node Exporter Full* → Kibana `obs-migrate-node-exporter-full`  
**Source:** 15 nested panels under collapsed Grafana row `Memory Meminfo`  
**Kibana:** section open (`layout_overrides` / pack collapsed:false)  
**Evidence:** `04-memory-meminfo.validation.json`  
**Controls:** `job=node_exporter`, `node=node:9100`, `now-15m`

No curated pack overrides in this section — all generic gauge/`LAST_OVER_TIME` timeseries translations.

---

## Section verdict

| # | Panel | Grafana → Kibana | Live | UI | Fidelity |
|---|-------|------------------|------|-----|----------|
| 1 | Memory Active / Inactive | stacked bytes → `area_stacked` | Yes | Yes | **Good** |
| 2 | Memory Committed | unstacked → `area` | Yes | Yes | **Good** |
| 3 | Memory Active / Inactive Detail | stacked → `area_stacked` | Yes | Yes | **Good** |
| 4 | Memory Writeback and Dirty | unstacked → `area` | Yes | Yes | **Good** |
| 5 | Memory Shared and Mapped | unstacked → `area` | Yes | Yes | **Good** |
| 6 | Memory Slab | stacked → `area_stacked` | Yes | Yes | **Good** |
| 7 | Memory Vmalloc | unstacked → `area` | Yes (0) | Yes | **Good** |
| 8 | Memory Bounce | unstacked → `area` | Yes (0) | Yes | **Good** — legend shows instance only (see notes) |
| 9 | Memory Anonymous | unstacked → `area` | Yes | Yes | **Good** — see neg-Y note |
| 10 | Memory Kernel / CPU | unstacked → `area` | Yes | Yes | **Good** |
| 11 | Memory HugePages Counter | unstacked, unit `short` → number | Yes (0) | Yes | **Good** |
| 12 | Memory HugePages Size | unstacked bytes → bytes | Yes (0) | Yes | **Good** |
| 13 | Memory DirectMap | unstacked → `area` | Yes | Yes | **Good** |
| 14 | Memory Unevictable and MLocked | unstacked → `area` | Yes (0) | Yes | **Good** |
| 15 | Memory NFS | unstacked → `area` | Yes (0) | Yes | **Good** — same single-series legend note |

**Stacking parity:** all 15 match Grafana `normal` → `area_stacked` vs `none` → `area`.

---

## Fixes applied

None required for translator bugs in this section. Live ES|QL returned rows for every panel; stacking/units/series counts match Grafana.

---

## Cross-cutting notes (validated)

### `TBUCKET(100)` + `LAST_OVER_TIME`
Appropriate here (gauges, not IRATE). No short-window IRATE empty-panel risk.

### Instance breakdown in legends
Queries group `BY … labels.instance, labels.job`. With one selected host, multi-series legends look like `node:9100 - Active - …`. Single-metric panels (**Bounce**, **NFS**) often show only `node:9100` in the Lens legend even though the measure column still carries the Grafana legend string as `label`. Cosmetic; same pattern as Memory Stack in section 3.

### Memory Anonymous “neg-Y”
Grafana panel includes a shared override `/.*Inactive *./` → `negative-Y`, but this panel’s series are **AnonHugePages** / **AnonPages** — the regex matches nothing. Kibana correctly emits **no** negation. Treating “panel has a neg-Y override” as a miss would be a false positive.

### HugePages Counter unit
Grafana `unit: short` (page counts) → Kibana `number` compact — correct, not bytes.

### Zero-valued series
VmallocUsed/Chunk, Bounce, HugePages_*, Unevictable/MLocked, NFS Unstable are **0** on this lab host. Charts still render; not empty-query failures.

---

## Panel formula checks (sample)

All panels are direct `node_memory_*` gauges (or simple aliases) with `AVG(LAST_OVER_TIME(metrics.…))` — no curated rewrite. Spot-checks:

| Panel | Grafana targets | Kibana metrics (count) |
|-------|-----------------|------------------------|
| Active / Inactive | Active, Inactive | 2 |
| Active / Inactive Detail | Active/Inactive anon+file | 4 |
| Committed | Committed_AS, CommitLimit | 2 |
| Writeback and Dirty | Writeback, WritebackTmp, Dirty | 3 |
| Shared and Mapped | Mapped, Shmem, ShmemHugePages, ShmemPmdMapped | 4 |
| Slab | SUnreclaim, SReclaimable | 2 |
| Anonymous | AnonHugePages, AnonPages | 2 |
| Bounce | Bounce | 1 (`metrics.node_memory_Bounce_bytes`) |
| NFS | NFS_Unstable | 1 (`metrics.node_memory_NFS_Unstable_bytes`) |

Field caps include both `metrics.node_memory_*` and bare aliases for Bounce/NFS; queries use the `metrics.` form.

---

## UI (Playwright)

Section expanded; all 15 titles present. Representative legends include Active/Inactive, Committed_AS/CommitLimit, Slab SUnreclaim/SReclaimable, AnonHugePages/AnonPages, DirectMap1G/2M/4k, HugePages Free/Rsvd/Surp.

---

## Sources checked

| Artifact | Role |
|----------|------|
| Grafana row `Memory Meminfo` children | Exprs, stack, units, overrides |
| Kibana SO section panels | Series type, formats, queries |
| ES field caps + `_query` | Metric presence + non-empty rows |
| Playwright exact titles | Render + legends |

---

## Next section

**05 — Memory Vmstat** (4 nested panels).
