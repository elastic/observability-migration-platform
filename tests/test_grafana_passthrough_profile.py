# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana ``--field-profile passthrough`` support.

The Datadog adapter has always exposed named field profiles (``otel``,
``passthrough``, ``elastic_agent``). Grafana maps labels dynamically through the
``SchemaResolver`` and historically hard-rejected every profile except ``otel``.
The ``passthrough`` profile disables automatic OTel/Prometheus normalization and
emits source label/metric names verbatim, while still honoring explicit
rule-pack overrides.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from observability_migration.adapters.source.grafana import cli as grafana_cli
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver


def _passthrough_resolver(rule_pack=None, field_cache=None):
    resolver = SchemaResolver(
        rule_pack or RulePackConfig(),
        es_url="https://es",
        index_pattern="metrics-*",
        passthrough=True,
    )
    # Seed live caps that WOULD trigger OTel/native remapping under `otel`, so a
    # passing test proves passthrough short-circuits before discovery/mapping.
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    resolver._field_cache = dict(field_cache or {})
    return resolver


class TestPassthroughLabelResolution(unittest.TestCase):
    def test_label_emitted_verbatim_even_when_otel_alias_exists(self):
        # `instance` normally remaps to `service.instance.id` under `otel`.
        resolver = _passthrough_resolver(
            field_cache={"instance": {}, "service.instance.id": {}}
        )
        self.assertEqual(resolver.resolve_label("instance"), "instance")
        self.assertEqual(resolver.resolve_label("namespace"), "namespace")

    def test_label_rewrites_still_win(self):
        rule_pack = RulePackConfig()
        rule_pack.label_rewrites = {"instance": "host.name"}
        resolver = _passthrough_resolver(rule_pack=rule_pack)
        self.assertEqual(resolver.resolve_label("instance"), "host.name")

    def test_ignored_labels_still_dropped(self):
        rule_pack = RulePackConfig()
        rule_pack.ignored_labels = ["origin_prometheus"]
        resolver = _passthrough_resolver(rule_pack=rule_pack)
        self.assertIsNone(resolver.resolve_label("origin_prometheus"))

    def test_control_field_overrides_still_win(self):
        rule_pack = RulePackConfig()
        rule_pack.control_field_overrides = {"node": "host.name"}
        resolver = _passthrough_resolver(rule_pack=rule_pack)
        self.assertEqual(resolver.resolve_control_field("node"), "host.name")
        # A control without an override falls through to verbatim passthrough.
        self.assertEqual(resolver.resolve_control_field("job"), "job")

    def test_metric_field_emitted_verbatim_on_native_shaped_cache(self):
        # A `metrics.<name>` cache would make `otel` emit `metrics.node_up`;
        # passthrough must keep the bare Prometheus metric name.
        resolver = _passthrough_resolver(
            field_cache={"metrics.node_up": {}, "labels.instance": {}}
        )
        self.assertEqual(resolver.resolve_metric_field("node_up"), "node_up")

    def test_prime_cooccurrence_is_noop_and_probes_nothing(self):
        resolver = _passthrough_resolver(field_cache={"instance": {}})
        with patch.object(resolver, "_cooccurring_candidates") as probe:
            resolver.prime_label_cooccurrence(["instance"], "node_up")
            probe.assert_not_called()


class TestGrafanaFieldProfileValidation(unittest.TestCase):
    def test_passthrough_is_accepted(self):
        args = SimpleNamespace(field_profile="passthrough")
        # Must not raise.
        grafana_cli._validate_field_profile(args)

    def test_otel_is_accepted(self):
        args = SimpleNamespace(field_profile="otel")
        grafana_cli._validate_field_profile(args)

    def test_unsupported_profile_still_rejected(self):
        for profile in ("prometheus", "elastic_agent", "custom.yaml"):
            with self.subTest(profile=profile):
                args = SimpleNamespace(field_profile=profile)
                with self.assertRaises(SystemExit) as ctx:
                    grafana_cli._validate_field_profile(args)
                self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
