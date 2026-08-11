# Section audit 05 — Memory Vmstat

**Source:** 4 nested panels under collapsed Grafana row `Memory Vmstat`  
**Kibana:** section present (`collapsed: true` in SO; panels still uploaded)  
**Evidence:** `05-memory-vmstat.validation.json`

---

## Section verdict

| # | Panel | Grafana → Kibana | Live | Fidelity |
|---|-------|------------------|------|----------|
| 1 | Memory Pages In / Out | irate in/out, neg-Y `*out*` → area + Pagesout negated | Yes | **Good** |
| 2 | Memory Pages Swap In / Out | irate pswpin/out, neg-Y out → Pswpout negated | Yes | **Good** |
| 3 | Memory Page Faults | stacked maj/min + unstacked Pgfault overlay | Yes | **Good** — 3 series; Pgfault on `line_1` overlay |
| 4 | OOM Killer | irate oom_kill → area | Yes | **Good** |

---

## Details

### Pages In / Out & Swap In / Out
Grafana `/.*out/` → `negative-Y`. Kibana emits `EVAL …out… = (-1 * …)`. Legends and signs validated.

### Memory Page Faults
Grafana targets:
- **Pgfault** (total) — override `stack: false` (outline)
- **Pgmajfault** — stacked
- **Pgminfault** — `pgfault - pgmajfault`, stacked

Kibana query computes all three; IR marks Pgfault `stack: false`. Lens has `area_stacked` (maj+min) + `line` (Pgfault).  
**Note:** Counting metrics from only the first datasource layer undercounts (2); overlay lives on `line_1`.

### OOM Killer
Single irate series; label `oom killer invocations`. Fine.

### Buckets
All use `TBUCKET(100)` + `IRATE`. Same short-window caveat as other IRATE charts; panels returned data at 15m here. Optional later: `TBUCKET(20)` if gaps appear.

---

## Fixes

None required.

---

## Next

**06 — System Timesync** (written in parallel).
