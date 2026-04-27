"""Gap 3: panels referencing metrics the cluster does not have downgrade.

When a SchemaResolver is connected to a live cluster (``--es-url``), it can
report ``field_exists("kube_pod_info") -> False`` for metrics not in the
mapping. The translator's ``cluster_known_metrics`` validator consults this
signal and marks the panel ``not_feasible`` so the broken query is never
compiled into the dashboard. Without cluster discovery (``field_exists`` returns
``None``), the validator is a no-op and behavior is unchanged.
"""

from __future__ import annotations

import unittest

from observability_migration.adapters.source.grafana import translate
from observability_migration.adapters.source.grafana.rules import RulePackConfig


class _StubResolver:
    """Mimics SchemaResolver.field_exists for unit tests."""

    def __init__(self, missing=None, present=None, missing_patterns=None):
        self._missing = set(missing or [])
        self._present = set(present or [])
        self._missing_patterns = set(missing_patterns or [])

    def field_exists(self, field_name):
        if field_name in self._missing:
            return False
        if field_name in self._present:
            return True
        return None

    def index_pattern_has_any_concrete_index(self, pattern):
        if pattern in self._missing_patterns:
            return False
        return None

    def resolve_label(self, label):
        return label

    def resolve_labels(self, labels):
        return list(labels or [])

    def resolve_control_field(self, label):
        return label

    def field_capability(self, field):
        return None

    def is_numeric_field(self, field):
        return False

    def is_searchable_field(self, field):
        return True

    def is_aggregatable_field(self, field):
        return True

    def is_text_like_field(self, field):
        return False

    def has_conflicting_types(self, field):
        return False

    def is_counter(self, field):
        return False

    def field_type(self, field):
        return None

    def field_type_family(self, field):
        return None


class ExtractMetricReferencesTests(unittest.TestCase):
    def test_extracts_aggregator_argument(self):
        refs = translate._extract_metric_references(
            "FROM x | STATS m = AVG(node_cpu_seconds_total) BY time_bucket"
        )
        self.assertIn("node_cpu_seconds_total", refs)

    def test_extracts_is_not_null_predicate(self):
        refs = translate._extract_metric_references(
            "FROM x | WHERE kube_pod_info IS NOT NULL"
        )
        self.assertIn("kube_pod_info", refs)

    def test_excludes_time_aliases_and_internals(self):
        refs = translate._extract_metric_references(
            "FROM x | STATS time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), "
            "_ts = COALESCE(_timeseries, \"\") "
            "| EVAL computed_value = AVG(metric)"
        )
        self.assertNotIn("time_bucket", refs)
        self.assertNotIn("computed_value", refs)
        self.assertNotIn("_ts", refs)
        self.assertIn("metric", refs)

    def test_extracts_aggregator_field_even_when_alias_reuses_name(self):
        """``STATS metric = AVG(metric)`` keeps ``metric`` as a referenced field."""
        refs = translate._extract_metric_references(
            "FROM x | STATS metric = AVG(metric) BY time_bucket"
        )
        self.assertIn("metric", refs)
        self.assertNotIn("time_bucket", refs)

    def test_extracts_rlike_field(self):
        refs = translate._extract_metric_references(
            "FROM x | WHERE NOT (device RLIKE \"rootfs\")"
        )
        self.assertIn("device", refs)

    def test_skips_nested_aggregator_function_names(self):
        """``SUM(RATE(metric, 5m))`` should yield ``metric``, not ``RATE``."""
        refs = translate._extract_metric_references(
            "FROM x | STATS x = SUM(RATE(node_cpu_seconds_total, 5m))"
        )
        self.assertEqual(refs, {"node_cpu_seconds_total"})

    def test_skips_increase_function_in_sum_wrapper(self):
        refs = translate._extract_metric_references(
            "FROM x | STATS x = SUM(INCREASE(http_requests_total, 5m))"
        )
        self.assertEqual(refs, {"http_requests_total"})

    def test_skips_label_var_leaks(self):
        """``label_<var>`` references are validated separately by the
        ``leaked_label_variables`` rule and must not be double-reported here."""
        refs = translate._extract_metric_references(
            "FROM x | WHERE metric IS NOT NULL "
            "| EVAL value = metric - label_scrape_interval"
        )
        self.assertNotIn("label_scrape_interval", refs)
        self.assertIn("metric", refs)


class ClusterKnownMetricsRuleTests(unittest.TestCase):
    def test_no_resolver_is_a_noop(self):
        result = translate.translate_promql_to_esql(
            "kube_pod_info{instance=\"foo\"}",
            datasource_index="metrics-*",
            rule_pack=RulePackConfig(),
            resolver=None,
        )
        self.assertNotEqual(result.feasibility, "not_feasible")

    def test_resolver_without_field_cache_is_a_noop(self):
        resolver = _StubResolver()
        result = translate.translate_promql_to_esql(
            "kube_pod_info{instance=\"foo\"}",
            datasource_index="metrics-*",
            rule_pack=RulePackConfig(),
            resolver=resolver,
        )
        self.assertNotEqual(result.feasibility, "not_feasible")

    def test_missing_metric_downgrades_panel(self):
        resolver = _StubResolver(missing={"kube_pod_info"})
        result = translate.translate_promql_to_esql(
            "kube_pod_info{instance=\"foo\"}",
            datasource_index="metrics-*",
            rule_pack=RulePackConfig(),
            resolver=resolver,
        )
        self.assertEqual(result.feasibility, "not_feasible")
        self.assertTrue(
            any("kube_pod_info" in w for w in result.warnings),
            f"expected missing-metric warning in {result.warnings!r}",
        )

    def test_present_metric_stays_feasible(self):
        resolver = _StubResolver(present={"node_cpu_seconds_total"})
        result = translate.translate_promql_to_esql(
            "rate(node_cpu_seconds_total[5m])",
            datasource_index="metrics-*",
            rule_pack=RulePackConfig(),
            resolver=resolver,
        )
        self.assertNotEqual(result.feasibility, "not_feasible")


class FromIndexPatternRuleTests(unittest.TestCase):
    def test_extract_from_picks_pattern(self):
        self.assertEqual(
            translate._extract_from_index_patterns("FROM metrics-prometheus-* | LIMIT 0"),
            ["metrics-prometheus-*"],
        )

    def test_extract_from_picks_ts_keyword(self):
        self.assertEqual(
            translate._extract_from_index_patterns("TS metrics-* | STATS x = AVG(metric)"),
            ["metrics-*"],
        )

    def test_extract_from_dedups_and_preserves_order(self):
        patterns = translate._extract_from_index_patterns(
            "FROM logs-* | EVAL m = METADATA(\"FROM logs-*\")"
        )
        self.assertEqual(patterns, ["logs-*"])

    def test_index_pattern_no_match_downgrades_panel(self):
        resolver = _StubResolver(missing_patterns={"logs-*"})
        # We can't run a logs panel through translate_promql_to_esql directly,
        # so simulate by building a minimal context and invoking the validator.
        ctx = translate.TranslationContext(
            promql_expr="logs",
            data_view="logs-*",
            index="logs-*",
            rule_pack=RulePackConfig(),
            resolver=resolver,
            esql_query="FROM logs-* | KEEP @timestamp, message",
            feasibility="feasible",
        )
        translate.cluster_index_pattern_has_data_rule(ctx)
        self.assertEqual(ctx.feasibility, "not_feasible")
        self.assertTrue(
            any("logs-*" in w for w in ctx.warnings),
            f"expected logs-* warning in {ctx.warnings!r}",
        )

    def test_index_pattern_no_resolver_is_a_noop(self):
        ctx = translate.TranslationContext(
            promql_expr="logs",
            data_view="logs-*",
            index="logs-*",
            rule_pack=RulePackConfig(),
            resolver=None,
            esql_query="FROM logs-* | KEEP @timestamp",
            feasibility="feasible",
        )
        translate.cluster_index_pattern_has_data_rule(ctx)
        self.assertEqual(ctx.feasibility, "feasible")


if __name__ == "__main__":
    unittest.main()
