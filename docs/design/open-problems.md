# Open Problems

Known-unsolved issues in the migration engine, with the evidence gathered so far
so the next person does not repeat the investigation. Each entry says how it was
found, what is proven, and what is still unknown.

Everything here is reproducible against the curated rig
(`parity-rig/curated/grafana_763_redis_exporter/docker-compose.yml`, ES on
`:9201`) and the pinned community corpus
(`python scripts/fetch_community_corpus.py --output-dir <dir>`, 69 dashboards).

---

## 1. Negated literal label matchers drop series with an absent label

**Status:** real defect, fix known, not applied.

PromQL has no NULL: a series that does not carry a label behaves as if the label
were `""`. So `label!="x"` and `label!~"x"` both MATCH a series lacking the
label. ES|QL NULL propagates, so `label != "x"` yields NULL there and the row is
dropped.

The parameterized half of this is fixed (`_absent_as_empty` in
`adapters/source/grafana/promql.py` wraps `?param` matchers in
`COALESCE(field, "")`). The literal half is not.

**Why it was not applied:** wrapping the literal branches shifted which filters
count as target-specific in fused panels and produced an invalid shape in the
`k8s-views-global` fixture. Retry after the CASE-shape normaliser is better
understood, and re-run `tests/test_grafana_fixture_structural_gate.py`.

**Evidence:** on the node index, 1006 documents carry `process_open_fds` and none
carry `release`; `release RLIKE ".*"` matched 0, `COALESCE(release, "") RLIKE ".*"`
matched 1006.

---

## 2. Structural oracle does not treat STATS BY keys as defined columns

**Status:** verifier false positive, 5 occurrences on the community corpus.

`STATS x = MAX(x) BY namespace, category, ...` defines `namespace` in its output,
but `EVAL_UNDEFINED_COLUMN` reports `defined columns: ['policy_report_result']`
and flags the following `EVAL namespace = namespace`.

Affects PolicyReport Details (Passed/Failed/Warning/Errored Resources) and
PolicyReports (Failing PolicyRules). The queries themselves are valid.

Two things to fix:
- the checker should count a STATS `BY` grouping key as a defined column;
- the translator emits a degenerate `EVAL namespace = namespace` that the
  following `KEEP` then discards. Harmless, but it is what trips the checker.

---

## 3. Bucket alignment on `$interval`-driven ratio panels

**Status:** 1 FAIL out of 2510 comparisons, unexplained.

Node Exporter Dashboard EN "Disk R/W Time (Reference: less than 100ms)(beta)":
`rate(node_disk_read_time_seconds_total[$interval]) / rate(node_disk_reads_completed_total[$interval])`.

Both sides return exactly one series, but no time bucket overlaps, so it scores
FAIL rather than a numeric difference. Left as a FAIL deliberately — absorbing it
into a lenient verdict is how the control-binding bug (below) stayed hidden.

Unknown whether the cause is `$interval` resolving differently on the two sides,
or `TBUCKET` vs the oracle's `step` disagreeing at this granularity.

---

## 4. Counters that Prometheus exports as `untyped`

**Status:** working as designed; documented here because it looks like a bug.

`rate()` is counter-only in PromQL, so the source asserts the field is a counter.
Real exporters emit many counters as `# TYPE untyped` — node_exporter does for
`node_netstat_*` and `node_vmstat_*`. Elasticsearch then maps them as gauges
(counter detection keys off the `_total` suffix), and `RATE()` on a gauge is
rejected.

The translator deliberately stays source-faithful and warns with the fix
instructions — 190 such warnings on the community corpus. The parity oracle
classifies the resulting failure as `DATA_GAP`, not `ERROR`.

There is no translator-side fix: the ingest mapping has to mark the field as a
counter, or the rule pack has to pin `metric_kinds: <metric>: gauge`. What is
worth considering is whether the CLI should offer to emit that rule-pack pin.

---

## 5. Fusion cannot express a nested aggregation

**Status:** now refuses correctly, but the panel degrades.

`_merge_pretranslated_xy_queries` models a single STATS. A target whose output
column comes from a second STATS (`min(sum(x) by (instance))`) is refused rather
than silently emitted broken. The panel then falls back to a non-fused path,
which is correct but may show fewer merged series than the source intended.

Making the merge carry an outer aggregate would fix the shape properly. Affects
Kubernetes Resource Requests "CPU Cores" and "Memory".

---

## 6. Datadog `key` control is inert

**Status:** data gap, not a defect. Re-check when Datadog-shaped data is seeded.

`labels.key` is absent from the target, so the control renders and is selectable
but filters nothing. Confirmed not a translator bug: 0 of 29 panels bind `?host`
or `?key`, so switching the control type would produce an inert control either
way.

---

## 7. Render audit reports no per-panel detail

**Status:** diagnosis gap in the verifier.

`render_audit_driver --elements` detects page-level error markers
("Unexpected error from Elasticsearch", "verification_exception") and correctly
scores the dashboard `fail`, but returns `panels: []`. There is no attribution,
so a failing dashboard says *that* something broke, never *which panel*.

Working around it means re-executing every panel query by hand against `_query`
to find the culprit — which is how the logs-panel bug was localised. Populating
per-panel results would make the audit self-sufficient.

---

## 8. Datadog `--field-profile prometheus` vs `prometheus_native`

**Status:** sharp edge, no error until every panel fails.

Two different target layouts share a confusingly similar name:

| profile | emits |
|---|---|
| `prometheus` | `prometheus.metrics.<m>` / `prometheus.labels.<l>` (Elastic Agent integration) |
| `prometheus_native` | `metrics.<m>` / `labels.<l>` |

Picking the wrong one migrates and uploads with a clean report, and then every
panel fails in Kibana with "Unknown column". Field discovery has the live
mapping and could detect the mismatch, but does not warn.

Worth considering: fail (or warn loudly) when the chosen profile's field prefix
is absent from the discovered index.

---

## Panel-type review — current state

Verified end to end against the live rig (migrate → upload → execute every query
→ browser render audit), not by reading routing tables.

**Grafana — 16/16 supported panel types render clean.** Canary:
`infra/grafana/dashboards/kitchen-sink-canary.json`. One bug found and fixed:
`logs` panels on a Prometheus datasource were routed as LogQL.

**Datadog — 22/22 non-log widget types render clean.** Canary:
`infra/datadog/dashboards/kitchen-sink-canary.json`. The 2 log widgets
(`log_stream`, `list_stream`) fail only because the rig has no `logs-*` index —
confirmed absent via `_resolve/index`, not a defect.

Every lossy conversion is disclosed with a reason rather than applied silently,
which is the behaviour to preserve:

| widget | becomes | disclosed as |
|---|---|---|
| `geomap` | markdown placeholder | `requires_manual` — needs Kibana Maps |
| `check_status` / `manage_status` | markdown placeholder | `requires_manual` — Synthetics/Alerts |
| `hostmap` | table | warning — "data-preserving ES|QL table" |
| `scatterplot` | XY | warning |
| `change` | table | warning — "comparison shift" |
| `bar_chart` / `toplist` | table | deliberate: same grouped-scalar data shape |
| `group` | (structural) | skipped |

Not yet reviewed to this depth: Grafana alert rules, and Datadog widgets driven
by log/APM/RUM data sources rather than metrics.

---

## Fixed this cycle — kept as cautionary notes

These are resolved. They are recorded because each one looked like something
other than what it was.

- **Oracle control bindings were merged across dashboards.** `job` from the
  PostgreSQL pack (`postgres`) overwrote Node Exporter Full's (`.*`), so those
  panels were filtered by another dashboard's default, matched nothing, and were
  reported as translation FAILs. Four corpus FAILs, one of which compared at 0.0
  relative error once fixed — the translation had been exact all along.
- **Per-target filters were ANDed across a fused panel.** No document carries
  every target's metric, so the query matched nothing and the panel rendered
  *empty rather than erroring* — nothing to notice, nothing to grep.
- **The comparison window ignored available data.** 174 of 356 panels reported
  "no overlapping time buckets" purely because the window reached past the data.
  Indistinguishable at a glance from a mass translator failure.
- **The CASE-shape normaliser recreated the shape it exists to remove.** A comma
  inside a CASE condition (`COALESCE(label, "")`) hid the outer shape from a
  `[^,]+` regex.

The through-line: every one of these produced a *plausible* result rather than an
error. A gate whose failures are not trustworthy gets ignored, so prefer
refusing, warning, or classifying honestly over emitting something that looks
like it worked.
