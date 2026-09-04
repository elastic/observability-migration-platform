# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana metric_map targets must not already carry the active profile prefix."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import pytest
import yaml

from observability_migration.adapters.source.grafana.metric_map_lint import (
    grafana_metric_map_prefix_errors,
)
from observability_migration.adapters.source.grafana.rules import (
    RulePackConfig,
    load_rule_pack_files,
)
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.core.metric_mapping import normalize_metric_map

_PACK_ROOT = (
    Path(__file__).resolve().parents[3]
    / "observability_migration"
    / "adapters"
    / "source"
    / "grafana"
    / "curated_packs"
)
_NAMED_PROMETHEUS_PLANS = (
    "prometheus_native",
    "prometheus_metrics",
    "prometheus_remote_write",
)


class GrafanaMetricMapPrefixHelperTests(unittest.TestCase):
    def test_native_namespaced_target_fails_and_suggests_logical_name(self):
        errors = grafana_metric_map_prefix_errors(
            {"redis_uptime_in_seconds": "metrics.uptime_seconds"},
            "prometheus_native",
        )
        self.assertEqual(len(errors), 1)
        self.assertEqual(
            errors[0],
            "Grafana metric_map target 'metrics.uptime_seconds' for source "
            "'redis_uptime_in_seconds' already uses the prometheus_native prefix; "
            "the profile would emit 'metrics.metrics.uptime_seconds'. Use the "
            "logical name 'uptime_seconds' instead.",
        )

    def test_prometheus_metrics_namespaced_target_fails(self):
        errors = grafana_metric_map_prefix_errors(
            {"http_requests_total": "prometheus.metrics.http_requests_total"},
            "prometheus_metrics",
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("prometheus.metrics.prometheus.metrics.http_requests_total", errors[0])
        self.assertIn("Use the logical name 'http_requests_total' instead.", errors[0])

    def test_remote_write_namespaced_target_strips_prefix_and_leaf(self):
        errors = grafana_metric_map_prefix_errors(
            {"http_requests_total": "prometheus.foo.counter"},
            "prometheus_remote_write",
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("prometheus.prometheus.foo.counter.value", errors[0])
        self.assertIn("Use the logical name 'foo' instead.", errors[0])

    def test_native_bare_target_is_clean(self):
        self.assertEqual(
            grafana_metric_map_prefix_errors(
                {"redis_uptime_in_seconds": "uptime_seconds"},
                "prometheus_native",
            ),
            [],
        )

    def test_underscore_metrics_name_is_not_a_prefix(self):
        self.assertEqual(
            grafana_metric_map_prefix_errors(
                {"src": "metrics_uptime"},
                "prometheus_native",
            ),
            [],
        )

    def test_otel_passthrough_and_none_do_not_fail(self):
        mapping = {"src": "metrics.uptime_seconds"}
        self.assertEqual(grafana_metric_map_prefix_errors(mapping, "otel"), [])
        self.assertEqual(grafana_metric_map_prefix_errors(mapping, "passthrough"), [])
        self.assertEqual(grafana_metric_map_prefix_errors(mapping, None), [])
        self.assertEqual(grafana_metric_map_prefix_errors(mapping, "auto"), [])

    def test_variant_only_namespaced_target_uses_parent_source_key(self):
        errors = grafana_metric_map_prefix_errors(
            {
                "net_bytes": {
                    "variants": [
                        {
                            "source_filter": {"direction": "receive"},
                            "target": "metrics.net_rx",
                        }
                    ]
                }
            },
            "prometheus_native",
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("for source 'net_bytes'", errors[0])
        self.assertIn("metrics.net_rx", errors[0])
        self.assertIn("logical name 'net_rx'", errors[0])

    def test_two_bad_keys_yield_two_errors(self):
        errors = grafana_metric_map_prefix_errors(
            {
                "a": "metrics.one",
                "b": "metrics.two",
            },
            "prometheus_native",
        )
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("for source 'a'" in line for line in errors))
        self.assertTrue(any("for source 'b'" in line for line in errors))

    def test_empty_scaffold_target_is_skipped(self):
        errors = grafana_metric_map_prefix_errors(
            {
                "src": {"target": "", "provenance": "scaffold"},
            },
            "prometheus_native",
        )
        self.assertEqual(errors, [])


class GrafanaMetricMapPrefixResolverTests(unittest.TestCase):
    def test_resolver_init_raises_for_native_namespaced_target(self):
        pack = RulePackConfig()
        pack.metric_map.update(normalize_metric_map({"src": "metrics.already_prefixed"}))
        with self.assertRaises(ValueError) as caught:
            SchemaResolver(pack, field_profile="prometheus_native")
        self.assertIn("Use the logical name 'already_prefixed' instead.", str(caught.exception))

    def test_resolver_init_allows_namespaced_target_under_otel(self):
        pack = RulePackConfig()
        pack.metric_map.update(normalize_metric_map({"src": "metrics.already_prefixed"}))
        SchemaResolver(pack, field_profile="otel")

    def test_copy_with_pack_raises_for_namespaced_pack_map(self):
        base = SchemaResolver(RulePackConfig(), field_profile="prometheus_native")
        curated = RulePackConfig()
        curated.metric_map.update(normalize_metric_map({"src": "metrics.already_prefixed"}))
        with self.assertRaises(ValueError) as caught:
            base.copy_with_pack(curated)
        self.assertIn("already_prefixed", str(caught.exception))

    def test_auto_lint_after_named_layout_is_detected(self):
        pack = RulePackConfig()
        pack.metric_map.update(normalize_metric_map({"src": "metrics.already_prefixed"}))
        resolver = SchemaResolver(pack, field_profile="auto")
        resolver._discovery_attempted = True
        resolver._field_cache = {
            "metrics.foo": {"double": {"aggregatable": True, "searchable": True}},
            "labels.instance": {
                "keyword": {"aggregatable": True, "searchable": True},
            },
        }
        with self.assertRaises(ValueError) as caught:
            resolver.resolve_auto_profile()
        self.assertIn("already_prefixed", str(caught.exception))

    def test_auto_otel_fallback_does_not_lint(self):
        pack = RulePackConfig()
        pack.metric_map.update(normalize_metric_map({"src": "metrics.already_prefixed"}))
        resolver = SchemaResolver(pack, field_profile="auto")
        resolver._discovery_attempted = True
        resolver._field_cache = {}
        resolver.resolve_auto_profile()
        self.assertEqual(resolver._auto_resolved_profile, "otel")


def test_shipped_pack_metric_maps_are_bare_under_named_prometheus_profiles():
    pack_yamls = sorted(_PACK_ROOT.glob("grafana_*/pack.yaml"))
    assert pack_yamls, "expected shipped Grafana curated packs"
    scanned = 0
    for pack_yaml in pack_yamls:
        pack = load_rule_pack_files([str(pack_yaml)])
        if not pack.metric_map:
            continue
        scanned += 1
        for profile in _NAMED_PROMETHEUS_PLANS:
            errors = grafana_metric_map_prefix_errors(pack.metric_map, profile)
            assert errors == [], f"{pack_yaml.parent.name} under {profile}: {errors}"
    assert scanned >= 1


class GrafanaMetricMapPrefixCliTests(unittest.TestCase):
    def test_migrate_exits_nonzero_for_native_namespaced_metric_map_file(self):
        from observability_migration.adapters.source.grafana import cli as grafana_cli

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            map_file = tmp_path / "map.yaml"
            map_file.write_text(
                yaml.safe_dump({"metric_map": {"src": "metrics.already_prefixed"}}),
                encoding="utf-8",
            )
            input_dir = tmp_path / "in"
            input_dir.mkdir()
            (input_dir / "dash.json").write_text(
                json.dumps(
                    {
                        "title": "Prefix lint probe",
                        "panels": [
                            {
                                "title": "A",
                                "type": "stat",
                                "targets": [{"expr": "up"}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            out_dir = tmp_path / "out"
            stderr = io.StringIO()
            old_stderr = sys.stderr
            try:
                sys.stderr = stderr
                with self.assertRaises(SystemExit) as caught:
                    grafana_cli.main(
                        [
                            "--source",
                            "files",
                            "--input-dir",
                            str(input_dir),
                            "--output-dir",
                            str(out_dir),
                            "--assets",
                            "dashboards",
                            "--field-profile",
                            "prometheus_native",
                            "--metric-map-file",
                            str(map_file),
                            "--no-curated-packs",
                        ]
                    )
            finally:
                sys.stderr = old_stderr
            self.assertEqual(caught.exception.code, 1)
            err = stderr.getvalue()
            self.assertIn("ERROR:", err)
            self.assertIn("Use the logical name 'already_prefixed'", err)
            self.assertFalse((out_dir / "dashboards" / "native").exists())


if __name__ == "__main__":
    pytest.main([__file__])
