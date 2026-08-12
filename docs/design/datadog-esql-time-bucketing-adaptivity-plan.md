# Datadog ES|QL Time-Bucketing Adaptivity Implementation Plan

> **Status:** implemented. Prefer
> [`datadog-esql-time-bucketing-adaptivity.md`](./datadog-esql-time-bucketing-adaptivity.md)
> §3.2 "As implemented" for the shipped `rate_safe` predicate (it includes the
> post-review `spec.emits_rate` and `_formula_needs_bucket_span` signals that
> some code blocks below omit). Task checkboxes below are historical and were
> not updated after execution.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify Datadog's `FROM`-path ES|QL bucket default (`BUCKET(@timestamp,
50, ...)`) with Grafana's live-verified 75/20 adaptive split, and convert the
`TS`-path rate emission from a fixed `TBUCKET(5 minute)` /
`RATE(field, 5 minute)` to adaptive, windowless bucketing — closing the
follow-up left open by `docs/design/esql-time-bucketing-strategy.md` §4.4.

**Architecture:** Two new module constants (`_ADAPTIVE_CHART_BUCKETS = 75`,
`_ADAPTIVE_RATE_BUCKETS = 20`) plus a `_time_bucket_expr(rate_safe: bool)`
helper in `translate.py`. `TIME_BUCKET_EXPR` keeps its existing name and
becomes the 75-bucket default (so every call site that must never see rate
math — logs, plain count/table/percentile reducers — needs **no edit**); only
the handful of `FROM`-path call sites whose query/formula needs rate safety
switch to `_time_bucket_expr(True)`. The `TS`-path rate branch drops its fixed
window entirely, mirroring Grafana's `RATE`/`INCREASE` windowless form.

**Tech Stack:** Python 3.12, pytest (snapshot + unittest), `make lint` (ruff),
`make typecheck` (mypy).

## Global Constraints

- Live-verified constants from `docs/design/datadog-esql-time-bucketing-adaptivity.md`
  §2: `_ADAPTIVE_CHART_BUCKETS = 75` (no cadence sensitivity), `_ADAPTIVE_RATE_BUCKETS = 20`
  (rate-safe at 15s cadence across all tested ranges; the 5-minute-range /
  ≥60s-cadence gap is an accepted, documented limitation — do not try to fix
  it in this plan).
- Do not touch the log-widget `TIME_BUCKET_EXPR` call sites
  (`_build_multi_log_timeseries_query` at `translate.py:2064`, the log
  timeseries branch at `translate.py:1982`) — logs have no counter fields, no
  rate concept, and must keep the plain 75-bucket default unconditionally.
- Do not change `_build_change_widget_esql` (`translate.py:547`) — change
  widgets use before/after span comparison, not time-bucketing.
- `_build_toplist_esql` / `_build_table_esql` (`translate.py:2260`, `2278`)
  are dead code (no callers outside their own module) — give them the new
  `rate_safe` parameter for signature consistency with `_build_categorical_esql`,
  but do not spend time inventing call-site rate context for them.
- Preserve "degrade gracefully" behavior — no new `not_feasible`/exception
  paths; this is purely a bucket-width/window change to existing emission.

---

### Task 1: Adaptive bucket constants and `_time_bucket_expr` helper

**Files:**
- Modify: `observability_migration/adapters/source/datadog/translate.py:56-58`
- Test: `tests/test_datadog_adaptive_time_bucketing.py` (new file)

**Interfaces:**
- Produces: `_ADAPTIVE_CHART_BUCKETS: int`, `_ADAPTIVE_RATE_BUCKETS: int`,
  `TIME_BUCKET_EXPR: str` (unchanged name, new value), `_RATE_SAFE_TIME_BUCKET_EXPR: str`,
  `_time_bucket_expr(rate_safe: bool) -> str` — all consumed by Tasks 2-4.

- [ ] **Step 1: Write the failing test**

Create `tests/test_datadog_adaptive_time_bucketing.py`:

```python
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression tests for Datadog's adaptive FROM/TS bucket-width unification.

See docs/design/datadog-esql-time-bucketing-adaptivity.md.
"""

from __future__ import annotations

import unittest

from observability_migration.adapters.source.datadog import translate


class TestTimeBucketExprHelper(unittest.TestCase):
    def test_default_time_bucket_expr_uses_75(self):
        self.assertEqual(
            translate.TIME_BUCKET_EXPR,
            "BUCKET(@timestamp, 75, ?_tstart, ?_tend)",
        )

    def test_rate_safe_time_bucket_expr_uses_20(self):
        self.assertEqual(
            translate._time_bucket_expr(True),
            "BUCKET(@timestamp, 20, ?_tstart, ?_tend)",
        )

    def test_non_rate_safe_time_bucket_expr_matches_default(self):
        self.assertEqual(
            translate._time_bucket_expr(False),
            translate.TIME_BUCKET_EXPR,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_datadog_adaptive_time_bucketing.py -v`
Expected: FAIL — `AttributeError: module '...translate' has no attribute '_time_bucket_expr'`
(and `TIME_BUCKET_EXPR` still equals the old `BUCKET(@timestamp, 50, ...)`).

- [ ] **Step 3: Write minimal implementation**

In `observability_migration/adapters/source/datadog/translate.py`, replace:

```python
TIME_BUCKET_EXPR = "BUCKET(@timestamp, 50, ?_tstart, ?_tend)"
TIME_FILTER = "@timestamp >= ?_tstart AND @timestamp <= ?_tend"
```

with:

```python
# Chart-resolution default: matches Grafana's panels.py constant of the same
# name and the underlying Kibana Lens AUTO_TARGET_NUMBER_OF_BUCKETS
# precedent. No cadence sensitivity -- safe for any order-independent
# reducer (avg/sum/min/max/percentile/count).
_ADAPTIVE_CHART_BUCKETS = 75
# Rate-safe floor for any query/formula whose aggregation needs >=2 samples
# per bucket (RATE/IRATE-style, or the FIRST/LAST bucket-endpoint fallback in
# _rate_approx_expr): too fine a bucket relative to the source's real sample
# cadence silently produces null (TS|QL RATE/INCREASE) or wrong (LAST==FIRST)
# values. Live-verified independently for Datadog's own cadence profile in
# docs/design/datadog-esql-time-bucketing-adaptivity.md -- not inherited from
# Grafana's panels.py constant of the same value by unverified analogy.
_ADAPTIVE_RATE_BUCKETS = 20
# ``TIME_BUCKET_EXPR`` keeps its historical name -- most call sites (logs,
# plain count/table/percentile widgets) reference it directly and must keep
# the flat chart-resolution default. Only the call sites whose query/formula
# needs rate safety (see ``_time_bucket_expr``) switch to the coarser form.
TIME_BUCKET_EXPR = f"BUCKET(@timestamp, {_ADAPTIVE_CHART_BUCKETS}, ?_tstart, ?_tend)"
_RATE_SAFE_TIME_BUCKET_EXPR = f"BUCKET(@timestamp, {_ADAPTIVE_RATE_BUCKETS}, ?_tstart, ?_tend)"
TIME_FILTER = "@timestamp >= ?_tstart AND @timestamp <= ?_tend"


def _time_bucket_expr(rate_safe: bool) -> str:
    """Return the FROM-path time-bucket expression for this query's needs.

    ``rate_safe=True`` selects the coarser 20-bucket floor; see the module
    constants above for why. Callers compute ``rate_safe`` from whatever
    rate/derivative signal is available in their own scope (a single query's
    ``as_rate``/``_needs_rate``, or a formula's derivative-function refs) --
    there is no single shared "is this widget a rate widget" flag because the
    two FROM-path entry points (`_translate_single_metric`,
    `_translate_formula_metric_widget`) have different natural signals.
    """
    return _RATE_SAFE_TIME_BUCKET_EXPR if rate_safe else TIME_BUCKET_EXPR
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_datadog_adaptive_time_bucketing.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add observability_migration/adapters/source/datadog/translate.py tests/test_datadog_adaptive_time_bucketing.py
git commit -m "feat(datadog): add adaptive 75/20 bucket constants and helper"
```

---

### Task 2: Thread `rate_safe` through the single-query FROM path

**Files:**
- Modify: `observability_migration/adapters/source/datadog/translate.py:408-544`
  (`_translate_single_metric`)
- Modify: `observability_migration/adapters/source/datadog/translate.py:2189-2205`
  (`_build_timeseries_esql`)
- Modify: `observability_migration/adapters/source/datadog/translate.py:2208-2244`
  (`_build_distribution_percentile_esql`)
- Modify: `observability_migration/adapters/source/datadog/translate.py:2295-2313`
  (`_build_scalar_esql`)
- Modify: `observability_migration/adapters/source/datadog/translate.py:2316-2348`
  (`_build_categorical_esql`)
- Modify: `observability_migration/adapters/source/datadog/translate.py:2260-2292`
  (`_build_toplist_esql`, `_build_table_esql` — signature-only, dead code)
- Test: `tests/test_datadog_adaptive_time_bucketing.py`

**Interfaces:**
- Consumes: `_time_bucket_expr` from Task 1.
- Produces: `_build_timeseries_esql(..., rate_safe: bool = False)`,
  `_build_distribution_percentile_esql(..., rate_safe: bool = False)`,
  `_build_scalar_esql(..., rate_safe: bool = False)`,
  `_build_categorical_esql(..., rate_safe: bool = False)` — all consumed by
  Task 3 for their formula-path callers (`_try_translate_formula_reducer`,
  `_try_translate_count_formula_pipeline` pass `rate_safe=False` explicitly;
  the main formula body passes a computed value).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_datadog_adaptive_time_bucketing.py`:

```python
from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.query_parser import parse_metric_query
from observability_migration.adapters.source.datadog.models import NormalizedWidget, WidgetQuery
from observability_migration.adapters.source.datadog.translate import translate_widget


class TestSingleQueryFromPathBucketSplit(unittest.TestCase):
    def _widget(self, query: str, widget_type: str = "timeseries") -> NormalizedWidget:
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        return NormalizedWidget(id="1", widget_type=widget_type, title="w", queries=[wq])

    def test_plain_gauge_timeseries_uses_75_bucket(self):
        widget = self._widget("avg:system.cpu.user{*}")
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertIn("BUCKET(@timestamp, 75, ?_tstart, ?_tend)", result.esql_query)
        self.assertNotIn("BUCKET(@timestamp, 20", result.esql_query)

    def test_as_rate_timeseries_uses_20_bucket(self):
        widget = self._widget("sum:http.requests{*}.as_rate()")
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertIn("BUCKET(@timestamp, 20, ?_tstart, ?_tend)", result.esql_query)
        self.assertNotIn("BUCKET(@timestamp, 75", result.esql_query)

    def test_as_rate_query_value_uses_20_bucket(self):
        # Grouped query_value collapses to a ranked table via
        # _build_categorical_esql(reducer=...) -- still must stay rate-safe.
        widget = self._widget("sum:http.requests{*}.as_rate() by {host}", widget_type="query_value")
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertIn("BUCKET(@timestamp, 20, ?_tstart, ?_tend)", result.esql_query)

    def test_plain_gauge_toplist_uses_75_bucket(self):
        widget = self._widget("avg:system.cpu.user{*} by {host}", widget_type="toplist")
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertIn("BUCKET(@timestamp, 75, ?_tstart, ?_tend)", result.esql_query)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_datadog_adaptive_time_bucketing.py -v`
Expected: `test_as_rate_timeseries_uses_20_bucket` and
`test_as_rate_query_value_uses_20_bucket` FAIL (still emit
`BUCKET(@timestamp, 75, ...)` for every case, since nothing branches on
rate yet). The two plain-gauge tests already PASS (75 is now the default
from Task 1).

- [ ] **Step 3: Write minimal implementation**

In `_translate_single_metric`, add the `rate_safe` computation right after
`spec` is built, and thread it into every downstream builder call:

```python
    spec = _build_metric_query_spec(wq, field_map, result)
    rate_safe = bool(wq.metric_query and (wq.metric_query.as_rate or _needs_rate(wq.metric_query)))
    top_config = _extract_top_function_config(wq.metric_query)
```

Then update each call site in the same function. The grouped-query_value
branch:

```python
        return _build_categorical_esql(
            spec.index,
            spec.where_str,
            spec.agg_expr,
            spec.group_fields,
            sort_field="value",
            sort_order="DESC",
            limit=100,
            reducer=reducer,
            rate_safe=rate_safe,
        )
```

The toplist branch:

```python
    if is_toplist:
        limit = top_config.limit or _extract_toplist_limit(widget)
        return _build_categorical_esql(
            spec.index,
            spec.where_str,
            spec.agg_expr,
            spec.group_fields,
            sort_field="value",
            sort_order=top_config.sort_order,
            limit=limit,
            reducer=reducer,
            rate_safe=rate_safe,
        )
```

The table/partition branch:

```python
    if is_table or is_partition:
        return _build_categorical_esql(
            spec.index,
            spec.where_str,
            spec.agg_expr,
            spec.group_fields,
            sort_field="value",
            sort_order="DESC",
            limit=100,
            reducer=reducer,
            rate_safe=rate_safe,
        )
```

The top()-on-timeseries branch:

```python
        if is_timeseries and top_config.limit is not None:
            group_clause = f"time_bucket = {_time_bucket_expr(rate_safe)}"
```

The distribution branch:

```python
            query = _build_distribution_percentile_esql(
                spec.index,
                spec.where_str,
                spec.es_metric,
                spec.group_fields,
                agg_expr=spec.agg_expr,
                rate_safe=rate_safe,
            )
```

The plain timeseries/heatmap branch:

```python
        return _build_timeseries_esql(
            spec.index, spec.where_str, spec.agg_expr, spec.group_fields,
            rate_safe=rate_safe,
        )
```

The scalar fallback:

```python
    return _build_scalar_esql(spec.index, spec.where_str, spec.agg_expr, reducer=reducer, rate_safe=rate_safe)
```

Now update the five builder functions to accept and use `rate_safe`:

```python
def _build_timeseries_esql(
    index: str,
    where: str,
    agg_expr: str,
    group_fields: list[str],
    rate_safe: bool = False,
) -> str:
    time_bucket = _time_bucket_expr(rate_safe)
    group_clause = f"time_bucket = {time_bucket}"
    if group_fields:
        group_clause += ", " + ", ".join(group_fields)

    return (
        f"FROM {index}\n"
        f"| WHERE {where}\n"
        f"| STATS value = {agg_expr} BY {group_clause}\n"
        f"| SORT time_bucket"
    )


def _build_distribution_percentile_esql(
    index: str,
    where: str,
    metric_field: str,
    group_fields: list[str],
    agg_expr: str = "",
    rate_safe: bool = False,
) -> str:
    """Approximate a Datadog distribution widget as percentile envelopes.

    ``agg_expr``, when provided, is the widget's own requested aggregator
    (e.g. ``AVG(field)``) and is kept as its own STATS term so the source
    aggregation is not silently dropped in favor of the (synthesized)
    percentile envelope — both are genuinely useful series on the chart.
    """
    field = (metric_field or "").strip() or "value"
    time_bucket = _time_bucket_expr(rate_safe)
    group_clause = f"time_bucket = {time_bucket}"
    if group_fields:
        group_clause += ", " + ", ".join(group_fields)
    stats_terms = []
    agg_expr = (agg_expr or "").strip()
    if agg_expr:
        agg_alias = _distribution_agg_alias(agg_expr)
        stats_terms.append(f"{agg_alias} = {agg_expr}")
    stats_terms.extend(
        [
            f"p50 = PERCENTILE({field}, 50)",
            f"p90 = PERCENTILE({field}, 90)",
            f"p99 = PERCENTILE({field}, 99)",
        ]
    )
    return (
        f"FROM {index}\n"
        f"| WHERE {where}\n"
        f"| STATS " + ", ".join(stats_terms) + f" BY {group_clause}\n"
        f"| SORT time_bucket"
    )


def _build_scalar_esql(
    index: str,
    where: str,
    agg_expr: str,
    reducer: str | None = None,
    rate_safe: bool = False,
) -> str:
    if reducer:
        lines = [
            f"FROM {index}",
            f"| WHERE {where}",
            f"| STATS _bucket_value = {agg_expr} BY time_bucket = {_time_bucket_expr(rate_safe)}",
        ]
        lines.append(f"| STATS value = {_series_reducer_expr(reducer, '_bucket_value')}")
        return "\n".join(lines)
    return (
        f"FROM {index}\n"
        f"| WHERE {where}\n"
        f"| STATS value = {agg_expr}"
    )


def _build_categorical_esql(
    index: str,
    where: str,
    agg_expr: str,
    group_fields: list[str],
    sort_field: str,
    sort_order: str,
    limit: int | None,
    reducer: str | None = None,
    rate_safe: bool = False,
) -> str:
    lines = [
        f"FROM {index}",
        f"| WHERE {where}",
    ]
    if reducer:
        group_clause = f"time_bucket = {_time_bucket_expr(rate_safe)}"
        if group_fields:
            group_clause += ", " + ", ".join(group_fields)
        lines.append(f"| STATS _bucket_value = {agg_expr} BY {group_clause}")
        reduce_expr = _series_reducer_expr(reducer, "_bucket_value")
        if group_fields:
            lines.append(f"| STATS value = {reduce_expr} BY {', '.join(group_fields)}")
        else:
            lines.append(f"| STATS value = {reduce_expr}")
    elif group_fields:
        lines.append(f"| STATS value = {agg_expr} BY {', '.join(group_fields)}")
    else:
        lines.append(f"| STATS value = {agg_expr}")
    if sort_field:
        lines.append(f"| SORT {sort_field} {sort_order}")
    if limit is not None and limit > 0:
        lines.append(f"| LIMIT {limit}")
    return "\n".join(lines)
```

Finally, add the same `rate_safe: bool = False` parameter (unused in their
own body but forwarded) to the two dead-code wrappers so their signatures
stay consistent with `_build_categorical_esql`:

```python
def _build_toplist_esql(
    index: str,
    where: str,
    agg_expr: str,
    group_fields: list[str],
    limit: int,
    rate_safe: bool = False,
) -> str:
    return _build_categorical_esql(
        index,
        where,
        agg_expr,
        group_fields,
        sort_field="value",
        sort_order="DESC",
        limit=limit,
        rate_safe=rate_safe,
    )


def _build_table_esql(
    index: str,
    where: str,
    agg_expr: str,
    group_fields: list[str],
    rate_safe: bool = False,
) -> str:
    return _build_categorical_esql(
        index,
        where,
        agg_expr,
        group_fields,
        sort_field="value",
        sort_order="DESC",
        limit=100,
        rate_safe=rate_safe,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_datadog_adaptive_time_bucketing.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the full Datadog unit suite to check for regressions**

Run: `.venv/bin/pytest tests/test_datadog_migrate.py -v 2>&1 | tail -60`
Expected: failures only in tests that assert the literal string
`BUCKET(@timestamp, 50, ...)` — note the failing test names, they are fixed
in Task 5. No failures with a different shape (e.g. exceptions, wrong panel
type) — if you see those, stop and re-check this task's edits before
continuing.

- [ ] **Step 6: Commit**

```bash
git add observability_migration/adapters/source/datadog/translate.py tests/test_datadog_adaptive_time_bucketing.py
git commit -m "feat(datadog): rate-safe FROM-path bucketing for single-query widgets"
```

---

### Task 3: Thread `rate_safe` through the formula FROM path

**Files:**
- Modify: `observability_migration/adapters/source/datadog/translate.py:617-736`
  (`_translate_formula_metric_widget`)
- Modify: `observability_migration/adapters/source/datadog/translate.py:1216-1261`
  (`_try_translate_formula_reducer`)
- Modify: `observability_migration/adapters/source/datadog/translate.py:1271-1310ish`
  (`_try_translate_count_formula_pipeline`)
- Modify: `observability_migration/adapters/source/datadog/translate.py:1501-1512`
  (`_metric_dimension_exprs`)
- Test: `tests/test_datadog_adaptive_time_bucketing.py`

**Interfaces:**
- Consumes: `_time_bucket_expr` from Task 1; `_MetricQuerySpec.mq` field
  (existing); `_needs_rate`, `_DERIVATIVE_FORMULA_FNS`,
  `_collect_derivative_query_refs` (existing).
- Produces: `_metric_dimension_exprs(..., rate_safe: bool = False)`, consumed
  only within this file.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_datadog_adaptive_time_bucketing.py`:

```python
from observability_migration.adapters.source.datadog.models import WidgetFormula
from observability_migration.adapters.source.datadog.query_parser import parse_formula


class TestFormulaFromPathBucketSplit(unittest.TestCase):
    def _formula_widget(self, queries: list[tuple[str, str]], formula_raw: str) -> NormalizedWidget:
        wqs = []
        for name, query in queries:
            mq = parse_metric_query(query)
            wqs.append(WidgetQuery(name=name, data_source="metrics", raw_query=query, metric_query=mq, query_type="metric"))
        wf = WidgetFormula(raw=formula_raw)
        wf.expression = parse_formula(formula_raw)
        return NormalizedWidget(id="1", widget_type="timeseries", title="w", queries=wqs, formulas=[wf])

    def test_plain_ratio_formula_uses_75_bucket(self):
        widget = self._formula_widget(
            [("query1", "sum:haproxy.backend.response.2xx{*}"), ("query2", "sum:haproxy.backend.response.4xx{*}")],
            "query1 / (query1 + query2) * 100",
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertIn("BUCKET(@timestamp, 75, ?_tstart, ?_tend)", result.esql_query)

    def test_rate_formula_on_gauge_uses_20_bucket(self):
        widget = self._formula_widget([("query1", "avg:mysql.performance.user_time{*}")], "rate(query1)")
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertIn("BUCKET(@timestamp, 20, ?_tstart, ?_tend)", result.esql_query)

    def test_diff_formula_on_gauge_uses_20_bucket(self):
        widget = self._formula_widget([("query1", "sum:redis.keyspace.hits{*}")], "diff(query1)")
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertIn("BUCKET(@timestamp, 20, ?_tstart, ?_tend)", result.esql_query)

    def test_as_rate_query_used_in_formula_uses_20_bucket(self):
        widget = self._formula_widget(
            [("query1", "sum:http.requests{*}.as_rate()"), ("query2", "sum:http.errors{*}.as_rate()")],
            "query1 - query2",
        )
        result = translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        self.assertIn("BUCKET(@timestamp, 20, ?_tstart, ?_tend)", result.esql_query)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_datadog_adaptive_time_bucketing.py -v`
Expected: the three rate/diff tests FAIL (still emit
`BUCKET(@timestamp, 75, ...)`); the plain-ratio test already PASSES.

- [ ] **Step 3: Write minimal implementation**

In `_metric_dimension_exprs`, add the parameter:

```python
def _metric_dimension_exprs(
    group_fields: list[str],
    include_time_bucket: bool,
    rate_safe: bool = False,
) -> tuple[list[str], list[str]]:
    exprs: list[str] = []
    aliases: list[str] = []
    if include_time_bucket:
        exprs.append(f"time_bucket = {_time_bucket_expr(rate_safe)}")
        aliases.append("time_bucket")
    exprs.extend(group_fields)
    aliases.extend(group_fields)
    return exprs, aliases
```

In `_translate_formula_metric_widget`, compute `rate_safe` before the
existing `dim_exprs, dim_aliases = _metric_dimension_exprs(...)` call (the
existing `derivative_refs` variable a few lines below is computed too late
to reuse directly — add a small standalone check instead, without
reordering the existing code):

```python
    include_time_bucket = (
        plan.kibana_type in ("xy", "heatmap")
        or reducer is not None
        or bool(output_reducers)
    )
    rate_safe = any(
        spec.mq.as_rate or _needs_rate(spec.mq) for spec in used_specs
    ) or any(
        _collect_derivative_query_refs(formula.ast) for formula in formulas
    )
    dim_exprs, dim_aliases = _metric_dimension_exprs(
        used_specs[0].group_fields,
        include_time_bucket=include_time_bucket,
        rate_safe=rate_safe,
    )
```

In `_try_translate_formula_reducer` (the `count_nonzero`/`count_not_null`
single-formula path), pass `rate_safe=False` explicitly at its
`_metric_dimension_exprs` call. This keeps the flat default even when the
referenced query emits rate math (e.g. via `.as_rate()` or a metric-map
`transform: to_rate` override) — a known gap documented in the design doc
§3.2, out of scope for this plan:

```python
    dim_exprs, _ = _metric_dimension_exprs(
        spec.group_fields,
        include_time_bucket=plan.kibana_type in ("xy", "heatmap"),
        rate_safe=False,
    )
```

Do the same in `_try_translate_count_formula_pipeline`:

```python
    dim_exprs, _dim_aliases = _metric_dimension_exprs(
        spec.group_fields,
        include_time_bucket=include_time_bucket,
        rate_safe=False,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_datadog_adaptive_time_bucketing.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Run the full Datadog unit suite**

Run: `.venv/bin/pytest tests/test_datadog_migrate.py -v 2>&1 | tail -80`
Expected: same class of failures as Task 2 Step 5 (literal `50` bucket
assertions), no new exception-shaped failures.

- [ ] **Step 6: Commit**

```bash
git add observability_migration/adapters/source/datadog/translate.py tests/test_datadog_adaptive_time_bucketing.py
git commit -m "feat(datadog): rate-safe FROM-path bucketing for formula widgets"
```

---

### Task 4: Convert the `TS` path to adaptive, windowless bucketing

**Files:**
- Modify: `observability_migration/adapters/source/datadog/translate.py:770-796`
  (the `ts_rate_spec` branch of `_translate_formula_metric_widget`)
- Modify: `tests/test_datadog_migrate.py:1728-1789`
  (`test_rate_formula_uses_ts_rate_when_metric_is_counter_typed`,
  `test_diff_formula_uses_ts_increase_when_metric_is_counter_typed`)
- Test: `tests/test_datadog_adaptive_time_bucketing.py`

**Interfaces:**
- Consumes: `_ADAPTIVE_RATE_BUCKETS` from Task 1.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_datadog_adaptive_time_bucketing.py`:

```python
from copy import deepcopy

from observability_migration.core.verification.field_capabilities import FieldCapability


class TestTsRatePathAdaptiveWindowless(unittest.TestCase):
    def _counter_profile(self, es_field: str, metric_type: str = "counter_long"):
        profile = deepcopy(OTEL_PROFILE)
        profile.field_caps[es_field] = FieldCapability(
            name=es_field, type=metric_type, time_series_metric_kind="counter",
        )
        return profile

    def test_ts_rate_uses_adaptive_20_bucket_no_window(self):
        profile = self._counter_profile("parity_counter")
        query = "sum:parity.counter{host:h1}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        wf = WidgetFormula(raw="rate(query1)")
        wf.expression = parse_formula("rate(query1)")
        widget = NormalizedWidget(id="1", widget_type="timeseries", title="w", queries=[wq], formulas=[wf])
        result = translate_widget(widget, plan_widget(widget), profile)
        self.assertIn("TBUCKET(20, ?_tstart, ?_tend)", result.esql_query)
        self.assertIn("RATE(parity_counter)", result.esql_query)
        self.assertNotIn("RATE(parity_counter, 5 minute)", result.esql_query)
        self.assertNotIn("TBUCKET(5 minute)", result.esql_query)

    def test_ts_increase_uses_adaptive_20_bucket_no_window(self):
        profile = self._counter_profile("parity_counter", "counter_double")
        query = "sum:parity.counter{host:h1}"
        mq = parse_metric_query(query)
        wq = WidgetQuery(name="query1", data_source="metrics", raw_query=query, metric_query=mq, query_type="metric")
        wf = WidgetFormula(raw="diff(query1)")
        wf.expression = parse_formula("diff(query1)")
        widget = NormalizedWidget(id="1", widget_type="timeseries", title="w", queries=[wq], formulas=[wf])
        result = translate_widget(widget, plan_widget(widget), profile)
        self.assertIn("TBUCKET(20, ?_tstart, ?_tend)", result.esql_query)
        self.assertIn("INCREASE(parity_counter)", result.esql_query)
        self.assertNotIn("INCREASE(parity_counter, 5 minute)", result.esql_query)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_datadog_adaptive_time_bucketing.py -v`
Expected: both new `TestTsRatePathAdaptiveWindowless` tests FAIL — the
emitted query still contains `TBUCKET(5 minute)` / `RATE(parity_counter, 5 minute)`.

- [ ] **Step 3: Write minimal implementation**

Replace the entire `ts_rate_spec is not None` branch body in
`_translate_formula_metric_widget` (this is the whole branch, including the
warning and `return` — nothing below it needs to change):

```python
    if ts_rate_spec is not None:
        # ES|QL native TS aggregation:
        # rate / monotonic counter rate / increase per bucket.
        es_agg = "RATE" if ts_fn_name == "rate" else "INCREASE"
        spec = ts_rate_spec
        alias = _safe_alias(formulas[0].alias or formulas[0].raw or f"{ts_fn_name}_{spec.alias}")
        by_clause = f"time_bucket = TBUCKET({_ADAPTIVE_RATE_BUCKETS}, ?_tstart, ?_tend)"
        if spec.group_fields:
            by_clause += ", " + ", ".join(spec.group_fields)
        ts_lines = [
            f"TS {spec.index}",
            f"| WHERE {spec.where_str}",
            f"| STATS {alias} = {es_agg}({spec.es_metric}) BY {by_clause}",
            "| KEEP time_bucket, " + ", ".join(spec.group_fields + [alias])
            if spec.group_fields
            else f"| KEEP time_bucket, {alias}",
            "| SORT time_bucket",
        ]
        if result is not None:
            _append_unique_warning(
                result,
                f"{ts_fn_name}() translated via ES|QL TS|QL "
                f"{es_agg}({spec.es_metric}) — requires the target "
                f"field to be a counter in a time_series index",
            )
        return "\n".join(ts_lines)
```

The only changes from the original are: the `window = "5 minute"` line is
deleted, `by_clause` uses `TBUCKET({_ADAPTIVE_RATE_BUCKETS}, ?_tstart, ?_tend)`
instead of `TBUCKET({window})`, and both the `STATS {alias} = {es_agg}(...)`
call and the warning message drop their trailing `, {window}` argument.

Now fix the two now-outdated assertions in `tests/test_datadog_migrate.py`:

```python
        self.assertIn("TS metrics-*", result.esql_query)
        self.assertIn("RATE(parity_counter)", result.esql_query)
        self.assertIn("TBUCKET(20, ?_tstart, ?_tend)", result.esql_query)
        # FIRST/LAST fallback should NOT appear when we go the TS path.
        self.assertNotIn("FIRST(parity_counter", result.esql_query)
```

and:

```python
        self.assertIn("TS metrics-*", result.esql_query)
        self.assertIn("INCREASE(parity_counter)", result.esql_query)
        self.assertIn("TBUCKET(20, ?_tstart, ?_tend)", result.esql_query)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_datadog_adaptive_time_bucketing.py tests/test_datadog_migrate.py -k "TsRatePath or ts_rate or ts_increase" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add observability_migration/adapters/source/datadog/translate.py tests/test_datadog_migrate.py tests/test_datadog_adaptive_time_bucketing.py
git commit -m "feat(datadog): adaptive windowless TS-path rate bucketing"
```

---

### Task 5: Regenerate snapshot fixtures and reconcile literal-bucket assertions

**Files:**
- Modify: `tests/snapshots/datadog_to_esql/*.txt` (regenerated, not hand-edited)
- Modify: `tests/test_datadog_migrate.py` (literal `BUCKET(@timestamp, 50, ...)`
  assertions flagged in Task 2/3 Step 5)

- [ ] **Step 1: Regenerate snapshots**

Run: `UPDATE_SNAPSHOTS=1 .venv/bin/pytest tests/test_datadog_esql_snapshots.py -v`
Expected: PASS, with every snapshot file under `tests/snapshots/datadog_to_esql/`
rewritten in place.

- [ ] **Step 2: Review the diff for correctness**

Run: `git diff tests/snapshots/datadog_to_esql/ | grep -E '^[+-].*BUCKET'`

Confirm every changed line follows exactly one of these two shapes — flag
anything else for manual review before continuing:
- `BUCKET(@timestamp, 50, ?_tstart, ?_tend)` → `BUCKET(@timestamp, 75, ?_tstart, ?_tend)`
  for non-rate widgets (e.g. `avg_cpu_by_host.txt`, `min_space_agg_by_device.txt`,
  `p99_percentile_by_resource.txt`, `haproxy_success_rate_formula.txt` — a
  ratio formula with no rate()/diff()/as_rate, stays at the flat default).
- `BUCKET(@timestamp, 50, ?_tstart, ?_tend)` → `BUCKET(@timestamp, 20, ?_tstart, ?_tend)`
  for rate/diff widgets: `as_rate_counter_sum.txt`, `default_zero_count_rate_formula.txt`
  (`.as_rate()` on the source query), `rate_formula_on_gauge_fallback.txt`,
  `formula_diff_counter.txt` (`rate()`/`diff()` formula wrapper).

- [ ] **Step 3: Fix remaining literal-bucket assertions in `test_datadog_migrate.py`**

Run: `.venv/bin/pytest tests/test_datadog_migrate.py -v 2>&1 | grep FAIL`

For each failing test, open it and update the literal bucket-width string
in its assertion to match the new value from Step 2's classification (`75`
for non-rate, `20` for rate/diff). Do not change any other part of the
assertion string.

- [ ] **Step 4: Run the full test suite**

Run: `.venv/bin/pytest tests/ -x -q 2>&1 | tail -40`
Expected: PASS, 0 failures.

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "test(datadog): regenerate snapshots for 75/20 adaptive bucket split"
```

---

### Task 6: Update `docs/sources/datadog.md`

**Files:**
- Modify: `docs/sources/datadog.md:561-562`

- [ ] **Step 1: Update the TS|QL/FROM path description**

Replace:

```markdown
  - **TS|QL path (preferred, counter-typed targets)**: when `time_series_metric_kind == "counter"` or `type ∈ {counter_long, counter_integer, counter_double}`, the translator emits `TS index | STATS rate_alias = RATE(metric, 5 minute) BY TBUCKET(5 minute)` (or `INCREASE(...)` for `diff`/`monotonic_diff`). This is the native ES|QL time-series aggregation — same pattern the Grafana adapter uses for PromQL `rate()`. Mirrors Datadog counter-rate semantics directly.
  - **FROM + FIRST/LAST path (fallback, gauges)**: when no counter capability is detected, the `STATS` clause emits `FIRST(metric, @timestamp)` and `LAST(metric, @timestamp)` alongside the standard aggregation, and `EVAL` computes `(last − first) / bucket_span_seconds` for `rate()` or `(last − first)` for `diff()`. A per-aggregation `WHERE metric IS NOT NULL` guard skips rows where the target column is null (needed when multiple metrics share the index).
```

with:

```markdown
  - **TS|QL path (preferred, counter-typed targets)**: when `time_series_metric_kind == "counter"` or `type ∈ {counter_long, counter_integer, counter_double}`, the translator emits `TS index | STATS rate_alias = RATE(metric) BY TBUCKET(20, ?_tstart, ?_tend)` (or `INCREASE(...)` for `diff`/`monotonic_diff`) — an adaptive, windowless bucket that grows/shrinks with the dashboard time range, same pattern the Grafana adapter uses for PromQL `rate()`/`irate()`. `20` is a live-verified rate-safe floor (see `docs/design/datadog-esql-time-bucketing-adaptivity.md`), not a fixed `5 minute` window. Mirrors Datadog counter-rate semantics directly.
  - **FROM + FIRST/LAST path (fallback, gauges)**: when no counter capability is detected, the `STATS` clause emits `FIRST(metric, @timestamp)` and `LAST(metric, @timestamp)` alongside the standard aggregation, and `EVAL` computes `(last − first) / bucket_span_seconds` for `rate()` or `(last − first)` for `diff()`. The `time_bucket` grouping for this path also uses the rate-safe 20-bucket floor (rather than the generic 75-bucket chart-resolution default) whenever the query or formula needs rate safety, for the same reason — too fine a bucket can make `FIRST`/`LAST` land in the same row, silently reading a wrong (not null) rate. A per-aggregation `WHERE metric IS NOT NULL` guard skips rows where the target column is null (needed when multiple metrics share the index).
```

- [ ] **Step 2: Verify no other stale `5 minute`/`BUCKET(@timestamp, 50` references remain in the doc**

Run: `grep -n "5 minute\|BUCKET(@timestamp, 50" docs/sources/datadog.md`
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add docs/sources/datadog.md
git commit -m "docs(datadog): describe adaptive 75/20 bucket split and windowless TS rate"
```

---

### Task 7: Full regression gate

**Files:** none (verification only)

- [ ] **Step 1: Full test suite**

Run: `make test`
Expected: PASS, 0 failures.

- [ ] **Step 2: Lint**

Run: `make lint`
Expected: PASS (ruff clean; source-header check clean — Task 1's new test
file needs the same SPDX header block used throughout this plan's code
snippets).

- [ ] **Step 3: Typecheck**

Run: `make typecheck`
Expected: PASS.

- [ ] **Step 4: Confirm no remaining `BUCKET(@timestamp, 50` / fixed `TBUCKET(5 minute)` in generated output**

Run: `grep -rn "BUCKET(@timestamp, 50\|TBUCKET(5 minute)" observability_migration/adapters/source/datadog/ tests/snapshots/datadog_to_esql/`
Expected: no output.

- [ ] **Step 5: Cross-link the parent design doc's Definition of Done**

Confirm `docs/design/esql-time-bucketing-strategy.md` §6 item 3 and its
Definition of Done already point at
`docs/design/datadog-esql-time-bucketing-adaptivity.md` (done during
brainstorming, in the same commit range as this plan's design doc) — no
further edit needed unless review flagged something.

- [ ] **Step 6: Update this design doc's own Definition of Done**

In `docs/design/datadog-esql-time-bucketing-adaptivity.md`, check off all
four Definition of Done items now that Tasks 1-6 are complete.

- [ ] **Step 7: Commit**

```bash
git add docs/design/datadog-esql-time-bucketing-adaptivity.md
git commit -m "docs(datadog): mark adaptive time-bucketing plan complete"
```
