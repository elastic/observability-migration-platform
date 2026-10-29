# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Counter functions over fields the live target stores as gauges.

Curated-pack sweep on Prometheus remote-write into ES ``/_prometheus``: exporter
``untyped`` counters (MySQL status, ``pg_stat_database_*``, node vmstat/netstat)
are stored as gauges, ES|QL ``RATE`` rejected them, and ~40 panels across the
MySQL, PostgreSQL and Node Exporter packs errored. The rewrite computes the rate
as ``DELTA(x, W) / W``, which matched Prometheus ``rate()`` on those panels
(10.741342 vs 10.74134; 0.499990 vs 0.499989).
"""

import unittest
from types import SimpleNamespace

from observability_migration.adapters.source.grafana.counter_typing import (
    apply_gauge_typed_counter_rewrites,
    rewrite_gauge_typed_counter_calls,
)
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver

_GAUGES = {"mysql_global_status_queries", "pg_stat_database_xact_commit", "metrics.node_vmstat_pgpgin"}


def _gauge(field):
    return field in _GAUGES


class RewriteTests(unittest.TestCase):
    def test_rate_uses_the_default_window_with_auto_buckets(self):
        query = "TS m | STATS q = SUM(RATE(mysql_global_status_queries)) BY tb = TBUCKET(20, ?_tstart, ?_tend)"
        out, fields = rewrite_gauge_typed_counter_calls(query, _gauge)
        self.assertEqual(
            out,
            "TS m | STATS q = SUM((DELTA(mysql_global_status_queries, 5 minutes) / 300)) "
            "BY tb = TBUCKET(20, ?_tstart, ?_tend)",
        )
        self.assertEqual(fields, ["mysql_global_status_queries"])

    def test_fixed_bucket_and_explicit_windows(self):
        bucketed = "TS m | STATS r = SUM(IRATE(pg_stat_database_xact_commit)) BY tb = TBUCKET(2 minute)"
        self.assertIn(
            "(DELTA(pg_stat_database_xact_commit, 2 minutes) / 120)",
            rewrite_gauge_typed_counter_calls(bucketed, _gauge)[0],
        )
        windowed = "TS m | STATS r = SUM(RATE(pg_stat_database_xact_commit, 1 minute)) BY tb = TBUCKET(5 minute)"
        self.assertIn(
            "(DELTA(pg_stat_database_xact_commit, 1 minute) / 60)",
            rewrite_gauge_typed_counter_calls(windowed, _gauge)[0],
        )

    def test_increase_becomes_delta_and_backticks_survive(self):
        query = "TS m | STATS a = SUM(INCREASE(`metrics.node_vmstat_pgpgin`)), b = SUM(INCREASE(mysql_global_status_queries, 1 hour))"
        out, fields = rewrite_gauge_typed_counter_calls(query, _gauge)
        self.assertIn("SUM(DELTA(`metrics.node_vmstat_pgpgin`))", out)
        self.assertIn("SUM(DELTA(mysql_global_status_queries, 1 hour))", out)
        self.assertEqual(fields, ["metrics.node_vmstat_pgpgin", "mysql_global_status_queries"])

    def test_counters_promql_and_unknown_fields_are_untouched(self):
        for query in (
            "TS m | STATS r = SUM(RATE(redis_commands_processed_total)) BY tb = TBUCKET(1 minute)",
            "PROMQL index=m value=(rate(mysql_global_status_queries[5m]))",
            "",
        ):
            with self.subTest(query=query):
                self.assertEqual(rewrite_gauge_typed_counter_calls(query, _gauge), (query, []))


_GAUGE_CAPS = {"double": {"type": "double", "searchable": True, "aggregatable": True, "time_series_metric": "gauge"}}
_COUNTER_CAPS = {"double": {"type": "double", "searchable": True, "aggregatable": True, "time_series_metric": "counter"}}
_KEYWORD_CAPS = {"keyword": {"type": "keyword", "searchable": True, "aggregatable": True}}


def _resolver(caps):
    resolver = SchemaResolver(RulePackConfig())
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    resolver._field_cache = dict(caps)
    return resolver


class ResolverAndPassTests(unittest.TestCase):
    def test_field_refutes_counter_needs_positive_evidence(self):
        resolver = _resolver({"g": _GAUGE_CAPS, "c": _COUNTER_CAPS, "k": _KEYWORD_CAPS})
        self.assertTrue(resolver.field_refutes_counter("g"))
        self.assertFalse(resolver.field_refutes_counter("c"))
        self.assertFalse(resolver.field_refutes_counter("k"))
        self.assertFalse(resolver.field_refutes_counter("absent"))
        self.assertFalse(SchemaResolver(RulePackConfig()).field_refutes_counter("g"))

    def test_pass_rewrites_query_ir_and_replaces_the_kept_rate_warning(self):
        query = "TS m | STATS q = SUM(RATE(mysql_global_status_queries)) BY tb = TBUCKET(20, ?_tstart, ?_tend)"
        panel = SimpleNamespace(
            esql_query=query,
            query_ir={"target_query": query},
            status="migrated",
            reasons=[
                "Source PromQL used rate() on mysql_global_status_queries but the target currently types "
                "this field as gauge; kept RATE because rate() is counter-only ...",
                "unrelated",
            ],
        )
        result = SimpleNamespace(panel_results=[panel])

        changed = apply_gauge_typed_counter_rewrites([result], _resolver({"mysql_global_status_queries": _GAUGE_CAPS}))

        self.assertEqual(changed, 1)
        self.assertIn("DELTA(mysql_global_status_queries, 5 minutes) / 300", panel.esql_query)
        self.assertEqual(panel.query_ir["target_query"], panel.esql_query)
        self.assertEqual(panel.status, "migrated_with_warnings")
        self.assertEqual(panel.reasons[0], "unrelated")
        self.assertIn("as a gauge", panel.reasons[1])

    def test_pass_is_a_no_op_offline(self):
        query = "TS m | STATS q = SUM(RATE(mysql_global_status_queries))"
        panel = SimpleNamespace(esql_query=query, query_ir={}, status="migrated", reasons=[])
        self.assertEqual(apply_gauge_typed_counter_rewrites([SimpleNamespace(panel_results=[panel])], SchemaResolver(RulePackConfig())), 0)
        self.assertEqual(panel.esql_query, query)


if __name__ == "__main__":
    unittest.main()
