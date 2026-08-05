# Node Exporter 1860 Curation Workplan

**Date:** 2026-08-05
**Status:** In progress
**Dashboard:** Grafana community dashboard 1860, "Node Exporter Full"

## Goal

Move the bundled Node Exporter 1860 migration from "uploads cleanly and is broadly useful"
to "curated for Kibana, operationally strong, and at least as useful as the source."

This document records the current evidence, the remaining gaps, and the order of work.

## Current live baseline

Current best verified live run on Wednesday, August 5, 2026:

- Path: [`/tmp/node-exporter-1860-recheck3.cbySd2/dashboards`](/tmp/node-exporter-1860-recheck3.cbySd2/dashboards)
- Target:
  - Kibana `9.5.0-SNAPSHOT`
  - Elasticsearch local no-auth stack
  - `metrics-node.prometheus-default`
  - schema profile `prometheus_native`
- Summary:
  - `117` renderable panels
  - `114` `migrated`
  - `3` `migrated_with_warnings`
  - `0` `requires_manual`
  - `0` `not_feasible`
  - `31` native `PROMQL`
  - `84` ES|QL `TS`
  - `1` ES|QL `FROM`
  - upload succeeded
  - `0` runtime error panels
  - `0` empty panels
  - `0` browser-audit visible errors

Remaining yellow panels in the live target are all data-readiness gaps:

- `Memory Stack`
  - missing `node_memory_HardwareCorrupted_bytes`
- `TCP Errors`
  - missing `node_netstat_TcpExt_TCPRcvQDrop`
- `TCP Connections`
  - missing `node_netstat_Tcp_MaxConn`

## Historical evidence

Two recent artifact bundles were reviewed from Tuesday, August 4, 2026:

1. **Best translation bundle**
   - Path: [`/tmp/node-promql-optin-upload-v2/dashboards`](/tmp/node-promql-optin-upload-v2/dashboards)
   - Summary:
     - 117 migrated panels
     - 62 `migrated`
     - 55 `migrated_with_warnings`
     - 0 `requires_manual`
     - 0 `not_feasible`
     - 33 native `PROMQL`, 84 ES|QL
     - verification packets: 45 green / 88 yellow / 0 red
   - Target:
     - `metrics-node.prometheus-default`
     - schema profile `prometheus_native`
     - 494 discovered fields

2. **Best upload / browser-smoke bundle**
   - Path: [`/tmp/grafana-node-fresh-v9-live/dashboards`](/tmp/grafana-node-fresh-v9-live/dashboards)
   - Summary:
     - upload succeeded
     - 0 dropped panels
     - 0 runtime query errors
     - 0 layout issues
     - 4 empty panels at runtime

Important limitation: these are not the same run. Translation quality improved after
the best smoke run, so a fresh upload/smoke pass is still needed after the next curations.

## Progress update

Completed on 2026-08-05:

- Added curated query overrides for `RAM Used`, `SWAP Used`, and `Uptime`
- Removed their generic arithmetic warnings
- Added pack/integration assertions so the 1860 dashboard test path exercises the curated pack
- Verified those three panels through native dashboard artifacts and live upload checks

Still pending:

- `Pressure` first-row presentation polish
- targeted seeding for the final three live-missing metrics
- multi-target native `PROMQL` revisit once the target runtime contract is clearer

## Current state

What is already good:

- The dashboard migrates end to end with no manual rebuilds.
- No panels are rejected by the Dashboards API.
- No red verification packets are present in the current live run.
- The pack already fixes several correctness issues:
  - suffix-less node/netstat counter typing
  - `Processes Memory`
  - `Sys Load`
  - `Root FS Used`
  - `RootFS Total`
  - `CPU Basic`
  - `Memory Basic`
  - `Network Traffic Basic`
  - `Disk Space Used Basic`

What is still imperfect:

- **3 warning panels** remain in the current live run, all due to live-missing metrics.
- The `$node` control still cannot preserve Grafana's `$job`-scoped control dependency.
- The query-path split is still mixed:
  - `31` native `PROMQL`
  - `84` ES|QL `TS`
  - `1` ES|QL `FROM`
- The remaining strategic question is not correctness, but whether more of the
  current `TS` set should move to native multi-target `PROMQL`.

## Main gap classes

### 1. Avoidable arithmetic warnings on high-visibility summary panels

These panels should be made exact and warning-free in the curated pack:

- `RAM Used`
- `SWAP Used`
- `Uptime`

Observed warning reasons:

- `Approximated PromQL arithmetic using same-bucket ES|QL math`
- source series collapsed before arithmetic
- labels not retained even though the panels are single-value summaries

For Kibana, these are better as explicit curated single-value queries than as generic fallback
expressions.

### 2. First-row polish is still weaker than source

The source first row is a compact operational summary. The Kibana result still has one weak point:

- `Pressure` is translated as a bar panel with warning `Approximated bargauge as bar chart`

This is functional, but not the best Kibana presentation. The end state should be a curated
first-viewport summary row with consistent "status tile / gauge / compact trend" semantics.

### 3. Query-path revisit: native `PROMQL` vs `TS`

Most renderable `TS` panels are now correct, but they are not all equal.

#### Keep on `FROM`

- `CPU Cores`

This panel is a single-value distinct-count tile:

```text
FROM metrics-node.prometheus-default
| STATS cpu_cores = COUNT_DISTINCT(labels.cpu)
```

This is exact and low-risk; there is no value in forcing it to `TS` or native
`PROMQL`.

#### `TS` panels blocked mainly by multi-target native `PROMQL`

These panels have multiple visible source targets and every target is
individually native-`PROMQL` compatible today, but the panel still stays on
`TS` because the translator's multi-target native path is disabled.

High-value shortlist:

- `Network Traffic`
- `Disk IOps`
- `I/O Usage Read / Write`
- `Memory Page Faults`
- `Systemd Units State`
- `Network Operational Status`
- `TCP Stat`
- `Sockstat TCP`
- `Sockstat UDP`
- `Pressure Stall Information`

Broader same-class examples:

- most mirrored in/out, read/write, receive/transmit charts
- most multi-series memory detail charts
- several netstat / sockstat families

#### `TS` panels that should probably stay `TS`

- `RAM Total`
- `SWAP Total`
- `Memory Stack`
- `CPU spent seconds in guests (VMs)`
- `Hardware temperature monitor`

These depend on one or more of:

- window reduction to a single stable dashboard value
- fused arithmetic
- join semantics
- composite legend / overlay shaping

Even if native multi-target `PROMQL` improves, these are not obvious wins to
move.

#### Runtime blocker status

The translator's multi-target native path in
[`observability_migration/adapters/source/grafana/panels.py`](../../observability_migration/adapters/source/grafana/panels.py)
is still hard-disabled because it relies on `label_replace()` for per-target
series labeling.

As of Wednesday, August 5, 2026:

- the local `9.5.0-SNAPSHOT` target still does not provide a clean runtime
  contract for this path
- direct `_query` probes against `PROMQL ... value=(label_replace(...))` and a
  simple `or` expression currently return a parser-side `500`
  `null_pointer_exception`, so this needs target/runtime clarification before
  enabling the path in the translator

### 4. Advanced panels are structurally fine but operationally weak without telemetry

The four empty panels from the reviewed smoke run look like data-readiness gaps, not translator
syntax failures. They depend on collectors or metrics that are not universally present:

- DirectMap memory metrics
- schedstat panels
- CPU scaling
- schedstat timeslices

These should not pollute the primary operational path. The curated target should either:

- move them into an explicit "Advanced / kernel collectors" section, or
- degrade them to a clearer placeholder / informational presentation when the telemetry is absent

### 5. Decorative / chained control fidelity is still below source

Known warning:

- `$node` is scoped by `$job` in Grafana through `label_values()` selector semantics
- Kibana ES|QL controls cannot express that dependency directly today

This is not a reason to block the dashboard, but it should shape the curation:

- prefer one strong live control over multiple weakly coupled controls
- do not pretend chained control semantics are preserved when they are not

## Work order

### Phase 1: Remove avoidable warning panels

Status: completed on 2026-08-05

1. Curated query overrides for:
   - `RAM Used`
   - `SWAP Used`
   - `Uptime`
2. Promote these panels from `migrated_with_warnings` to `migrated`
3. Update Node Exporter snapshots/tests

Expected effect:

- lower warning count
- better first-row credibility
- fewer "same-bucket arithmetic" approximations in operator-facing summary panels

### Phase 2: Improve first-row Kibana presentation

1. Revisit `Pressure`
2. Decide whether the curated target should remain a compact bar summary or move to a more
   Kibana-native presentation
3. Preserve the dense first-row layout while removing the current visual inconsistency

Expected effect:

- first viewport reads like a deliberate Kibana operations dashboard

### Phase 3: Native multi-target `PROMQL` revisit

1. Keep the current single-target native path as-is.
2. Revisit the disabled multi-target native helper only after the target runtime
   gives a stable answer on:
   - `label_replace()`
   - structural `or` between native series
   - dashboard-context control binding for any rewritten params
3. Start with the shortlist in this order:
   - `Network Traffic`
   - `Disk IOps`
   - `I/O Usage Read / Write`
   - `Network Operational Status`
   - `Sockstat TCP`
   - `TCP Stat`

Expected effect:

- native-`PROMQL` expansion only where it buys real fidelity
- no churn on panels that are already better served by `TS`

### Phase 4: Targeted telemetry seeding for the last three yellows

Seed or fixture only the live-missing metrics:

- `node_memory_HardwareCorrupted_bytes`
- `node_netstat_TcpExt_TCPRcvQDrop`
- `node_netstat_Tcp_MaxConn`

Expected effect:

- `114/3` should become `117/0` if the seeded metrics land in
  `metrics-node.prometheus-default` with the expected labels and types

### Phase 5: Fresh end-to-end verification

After the curation edits:

1. regenerate Node Exporter artifacts
2. upload the curated dashboard
3. rerun smoke / browser QA
4. run compare/native-oracle verification where applicable

Required before calling the dashboard "perfect":

- the best translation bundle and the best upload/smoke bundle must be the same run

## Definition of done

The Node Exporter 1860 curated pack is "done" when:

- summary-row panels are warning-free or intentionally curated with documented tradeoffs
- first-row presentation looks deliberate in Kibana
- the final three data-readiness gaps are either seeded green or explicitly
  documented as absent from the validation target
- the `TS` vs native-`PROMQL` split is explained by current runtime constraints,
  not accidental fallback
- a fresh single run proves:
  - migration succeeds
  - upload succeeds
  - no panel drops
  - smoke/browser QA is clean
  - warning panels, if any, are honest target-data gaps

## Immediate next step

Start with **Phase 4**:

- either get the three live-missing metrics into `metrics-node.prometheus-default`
- or write a minimal targeted fixture path when the generic seeder will not emit
  them into the node stream
- then rerun the live upload/smoke/browser path and confirm whether `114/3`
  becomes `117/0`
