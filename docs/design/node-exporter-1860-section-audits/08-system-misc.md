# Section audit 08 — System Misc

**Dashboard:** Grafana community 1860 *Node Exporter Full* → Kibana `obs-migrate-node-exporter-full`  
**Source JSON:** `/tmp/node-exporter-input.ClIz2R/node-exporter-full.json`  
**Live data:** ES `http://localhost:9201`, index `metrics-node.prometheus-default`, controls `job=node_exporter`, `node=node:9100`, range `now-15m`  
**Evidence dump:** `08-system-misc.validation.json`

---

## Section verdict

| # | Panel | Grafana → Kibana | Live | Fidelity |
|---|-------|------------------|------|----------|
| 1 | Context Switches / Interrupts | 2 irate → area | Yes | **Good** |
| 2 | System Load | load1/5/15 → area | Yes | **Good** |
| 3 | CPU Frequency Scaling | per-cpu + Max/Min → line (curated) | Yes | **Good** after fix |
| 4 | Pressure Stall Information | 5 rate → area (`percentunit`) | Yes | **Good** |
| 5 | Interrupts Detail | irate by type/info → area | Yes | **Good** |
| 6 | Schedule timeslices executed by each cpu | irate by cpu → area | Yes | **Good** |
| 7 | Entropy | gauge → area | Yes | **Good** |
| 8 | CPU time spent in user and system contexts | process_cpu irate → area | Yes | **Good** |
| 9 | File Descriptors | max + open fds → area | Yes | **Good** |

---

## CPU Frequency Scaling (fixed)

**Gap:** Pack averaged `node_cpu_scaling_frequency_hertz` across all CPUs into a single `"CPU"` series, while Grafana legends `CPU {{ cpu }}` (one line per CPU) plus `avg(…_max/min_hertz)` reference lines.

**Fix (pack):** `STATS … BY time_bucket, labels.cpu`, legend `CPU {cpu}`, and emit Max/Min only when `labels.cpu == "0"` so reference lines are not duplicated N times.

**Validated after remigrate/upload:** live series include `CPU 0`–`CPU 3`, `Max`, `Min` with distinct per-cpu hertz values.

---

## Notes

- Pressure Stall `rate(..._seconds_total)` under `percentunit` yields ~0–1 fractions (live ~0.001–0.08); matches Grafana unit semantics.
- No other pack overrides in this section besides CPU Frequency Scaling.

---

## Fixes

- `pack.yaml` — CPU Frequency Scaling per-cpu + single Max/Min (remigrated & uploaded).

---

## Next section

**09 — Hardware Misc**
