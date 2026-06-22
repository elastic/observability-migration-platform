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


def _live_resolver(field_cache, cooccurrence):
    """A resolver with live caps and a stubbed co-occurrence probe.

    ``cooccurrence`` maps ``(metric_field, candidate)`` to True/False/None, the
    contract of the real ES|QL COUNT probe.
    """
    resolver = SchemaResolver(RulePackConfig(), es_url="https://es", index_pattern="metrics-*")
    resolver._discovery_attempted = True
    resolver._field_cache = dict(field_cache)
    resolver._discovery_status = "ok"
    resolver._cooccurs = lambda metric, candidate: cooccurrence.get((metric, candidate))
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
        # with the metric. The profile's namespaced form must win — metric-aware
        # resolution must not regress source-faithful Prometheus behaviour.
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
        )
        self.assertEqual(resolver.schema_profile(), "prometheus_remote_write")
        self.assertEqual(
            resolver.resolve_label("instance", metric_field="prometheus.redis_up.value"),
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
        resolver._cooccurs = lambda metric, candidate: {
            ("metrics.redis_up", "instance"): False,
            ("metrics.redis_up", "service.instance.id"): True,
        }.get((metric, candidate))
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
        resolver._cooccurs = lambda metric, candidate: {
            ("redis_up", "instance"): False,
            ("redis_up", "service.instance.id"): True,
        }.get((metric, candidate))
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
        resolver._cooccurs = lambda metric, candidate: {
            ("redis_up", "instance"): False,
            ("redis_up", "service.instance.id"): True,
        }.get((metric, candidate))
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
            json=lambda: {"columns": [{"name": "c", "type": "long"}], "values": [[889]]},
        )
        resolver = self._resolver()
        self.assertIs(resolver._cooccurs("metrics.redis_up", "service.instance.id"), True)
        # The probe scopes both fields with IS NOT NULL and backticks them itself.
        _, kwargs = mock_post.call_args
        query = kwargs["json"]["query"]
        self.assertIn("`metrics.redis_up` IS NOT NULL", query)
        self.assertIn("`service.instance.id` IS NOT NULL", query)

    @patch("observability_migration.adapters.source.grafana.schema.requests.post")
    def test_probe_returns_false_when_count_zero(self, mock_post):
        mock_post.return_value = Mock(status_code=200, json=lambda: {"values": [[0]]})
        resolver = self._resolver()
        self.assertIs(resolver._cooccurs("metrics.redis_up", "instance"), False)

    @patch("observability_migration.adapters.source.grafana.schema.requests.post")
    def test_probe_returns_none_on_error_status(self, mock_post):
        mock_post.return_value = Mock(status_code=400, json=lambda: {})
        resolver = self._resolver()
        self.assertIsNone(resolver._cooccurs("metrics.redis_up", "instance"))

    @patch("observability_migration.adapters.source.grafana.schema.requests.post")
    def test_probe_is_cached_per_pair(self, mock_post):
        mock_post.return_value = Mock(status_code=200, json=lambda: {"values": [[5]]})
        resolver = self._resolver()
        resolver._cooccurs("metrics.redis_up", "service.instance.id")
        resolver._cooccurs("metrics.redis_up", "service.instance.id")
        self.assertEqual(mock_post.call_count, 1)
