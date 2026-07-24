# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Issue #197 review (Medium): an explicit ``by(...)`` label that is neither an
``on(...)`` match key nor a ``group_left(...)`` enrichment label is assumed to
survive on the primary metric after the join RHS is dropped.

Historically, when live target field capabilities proved that grouping field
absent, the panel was flipped to ``not_feasible``. Issue #187 supersedes that:
the translation is correct ES|QL and a missing target field is a transient
*data readiness* condition, not a translation infeasibility, so the panel stays
``feasible`` (with a data-readiness warning) and the verdict no longer depends
on whether ``--es-url`` was supplied.
"""

import unittest

from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.adapters.source.grafana.translate import translate_promql_to_esql

# `by(rabbitmq_node)` where rabbitmq_node is an on()-key nor enrichment label is
# the enrichment case (covered elsewhere). Here we use a by-label that is neither
# — the assumed-on-primary case this fix guards.
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


class TestJoinGroupLabelVerify(unittest.TestCase):
    def test_absent_group_field_live_is_data_readiness_not_not_feasible(self):
        # Live caps: primary metric present, but the `shard` grouping field is
        # absent from the target (not yet ingested). The translation is correct
        # ES|QL, so per issue #187 the panel stays feasible with a
        # data-readiness warning rather than flipping to not_feasible.
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

    def test_present_group_field_live_stays_feasible(self):
        # Same query, but `shard` IS advertised by the target (and co-occurs with
        # the metric) — the assumption holds, so the panel migrates.
        resolver = _live_resolver(
            field_cache={
                "rabbitmq_queue_messages_ready": {},
                "instance": {},
                "job": {},
                "shard": {},
            },
            cooccurrence={("rabbitmq_queue_messages_ready", "shard"): True},
        )
        result = _translate(resolver)
        self.assertEqual(result.feasibility, "feasible")

    def test_absent_group_field_offline_stays_feasible(self):
        # No live field capabilities: we cannot single out `shard` as absent, so
        # the guard is skipped and the pre-existing feasible behavior is kept
        # (the field gap surfaces later via live_validate instead).
        resolver = SchemaResolver(RulePackConfig(), index_pattern="metrics-*")
        result = _translate(resolver)
        self.assertEqual(result.feasibility, "feasible")


if __name__ == "__main__":
    unittest.main()
