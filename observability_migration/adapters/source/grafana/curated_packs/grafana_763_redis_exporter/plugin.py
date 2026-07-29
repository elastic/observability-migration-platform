# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import re

_MEMORY_RATIO_RE = re.compile(
    r"redis_memory_used_bytes.*redis_memory_max_bytes"
    r"|redis_memory_max_bytes.*redis_memory_used_bytes",
    re.IGNORECASE | re.DOTALL,
)

_NET_INPUT_RE = re.compile(r"\bredis_net_input_bytes_total\b", re.IGNORECASE)
_NET_OUTPUT_RE = re.compile(r"\bredis_net_output_bytes_total\b", re.IGNORECASE)


_PACK_MARKER = "_grafana_763_redis_exporter"


def register(api):
    # Mark the curated RulePackConfig so rules below only fire when this pack
    # is active.  The marker survives deepcopy into the merged pack returned by
    # resolve_pack_for_dashboard but is absent from any unrelated RulePackConfig.
    # This prevents the globally-registered rules from firing for other dashboards
    # that happen to use the same PromQL expressions (e.g. gnet_id=11835).
    setattr(api["rule_pack"], _PACK_MARKER, True)

    @api["query_classifiers"].register("redis_network_io_legend_fix", priority=0)
    def redis_network_io_legend_fix(context):
        """Remove phantom label group-by from Network I/O panel targets.

        The Grafana Network I/O panel uses ``{{ input }}`` / ``{{ output }}`` as
        legendFormat, but these are NOT real Prometheus labels on the
        redis_net_*_bytes_total metrics.  The general translator treats them as
        BY-clause dimensions, which prevents the two targets from fusing (different
        group columns) and emits only one series with a non-existent breakdown field.

        Clearing preferred_group_labels makes both targets group by time_bucket only
        so they merge correctly into a two-series line chart (input + output).
        """
        if not getattr(context.rule_pack, _PACK_MARKER, False):
            return None
        expr = context.promql_expr or ""
        is_input = bool(_NET_INPUT_RE.search(expr)) and not bool(_NET_OUTPUT_RE.search(expr))
        is_output = bool(_NET_OUTPUT_RE.search(expr)) and not bool(_NET_INPUT_RE.search(expr))
        if not is_input and not is_output:
            return None
        context.metadata["preferred_group_labels"] = []
        context.metadata.pop("preferred_group_labels_origin", None)
        context.metadata["series_alias"] = "input" if is_input else "output"
        return f"cleared phantom legend label for redis network {'input' if is_input else 'output'} series"

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
        if not getattr(context.rule_pack, _PACK_MARKER, False):
            return None
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

        Only fires when this curated pack's RulePackConfig is active (pack marker set)
        AND both metric names appear in the same PromQL expression.
        """
        if not getattr(context.rule_pack, _PACK_MARKER, False):
            return None
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
