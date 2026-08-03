# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for adapter interfaces and registries."""

import unittest

from observability_migration.core.interfaces import (
    SourceAdapter,
    TargetAdapter,
    source_registry,
    target_registry,
)


class TestSourceRegistry(unittest.TestCase):
    def test_grafana_registered(self):
        import observability_migration.adapters.source.grafana.adapter  # noqa: F401
        self.assertIn("grafana", source_registry)

    def test_datadog_registered(self):
        import observability_migration.adapters.source.datadog.adapter  # noqa: F401
        self.assertIn("datadog", source_registry)

    def test_get_unknown_raises(self):
        with self.assertRaises(KeyError):
            source_registry.get("nonexistent_source")

    def test_adapter_capabilities(self):
        import observability_migration.adapters.source.grafana.adapter  # noqa: F401
        cls = source_registry.get("grafana")
        adapter = cls()
        self.assertIn("dashboards", adapter.supported_assets)
        self.assertIn("files", adapter.supported_input_modes)

    def test_every_adapter_declaring_alert_support_can_translate_alerts(self):
        """``supported_assets`` must not under- or over-report alert support.

        Datadog omitted ``alerts`` while shipping full monitor migration
        (``monitor_translate``/``alert_pipeline``/``monitor_seed``, reachable via
        ``--assets alerts``). Nothing gates on the declaration, so the
        inconsistency was invisible -- but it is the introspection surface
        ``docs/contributing/add-source.md`` tells new adapters to fill in
        accurately, so it is pinned here in both directions.
        """
        import observability_migration.adapters.source.datadog.adapter
        import observability_migration.adapters.source.grafana.adapter  # noqa: F401

        expected_alert_builder = {
            "grafana": "build_alerting_ir_from_grafana",
            "datadog": "build_alerting_ir_from_datadog",
        }
        from observability_migration.core.assets import alerting

        for name, builder in expected_alert_builder.items():
            adapter = source_registry.get(name)()
            can_translate = hasattr(alerting, builder)
            declares = "alerts" in adapter.supported_assets
            self.assertEqual(
                declares,
                can_translate,
                f"{name}: declares alerts={declares} but alerting.{builder} "
                f"present={can_translate}",
            )

    def test_grafana_extension_catalog_available(self):
        import observability_migration.adapters.source.grafana.adapter  # noqa: F401

        adapter = source_registry.get("grafana")()
        catalog = adapter.build_extension_catalog()

        self.assertEqual(catalog["adapter"], "grafana")
        self.assertTrue(any(surface["id"] == "grafana.rule_pack" for surface in catalog["current_surfaces"]))
        self.assertTrue(any(rule["registry"] == "query_translators" for rule in catalog["rules"]))

    def test_datadog_extension_catalog_available(self):
        import observability_migration.adapters.source.datadog.adapter  # noqa: F401

        adapter = source_registry.get("datadog")()
        catalog = adapter.build_extension_catalog()

        self.assertEqual(catalog["adapter"], "datadog")
        self.assertTrue(any(surface["id"] == "datadog.field_profile" for surface in catalog["current_surfaces"]))
        self.assertTrue(any(rule["id"] == "datadog.plan.metric_timeseries" for rule in catalog["rules"]))
        self.assertTrue(any(rule["registry"] == "metric_translators" for rule in catalog["rules"]))


class TestTargetRegistry(unittest.TestCase):
    def test_kibana_registered(self):
        import observability_migration.targets.kibana.adapter  # noqa: F401

        self.assertIn("kibana", target_registry)

    def test_kibana_target_adapter_smoke_method_available(self):
        import observability_migration.targets.kibana.adapter  # noqa: F401

        adapter = target_registry.get("kibana")()
        self.assertTrue(callable(adapter.smoke))


class TestAdapterABC(unittest.TestCase):
    def test_cannot_instantiate_abstract(self):
        with self.assertRaises(TypeError):
            SourceAdapter()
        with self.assertRaises(TypeError):
            TargetAdapter()


if __name__ == "__main__":
    unittest.main()
