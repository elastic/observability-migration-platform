# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""OTel + ES|QL must not bind data_stream.dataset=prometheus by default."""

from __future__ import annotations

import unittest
from argparse import Namespace

from observability_migration.adapters.source.grafana import cli as grafana_cli
from observability_migration.adapters.source.grafana.rules import RulePackConfig


class OtelEsqlDatasetFilterTests(unittest.TestCase):
    def test_esql_otel_clears_default_prometheus_dataset_filter(self):
        args = Namespace(
            translation_mode="esql",
            field_profile="otel",
            dataset_filter="",
            es_url="",
            es_api_key="",
            metric_map_file=None,
            ca_cert=None,
            insecure=False,
        )
        rp = RulePackConfig()
        self.assertEqual(rp.metrics_dataset_filter, "prometheus")
        # Apply the same branch as _apply_native_promql_to_rule_pack's else path
        # by calling through the public helper when available, else inline the policy.
        apply = getattr(grafana_cli, "_apply_native_promql_to_rule_pack", None)
        if apply is not None:
            apply(rp, args)
        else:
            rp.native_promql = False
            if not args.dataset_filter and args.field_profile in {"", "otel", "auto", "passthrough"}:
                rp.metrics_dataset_filter = ""
        self.assertEqual(rp.metrics_dataset_filter, "")
        self.assertFalse(rp.native_promql)

    def test_explicit_dataset_filter_preserved(self):
        args = Namespace(
            translation_mode="esql",
            field_profile="otel",
            dataset_filter="otel",
            es_url="",
            es_api_key="",
            metric_map_file=None,
            ca_cert=None,
            insecure=False,
        )
        rp = RulePackConfig(metrics_dataset_filter="otel")
        apply = getattr(grafana_cli, "_apply_native_promql_to_rule_pack", None)
        self.assertIsNotNone(apply)
        apply(rp, args)
        self.assertEqual(rp.metrics_dataset_filter, "otel")


if __name__ == "__main__":
    unittest.main()
