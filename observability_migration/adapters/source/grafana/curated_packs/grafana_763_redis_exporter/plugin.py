# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import re

_MEMORY_RATIO_RE = re.compile(
    r"redis_memory_used_bytes.*redis_memory_max_bytes"
    r"|redis_memory_max_bytes.*redis_memory_used_bytes",
    re.IGNORECASE | re.DOTALL,
)


def register(api):
    @api["query_classifiers"].register("redis_memory_ratio_unblock", priority=0)
    def redis_memory_ratio_unblock(context):
        """Clear not_feasible_reasons before fragment_guardrails/family_classifier run.

        The binary_expr parser marks cross-metric division as not_feasible — a correct
        default for independent time series that can't be safely joined in ES|QL.
        For this specific redis memory ratio, both metrics are written into the same
        prometheus_native document per label-set by the redis_scraper, so an ES|QL
        EVAL can compute the ratio within a single FROM query.  We clear the block
        here (priority=0, before fragment_guardrails at priority=1) so the translator
        at priority=0 can run.
        """
        if not _MEMORY_RATIO_RE.search(context.promql_expr or ""):
            return None
        frag = context.fragment
        if frag and isinstance(getattr(frag, "extra", None), dict):
            frag.extra.pop("not_feasible_reasons", None)
        return "cleared not_feasible_reasons for co-located redis memory ratio"

    @api["query_translators"].register("redis_memory_ratio", priority=0)
    def redis_memory_ratio_rule(context):
        """Translate the memory-usage ratio formula to a co-located EVAL query.

        PromQL: sum(100 * (redis_memory_used_bytes{...} / redis_memory_max_bytes{...}))

        The standard binary_expr translator marks this not_feasible because it cannot
        safely divide two independent time series in ES|QL.  In the prometheus_native
        scraper layout both metrics are written into the same document per label-set,
        so the ratio can be computed via EVAL within a single FROM query.

        Only fires when both metric names appear in the same PromQL expression.
        """
        if not _MEMORY_RATIO_RE.search(context.promql_expr or ""):
            return None

        index = context.index or "metrics-redis.prometheus-default"
        esql = "\n".join([
            f"FROM {index}",
            "| WHERE metrics.redis_memory_used_bytes IS NOT NULL",
            "  AND metrics.redis_memory_max_bytes IS NOT NULL",
            "  AND metrics.redis_memory_max_bytes > 0",
            "| WHERE labels.instance RLIKE ?instance",
            "| EVAL memory_pct = 100.0 * metrics.redis_memory_used_bytes / metrics.redis_memory_max_bytes",
            "| STATS memory_pct = AVG(memory_pct) BY time_bucket = TBUCKET(100, ?_tstart, ?_tend)",
            "| SORT time_bucket ASC",
            "| STATS time_bucket = MAX(time_bucket), memory_pct = MAX(memory_pct)",
            "| KEEP time_bucket, memory_pct",
        ])

        context.esql_query = esql
        context.output_metric_field = "memory_pct"
        context.metric_name = "redis_memory_usage_pct"
        context.feasibility = "feasible"
        context.confidence = 0.85
        context.translation_complete = True
        api["append_unique"](
            context.warnings,
            "Memory ratio (used/max × 100) computed via EVAL — valid when both metrics are "
            "co-located in the same prometheus_native document (default scraper layout); "
            "panel shows empty when Redis maxmemory=0 (no limit configured)",
        )
        return "translated co-located memory ratio via EVAL"
