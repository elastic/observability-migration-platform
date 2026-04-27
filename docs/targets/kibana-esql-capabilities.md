# Kibana ES|QL Capability Snapshot

This note is a working reference for what Kibana's current ES|QL stack can do,
which parts are editor-supported versus publicly documented, and which features
look most useful for observability translation work in this repo.

For the concrete repo-level implementation plan that follows from this survey,
see `docs/targets/kibana-esql-upgrade-matrix.md`.

Snapshot date: `2026-03-30`

## Sources Used

Local Kibana source inspected:

- `src/platform/plugins/shared/esql/ADD_COMMAND_GUIDE.md`
- `src/platform/packages/shared/kbn-esql-language/README.md`
- `src/platform/packages/shared/kbn-esql-language/src/commands/registry/index.ts`
- `src/platform/packages/shared/kbn-esql-language/src/commands/definitions/generated/commands/commands.ts`
- `src/platform/packages/shared/kbn-esql-language/src/commands/definitions/generated/aggregation_functions.ts`
- `src/platform/packages/shared/kbn-esql-language/src/commands/definitions/generated/time_series_agg_functions.ts`
- `src/platform/packages/shared/kbn-esql-language/src/commands/definitions/generated/grouping_functions.ts`
- `src/platform/packages/shared/kbn-esql-language/src/commands/definitions/generated/scalar_functions.ts`
- `src/platform/packages/shared/kbn-esql-language/src/commands/definitions/generated/operators.ts`
- Representative registry modules such as `from/index.ts`, `timeseries/index.ts`,
  `promql/index.ts`, `join/index.ts`, `set/index.ts`, `enrich/index.ts`,
  `mmr/index.ts`, and `rerank/index.ts`

Published Elastic docs inspected:

- [ES|QL syntax reference](https://www.elastic.co/docs/reference/query-languages/esql/esql-syntax-reference)
- [Basic ES|QL syntax](https://www.elastic.co/docs/reference/query-languages/esql/esql-syntax)
- [ES|QL processing commands](https://www.elastic.co/docs/reference/query-languages/esql/commands/processing-commands)
- [ES|QL functions and operators](https://www.elastic.co/docs/reference/query-languages/esql/esql-functions-operators)
- [Use ES|QL in the Kibana UI](https://www.elastic.co/docs/explore-analyze/query-filter/languages/esql-kibana)
- [Advanced workflows in ES|QL](https://www.elastic.co/docs/reference/query-languages/esql/esql-advanced)
- [ES|QL LOOKUP JOIN command](https://www.elastic.co/docs/reference/query-languages/esql/commands/lookup-join)

## Big Picture

- Kibana's ES|QL stack spans two layers:
  `@elastic/esql` owns AST/parser/pretty-printing, while
  `kbn-esql-language` owns editor behavior like validation, autocomplete,
  hover, signature help, and column tracking.
- The local editor registry is not a perfect mirror of the published Elastic
  reference. Some commands are named differently in code, some are editor-only,
  and some published names are generated metadata rather than direct registry
  folders.
- For translation work in this repo, the safest targets are the intersection of:
  published docs, editor support, and features we can actually lint/compile/run
  against the target deployment.

## Commands In This Checkout

### Source And Header Commands

| Command | Notes |
|---|---|
| `FROM` | Primary source command for data streams, indices, and aliases |
| `TS` | Metrics-specific source command for TSDB indices; marked preview in local registry |
| `ROW` | Inline row source, useful for examples/testing |
| `SHOW` | Source-style introspection command |
| `SET` | Query setting header; local generated settings include `approximation`, `project_routing`, `time_zone`, `unmapped_fields` |
| `PROMQL` | Preview source command in local registry for native PromQL execution |

### Core Processing Commands

| Command | Notes |
|---|---|
| `WHERE` | Filtering |
| `EVAL` | Expressions, formulas, aliases |
| `STATS` | Aggregation and group-by |
| `INLINE STATS` | Window-like aggregate enrichment without collapsing rows |
| `SORT` | Ordering |
| `KEEP` / `DROP` / `RENAME` | Column shaping |
| `LIMIT` | Row limiting |
| `DISSECT` / `GROK` | Query-time parsing |
| `ENRICH` | Enrich-policy joins |
| `LOOKUP JOIN` | Lookup-index joins; implemented under the local `join/` registry folder |

### Advanced Or Less Likely Translation Targets

| Command | Notes |
|---|---|
| `CHANGE_POINT` | Change-point detection |
| `COMPLETION` | Inference/completion workflow |
| `FORK` | Branch processing |
| `FUSE` | Hidden in local registry |
| `METRICS_INFO` / `TS_INFO` | Introspection-oriented |
| `MMR` | Result diversification |
| `MV_EXPAND` | Multivalue row expansion |
| `REGISTERED_DOMAIN` / `URI_PARTS` | URL/domain parsing helpers |
| `RERANK` | Inference reranking |
| `SAMPLE` | Sampling |

## Registry Vs Published Docs

- Local registry-only names observed: `FROM`, `TS`, `ROW`, `SHOW`, `SET`,
  `PROMQL`, `completion`, `fuse`, and `join`.
- Generated or published names that do not map 1:1 to registry folder names:
  `LOOKUP_JOIN`, `INLINE_STATS`, `LOOKUP`, `EXPLAIN`, and `INSIST`.
- Important practical nuance:
  the local `join` registry module currently exposes `LOOKUP JOIN`, not a broad
  family of general-purpose SQL-style joins.
- Another practical nuance:
  the local Kibana editor supports a `PROMQL` source command even though it does
  not appear in the public ES|QL command reference pages inspected here.

## Function Surface Area

Generated definitions in this Kibana checkout include:

- `21` aggregation functions
- `19` time-series aggregation functions
- `3` grouping functions
- `163` scalar functions
- `21` operators
- `57` PromQL function definitions
- `16` PromQL operators
- `4` PromQL label matchers

The published docs currently group ES|QL functions/operators into:

- aggregate
- time-series aggregate
- grouping
- conditional
- date/time
- IP
- math
- search
- spatial
- string
- type conversion
- dense vector
- multivalue
- operators

The highest-value families for this repo are:

- aggregation and time-series aggregation
- grouping
- search functions such as `KQL()`, `MATCH()`, `MATCH_PHRASE()`, `QSTR()`
- string/parsing functions
- multivalue functions
- date/time functions
- type conversion functions
- conditional functions such as `CASE()` and `COALESCE()`

## Translation-Relevant Features

| Feature | Why it matters for us |
|---|---|
| `TS` | Gives us a native time-series source instead of always forcing `FROM` |
| `RATE`, `INCREASE`, `IRATE`, `DELTA`, `*_OVER_TIME` | May let us map PromQL-style temporal semantics more directly |
| `BUCKET()` and `TBUCKET()` | Better time bucket alignment for dashboard panels |
| `PROMQL` | Strong candidate for native fallback or preferred path when deployment support exists |
| `KQL()`, `MATCH()`, `MATCH_PHRASE()`, `QSTR()` | Useful bridge for Datadog log search, Grafana free-text filters, and KQL carryover |
| `DISSECT`, `GROK`, `URI_PARTS`, `REGISTERED_DOMAIN` | Query-time extraction for logs and string-heavy datasets |
| `LOOKUP JOIN` and `ENRICH` | Useful for ownership, inventory, threat intel, or metadata correlation panels |
| `MV_EXPAND` and `MV_*` functions | Useful when tags or controls are multivalued |
| `EVAL`, `CASE`, `COALESCE`, conversion functions | Needed for formulas, null handling, aliases, and type cleanup |
| `KEEP`, `DROP`, `RENAME`, `SORT`, `LIMIT` | Important for emitted-query hygiene and Kibana/YAML friendliness |

## Kibana UI Patterns To Preserve

- Queries start with `FROM` or `TS`, then chain processing commands with pipes.
- `?_tstart` and `?_tend` are first-class time parameters for Kibana-backed
  ES|QL queries and work especially well with `BUCKET()`.
- Kibana supports variables and controls using `?value`, `??field`,
  and `??function`.
- Multi-select controls use `MV_CONTAINS(?values, field)`.
- `WHERE KQL("...")` is a first-class bridge inside the ES|QL editor and is
  worth treating as a supported target pattern.
- `LOOKUP JOIN` requires lookup-mode indices.
- `ENRICH` requires a prepared enrich policy.
- Elastic's Kibana UI docs explicitly warn against relying on
  `SET time_zone` in Kibana apps because UI display timezone can diverge from
  query execution timezone.
- When queries hit many indices, `KEEP` and `DROP` are recommended to reduce
  response size.

## What Looks Most Promising For This Repo

### Highest-Value Areas To Explore

- Re-evaluate parts of our Grafana PromQL translator around `TS` and the
  time-series aggregate family before adding more handcrafted
  `FROM ... | STATS ... BY BUCKET(...)` rewrites.
- Keep the native `PROMQL` path first-class and capability-detect it rather
  than treating it as a legacy escape hatch.
- Expand log translation around `KQL()`, `MATCH*`, `DISSECT`, `GROK`,
  `URI_PARTS`, and `REGISTERED_DOMAIN`.
- Consider `LOOKUP JOIN` and `ENRICH` for dashboards that need runtime metadata
  correlation, ownership mapping, or lookup-table augmentation.
- Improve variable/control-aware emission using `?value`, `??field`,
  `??function`, and `MV_CONTAINS`.

### Areas To Treat Carefully

- Preview or hidden features such as `TS`, `PROMQL`, `MV_EXPAND`, `FORK`,
  `FUSE`, some time-series functions, and some vector/search features should
  be capability-detected before emission.
- Editor support does not guarantee runtime availability on every cluster or
  every Stack/Serverless version.
- `LOOKUP JOIN` and `ENRICH` have operational prerequisites, so they should not
  become default translation targets without environment checks.
- Commands like `COMPLETION`, `RERANK`, `MMR`, and dense-vector functions are
  interesting, but they are not obvious priorities for dashboard migration.

## Practical Recommendation

If we want better translation fidelity with the current Elastic surface area,
the best next investigations are:

1. `TS` plus time-series aggregate functions for PromQL-like panels.
2. `PROMQL` capability detection and native-use heuristics.
3. Stronger log/query-text translation around `KQL()`, `MATCH*`, and parsing
   commands.
4. Optional enrichment paths built on `LOOKUP JOIN` or `ENRICH`.

## Upstream Implementation Pointers

Use these when the snapshot needs refreshing:

- Kibana command-extension flow:
  `src/platform/plugins/shared/esql/ADD_COMMAND_GUIDE.md`
- Kibana ES|QL language package overview:
  `src/platform/packages/shared/kbn-esql-language/README.md`
- Current registry entrypoint:
  `src/platform/packages/shared/kbn-esql-language/src/commands/registry/index.ts`
- Current generated command metadata:
  `src/platform/packages/shared/kbn-esql-language/src/commands/definitions/generated/commands/commands.ts`
- Current generated function metadata:
  `src/platform/packages/shared/kbn-esql-language/src/commands/definitions/generated/`
