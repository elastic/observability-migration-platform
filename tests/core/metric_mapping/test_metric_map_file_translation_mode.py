# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana auto-selects ES|QL when --metric-map-file is set."""

from __future__ import annotations

import argparse
import unittest

from observability_migration.adapters.source.grafana.cli import _resolve_native_promql


class MetricMapFileTranslationModeTests(unittest.TestCase):
    def test_metric_map_file_auto_disables_native_promql(self):
        native = _resolve_native_promql(
            argparse.Namespace(
                translation_mode="auto",
                metric_map_file=["./my-otel-metric-map.yaml"],
                es_url="",
                es_api_key="",
            )
        )
        self.assertFalse(native)

    def test_explicit_native_still_wins_over_metric_map_file(self):
        native = _resolve_native_promql(
            argparse.Namespace(
                translation_mode="native",
                metric_map_file=["./my-otel-metric-map.yaml"],
                es_url="",
                es_api_key="",
            )
        )
        self.assertTrue(native)

    def test_auto_without_metric_map_file_keeps_native_default_offline(self):
        native = _resolve_native_promql(
            argparse.Namespace(
                translation_mode="auto",
                metric_map_file=[],
                es_url="",
                es_api_key="",
            )
        )
        self.assertTrue(native)


if __name__ == "__main__":
    unittest.main()
