# Kibana ES|QL Upgrade Matrix

This document turns the capability survey in
`docs/targets/kibana-esql-capabilities.md` into an implementation matrix for
`observability_migration`.

The goal is not to use every shiny ES|QL feature. The goal is to improve
translation fidelity where Kibana's newer ES|QL surface gives us a better,
more honest, or more maintainable target.

## Operating Rules

- Prefer the stable intersection of published docs, Kibana editor support, and
  target-runtime availability.
- Treat preview-only features as capability-detected, not unconditional.
- Preserve the current honesty rule: if a richer ES|QL path is not safe, keep
  the existing `not_feasible`, `requires_manual`, or native-source fallback.
- Land each upgrade with query -> IR -> YAML -> compile coverage, plus an
  explicit regression fixture for the unsupported side of the boundary.

## Current Baseline

| Area | Current state |
|---|---|
| Grafana metrics | Mature rule-engine ES|QL path plus a strong native `PROMQL` path |
| Grafana logs | ES|QL translation exists, but richer search/parsing bridges are limited |
| Grafana controls | Dashboard controls are emitted, but query translation often expands template variables to wildcards or literal `1` rather than preserving parameters |
| Datadog metrics | Clean AST-to-ES|QL pipeline for many widgets and formulas |
| Datadog logs | Already uses `KQL()` as a bridge when free-text terms are present |
| Datadog controls | Kibana dashboard controls are emitted from template variables, but some query-level semantics still degrade to broader matches or warnings where Datadog behavior has no exact target equivalent |
| Enrichment / lookup | Not yet a first-class translation target in either adapter |

## Type Constraints And Function Safety

This is the main rule:

- do not pick an ES|QL function just because it exists
- pick it only when the target field types and runtime capabilities make it safe

We already have pieces of this today:

- Grafana has `SchemaResolver` with `field_exists()`, `field_type()`,
  `resolve_label()`, and `is_counter()`
- Datadog already has a preflight model with `FieldCapability` carrying
  `type`, `searchable`, `aggregatable`, and conflicting-type information
- Grafana's live validator can catch runtime field/type failures after query
  generation and suggest fixes

What is missing is a shared, first-class type gate before the translator chooses
an ES|QL function or command.

### Proposed Safety Model

Each candidate translation step should declare:

- required fields
- expected input type families
- extra capability requirements
- safe fallback if the requirement is not met

Examples:

| ES|QL feature | Minimum safe requirements | If requirements fail |
|---|---|---|
| `RATE`, `IRATE`, `INCREASE`, `DELTA` | metric exists, metric is numeric, and source/field metadata says counter or compatible time-series metric | fall back to plain aggregate, native `PROMQL`, or explicit `not_feasible` depending on semantic risk |
| `BUCKET()` / `TBUCKET()` | time field is date/date_nanos and target environment supports the chosen form | fall back to simpler bucketing or block |
| `KQL()` / `MATCH()` / `MATCH_PHRASE()` / `QSTR()` | fields are searchable, and semantics match free-text or phrase search rather than exact filtering | fall back to structured `WHERE` or reviewer-facing warning |
| `LOOKUP JOIN` | join fields exist on both sides, type families are compatible, and lookup index exists | skip enrichment path and keep base query |
| `ENRICH` | enrich policy exists, match field is valid, and enrich output fields are usable | skip enrichment path and report prerequisite gap |
| `MV_CONTAINS` | control is multi-select and field comparison semantics are stable | use single-value filter or explicit limitation note |
| `DISSECT` / `GROK` | source pattern is explicit and stable enough that extraction is not guesswork | keep manual-review guidance rather than emitting brittle parsing |
| `INLINE STATS` | row-preserving aggregate semantics are actually needed and output shape remains representable in Kibana YAML | stay with `STATS`, reducer, or manual redesign |

### Shared Capability Layer We Should Add

We should introduce a repo-level capability model used by both adapters, for
example:

- `exists`
- `type_family`: `numeric`, `keyword`, `text`, `date`, `ip`, `boolean`,
  `geo`, `unknown`
- `exact_type`: raw ES type such as `long`, `keyword`, `date_nanos`
- `searchable`
- `aggregatable`
- `conflicting_types`
- `time_series_metric_kind`: `counter`, `gauge`, or `unknown`

This should be backed by `_field_caps` where available and by field-profile or
rule-pack hints where it is not.

### Translator Decision Order

For any new ES|QL capability, the translator should use this order:

1. Resolve candidate target fields.
2. Gather capability data for each field.
3. Check whether the intended ES|QL feature is valid for those capabilities.
4. If valid, emit the richer query.
5. If invalid but a lower-fidelity safe alternative exists, emit it and record
   the semantic loss.
6. If no safe alternative exists, mark the result `not_feasible` or
   `requires_manual`.

That prevents silent misuse of functions on the wrong field types.

### What To Validate Before Translation

Before translation or during planning/preflight, validate:

- field existence
- numeric vs string/date mismatches
- searchable vs aggregatable usage
- conflicting types across the target index pattern
- counter vs gauge assumptions for time-series functions
- target-version or deployment support for preview-only commands
- prerequisites for lookup indices and enrich policies

### What To Store In IR

For every translated query, the IR should be able to record:

- required target fields
- required type families
- assumptions such as "treated as counter" or "assumed searchable text field"
- whether a cast or coercion was introduced
- whether the chosen ES|QL feature was the preferred path or a fallback path

This belongs naturally alongside `QueryIR.metadata` and warnings so reviewers
can see why a function was chosen.

### Safe Casting Policy

We should allow casts only when they are explicit and reviewable.

Good examples:

- `TO_DATETIME(...)` for known string-to-date migrations
- `TO_DOUBLE(...)` when a numeric-looking string field is known to need numeric aggregation
- `TO_STRING(...)` for display-only or label-normalization purposes

Bad examples:

- spraying casts into generated queries just to satisfy the compiler
- coercing unknown field types after an ES|QL error without proving the result is semantically valid

The validator may suggest fixes, but auto-fixes should stay conservative:

- okay: exact field rename, known alias swap, index narrowing
- risky: changing function families or inserting broad casts without evidence

### Concrete Repo Changes

To make this real, the next type-safety work should be:

1. Lift Grafana `SchemaResolver` and Datadog `FieldCapability` into a shared
   target-capability abstraction.
2. Add helper predicates such as:
   `is_numeric_field()`, `is_text_like_field()`, `is_date_like_field()`,
   `is_searchable_field()`, `is_aggregatable_field()`,
   `is_counter_metric_field()`, and `has_conflicting_types()`.
3. Make each advanced translator path call those helpers before emitting richer
   ES|QL features.
4. Extend preflight to validate feature prerequisites, not just field presence.
5. Extend `QueryIR.metadata` with type assumptions and capability-gating
   decisions.
6. Keep live ES|QL validation as the last line of defense, not the first one.

## Unsupported PromQL Families (The Honest Boundary)

Some PromQL families have no faithful ES|QL target. Rather than emit a query
that silently computes the wrong thing, the Grafana adapter marks them
`not_feasible` and the panel degrades to a "Migration Required" placeholder
with a reviewer-facing reason. This section documents that boundary so the
"degrade gracefully" rule (see `CLAUDE.md`) stays auditable. Ground truth is
`HARD_UNSUPPORTED_CALL_REASONS` / `HARD_UNSUPPORTED_AST_REASONS` in
`observability_migration/adapters/source/grafana/promql.py` and the family
classifier in `.../grafana/translate.py`.

### Always `not_feasible` (no ES|QL equivalent)

| PromQL | Why there is no honest ES|QL translation |
|---|---|
| `bottomk(k, …)` | Per-series bottom-N selection requires manual redesign (mirror of `topk`, but without the latest-bucket approximation that `topk` accepts). |
| `label_join(…)` | Concatenates several source labels into a new label, rewriting series identity in a way ES|QL's column model cannot reproduce per-series. |
| `count_values("l", v)` | Buckets samples by their *value* into a new label — ES|QL has no value-as-dimension grouping. |
| `absent(v)` / `absent_over_time(v)` | Returns a synthetic series when the input is empty; ES|QL cannot synthesize a row from the absence of data. |
| `changes(v[range])` | Counts value transitions over the raw sample stream; ES|QL aggregates are bucket-oriented, not sample-transition-oriented. |
| `resets(v[range])` | Counts counter resets in the raw stream; same sample-level semantics gap as `changes`. |
| `vector(s)` | Converts a scalar into an instant vector; no ES|QL equivalent for fabricating a series from a constant. |
| `group(…)` | Returns the constant `1` per label set (value-discarding). Aggregating the metric value instead would change the result. |
| `stdvar(v)` | Population variance; ES|QL has no variance aggregation and `STATS` cannot square `STD_DEV()` inline. |
| `<expr> offset <d>` | Time-shifts the lookup window at sample granularity; ES|QL has no per-query time offset on the bucket timeline. |
| `<expr> @ <ts>` | Pins evaluation to an absolute timestamp; same time-axis-control gap as `offset`. |
| `<subquery>[r:s]` | Subqueries evaluate an inner range expression on a sliding step; ES|QL cannot express the nested resolution. |
| `… without (labels)` | Aggregation by exclusion needs the full label universe to invert; ES|QL `STATS BY` only lists kept dimensions. |
| `__name__` introspection | Selecting/relabeling by metric name treats the metric name as data; ES|QL has no metric-name-as-field model. |

Root causes cluster into four buckets: (1) **no equivalent operation**
(`absent`, `changes`, `resets`, `stdvar`, `count_values`, `group`, `vector`);
(2) **sample-level / time-shift semantics** ES|QL's bucketed aggregation
cannot reach (`offset`, `@`, subquery); (3) **label algebra that changes
series identity** (`label_join`, complex `label_replace`); (4) **type
prerequisites we cannot prove** (see conditional cases below).

### Conditionally supported (feasible only when prerequisites hold)

| PromQL | Behavior | Falls back to |
|---|---|---|
| `histogram_quantile(phi, …)` | Translates to ES|QL `PERCENTILE()` **only** when the base metric is provably histogram / exponential-histogram typed. | `not_feasible` when the field type cannot be confirmed — we will not emit a `PERCENTILE()` that silently misreads a non-histogram field. |
| `topk(k, …)` / `topk by (…) (k, …)` | **Feasible**, approximated as a latest-bucket ES|QL top-N. | Grouped form keeps inner labels; ungrouped form collapses to a single series and warns to add a `preferred_group_labels` hint. |
| `label_replace(v, dst, repl, src, rx)` | **Feasible** via `EVAL` for a full copy (`$1` over `(.*)`) or a constant replacement, and via an anchored `GROK` for a single capture group. | Complex multi-capture replacements skip the rename (warning emitted) but the underlying query still runs. |

When growing ES|QL coverage, the upgrade matrix below is where a conditional
case graduates to unconditional, or an unsupported family gains a safe target.
Any such change must keep an explicit regression fixture for the unsupported
side of the boundary (see the testing policy at the end of this doc).

## Upgrade Matrix

| Priority | Capability | Current repo state | Concrete upgrade path | Primary code targets | Acceptance |
|---|---|---|---|---|---|
| `P0` | End-to-end controls: `?value`, `??field`, `??function`, `MV_CONTAINS` | Grafana emits `controls` but often inlines variables during query translation; Datadog parses `template_variables` but does not emit controls | Grafana: preserve safe variable references in ES|QL and native `PROMQL`, emit `MV_CONTAINS` for multi-selects, stop replacing arithmetic variables with literals when a parameterized path is valid. Datadog: emit controls from `NormalizedDashboard.template_variables`, bind tag filters to `?value`, and add multi-select support instead of broad matches. | `observability_migration/adapters/source/grafana/panels.py`, `observability_migration/adapters/source/grafana/translate.py`, `observability_migration/adapters/source/datadog/normalize.py`, `observability_migration/adapters/source/datadog/translate.py`, `observability_migration/core/assets/control.py` | New Grafana and Datadog fixtures show controls surviving into YAML and being referenced by emitted queries; compiled dashboards keep placeholders; unsupported variable positions still degrade explicitly |
| `P0` | `TS` source command plus time-series aggregate family | Grafana `promql.py` already switches between `FROM` and `TS` in a few cases through `ts_bucket` and `from_bucket`; Datadog always builds `FROM ... | STATS ... BY BUCKET(...)` queries | Grafana: formalize `TS` selection for counters and range-vector families, widen to more safe PromQL patterns, and reduce handcrafted `FROM` rewrites where `TS` is semantically closer. Datadog: detect rate/rollup/counter cases that should use `TS` instead of generic `FROM`, especially for OpenTelemetry-style metrics. | `observability_migration/adapters/source/grafana/promql.py`, `observability_migration/adapters/source/grafana/translate.py`, `observability_migration/adapters/source/datadog/query_parser.py`, `observability_migration/adapters/source/datadog/planner.py`, `observability_migration/adapters/source/datadog/translate.py` | Focused fixtures for `rate`, `increase`, `irate`, rollups, and grouped time-series panels prove query, IR, YAML, and compile behavior; live validation can compare `TS` and `FROM` output on a fixed dataset |
| `P0` | Native `PROMQL` auto-routing | Grafana prefers the high-fidelity native `PROMQL` path; when `--es-url` is configured, capability detection downgrades to ES|QL translation only when the `PROMQL` command is confirmed unsupported (an inconclusive probe keeps native and warns). `--translation-mode {auto,native,esql}` exists for explicit operator override; `auto` remains the normal path. Datadog accepts the flag for CLI parity, but it has no native-PROMQL path. | Grafana: keep capability detection deterministic and prefer native `PROMQL` automatically for safe query families, recording the decision in reports and IR. Keep ES|QL rule translation as fallback for unsupported PromQL families and for environments without native support. Datadog: not applicable directly. | `observability_migration/adapters/source/grafana/panels.py`, `observability_migration/adapters/source/grafana/cli.py`, `observability_migration/core/assets/query.py`, `observability_migration/adapters/source/grafana/manifest.py`, `observability_migration/adapters/source/grafana/report.py` | Panel-level tests prove deterministic path selection; manifests explain why native or ES|QL was chosen; compilation and validation still pass for mixed dashboards |
| `P1` | Search bridges: `KQL()`, `MATCH()`, `MATCH_PHRASE()`, `QSTR()` | Datadog already chooses `esql_with_kql` for some log queries; Grafana LogQL path mostly relies on message-filter rewriting | Grafana: add an explicit search-bridge decision for LogQL text search, textbox variables, and free-text filters, choosing between structured `WHERE`, `KQL()`, and `MATCH*` based on what the source query is actually asking for. Datadog: refine the current log path so quoted phrases, Lucene-like text, and structured tags map to the most faithful bridge instead of defaulting too broadly to `KQL()`. | `observability_migration/adapters/source/grafana/translate.py`, `observability_migration/adapters/source/grafana/promql.py`, `observability_migration/adapters/source/datadog/log_parser.py`, `observability_migration/adapters/source/datadog/planner.py`, `observability_migration/adapters/source/datadog/translate.py` | New log fixtures cover quoted phrases, negation, free text, and mixed structured filters; result summaries stay stable across bridge choice; unsupported search semantics still produce reviewer-visible warnings |
| `P1` | Parsing commands: `DISSECT`, `GROK`, `URI_PARTS`, `REGISTERED_DOMAIN` | Neither adapter currently emits these commands as first-class translation targets | Grafana: turn some current redesign-only log transformations into runnable ES|QL when the source pattern is explicit and safe. Datadog: support common access-log, URL, and domain-oriented queries with query-time parsing instead of falling back to manual review or generic text search. | `observability_migration/adapters/source/grafana/transforms.py`, `observability_migration/adapters/source/grafana/translate.py`, `observability_migration/adapters/source/datadog/log_parser.py`, `observability_migration/adapters/source/datadog/translate.py`, `observability_migration/adapters/source/datadog/field_map.py` | Real-world fixtures such as NGINX, Apache, URL, and domain examples prove extracted fields flow into IR, YAML, and compilable ES|QL; ambiguous parse cases remain blocked |
| `P1` | `INLINE STATS` for row-preserving formulas and tables | Current translators mostly choose between collapsing `STATS`, scalar reducers, or manual redesign when a panel needs both grouped rows and aggregate context | Grafana: use `INLINE STATS` where table-like panels or transformations need row context plus totals, percentages, or comparative values. Datadog: apply it to query tables, toplists, and formula-driven widgets that need per-row values and overall context at the same time. | `observability_migration/adapters/source/grafana/panels.py`, `observability_migration/adapters/source/grafana/translate.py`, `observability_migration/adapters/source/datadog/planner.py`, `observability_migration/adapters/source/datadog/translate.py` | New table and toplist fixtures prove that grouped rows survive while derived totals and percentages remain correct; compiled YAML stays valid and visual output shape is explicit in `QueryIR` / `VisualIR` |
| `P2` | Correlation and enrichment: `LOOKUP JOIN` and `ENRICH` | Supported by Kibana/ES|QL, but unused as a translation primitive in this repo | Add opt-in, profile-driven enrichment steps for service ownership, host inventory, environment metadata, or threat-intel joins. Grafana and Datadog should both treat this as an optional augmentation layer, not as the default translation path. | `observability_migration/adapters/source/grafana/translate.py`, `observability_migration/adapters/source/datadog/translate.py`, `observability_migration/adapters/source/grafana/preflight.py`, `observability_migration/adapters/source/datadog/preflight.py`, `observability_migration/adapters/source/datadog/field_map.py` | Preflight can verify enrich-policy or lookup-index prerequisites; opt-in fixtures prove correct join shape; missing prerequisites yield explicit block reasons instead of broken queries |
| `P2` | Multivalue semantics: `MV_EXPAND` and `MV_*` functions | Neither adapter uses multivalue commands/functions beyond basic tag matching | Grafana: improve multi-select variables, repeated panels, and tag-array handling without flattening semantics into wildcards. Datadog: support widgets and filters that operate on multivalued tag or field arrays, especially when paired with generated controls. | `observability_migration/adapters/source/grafana/panels.py`, `observability_migration/adapters/source/grafana/translate.py`, `observability_migration/adapters/source/datadog/query_parser.py`, `observability_migration/adapters/source/datadog/translate.py`, `observability_migration/core/assets/control.py` | Fixtures prove correct handling for multi-select controls and multivalue fields; no silent row explosion; visual output shape remains explicit and testable |

## Recommended Implementation Order

1. End-to-end controls and parameter binding.
2. `TS` plus time-series aggregate modernization.
3. Native `PROMQL` auto-routing on the Grafana path.
4. Log search bridge refinement.
5. Parsing commands and `INLINE STATS`.
6. Optional enrichment and multivalue work.

That order is deliberate:

- `P0` items improve the most common dashboards first.
- `P0` and `P1` items reinforce the current honesty model instead of fighting it.
- `P2` items need more environment contracts and should stay opt-in.

## What To Defer

These features exist in the local Kibana ES|QL surface, but they are not
obvious migration priorities for dashboard translation right now:

- `COMPLETION`
- `MMR`
- `RERANK`
- `FUSE`
- dense-vector functions
- inference-heavy search features

They may become relevant later for assistant workflows or semantic search, but
they should not distract from dashboard fidelity work.

## Testing Policy For Every Upgrade

Each matrix item should land with:

- one or more source-realistic fixtures in `tests/test_grafana_extended.py` or
  `tests/test_datadog_migrate.py`
- a query -> IR -> YAML assertion, not just a string-match query test
- a compile or target-runtime assertion where practical
- an explicit unsupported-regression fixture when the feature still has known
  unsafe boundaries

## Companion Docs

- `docs/targets/kibana-esql-capabilities.md`
- `docs/targets/kibana.md`
