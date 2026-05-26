# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Gap 2: panels with bare ``$varname`` that survive into ES|QL must downgrade.

The Grafana macro preprocessor rewrites unbound ``$var`` into ``label_<var>`` so
the PromQL parser does not choke. When that ``label_<var>`` token survives into
the final ES|QL (rather than being dropped by the matcher converter / LogQL
filter), the resulting query is invalid: ES validation rejects it with
"Unknown column [label_<var>]". A late-stage validator marks such panels
``not_feasible`` so the broken query never reaches Kibana.
"""

from __future__ import annotations

import unittest

from observability_migration.adapters.source.grafana import promql, translate
from observability_migration.adapters.source.grafana.rules import RulePackConfig


class PreprocessUnresolvedTrackingTests(unittest.TestCase):
    """``preprocess_grafana_macros`` records degraded ``$var`` substitutions."""

    def test_unresolved_set_records_arithmetic_variable(self):
        unresolved: set[str] = set()
        result = promql.preprocess_grafana_macros(
            "rate(http_requests_total[5m]) - $scrape_interval",
            unresolved_out=unresolved,
        )
        self.assertEqual(unresolved, {"scrape_interval"})
        self.assertIn("label_scrape_interval", result)

    def test_unresolved_set_skips_double_underscore_macros(self):
        unresolved: set[str] = set()
        result = promql.preprocess_grafana_macros(
            "rate(http_requests_total[$__rate_interval])",
            unresolved_out=unresolved,
        )
        self.assertEqual(unresolved, set())
        self.assertNotIn("$__rate_interval", result)

    def test_unresolved_set_skips_matcher_position_after_broadening(self):
        """Matcher-position variables get broadened to ``=~".*"`` and never
        reach the ``label_<var>`` fallback."""
        unresolved: set[str] = set()
        promql.preprocess_grafana_macros(
            'metric{instance="$node",job="$job"}',
            unresolved_out=unresolved,
        )
        self.assertEqual(unresolved, set())


class LeakedLabelDetectorTests(unittest.TestCase):
    def test_detects_leaked_token_in_arithmetic_position(self):
        leaked = translate._detect_leaked_label_variables(
            "FROM x | EVAL value = metric - label_scrape_interval",
            ["scrape_interval", "instance"],
        )
        self.assertEqual(leaked, ["scrape_interval"])

    def test_returns_empty_when_token_was_dropped_downstream(self):
        leaked = translate._detect_leaked_label_variables(
            "FROM x | WHERE field IS NOT NULL | STATS x = AVG(metric)",
            ["scrape_interval"],
        )
        self.assertEqual(leaked, [])

    def test_returns_empty_when_no_candidates(self):
        self.assertEqual(
            translate._detect_leaked_label_variables(
                "FROM x | EVAL v = label_anything", []
            ),
            [],
        )


class TranslatePromqlMarksLeakedVariablesNotFeasible(unittest.TestCase):
    """The full translator marks panels with leaked ``label_<var>`` not-feasible."""

    def test_arithmetic_variable_leaks_into_esql_and_downgrades(self):
        result = translate.translate_promql_to_esql(
            "prometheus_target_interval_length_seconds{quantile=\"0.99\"} - $scrape_interval",
            datasource_index="metrics-*",
            rule_pack=RulePackConfig(),
        )
        self.assertEqual(result.feasibility, "not_feasible")
        self.assertTrue(
            any(
                "Unresolved Grafana template variable(s) leaked" in w
                for w in result.warnings
            ),
            f"expected leaked-variable warning in {result.warnings!r}",
        )

    def test_matcher_only_variable_remains_feasible(self):
        """Variables in label-matcher position get broadened/dropped without
        leaking into ES|QL, so the panel stays feasible."""
        result = translate.translate_promql_to_esql(
            'rate(http_requests_total{instance="$instance"}[5m])',
            datasource_index="metrics-*",
            rule_pack=RulePackConfig(),
        )
        self.assertNotEqual(result.feasibility, "not_feasible")
        self.assertNotIn("label_instance", result.esql_query or "")


if __name__ == "__main__":
    unittest.main()
