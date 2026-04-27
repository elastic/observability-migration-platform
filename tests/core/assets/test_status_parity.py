"""Cross-source asset status parity tests.

Verifies that both Grafana and Datadog status vocabularies map
consistently into the shared AssetStatus vocabulary.
"""

import unittest

from observability_migration.core.assets.status import AssetStatus


class TestStatusParity(unittest.TestCase):
    """Both sources must produce the same shared status vocabulary."""

    def test_all_grafana_statuses_map(self):
        grafana_statuses = ["migrated", "migrated_with_warnings", "requires_manual", "not_feasible"]
        for status in grafana_statuses:
            result = AssetStatus.from_grafana(status)
            self.assertIsInstance(result, AssetStatus)

    def test_all_datadog_statuses_map(self):
        datadog_statuses = ["ok", "warning", "blocked"]
        for status in datadog_statuses:
            result = AssetStatus.from_datadog(status)
            self.assertIsInstance(result, AssetStatus)

    def test_success_maps_to_same_status(self):
        self.assertEqual(
            AssetStatus.from_grafana("migrated"),
            AssetStatus.from_datadog("ok"),
        )

    def test_warning_maps_to_same_status(self):
        self.assertEqual(
            AssetStatus.from_grafana("migrated_with_warnings"),
            AssetStatus.from_datadog("warning"),
        )

    def test_failure_maps_to_same_status(self):
        self.assertEqual(
            AssetStatus.from_grafana("not_feasible"),
            AssetStatus.from_datadog("blocked"),
        )

    def test_status_values_are_strings(self):
        for status in AssetStatus:
            self.assertIsInstance(status.value, str)
            self.assertTrue(len(status.value) > 0)


if __name__ == "__main__":
    unittest.main()
