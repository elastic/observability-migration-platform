# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import re

# ``avg( irate(hits) / (irate(misses) + irate(hits)) ) by (instance)`` — the
# same metric appears in both numerator and denominator, which the general
# binary-expression translator cannot align, so it degrades to not_feasible.
_HIT_RATIO_RE = re.compile(
    r"redis_keyspace_hits_total.*redis_keyspace_misses_total.*redis_keyspace_hits_total",
    re.IGNORECASE | re.DOTALL,
)

_PACK_MARKER = "_grafana_14091_redis_exporter_quickstart"


def register(api):
    # Mark the curated RulePackConfig so these rules only fire for gnetId 14091.
    # Without the marker the globally-registered rules would also rewrite the
    # same expression on unrelated dashboards (763 and 11835 ship a similar
    # hits/misses panel).
    setattr(api["rule_pack"], _PACK_MARKER, True)

    @api["query_classifiers"].register("redis_hit_ratio_unblock", priority=0)
    def redis_hit_ratio_unblock(context):
        """Clear not_feasible before the blocking classifiers run.

        The binary_expr parser marks a self-referential ratio not-feasible — a
        correct default, since dividing independent series is unsafe in general.
        Here both counters are co-located in the same prometheus_native document
        per label-set, so a single STATS + EVAL computes the ratio exactly.
        """
        if not getattr(context.rule_pack, _PACK_MARKER, False):
            return None
        if not _HIT_RATIO_RE.search(context.promql_expr or ""):
            return None
        frag = context.fragment
        if frag and isinstance(getattr(frag, "extra", None), dict):
            frag.extra.pop("not_feasible_reasons", None)
        return "cleared not_feasible_reasons for co-located redis hit ratio"

    @api["query_translators"].register("redis_hit_ratio", priority=0)
    def redis_hit_ratio_rule(context):
        """Translate the cache hit ratio to a single-query ES|QL form.

        PromQL: avg(irate(hits[1m]) / (irate(misses[1m]) + irate(hits[1m]))) by (instance)

        Emitted as one panel (no split needed): SUM the two per-series IRATEs per
        bucket and instance, then EVAL the ratio. Mathematically identical to the
        source — verified against live data (hits=10/misses=10 -> 0.50;
        hits=8/misses=2 -> 0.80).

        The dashboard's second target (``expr: 1``, "Target hit ratio for cache")
        is a reference line. Kibana's Dashboards API (2023-10-31) has no
        reference-line layer — verified: posting a ``reference_line`` layer is
        rejected with HTTP 400 while the same panel without it returns 200 — and
        a constant column added to KEEP is not picked up as a second series. It
        is therefore dropped rather than carried as a dead column, and the loss
        is recorded in the fidelity manifest instead of being papered over.
        """
        if not getattr(context.rule_pack, _PACK_MARKER, False):
            return None
        if not _HIT_RATIO_RE.search(context.promql_expr or ""):
            return None

        index = context.index or "metrics-redis.prometheus-default"
        esql = "\n".join([
            f"TS {index}",
            "| WHERE labels.instance RLIKE ?instance",
            "| WHERE metrics.redis_keyspace_hits_total IS NOT NULL",
            "  OR metrics.redis_keyspace_misses_total IS NOT NULL",
            "| STATS hits = SUM(IRATE(metrics.redis_keyspace_hits_total, 1m)),"
            " misses = SUM(IRATE(metrics.redis_keyspace_misses_total, 1m))"
            " BY time_bucket = TBUCKET(100, ?_tstart, ?_tend), labels.instance",
            "| EVAL hit_ratio = hits / (hits + misses)",
            "| KEEP time_bucket, `labels.instance`, hit_ratio",
            "| SORT time_bucket ASC",
        ])

        context.esql_query = esql
        context.source_type = "TS"
        context.output_metric_field = "hit_ratio"
        context.metric_name = "redis_keyspace_hit_ratio"
        context.output_group_fields = ["time_bucket", "labels.instance"]
        context.feasibility = "feasible"
        context.confidence = 0.9
        context.translation_complete = True
        api["append_unique"](
            context.warnings,
            "Cache hit ratio computed as hits/(hits+misses) in a single ES|QL query — "
            "exact for co-located prometheus_native documents (default redis_exporter "
            "scrape layout). The 'Target hit ratio for cache' reference line from the "
            "source panel is dropped: the Kibana Dashboards API has no reference-line "
            "layer type, so there is no faithful single-panel equivalent",
        )
        return "translated co-located redis hit ratio via EVAL"
