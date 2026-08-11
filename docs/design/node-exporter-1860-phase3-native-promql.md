# Phase 3 — Native multi-target PROMQL (decision)

**Date:** 2026-08-07  
**Dashboard context:** Grafana 1860 Node Exporter Full curation  
**Lab target:** Elasticsearch `9.5.0-SNAPSHOT` (`localhost:9201`), index
`metrics-node.prometheus-default`

## Verdict

**Keep multi-target native PROMQL disabled.** Continue using the established
ES|QL `TS` fusion / curated pack path for multi-target XY panels.

Enabling
`_translate_multi_target_native_promql` would emit queries the target cannot
run correctly today.

## What the helper needs

[`panels._translate_multi_target_native_promql`](../../observability_migration/adapters/source/grafana/panels.py)
builds one native command by wrapping each target:

```text
label_replace(<expr_A>, "__series", "<legendA>", "", "")
  or
label_replace(<expr_B>, "__series", "<legendB>", "", "")
```

Lens then breaks down on `__series`. That design requires:

| Capability | Lab result (2026-08-07) |
|------------|-------------------------|
| `label_replace(...)` | **Fail** — `Function [label_replace] is not yet implemented` |
| Bare structural `or` (no modifiers) | **Parses / runs** |
| `or on()` / group modifiers | **Fail** — not supported |
| `and` / `unless` | **Fail** — set operators not supported |
| Adaptive `start`/`end`/`buckets` PROMQL | **OK** (ISO timestamps) |
| Single-target `rate(metrics.*)` | **OK** |

Elastic docs (PromQL functions, “Not yet supported”) still list
`label_replace` among unimplemented functions (docs refreshed 2026-08-05).

## Why bare `or` is not enough

Mirrored Node Exporter panels (Receive/Transmit, Read/Write) share the same
non-name labels (`device`, `instance`, `job`, …). After `rate()`, `__name__`
is gone from the PROMQL result labels.

Probe on this host (`eth0`, 10m, `step=1m`):

```text
rate(...receive...{device="eth0"}[5m])
  or
rate(...transmit...{device="eth0"}[5m])
→ 1 unique series (labels collapse)
```

Without a distinguishing label (`__series` via `label_replace`, or equivalent),
`or` **drops or overwrites** one of the two series. Enabling a
`label_replace`-free combiner would silently lose fidelity vs Grafana.

## Secondary gate (even after `label_replace`)

Shortlist panels bind `$node` / `$job` in matchers. The helper also requires
`_kibana_binds_promql_control_params` (preferred for offline migration and
verified when `--kibana-url` reports Kibana >= 9.5) before emitting native
queries with template variables. A verified older Kibana forces ES|QL; ES
alone cannot prove Kibana forwards those params into inner PROMQL.

## Shortlist disposition

| Panel | Keep on |
|-------|---------|
| Network Traffic (by Packets / Errors / …) | ES\|QL `TS` (neg-Y + device) |
| Disk IOps / R/W Data / … | ES\|QL `TS` |
| I/O Usage Read / Write | ES\|QL `TS` |
| Network Operational Status | Curated `TS` unpivot |
| Sockstat TCP / UDP | ES\|QL `TS` |
| TCP Stat | ES\|QL `TS` |
| Pressure Stall Information | ES\|QL `TS` |

These were audited live under section reports 05–15; ES|QL paths are correct.

## Unblock criteria (revisit when **all** are true)

1. ES PromQL implements `label_replace` (or another way to inject a constant
   series label without group modifiers).
2. Live `_query` accepts the helper’s exact
   `label_replace(...) or label_replace(...)` shape and returns **N** distinct
   `__series` (or equivalent) values for an N-target mirrored panel.
3. Kibana control-param binding for PROMQL matchers is verified for the
   operator path (or panels are rewritten without `$var` matchers).
4. Tests in `tests/test_grafana_extended.py`
   (`test_multi_target_overlay_is_windowless_and_stepless`) flip from
   “returns None” to “emits native PROMQL + survives live validate”.

## Probe snippets (lab)

```esql
PROMQL index=metrics-node.prometheus-default step=1m start="<iso>" end="<iso>"
  value=(label_replace(rate(metrics.node_network_receive_packets_total{instance=~"node:9100"}[5m]), "__series", "Receive", "", ""))
-- → Function [label_replace] is not yet implemented

PROMQL index=metrics-node.prometheus-default step=1m start="<iso>" end="<iso>"
  value=(rate(metrics.node_network_receive_packets_total{instance=~"node:9100",device="eth0"}[5m])
     or rate(metrics.node_network_transmit_packets_total{instance=~"node:9100",device="eth0"}[5m]))
-- → 1 series (collapse)
```

## Code stance

Leave the early `return None` in `_translate_multi_target_native_promql`
(comment dated with this probe). Do not ship a partial `or`-only combiner.
