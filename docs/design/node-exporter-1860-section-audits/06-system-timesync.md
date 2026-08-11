# Section audit 06 — System Timesync

**Source:** 4 nested panels under collapsed Grafana row `System Timesync`  
**Kibana:** section present (`collapsed: true`)  
**Evidence:** `06-system-timesync.validation.json`

---

## Section verdict

| # | Panel | Grafana → Kibana | Live | Fidelity |
|---|-------|------------------|------|----------|
| 1 | Time Synchronized Drift | 3 gauges (s) → area, duration/seconds | Yes | **Good** |
| 2 | Time PLL Adjust | loop time constant → area | Yes | **Good** |
| 3 | Time Synchronized Status | sync status + freq adj → area | Yes | **Good** |
| 4 | Time Misc | tick + TAI offset (s) → area | Yes | **Good** |

All generic `LAST_OVER_TIME` translations; stacking `none` → `area`; target counts match.

---

## Details

| Panel | Grafana metrics | Kibana fields |
|-------|-----------------|---------------|
| Drift | estimated_error, offset, maxerror seconds | same three named series |
| PLL Adjust | `node_timex_loop_time_constant` | aliased with legend “Phase-locked loop time adjust” |
| Status | sync_status (0/1), frequency_adjustment_ratio | both present |
| Misc | tick_seconds, tai_offset_seconds | both present |

No neg-Y overrides. No curated pack entries. Live `_query` returned rows for all four.

---

## Fixes

None required.

---

## Next section

**07 — System Processes**
