# Node Exporter 1860 Curation Workplan

**Date:** 2026-08-05 (updated 2026-08-07)
**Status:** Curation Phases 1–2, 4–5 complete on lab; Phase 3 blocked on runtime
**Dashboard:** Grafana community dashboard 1860, "Node Exporter Full"

## Section-by-section audit (2026-08-06 → 2026-08-07)

Full row-by-row Grafana vs Kibana reports live under
[`node-exporter-1860-section-audits/`](node-exporter-1860-section-audits/README.md)
(16 sections + validation JSON). Audits drove pack fixes for Pressure,
Memory Basic overlay, IRATE `TBUCKET(20)` on selected panels, and
per-CPU **CPU Frequency Scaling**.

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

Completed through 2026-08-07:

- Phase 1: curated `RAM Used` / `SWAP Used` / `Uptime` (+ pack tests)
- Phase 2: Pressure → metric tiles + first-row polish
- Section-by-section audits (16 rows) with pack fixes (Memory Basic overlay,
  IRATE `TBUCKET(20)`, per-CPU Frequency Scaling, …)
- Phase 4: lab seed of three optional metrics → 117/0 yellows
- Phase 3: re-probed and **deferred** (`label_replace` still missing)
- Phase 5: canonical migrate+upload+smoke
  `/tmp/node-exporter-phase5-20260807-030538`

Still deferred:

- multi-target native `PROMQL` (runtime `label_replace`)
- `$job`-scoped `$node` control dependency
- optional: restore TCPRcvQDrop optionality in curated TCP Errors for hosts
  without the field

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

As of **Thursday, August 7, 2026** (re-probe on lab `9.5.0-SNAPSHOT`):

- `label_replace(...)` still fails:
  `Function [label_replace] is not yet implemented`
- bare structural `or` runs, but Receive/Transmit (same device labels) collapses
  to **one** series — so `or` alone is not a correct multi-target overlay
- `or on()` / `and` / `unless` remain unsupported
- Decision write-up:
  [`node-exporter-1860-phase3-native-promql.md`](node-exporter-1860-phase3-native-promql.md)

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

Status: **completed (2026-08-06/07 section audit + pack fixes)**

1. Revisited `Pressure` — curated as Lens **metric** tiles (`kibana_type_override:
   metric`), unpivot CPU/I/O/Mem, penultimate non-null IRATE collapse, `* 100`
   color domain.
2. First-row gauges/stats remain curated (`RAM Used`, `SWAP Used`, `Uptime`,
   `Sys Load`, `Root FS Used`, `CPU Busy`, …) with documented `/oldroot`
   adaptation where needed.
3. Dense first-row layout preserved (Pressure + gauges + stats).

Expected effect:

- first viewport reads like a deliberate Kibana operations dashboard

Evidence: [`node-exporter-1860-section-audits/01-quick-cpu-mem-disk.md`](node-exporter-1860-section-audits/01-quick-cpu-mem-disk.md)

### Phase 3: Native multi-target `PROMQL` revisit

Status: **blocked / deferred (re-probed 2026-08-07)** — see
[`node-exporter-1860-phase3-native-promql.md`](node-exporter-1860-phase3-native-promql.md).

1. Keep the current single-target native path as-is. *(unchanged)*
2. Revisit the disabled multi-target native helper only after the target runtime
   gives a stable answer on:
   - `label_replace()` — **still unimplemented** on `9.5.0-SNAPSHOT`
   - structural `or` between native series — parses, but **collapses** mirrored
     same-label series without `label_replace`
   - dashboard-context control binding for any rewritten params — secondary gate
3. Start with the shortlist in this order *(when unblocked)*:
   - `Network Traffic`
   - `Disk IOps`
   - `I/O Usage Read / Write`
   - `Network Operational Status`
   - `Sockstat TCP`
   - `TCP Stat`

Expected effect *(when unblocked)*:

- native-`PROMQL` expansion only where it buys real fidelity
- no churn on panels that are already better served by `TS`

**2026-08-07 decision:** do not enable the helper; do not ship an `or`-only
workaround. Shortlist panels remain on audited ES|QL `TS` / curated paths.

### Phase 4: Targeted telemetry seeding for the last three yellows

Status: **completed on lab validation host (2026-08-07)**

Seeded into `metrics-node.prometheus-default` with labels
`instance=node:9100`, `job=node_exporter` (and TSDB `time_series_metric`
mappings):

- `node_memory_HardwareCorrupted_bytes` (gauge)
- `node_netstat_TcpExt_TCPRcvQDrop` (counter)
- `node_netstat_Tcp_MaxConn` (gauge)

Also restored `TCPRcvQDrop` in the curated **TCP Errors** pack query (field
must be present at remigrate time).

Post-seed remigrate (`/tmp/node-exporter-fix-20260807-025842`):

- `117` migrated / `0` warnings
- Verification gate: **117 Green / 0 Yellow / 0 Red**
- Smoke: `0` runtime errors, `0` empty panels

Note: seeding is a **lab validation** step, not an operator CLI requirement.
Real node-exporter scrapes that lack these collectors still degrade via
`live_optional_metrics` on the generic path; curated TCP Errors assumes the
counter exists once included in the pack override.

Expected effect:

- `114/3` → `117/0` on a target that has these fields (achieved on this lab)

### Phase 5: Fresh end-to-end verification

Status: **completed 2026-08-07** — see
[`node-exporter-1860-phase5-verification.md`](node-exporter-1860-phase5-verification.md).

Canonical run (translate **and** upload/smoke in one bundle):

- Path: `/tmp/node-exporter-phase5-20260807-030538`
- `117` migrated / `0` warnings
- Verification: **117 Green / 0 Yellow / 0 Red**
- Smoke: `0` runtime errors, `0` empty panels, `0` layout issues
- Spot-checked Pressure, CPU Busy, Memory Stack, TCP Errors/Connections,
  CPU Frequency Scaling, Network Traffic by Packets via live `_query`

Required before calling the dashboard "perfect":

- the best translation bundle and the best upload/smoke bundle must be the same run
  → **satisfied by this Phase 5 run** (with Phase 3 / control / seed caveats below)

## Definition of done

The Node Exporter 1860 curated pack is "done" when:

- [x] summary-row panels are warning-free or intentionally curated with documented tradeoffs
- [x] first-row presentation looks deliberate in Kibana (Pressure → metric tiles)
- [x] the final three data-readiness gaps are either seeded green or explicitly
  documented as absent from the validation target *(seeded green on this lab)*
- [x] the `TS` vs native-`PROMQL` split is explained by current runtime constraints,
  not accidental fallback *(Phase 3 decision doc)*
- [x] a fresh single run proves migration + upload + no drops + clean smoke
  *(Phase 5 canonical run)*

**Still out of scope / deferred (honest gaps):**

- multi-target native `PROMQL` (blocked on `label_replace`)
- Grafana-style `$job`-scoped `$node` control chaining
- full browser render / interaction nightly gates as part of this workplan pass
- operator environments that lack the three optional metrics (degrade path)

## Immediate next step

Curation workplan Phases 1–2 / 4–5 are complete on the lab target; Phase 3 waits
on Elasticsearch PromQL `label_replace`.

Operator/product follow-ups if desired:

1. Open or track an ES/Kibana dependency for PromQL `label_replace` (Phase 3 unblock).
2. Decide whether curated **TCP Errors** should keep requiring `TCPRcvQDrop` or
   regain optional stripping when the field is absent.
3. PR the pack + design docs from this branch when ready.
