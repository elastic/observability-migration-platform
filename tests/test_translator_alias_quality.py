# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for translator-side alias readability (Workstream B).

These tests pin down the public contract of the alias generators and the
end-to-end ES|QL emitted for multi-target panels with messy legend formats.
The goal is to keep STATS / EVAL aliases human-readable: no mid-word
60-character truncation, no ``on____instance`` substitution leaks, and no
``_matcher_alias_suffix`` noise once a refId-based suffix is already in play.
"""

from __future__ import annotations

import re
import unittest

from observability_migration.adapters.source.grafana import panels, promql, rules, schema


class SafeAliasTests(unittest.TestCase):
    def test_collapses_consecutive_underscores(self):
        """Whitespace runs and template braces should not stack underscores."""
        self.assertEqual(promql._safe_alias("foo  bar"), "foo_bar")
        self.assertEqual(promql._safe_alias("a {{b}} c"), "a_b_c")

    def test_returns_value_for_empty_input(self):
        self.assertEqual(promql._safe_alias(""), "value")
        self.assertEqual(promql._safe_alias(None), "value")

    def test_prefixes_leading_digit(self):
        self.assertEqual(promql._safe_alias("5m load"), "series_5m_load")

    def test_keeps_short_aliases_unchanged(self):
        self.assertEqual(
            promql._safe_alias("node_cpu_seconds_total", "A"),
            "node_cpu_seconds_total_A",
        )

    def test_truncates_at_word_boundary_for_long_aliases(self):
        """Aliases over the limit must keep complete underscore-delimited words."""
        long_metric = "prometheus_target_scrapes_exceeded_sample_limit_total"
        suffix = "_".join(["alpha", "beta", "gamma", "delta"] * 10)
        alias = promql._safe_alias(long_metric, suffix)
        self.assertLessEqual(len(alias), promql.MAX_ALIAS_LENGTH)
        self.assertFalse(alias.endswith("_"))
        valid_tokens = set(long_metric.split("_")) | {"alpha", "beta", "gamma", "delta"}
        for part in alias.split("_"):
            self.assertIn(
                part,
                valid_tokens,
                f"Alias {alias!r} contains mid-word truncated token {part!r}",
            )

    def test_no_three_underscore_runs(self):
        legend = "exceeded sample limit on  {{instance}}"
        alias = promql._safe_alias(legend)
        self.assertNotRegex(alias, r"__+")


class UniqueSafeAliasTests(unittest.TestCase):
    def test_legend_substitution_does_not_leak_into_alias(self):
        used: set[str] = set()
        alias = promql._unique_safe_alias(
            "exceeded sample limit on {{instance}}",
            used,
            fallback_suffix="A",
        )
        self.assertNotRegex(alias, r"_{2,}")
        self.assertNotIn("instance", alias.split("_"))
        self.assertEqual(alias, "exceeded_sample_limit_on")

    def test_legend_substitution_with_dotted_label(self):
        used: set[str] = set()
        alias = promql._unique_safe_alias(
            "request rate for {{cluster.name}}",
            used,
            fallback_suffix="A",
        )
        self.assertNotRegex(alias, r"_{2,}")
        self.assertNotIn("cluster", alias.split("_"))
        self.assertNotIn("name", alias.split("_"))
        self.assertEqual(alias, "request_rate_for")


class MultiTargetAliasIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.rule_pack = rules.RulePackConfig()
        self.resolver = schema.SchemaResolver(self.rule_pack)

    def translate_panel(self, panel):
        return panels.translate_panel(
            panel,
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=self.rule_pack,
            resolver=self.resolver,
        )

    def test_multi_target_stats_aliases_are_short_and_readable(self):
        """STATS aliases should be ``<metric>_<refId>`` without 60-char truncation."""
        panel = {
            "id": 99,
            "type": "graph",
            "title": "Service discovery errors",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {
                    "expr": (
                        'sum(increase(prometheus_target_scrapes_exceeded_sample_limit_total'
                        '{instance="$instance"}[$aggregation_interval])) by (instance) > 0'
                    ),
                    "refId": "A",
                    "legendFormat": "exceeded sample limit on {{instance}}",
                },
                {
                    "expr": (
                        'sum(increase(prometheus_sd_file_read_errors_total'
                        '{instance="$instance"}[$aggregation_interval])) by (instance) > 0'
                    ),
                    "refId": "E",
                    "legendFormat": "sd_file_read_error on {{instance}}",
                },
            ],
        }
        yaml_panel, result = self.translate_panel(panel)
        query = yaml_panel["esql"]["query"]
        self.assertIn("prometheus_target_scrapes_exceeded_sample_limit_total_A", query)
        self.assertIn("prometheus_sd_file_read_errors_total_E", query)
        self.assertNotRegex(query, r"_A_inst\b")
        self.assertNotRegex(query, r"_E_instance_increase_sum\b")
        self.assertNotRegex(query, r"\b\w*_inst\b")
        self.assertNotRegex(query, r"\b\w*_incr\b")
        for stmt in re.findall(r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]*)\s*=", query):
            self.assertLessEqual(
                len(stmt),
                promql.MAX_ALIAS_LENGTH,
                f"Alias {stmt!r} exceeds MAX_ALIAS_LENGTH",
            )
        self.assertIn(result.status, {"migrated", "migrated_with_warnings"})

    def test_multi_target_eval_aliases_strip_template_substitution(self):
        """EVAL aliases derived from legend formats must not leak ``{{label}}``."""
        panel = {
            "id": 100,
            "type": "graph",
            "title": "Errors",
            "datasource": {"type": "prometheus", "uid": "prom"},
            "targets": [
                {
                    "expr": (
                        'sum(increase(foo_errors_total'
                        '{instance="$instance"}[$aggregation_interval])) by (instance) > 0'
                    ),
                    "refId": "A",
                    "legendFormat": "foo errors on {{instance}}",
                },
                {
                    "expr": (
                        'sum(increase(bar_errors_total'
                        '{instance="$instance"}[$aggregation_interval])) by (instance) > 0'
                    ),
                    "refId": "B",
                    "legendFormat": "bar errors on  {{instance}}",
                },
            ],
        }
        yaml_panel, _ = self.translate_panel(panel)
        query = yaml_panel["esql"]["query"]
        self.assertNotRegex(query, r"on_{2,}instance")
        self.assertNotRegex(query, r"on___+instance")
        eval_aliases = re.findall(r"\| EVAL ([A-Za-z][A-Za-z0-9_]*) =", query)
        self.assertIn("foo_errors_on", eval_aliases)
        self.assertIn("bar_errors_on", eval_aliases)
        self.assertNotIn("foo_errors_on____instance", eval_aliases)


class MeasureSpecAliasTests(unittest.TestCase):
    """Single-target specs use refId when supplied; otherwise matcher_alias_suffix."""

    def setUp(self):
        self.rule_pack = rules.RulePackConfig()
        self.resolver = schema.SchemaResolver(self.rule_pack)

    def _spec(self, expr, alias_hint=""):
        frag = promql._parse_fragment(promql.preprocess_grafana_macros(expr, self.rule_pack))
        return promql._build_measure_spec(
            frag, self.resolver, self.rule_pack, alias_hint=alias_hint,
        )

    def test_alias_hint_takes_precedence_over_matcher_suffix(self):
        """When refId is present, the alias is ``<metric>_<refId>`` only."""
        spec = self._spec(
            'sum(increase(prometheus_sd_file_read_errors_total'
            '{instance="x"}[5m])) by (instance)',
            alias_hint="E",
        )
        self.assertIsNotNone(spec)
        self.assertEqual(spec.alias, "prometheus_sd_file_read_errors_total_E")

    def test_no_alias_hint_falls_back_to_matcher_suffix(self):
        """Single-target binary expressions still get matcher-derived suffixes."""
        spec = self._spec(
            'sum(rate(http_requests_total{status="500"}[5m]))',
            alias_hint="",
        )
        self.assertIsNotNone(spec)
        self.assertNotEqual(spec.alias, "http_requests_total")
        self.assertTrue(spec.alias.startswith("http_requests_total_"))


class MixedTimeSeriesStatsTests(unittest.TestCase):
    """A single TS ``STATS`` must never mix bare and wrapped time-series aggregates.

    Elasticsearch rejects ``TS ... | STATS x = AVG_OVER_TIME(...), y =
    AVG(AVG_OVER_TIME(...)) BY time_bucket, dim`` at runtime with "Cannot mix
    time-series aggregate ... and regular aggregate ... in the same
    TimeSeriesAggregate". This reproduces the multi-target merge that previously
    left an instant-selector target bare alongside ``irate`` siblings.
    """

    _BARE_TS = re.compile(r"^[A-Z_]+_OVER_TIME\(")
    _WRAPPED_TS = re.compile(r"^(?:AVG|SUM|MIN|MAX|COUNT)\(\s*[A-Z_]+_OVER_TIME\(")

    def _spec(self, *, stats_expr, alias, metric_field, group_fields):
        return promql.MeasureSpec(
            source_type="TS",
            time_filter="@timestamp >= ?_tstart AND @timestamp <= ?_tend",
            bucket_expr="time_bucket = TBUCKET(5 minute)",
            group_fields=list(group_fields),
            filters=[],
            alias=alias,
            stats_expr=stats_expr,
            final_alias=alias,
            metric_field=metric_field,
        )

    def test_bare_ts_target_is_wrapped_when_grouped_with_regular_aggregate(self):
        specs = [
            self._spec(
                stats_expr="AVG(AVG_OVER_TIME(process_virtual_memory_bytes, 5m))",
                alias="process_virtual_memory_bytes_A",
                metric_field="process_virtual_memory_bytes",
                group_fields=["instance", "job"],
            ),
            self._spec(
                stats_expr="AVG_OVER_TIME(process_resident_memory_max_bytes, 5m)",
                alias="process_resident_memory_max_bytes_B",
                metric_field="process_resident_memory_max_bytes",
                group_fields=["instance", "job"],
            ),
        ]

        normalized = promql._normalize_mixed_ts_stats_exprs(specs)
        exprs = [s.stats_expr.strip() for s in normalized]

        # No term may be a bare time-series aggregate now.
        self.assertFalse(
            any(self._BARE_TS.match(e) for e in exprs),
            f"bare time-series aggregate survived: {exprs!r}",
        )
        # The instant-selector target is wrapped in a matching outer aggregate.
        self.assertIn(
            "AVG(AVG_OVER_TIME(process_resident_memory_max_bytes, 5m))",
            exprs,
        )

    def test_shared_pipeline_does_not_mix_bare_and_wrapped_ts_aggregates(self):
        specs = [
            self._spec(
                stats_expr="AVG(AVG_OVER_TIME(process_virtual_memory_bytes, 5m))",
                alias="process_virtual_memory_bytes_A",
                metric_field="process_virtual_memory_bytes",
                group_fields=["instance", "job"],
            ),
            self._spec(
                stats_expr="AVG_OVER_TIME(process_resident_memory_max_bytes, 5m)",
                alias="process_resident_memory_max_bytes_B",
                metric_field="process_resident_memory_max_bytes",
                group_fields=["instance", "job"],
            ),
        ]

        result = promql._build_shared_measure_pipeline("metrics-*", specs)
        self.assertIsNotNone(result)
        parts, _, _ = result
        stats_line = next(line for line in parts if line.startswith("| STATS"))

        # A bare TS term appears as ``alias = X_OVER_TIME(`` right after STATS or
        # a comma; a wrapped term as ``= AGG(X_OVER_TIME(``. The two must not
        # coexist in one STATS.
        has_bare = bool(
            re.search(r"(?:STATS|,)\s*[A-Za-z0-9_.`]+\s*=\s*[A-Z]+_OVER_TIME\(", stats_line)
        )
        has_wrapped = bool(
            re.search(r"=\s*(?:AVG|SUM|MIN|MAX|COUNT)\(\s*[A-Z]+_OVER_TIME\(", stats_line)
        )
        self.assertFalse(
            has_bare and has_wrapped,
            f"STATS mixes bare and wrapped time-series aggregates: {stats_line!r}",
        )

    def test_bucket_only_single_target_keeps_bare_ts_aggregate(self):
        # Time-bucket-only grouping with a single TS term must keep the bare
        # form — wrapping it would needlessly change long-standing output and is
        # not required for validity.
        specs = [
            self._spec(
                stats_expr="MAX_OVER_TIME(node_memory_MemAvailable_bytes, 1h)",
                alias="node_memory_MemAvailable_bytes",
                metric_field="node_memory_MemAvailable_bytes",
                group_fields=[],
            ),
        ]

        normalized = promql._normalize_mixed_ts_stats_exprs(specs)
        self.assertEqual(
            normalized[0].stats_expr,
            "MAX_OVER_TIME(node_memory_MemAvailable_bytes, 1h)",
        )


if __name__ == "__main__":
    unittest.main()
