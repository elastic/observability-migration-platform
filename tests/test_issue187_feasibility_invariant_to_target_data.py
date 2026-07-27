# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Issue #187: the feasibility verdict must answer "was the dashboard
translated successfully?" and must NOT depend on whether the target cluster
already has the panel's data ingested.

Concretely, the verdict for a given panel must be invariant to whether
``--es-url`` was supplied. A panel that translates into valid ES|QL but whose
referenced target field has not yet been ingested is a *data readiness*
concern, not a translation infeasibility, so it stays ``feasible`` (with a
surfaced data-readiness warning) instead of flipping to ``not_feasible`` the
moment a live schema is probed.
"""

import unittest

from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.adapters.source.grafana.translate import translate_promql_to_esql

# Safe-subset aggregated group_left join (issue #197): the enrichment RHS is
# dropped and the primary metric is aggregated by `shard`. The translation is
# correct ES|QL; whether `shard` is present in the target is a data-readiness
# property, not a translation limitation.
EXPR = (
    "sum(rabbitmq_queue_messages_ready"
    " * on(instance, job) group_left(rabbitmq_cluster) rabbitmq_identity_info)"
    " by(shard)"
)


def _live_resolver(field_cache, cooccurrence=None):
    resolver = SchemaResolver(RulePackConfig(), es_url="https://es", index_pattern="metrics-*")
    resolver._discovery_attempted = True
    resolver._field_cache = dict(field_cache)
    resolver._discovery_status = "ok"
    resolver._cooccurrence_cache = dict(cooccurrence or {})
    return resolver


def _translate(resolver):
    return translate_promql_to_esql(
        EXPR,
        datasource_index="metrics-*",
        panel_type="timeseries",
        rule_pack=resolver._rule_pack,
        resolver=resolver,
    )


class TestFeasibilityInvariantToTargetData(unittest.TestCase):
    def test_absent_group_field_live_is_data_readiness_not_not_feasible(self):
        # Live caps: primary metric present, but the `shard` grouping field has
        # not been ingested yet. The translation itself is correct, so the
        # panel must NOT be reclassified as not_feasible on the basis of
        # missing target data (issue #187).
        resolver = _live_resolver(
            field_cache={
                "rabbitmq_queue_messages_ready": {},
                "instance": {},
                "job": {},
            },
        )
        result = _translate(resolver)
        self.assertEqual(result.feasibility, "feasible")
        self.assertTrue(
            any("shard" in w and "data readiness" in w for w in result.warnings),
            result.warnings,
        )

    def test_feasibility_invariant_to_es_url(self):
        # Acceptance criterion: identical inputs, the only variable being
        # whether the target's data was probed (`--es-url`). The verdict must
        # be the same in both runs.
        offline = SchemaResolver(RulePackConfig(), index_pattern="metrics-*")
        offline_result = _translate(offline)

        live_missing = _live_resolver(
            field_cache={
                "rabbitmq_queue_messages_ready": {},
                "instance": {},
                "job": {},
            },
        )
        live_result = _translate(live_missing)

        self.assertEqual(offline_result.feasibility, "feasible")
        self.assertEqual(live_result.feasibility, offline_result.feasibility)


if __name__ == "__main__":
    unittest.main()
