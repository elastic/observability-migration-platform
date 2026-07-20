# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Issue #163: label resolution must be metric-aware.

A ``label_values(metric, label)`` control (and panel selectors / group-bys
scoped to a metric) must resolve the label to the candidate field that
*co-occurs* with the scoped metric, not just any field that exists globally in
the index. The classic failure: ``instance`` exists in ``metrics-*`` (written by
some unrelated source) so the global short-circuit emitted ``instance``, but the
scoped metric's documents carry the instance under ``service.instance.id`` — so
scope and label selected disjoint document sets and everything rendered empty.
"""

import unittest
from unittest.mock import Mock, patch

from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver


def _live_resolver(field_cache, cooccurrence, field_profile="otel"):
    """A resolver with live caps and pre-seeded co-occurrence results.

    ``cooccurrence`` maps ``(metric_field, candidate)`` to True/False/None, the
    contract of the real ES|QL COUNT probe. Seeding the per-pair cache exercises
    the real resolution path (issue #182 batched probe) without any network.
    """
    resolver = SchemaResolver(
        RulePackConfig(),
        es_url="https://es",
        index_pattern="metrics-*",
        field_profile=field_profile,
    )
    resolver._discovery_attempted = True
    resolver._field_cache = dict(field_cache)
    resolver._discovery_status = "ok"
    resolver._cooccurrence_cache = dict(cooccurrence)
    return resolver


class TestMetricAwareLabelResolution(unittest.TestCase):
    def test_otel_shape_resolves_to_cooccurring_otel_field(self):
        # `instance` exists globally (other sources write it) but does NOT
        # co-occur with the scoped metric; `service.instance.id` does.
        resolver = _live_resolver(
            field_cache={"instance": {}, "service.instance.id": {}, "metrics.redis_up": {}},
            cooccurrence={
                ("metrics.redis_up", "instance"): False,
                ("metrics.redis_up", "service.instance.id"): True,
            },
        )
        self.assertEqual(
            resolver.resolve_label("instance", metric_field="metrics.redis_up"),
            "service.instance.id",
        )

    def test_prometheus_shape_resolves_to_bare_label(self):
        # Symmetric case: the metric's label lands flat under `instance`, so the
        # bare label co-occurs and the OTel field does not.
        resolver = _live_resolver(
            field_cache={"instance": {}, "service.instance.id": {}, "node_cpu_seconds_total": {}},
            cooccurrence={
                ("node_cpu_seconds_total", "instance"): True,
                ("node_cpu_seconds_total", "service.instance.id"): False,
            },
        )
        self.assertEqual(
            resolver.resolve_label("instance", metric_field="node_cpu_seconds_total"),
            "instance",
        )

    def test_no_cooccurrence_falls_back_to_global_short_circuit(self):
        # Data-gap case (namespace/pod): nothing co-occurs with the metric, so
        # resolution falls back to the index-global source-faithful field.
        resolver = _live_resolver(
            field_cache={"instance": {}, "service.instance.id": {}, "metrics.redis_up": {}},
            cooccurrence={
                ("metrics.redis_up", "instance"): False,
                ("metrics.redis_up", "service.instance.id"): False,
            },
        )
        self.assertEqual(
            resolver.resolve_label("instance", metric_field="metrics.redis_up"),
            "instance",
        )

    def test_no_metric_field_preserves_global_behaviour(self):
        # Backward compatible: without a metric, the global short-circuit wins
        # exactly as before — no probe is consulted.
        resolver = _live_resolver(
            field_cache={"instance": {}, "service.instance.id": {}},
            cooccurrence={},
        )
        self.assertEqual(resolver.resolve_label("instance"), "instance")

    def test_remote_write_profile_prefers_namespaced_label_over_otel(self):
        # Dual-shipping prometheus_remote_write index: both the source-faithful
        # `prometheus.labels.instance` and an OTel `service.instance.id` co-occur
        # with the metric. The planned profile's namespaced form must win —
        # metric-aware co-occurrence must not regress planned Prometheus emit.
        resolver = _live_resolver(
            field_cache={
                "prometheus.labels.instance": {},
                "prometheus.redis_up.value": {},
                "service.instance.id": {},
            },
            cooccurrence={
                ("prometheus.redis_up.value", "prometheus.labels.instance"): True,
                ("prometheus.redis_up.value", "service.instance.id"): True,
            },
            field_profile="prometheus_remote_write",
        )
        self.assertEqual(resolver.schema_profile(), "prometheus_remote_write")
        self.assertEqual(
            resolver.resolve_label("instance", metric_field="prometheus.redis_up.value"),
            "prometheus.labels.instance",
        )

    def test_prometheus_metrics_profile_prefers_namespaced_label_over_otel(self):
        resolver = _live_resolver(
            field_cache={
                "prometheus.labels.instance": {},
                "prometheus.metrics.redis_up": {},
                "service.instance.id": {},
            },
            cooccurrence={
                ("prometheus.metrics.redis_up", "prometheus.labels.instance"): True,
                ("prometheus.metrics.redis_up", "service.instance.id"): True,
            },
            field_profile="prometheus_metrics",
        )
        self.assertEqual(resolver.schema_profile(), "prometheus_metrics")
        self.assertEqual(
            resolver.resolve_label("instance", metric_field="prometheus.metrics.redis_up"),
            "prometheus.labels.instance",
        )

    def test_native_profile_prefers_namespaced_label_over_otel(self):
        # Same guard for the native `/_prometheus` layout: `labels.instance`
        # is the source-faithful field and must be preferred over `service.instance.id`.
        resolver = _live_resolver(
            field_cache={
                "labels.instance": {},
                "metrics.redis_up": {},
                "service.instance.id": {},
            },
            cooccurrence={
                ("metrics.redis_up", "labels.instance"): True,
                ("metrics.redis_up", "service.instance.id"): True,
            },
            field_profile="prometheus_native",
        )
        self.assertEqual(resolver.schema_profile(), "prometheus_native")
        self.assertEqual(
            resolver.resolve_label("instance", metric_field="metrics.redis_up"),
            "labels.instance",
        )


class TestMetricAwareForwarding(unittest.TestCase):
    """resolve_control_field / resolve_labels forward the scoped metric so all
    three buggy sites (control breakdown, panel WHERE, panel BY/KEEP) inherit
    the metric-aware resolution from the one primitive."""

    @staticmethod
    def _resolver():
        resolver = _live_resolver(
            field_cache={"instance": {}, "service.instance.id": {}, "metrics.redis_up": {}},
            cooccurrence={
                ("metrics.redis_up", "instance"): False,
                ("metrics.redis_up", "service.instance.id"): True,
            },
        )
        return resolver

    def test_resolve_control_field_forwards_metric(self):
        resolver = self._resolver()
        self.assertEqual(
            resolver.resolve_control_field("instance", metric_field="metrics.redis_up"),
            "service.instance.id",
        )

    def test_resolve_labels_forwards_metric(self):
        resolver = self._resolver()
        self.assertEqual(
            resolver.resolve_labels(["instance"], metric_field="metrics.redis_up"),
            ["service.instance.id"],
        )


class TestControlBreakdownIsMetricAware(unittest.TestCase):
    """End-to-end at the control site: a live field cache containing a bare
    `instance` (written by unrelated sources) must NOT win when the scoped
    metric carries the instance under `service.instance.id` (issue #163)."""

    def _resolver(self):
        rp = RulePackConfig()
        resolver = SchemaResolver(rp, es_url="https://es", index_pattern="metrics-*")
        resolver._discovery_attempted = True
        resolver._discovery_status = "ok"
        resolver._field_cache = {
            "instance": {"keyword": {"type": "keyword", "aggregatable": True, "searchable": True}},
            "service.instance.id": {
                "keyword": {"type": "keyword", "aggregatable": True, "searchable": True}
            },
            "metrics.redis_up": {"double": {"type": "double", "aggregatable": True}},
        }
        resolver._cooccurrence_cache = {
            ("metrics.redis_up", "instance"): False,
            ("metrics.redis_up", "service.instance.id"): True,
        }
        return rp, resolver

    def test_control_breakdown_uses_cooccurring_field(self):
        from observability_migration.adapters.source.grafana.panels import translate_variables
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_LABEL_MATCHER_PARAMS,
            set_runtime_feature,
        )

        rp, resolver = self._resolver()
        # The scoping metric resolves to the physical `metrics.redis_up` field.
        resolver.resolve_metric_field = lambda name, **kw: "metrics.redis_up"
        set_runtime_feature(rp, PROMQL_LABEL_MATCHER_PARAMS, supported=True, source="probe")

        controls = translate_variables(
            [{
                "type": "query",
                "name": "instance",
                "label": "Instance",
                "multi": False,
                "query": "label_values(redis_up, instance)",
            }],
            datasource_index="metrics-*",
            rule_pack=rp,
            resolver=resolver,
        )
        self.assertEqual(len(controls), 1)
        query = controls[0]["query"]
        self.assertIn("BY `service.instance.id`", query)
        self.assertNotIn("BY instance", query)


class TestPanelPathsAreMetricAware(unittest.TestCase):
    """The panel selector (WHERE) and group-by (BY/KEEP) paths must resolve a
    metric-scoped label by co-occurrence with the fragment's metric (#163)."""

    def _resolver(self):
        rp = RulePackConfig()
        resolver = SchemaResolver(rp, es_url="https://es", index_pattern="metrics-*")
        resolver._discovery_attempted = True
        resolver._discovery_status = "ok"
        resolver._field_cache = {
            "instance": {"keyword": {"type": "keyword", "aggregatable": True, "searchable": True}},
            "service.instance.id": {
                "keyword": {"type": "keyword", "aggregatable": True, "searchable": True}
            },
            "redis_up": {"double": {"type": "double", "aggregatable": True}},
        }
        resolver._cooccurrence_cache = {
            ("redis_up", "instance"): False,
            ("redis_up", "service.instance.id"): True,
        }
        return rp, resolver

    def _translate(self, expr, rp, resolver):
        from observability_migration.adapters.source.grafana.translate import (
            translate_promql_to_esql,
        )

        return translate_promql_to_esql(
            expr,
            esql_index="metrics-*",
            panel_type="timeseries",
            rule_pack=rp,
            resolver=resolver,
        )

    def test_panel_group_by_uses_cooccurring_field(self):
        rp, resolver = self._resolver()
        ctx = self._translate("sum(redis_up) by (instance)", rp, resolver)
        self.assertIn("service.instance.id", ctx.esql_query)
        self.assertNotIn(" BY instance", ctx.esql_query)
        self.assertNotIn(", instance", ctx.esql_query)

    def test_panel_selector_where_uses_cooccurring_field(self):
        rp, resolver = self._resolver()
        ctx = self._translate('redis_up{instance=~"redis.*"}', rp, resolver)
        self.assertIn("service.instance.id", ctx.esql_query)
        self.assertNotIn("WHERE instance ", ctx.esql_query)

    def _resolver_with_numeric_bare_label(self):
        # Global `instance` is numeric (a string matcher on it would be flagged
        # incompatible), but the metric-scoped instance lands on the keyword
        # `service.instance.id` that co-occurs with the metric.
        rp = RulePackConfig()
        resolver = SchemaResolver(rp, es_url="https://es", index_pattern="metrics-*")
        resolver._discovery_attempted = True
        resolver._discovery_status = "ok"
        resolver._field_cache = {
            "instance": {"double": {"type": "double", "aggregatable": True, "searchable": True}},
            "service.instance.id": {
                "keyword": {"type": "keyword", "aggregatable": True, "searchable": True}
            },
            "redis_up": {"double": {"type": "double", "aggregatable": True}},
        }
        resolver._cooccurrence_cache = {
            ("redis_up", "instance"): False,
            ("redis_up", "service.instance.id"): True,
        }
        return rp, resolver

    def test_no_false_incompatible_filter_warning_when_metric_aware_field_is_compatible(self):
        # The incompatibility check must resolve with the same scoped metric the
        # generator uses; otherwise it inspects numeric `instance` and falsely
        # claims an incompatible-field drop while the query correctly uses the
        # keyword `service.instance.id` (#163 review).
        rp, resolver = self._resolver_with_numeric_bare_label()
        ctx = self._translate('redis_up{instance=~"redis.*"}', rp, resolver)
        self.assertIn("service.instance.id", ctx.esql_query)
        self.assertFalse(
            any("incompatible target field" in w for w in ctx.warnings),
            f"unexpected incompatible-field drop warning: {ctx.warnings}",
        )

    def test_no_false_incompatible_group_warning_when_metric_aware_field_is_compatible(self):
        rp, resolver = self._resolver_with_numeric_bare_label()
        ctx = self._translate("sum(redis_up) by (instance)", rp, resolver)
        self.assertIn("service.instance.id", ctx.esql_query)
        self.assertFalse(
            any("incompatible target field" in w for w in ctx.warnings),
            f"unexpected incompatible-field drop warning: {ctx.warnings}",
        )


class TestPanelTranslationPrimesPerMetric(unittest.TestCase):
    """Issue #182: translating a panel must prime co-occurrence for all of a
    fragment's labels in ONE batched probe, not one probe per label."""

    @patch("observability_migration.adapters.source.grafana.schema.requests.post")
    def test_multi_label_panel_issues_single_probe(self, mock_post):
        from observability_migration.adapters.source.grafana.translate import (
            translate_promql_to_esql,
        )

        # `instance` co-occurs via service.instance.id; `job` via service.name.
        mock_post.return_value = Mock(
            status_code=200,
            json=lambda: {
                "columns": [
                    {"name": "c0", "type": "long"},  # instance
                    {"name": "c1", "type": "long"},  # service.instance.id
                    {"name": "c2", "type": "long"},  # service.name (job)
                ],
                "values": [[0, 9, 4]],
            },
        )
        rp = RulePackConfig()
        resolver = SchemaResolver(rp, es_url="https://es", index_pattern="metrics-*")
        resolver._discovery_attempted = True
        resolver._discovery_status = "ok"
        resolver._field_cache = {
            "instance": {"keyword": {"type": "keyword", "aggregatable": True, "searchable": True}},
            "service.instance.id": {
                "keyword": {"type": "keyword", "aggregatable": True, "searchable": True}
            },
            "service.name": {
                "keyword": {"type": "keyword", "aggregatable": True, "searchable": True}
            },
            "redis_up": {"double": {"type": "double", "aggregatable": True}},
        }

        ctx = translate_promql_to_esql(
            'sum(redis_up{instance=~"redis.*"}) by (instance, job)',
            esql_index="metrics-*",
            panel_type="timeseries",
            rule_pack=rp,
            resolver=resolver,
        )
        # Both labels resolved to their co-occurring fields...
        self.assertIn("service.instance.id", ctx.esql_query)
        self.assertIn("service.name", ctx.esql_query)
        # ...in a single batched co-occurrence probe for the whole fragment.
        self.assertEqual(mock_post.call_count, 1)


class TestCooccurrenceProbe(unittest.TestCase):
    @staticmethod
    def _resolver():
        resolver = SchemaResolver(RulePackConfig(), es_url="https://es", index_pattern="metrics-*")
        resolver._discovery_attempted = True
        resolver._field_cache = {"metrics.redis_up": {}, "service.instance.id": {}}
        resolver._discovery_status = "ok"
        return resolver

    @patch("observability_migration.adapters.source.grafana.schema.requests.post")
    def test_probe_returns_true_when_count_positive(self, mock_post):
        mock_post.return_value = Mock(
            status_code=200,
            json=lambda: {"columns": [{"name": "c0", "type": "long"}], "values": [[889]]},
        )
        resolver = self._resolver()
        self.assertIs(resolver._cooccurs("metrics.redis_up", "service.instance.id"), True)
        # The probe scopes the metric in WHERE and counts the candidate, both
        # backticked by the probe itself.
        _, kwargs = mock_post.call_args
        query = kwargs["json"]["query"]
        self.assertIn("`metrics.redis_up` IS NOT NULL", query)
        self.assertIn("COUNT(`service.instance.id`)", query)

    @patch("observability_migration.adapters.source.grafana.schema.requests.post")
    def test_probe_returns_false_when_count_zero(self, mock_post):
        mock_post.return_value = Mock(
            status_code=200,
            json=lambda: {"columns": [{"name": "c0", "type": "long"}], "values": [[0]]},
        )
        resolver = self._resolver()
        self.assertIs(resolver._cooccurs("metrics.redis_up", "instance"), False)

    @patch("observability_migration.adapters.source.grafana.schema.requests.post")
    def test_probe_returns_none_on_error_status(self, mock_post):
        mock_post.return_value = Mock(status_code=400, json=lambda: {})
        resolver = self._resolver()
        self.assertIsNone(resolver._cooccurs("metrics.redis_up", "instance"))

    @patch("observability_migration.adapters.source.grafana.schema.requests.post")
    def test_probe_is_cached_per_pair(self, mock_post):
        mock_post.return_value = Mock(
            status_code=200,
            json=lambda: {"columns": [{"name": "c0", "type": "long"}], "values": [[5]]},
        )
        resolver = self._resolver()
        resolver._cooccurs("metrics.redis_up", "service.instance.id")
        resolver._cooccurs("metrics.redis_up", "service.instance.id")
        self.assertEqual(mock_post.call_count, 1)


class TestBatchedCooccurrenceProbe(unittest.TestCase):
    """Issue #182: resolving a metric-scoped label must collapse the
    per-candidate co-occurrence checks into a SINGLE batched ``/_query`` probe
    per metric, instead of one blocking round-trip per candidate."""

    @staticmethod
    def _resolver():
        resolver = SchemaResolver(
            RulePackConfig(), es_url="https://es", index_pattern="metrics-*"
        )
        resolver._discovery_attempted = True
        resolver._field_cache = {
            "instance": {},
            "service.instance.id": {},
            "metrics.redis_up": {},
        }
        resolver._discovery_status = "ok"
        return resolver

    @patch("observability_migration.adapters.source.grafana.schema.requests.post")
    def test_resolution_issues_single_batched_probe(self, mock_post):
        # `instance` does not co-occur with the metric, `service.instance.id`
        # does. One batched STATS query must count both candidates at once.
        mock_post.return_value = Mock(
            status_code=200,
            json=lambda: {
                "columns": [
                    {"name": "c0", "type": "long"},
                    {"name": "c1", "type": "long"},
                ],
                "values": [[0, 7]],
            },
        )
        resolver = self._resolver()
        resolved = resolver.resolve_label("instance", metric_field="metrics.redis_up")
        self.assertEqual(resolved, "service.instance.id")
        self.assertEqual(mock_post.call_count, 1)
        _, kwargs = mock_post.call_args
        query = kwargs["json"]["query"]
        # The metric is scoped once in WHERE; each candidate is counted in STATS.
        self.assertIn("`metrics.redis_up` IS NOT NULL", query)
        self.assertIn("COUNT(`instance`)", query)
        self.assertIn("COUNT(`service.instance.id`)", query)

    @patch("observability_migration.adapters.source.grafana.schema.requests.post")
    def test_batch_results_are_cached_per_pair(self, mock_post):
        mock_post.return_value = Mock(
            status_code=200,
            json=lambda: {
                "columns": [
                    {"name": "c0", "type": "long"},
                    {"name": "c1", "type": "long"},
                ],
                "values": [[0, 7]],
            },
        )
        resolver = self._resolver()
        resolver.resolve_label("instance", metric_field="metrics.redis_up")
        resolver.resolve_label("instance", metric_field="metrics.redis_up")
        # Second resolution must be served entirely from the per-pair cache.
        self.assertEqual(mock_post.call_count, 1)

    @patch("observability_migration.adapters.source.grafana.schema.requests.post")
    def test_prime_batches_every_label_in_one_probe(self, mock_post):
        # Per-metric batching (issue #182): priming a fragment's whole label set
        # (`instance` → 2 candidates, `job` → 1) issues ONE query covering every
        # candidate; the subsequent per-label resolutions hit the warm cache and
        # issue no further probes.
        mock_post.return_value = Mock(
            status_code=200,
            json=lambda: {
                "columns": [
                    {"name": "c0", "type": "long"},  # instance
                    {"name": "c1", "type": "long"},  # service.instance.id
                    {"name": "c2", "type": "long"},  # host.name
                    {"name": "c3", "type": "long"},  # service.name (job)
                ],
                "values": [[0, 7, 0, 5]],
            },
        )
        resolver = SchemaResolver(
            RulePackConfig(), es_url="https://es", index_pattern="metrics-*"
        )
        resolver._discovery_attempted = True
        resolver._discovery_status = "ok"
        resolver._field_cache = {
            "instance": {},
            "service.instance.id": {},
            "host.name": {},
            "service.name": {},
            "metrics.foo": {},
        }
        resolver.prime_label_cooccurrence(["instance", "job"], "metrics.foo")
        self.assertEqual(mock_post.call_count, 1)
        # One query, every candidate counted.
        query = mock_post.call_args.kwargs["json"]["query"]
        for field in ("instance", "service.instance.id", "host.name", "service.name"):
            self.assertIn(f"COUNT(`{field}`)", query)
        # Resolution now served entirely from the primed cache.
        self.assertEqual(
            resolver.resolve_label("instance", metric_field="metrics.foo"),
            "service.instance.id",
        )
        self.assertEqual(
            resolver.resolve_label("job", metric_field="metrics.foo"), "service.name"
        )
        self.assertEqual(mock_post.call_count, 1)

    @patch("observability_migration.adapters.source.grafana.schema.requests.post")
    def test_prime_skips_ignored_and_rewritten_labels(self, mock_post):
        # `resolve_label` short-circuits rule-pack ignored/rewritten labels before
        # any probe; priming must mirror that, or it issues round-trips for labels
        # resolution will never probe (defeats #182's goal).
        mock_post.return_value = Mock(
            status_code=200,
            json=lambda: {"columns": [{"name": "c0", "type": "long"}], "values": [[5]]},
        )
        rp = RulePackConfig()
        rp.label_rewrites = {"instance": "host.name"}
        rp.ignored_labels = list(rp.ignored_labels) + ["job"]
        resolver = SchemaResolver(rp, es_url="https://es", index_pattern="metrics-*")
        resolver._discovery_attempted = True
        resolver._discovery_status = "ok"
        resolver._field_cache = {
            "instance": {},
            "service.instance.id": {},
            "host.name": {},
            "service.name": {},
            "metrics.foo": {},
        }
        # Only ignored/rewritten labels → nothing to probe.
        resolver.prime_label_cooccurrence(["instance", "job"], "metrics.foo")
        self.assertEqual(mock_post.call_count, 0)
        # A normal label (node → host.name candidate, present in cache) still
        # primes in a single batched probe.
        resolver.prime_label_cooccurrence(["node"], "metrics.foo")
        self.assertEqual(mock_post.call_count, 1)

    @patch("observability_migration.adapters.source.grafana.schema.requests.post")
    def test_batch_error_falls_back_to_per_candidate_probes(self, mock_post):
        # Issue #182 regression guard: a batched STATS couples every candidate.
        # If one lower-priority candidate makes the whole query 400 (e.g. a type
        # conflict across dual-shipping indices), the batch returns None for the
        # entire set. Without a fallback that would cache None for the primary
        # candidate too and revert the label to index-global resolution —
        # re-introducing the #163 disjoint-document-set bug. The resolver must
        # re-probe each candidate alone so the co-occurring primary still wins.
        batch_400 = Mock(status_code=400, json=lambda: {})

        def per_candidate(*_args, **kwargs):
            query = kwargs["json"]["query"]
            if "COUNT(`instance`)" in query and "COUNT(`service.instance.id`)" in query:
                # The combined batch query fails (one field is incompatible).
                return batch_400
            if "COUNT(`service.instance.id`)" in query:
                return Mock(
                    status_code=200,
                    json=lambda: {
                        "columns": [{"name": "c0", "type": "long"}],
                        "values": [[7]],
                    },
                )
            # The incompatible candidate, probed alone, still errors.
            return batch_400

        mock_post.side_effect = per_candidate
        resolver = self._resolver()
        resolved = resolver.resolve_label("instance", metric_field="metrics.redis_up")
        # Primary candidate resolves correctly despite the poisoned batch.
        self.assertEqual(resolved, "service.instance.id")
        # One batched probe + one per-candidate probe each for the fallback.
        self.assertEqual(mock_post.call_count, 3)

    @patch("observability_migration.adapters.source.grafana.schema.requests.post")
    def test_single_candidate_batch_error_does_not_re_probe(self, mock_post):
        # With only one candidate the batch query IS the per-candidate query, so
        # the error fallback must not issue a redundant second identical probe.
        mock_post.return_value = Mock(status_code=400, json=lambda: {})
        resolver = SchemaResolver(
            RulePackConfig(), es_url="https://es", index_pattern="metrics-*"
        )
        resolver._discovery_attempted = True
        resolver._discovery_status = "ok"
        resolver._field_cache = {"instance": {}, "metrics.foo": {}}
        self.assertIsNone(resolver._cooccurs("metrics.foo", "instance"))
        self.assertEqual(mock_post.call_count, 1)

    @patch("observability_migration.adapters.source.grafana.schema.requests.post")
    def test_batch_probe_maps_results_by_column_name(self, mock_post):
        # Robust against column reordering: map COUNT aliases by name, not index.
        mock_post.return_value = Mock(
            status_code=200,
            json=lambda: {
                "columns": [
                    {"name": "c1", "type": "long"},
                    {"name": "c0", "type": "long"},
                ],
                "values": [[7, 0]],
            },
        )
        resolver = self._resolver()
        resolved = resolver.resolve_label("instance", metric_field="metrics.redis_up")
        self.assertEqual(resolved, "service.instance.id")
