# Field Profile & Schema Resolution — Design Review

**Status:** Reviewed and corrected 2026-07-30. Ready for issue filing.
**Scope:** `--field-profile`, `SchemaResolver`, `FieldMapProfile`, `metric_map`, `auto` detection
**Purpose:** Identify genuine correctness and honesty problems in the field-resolution
design, from operator first principles.

> **Review note.** An earlier draft of this document listed six problems. Three
> of them did not survive verification against the code and a live cluster, and
> one was materially mis-analysed. Those are recorded in §7 (Retracted Claims)
> rather than deleted, so the same wrong conclusions do not get re-derived. Every
> claim below that is marked **[VERIFIED]** was checked by executing code or
> querying a live index, not by reading a doc.

---

## 1. The Operator's Starting Point

The operator was running Grafana (Prometheus data sources) or Datadog. They now
want those assets in Kibana. They do not know what a "field profile" is. The
question `obs-migrate` must answer for them:

> **"Where in Elasticsearch is `redis_uptime_in_seconds` stored, and where is
> `instance` stored?"**

The answer depends on the **Elastic ingestion pipeline**, not on Prometheus or
Grafana. The source tool supplies the logical name; the physical ES field name
comes from how the data was ingested.

---

## 2. Confirmed Ingestion Schemas

### 2A. Grafana + Prometheus → Elastic

**Path 1 — ES native `/_prometheus/api/v1/write` endpoint. [VERIFIED]**

Verified against official docs *and* a live index
(`.ds-metrics-redis.prometheus-default-*`):

```
Metric: metrics.<metric_name>      e.g. metrics.redis_uptime_in_seconds
Label:  labels.<label_name>        e.g. labels.instance
Index:  metrics-{dataset}.prometheus-{namespace}
Type:   _total/_sum/_count/_bucket -> counter; else gauge
```

**Critical detail the first draft missed: `labels` is an Elasticsearch
`passthrough` field. [VERIFIED]** Live mapping:

```json
"labels": { "type": "passthrough", "time_series_dimension": true, "priority": 10 }
```

Consequence: every Prometheus label is queryable **both** as `labels.instance`
**and** as bare `instance`. `_field_caps` reports both as real, searchable,
aggregatable `keyword` dimensions, even though `_source` contains only the
nested `labels.instance` form. `metrics` is *not* passthrough — bare
`redis_uptime_in_seconds` is **not** a queryable field.

This asymmetry (labels resolvable bare, metrics not) is the single most
load-bearing fact in this document.

**Path 2 — Elastic Agent / Fleet Prometheus integration (`use_types=true`).**

```
Metric: prometheus.<metric>.counter | .value | .rate
Label:  prometheus.labels.<label>
```

**Path 3 — Metricbeat prometheus module (`use_types=false`).**

```
Metric: prometheus.metrics.<metric>
Label:  prometheus.labels.<label>
```

**Path 4 — OTel Collector (Prometheus receiver) → ES exporter, `otel` mapping
mode. [VERIFIED against upstream exporter README + EDOT docs]**

```
Metric: metrics.<metric_name>
Label:  resource.attributes.<attr>   -- also queryable bare via passthrough
Index:  metrics-{dataset}.otel-{namespace}   (dataset always gets ".otel")
```

The Prometheus receiver **preserves** Prometheus metric names —
`redis_connected_clients` is not renamed to OTel semantic conventions. Only
OTel *SDK-native* instrumentation uses semconv names (`k8s.pod.cpu.time`), and
those would need `metric_map`.

**Paths 1 and 4 both rely on Elasticsearch passthrough fields for labels.** They
are far more similar than the first draft claimed. The difference is the stored
path (`labels.*` vs `resource.attributes.*`); the *queryable* bare form is the
same in both.

### 2B. Datadog → Elastic

| Path | Metric in ES | Tags |
|---|---|---|
| A: OTel Collector + Datadog receiver | `metrics.<otel_semconv_name>` | resource attributes |
| B: Elastic Agent Datadog integration | dot→underscore, ECS-shaped | ECS fields |
| C: Metricbeat prometheus (rare) | as Grafana Path 3 | `prometheus.labels.*` |

Datadog metric names (`system.cpu.user`) never map directly to ES field names,
so `--metric-map-file` is effectively mandatory for meaningful Datadog coverage.

---

## 3. Current Design

### 3A. Grafana profiles

```
Profile                  Metric emitted                Label emitted
───────────────────────  ────────────────────────────  ───────────────────────
otel (default)           bare if in caps; else         bare if in caps and
                         metrics.X if in caps;         searchable+aggregatable;
                         else bare  (!)                else OTel candidate;
                                                       else OTel candidate (!)
prometheus_remote_write  prometheus.<m>.{counter|      prometheus.labels.<l>
                         value|rate}
prometheus_metrics       prometheus.metrics.<m>        prometheus.labels.<l>
prometheus_native        metrics.<m>                   labels.<l>
passthrough              <m> verbatim                  <l> verbatim
auto (needs --es-url)    detected profile              detected profile
```

### 3B. `auto` detection — actual behaviour [VERIFIED]

`_compute_schema_profile` requires **two** signals to name a layout. Executed
against synthetic field caps for each path:

| Field cache | Detected |
|---|---|
| `metrics.*` + `labels.*` | `prometheus_native` |
| `prometheus.labels.*` + `prometheus.<m>.value` | `prometheus_remote_write` |
| `prometheus.labels.*` + `prometheus.metrics.*` | `prometheus_metrics` |
| `metrics.*` + `resource.attributes.*` (Path 4) | `None` → falls back to `otel` |
| `metrics.k8s.pod.cpu.time` (OTel SDK) | `None` → falls back to `otel` |

Detection is **conservative and correct**: it declines to name a layout it
cannot positively identify, and the `otel` fallback it chooses for Path 4 is
exactly the right answer for Path 4.

---

## 4. Confirmed Problems

### Problem A — Offline default emits metric names that exist in no layout [VERIFIED]

**Severity:** High (correctness) — but see the mitigation below, which is
already shipped.

With no `--es-url` the field cache is empty, so the `otel` metric branch falls
through to the bare source name:

```
TS metrics-*
| WHERE labels.instance RLIKE ?instance
| WHERE redis_uptime_in_seconds IS NOT NULL      <-- not a field in ANY of Paths 1-4
```

Because `metrics` is not a passthrough object (§2A), the bare metric name is
never queryable. Labels fare better: on Paths 1 and 4 the bare label form *does*
resolve via passthrough, so offline label resolution is frequently correct while
offline metric resolution is reliably wrong.

**Already mitigated (do not re-implement).** The run prints two layered
warnings and records the state in machine-readable artifacts:

```
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
WARNING: migrated panels may render empty
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
  target schema discovery did not run (no --es-url provided).
  Fix: re-run pointing the migration at your target data --
       --es-url (and --es-api-key if required) for schema discovery, and
       --esql-index for the index/data stream your metrics live in.
```

plus a metrics-target readiness warning about wildcard `metrics-*`, and
`field_discovery: {status: "offline", otel_fallback: true, field_count: 0}` in
both `migration_report.json` and `run_summary.json`.

**The remaining open question is narrow:** is a loud warning sufficient, or
should offline `otel` emit an explicit unresolved marker instead of a
plausible-looking bare name? A warning can be scrolled past; a query that *looks*
valid is the thing that misleads. That is a real design question — but it is a
question about escalating an existing signal, not about adding a missing one.

### Problem B — `metric_map` targets bypass the profile prefix, undocumented [VERIFIED]

Executed:

```
--field-profile prometheus_native, metric_map {redis_uptime_in_seconds: system.uptime}

  mapped   redis_uptime_in_seconds -> system.uptime                    (no prefix)
  unmapped redis_connected_clients -> metrics.redis_connected_clients  (prefix)
```

A partially-mapped file therefore emits two different field shapes in one
dashboard. The behaviour is intentional (a rename must be able to target any
field), but it was documented nowhere.

**Fixed in this change:** `docs/command-contract.md` now documents it with
per-profile examples.

### Problem C — Profile names diverge between Grafana and Datadog [VERIFIED]

| Real layout | Grafana | Datadog |
|---|---|---|
| ES native endpoint | `prometheus_native` | `prometheus_native` |
| Fleet `use_types` typed leaves | `prometheus_remote_write` | *(missing)* |
| Metricbeat `prometheus.metrics.*` | `prometheus_metrics` | `prometheus` |
| OTel Collector | `otel` | `otel` |
| Elastic Agent native | *(missing)* | `elastic_agent` |
| Auto-detect | `auto` | *(missing)* |

One ingestion layout, two names. An operator migrating both sources against one
target has to learn both.

**Partially fixed in this change:** Datadog now accepts `prometheus_metrics` as
an alias for `prometheus` (additive, non-breaking; `prometheus` remains
canonical). The missing profiles and Datadog `auto` remain open — see Issue 2.

### Problem D — No per-panel field-resolution granularity

The run-level signal exists and is honest (`otel_fallback`, `status`,
`field_count`, `profile_mismatch`, `planned` vs `detected` profile). What does
not exist is per-panel attribution: an operator cannot see *which* panels
depend on unverified fields. On a 141-panel dashboard the aggregate signal does
not tell them where to look.

This is a real enhancement — but it is "increase resolution of an existing
signal", not "add a missing one".

### Problem E — Recording-rule metric names are invisible to auto-detection (minor)

`_NATIVE_METRIC_RE` is `^metrics\.[A-Za-z_][A-Za-z0-9_]*$`, which excludes the
colon that is legal in Prometheus metric names and conventional in recording
rules (`job:request_latency:mean5m`). An index containing *only* recording-rule
metrics would fail `prometheus_native` detection.

Impact is low: detection needs one matching field and real indices carry many
conventional metrics, and `resolve_metric_field` prefixes unconditionally once
the profile is chosen, so resolution of recording rules is unaffected. Recorded
for completeness; not worth a code change on its own.

---

## 5. Operator Decision Tree

```
GRAFANA — how did Prometheus data reach Elasticsearch?

  ES /_prometheus/api/v1/write endpoint
      -> --field-profile prometheus_native
  Elastic Agent / Fleet Prometheus integration (use_types=true)
      -> --field-profile prometheus_remote_write
  Metricbeat prometheus module (use_types=false)
      -> --field-profile prometheus_metrics
  OTel Collector with Prometheus receiver
      -> --field-profile otel --es-url <url>
         (metrics resolve from live caps; labels resolve bare via passthrough)
  Unknown / mixed
      -> --field-profile auto --es-url <url>
         (names Paths 1-3 positively; correctly falls back to otel otherwise)

  Do ES metric names match Prometheus names?
      YES (standard exporters, Paths 1-4 via Prometheus receiver) -> done
      NO  (OTel SDK semconv names, recording rules, custom pipelines)
          -> --metric-map-file, with fully-qualified targets (Problem B)

DATADOG — how did Datadog metrics reach Elasticsearch?

  OTel Collector + Datadog receiver -> --field-profile otel + --metric-map-file
  Elastic Agent Datadog integration -> --field-profile elastic_agent + --metric-map-file
  Metricbeat prometheus            -> --field-profile prometheus
                                      (alias: prometheus_metrics)
```

Whatever the path: finish with `--es-url --preflight` so every field flips to
`confirmed` or `missing` in `target_readiness_contract.json`.

---

## 6. Proposed Issues

### Issue 1 — Escalate offline `otel` from warning to explicit unresolved state
**Priority:** Medium · **Breaking:** Yes (opt-in) · *(Problem A)*

The warning already exists. The question is whether offline `otel` should stop
emitting a plausible-looking bare metric name. Options: emit an unresolved
marker; refuse to write a metric query without `--es-url` or an explicit
profile; or downgrade affected panels to a distinct status. Needs a decision on
whether assets-first (migrate before telemetry exists) remains a first-class
workflow — it currently is, and that is a legitimate reason to keep emitting.

### Issue 2 — Close the Grafana/Datadog profile gaps
**Priority:** Medium · **Breaking:** No (additive) · *(Problem C)*

Alias landed. Remaining: add `auto` to Datadog (reuse `_compute_schema_profile`),
add `elastic_agent` to Grafana, add `prometheus_remote_write` to Datadog.

### Issue 3 — Per-panel `field_resolution` attribution
**Priority:** Medium · **Breaking:** No · *(Problem D)*

Per-panel `confirmed` / `assumed` / `unresolved`, plus an aggregate line naming
the panels needing attention.

### Issue 4 — Document Path 4 and the passthrough mechanism
**Priority:** High · **Breaking:** No · *(§2A)*

`docs/sources/grafana.md` should explain that Paths 1 and 4 both expose labels
bare via ES passthrough, that this is why `otel` resolves labels correctly on
both, and that metrics have no such fallback. This single fact explains most
otherwise-confusing field-resolution behaviour.

---

## 7. Retracted Claims

Recorded so they are not re-derived.

| First-draft claim | Verdict | Evidence |
|---|---|---|
| "No warning is emitted" for offline `otel` | **False** | `print_field_discovery_warning` prints a banner; observed in a real run. Two layered warnings, both with actionable fixes. |
| "Reports 0 not-feasible with every metric query silently broken" | **False** | Nothing is silent; see above. |
| Proposed "Issue A — add a warning" | **Obsolete** | Proposes building what already ships, including the same remediation text. |
| "`auto` misidentifies Path 4 as `prometheus_native`" | **False** | Detection needs `metrics.*` **and** `labels.*`. Path 4 returns `None` → `otel`, the correct answer. |
| "`otel + --es-url` works for Path 4 only by accident" | **Misleading** | Both paths use ES `passthrough`; bare-name resolution is the designed mechanism, not luck. |
| "Migration report has no honesty signal" | **False** | `field_discovery` with `otel_fallback`/`status`/`field_count` is in `migration_report.json` and `run_summary.json`. True only at per-panel granularity → Problem D. |

**Process note.** The first draft's most consequential errors came from reading
code paths without executing them, and from treating `resource.attributes.*` and
`labels.*` as different *kinds* of thing without checking the live mapping —
where both turned out to be passthrough. Claims about field layout should be
checked against `_field_caps` and `_mapping` on a real index.

**Environment caveat found while verifying.** In this checkout `obs-migrate`
resolves to `/Users/subhamsarkar/mig-to-kbn/.venv/bin/obs-migrate` even after
activating the repo venv, so CLI runs can silently exercise stale code. An early
verification run in this review produced wrong output for exactly that reason.
Use `.venv/bin/obs-migrate` explicitly when validating engine behaviour.

---

## Sources

Official docs cross-checked 2026-07-30. Field layouts marked **[VERIFIED]** were
additionally confirmed by executing code or querying a live index.

- [ES native Prometheus remote write](https://www.elastic.co/docs/manage-data/data-store/data-streams/tsds-ingest-prometheus-remote-write) — `metrics.*` + `labels.*`, index naming, counter/gauge inference
- [Prometheus integration (Elastic Agent/Fleet)](https://www.elastic.co/docs/reference/integrations/prometheus) — `use_types`, `prometheus.<m>.{counter,value}`
- [Metricbeat prometheus remote_write metricset](https://www.elastic.co/docs/reference/beats/metricbeat/metricbeat-metricset-prometheus-remote_write) — `prometheus.metrics.*` + `prometheus.labels.*`
- [OTel Collector ES exporter README](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/elasticsearchexporter/README.md) — `metrics.<name>` in `otel` mode, `.otel` dataset suffix, mapping modes
- [OTel Prometheus receiver README](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/receiver/prometheusreceiver/README.md) — Prometheus metric names preserved
- [EDOT OTel data streams](https://www.elastic.co/docs/reference/opentelemetry/compatibility/data-streams) — `resource.attributes.*` storage with passthrough to top-level
