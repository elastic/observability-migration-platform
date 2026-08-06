# Section audit 09 — Hardware Misc

**Dashboard:** Grafana community 1860 *Node Exporter Full* → Kibana `obs-migrate-node-exporter-full`  
**Source JSON:** `/tmp/node-exporter-input.ClIz2R/node-exporter-full.json`  
**Live data:** ES `http://localhost:9201`, index `metrics-node.prometheus-default`, controls `job=node_exporter`, `node=node:9100`, range `now-15m`  
**Evidence dump:** `09-hardware-misc.validation.json`

---

## Section verdict

| # | Panel | Grafana → Kibana | Live | Fidelity |
|---|-------|------------------|------|----------|
| 1 | Hardware temperature monitor | temp + Critical by chip/sensor → area | Yes | **Good** |
| 2 | Throttle cooling device | current + max by name/type → area | Yes | **Good** |
| 3 | Power supply | online by power_supply → area | Yes | **Good** |

---

## Details

### Hardware temperature monitor

- Grafana: visible targets are temp + Critical (alarm/max/hyst targets are `hide: true`).
- Kibana: `LAST_OVER_TIME` on `node_hwmon_temp_celsius` + `node_hwmon_temp_crit_celsius`, `BY labels.chip_name, labels.sensor`, `splitAccessors` on `legend`.
- Live: temps ~46–55°C, Critical 90°C for `coretemp` / `nct6775`.
- Grafana joins `node_hwmon_chip_names` for `chip_name`; this scrape already carries `labels.chip_name`, so the join is unnecessary here.

### Throttle cooling device

- Current + Max state by `name`/`type`; live `thermal_zone*` Processor states 0–1 current, max 4.

### Power supply

- `node_power_supply_online` by `power_supply`; live `AC0`/`BAT0` 0/1.

---

## Audit false positives

Naive target-vs-column counts flagged 2→3 because the `legend` breakdown column was counted as a metric. Real metric count matches Grafana’s two visible series.

---

## Fixes

None required.

---

## Next section

**10 — Systemd**
