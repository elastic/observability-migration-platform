import unittest

from observability_migration.core.verification.comparators import (
    build_comparison_result,
    comparison_gate_override,
)


class TestComparisonSemantics(unittest.TestCase):
    def test_time_series_result_summaries_within_tolerance_are_green(self):
        query_ir = {
            "output_shape": "time_series",
            "source_expression": 'sum(rate(http_server_requests_total{service="api"}[5m])) by (service)',
            "target_query": "TS metrics-* | STATS requests = SUM(RATE(http_server_requests_total, 5m)) BY time_bucket, service",
        }
        source_execution = {
            "status": "pass",
            "result_summary": {
                "rows": 10,
                "columns": ["time_bucket", "service", "requests"],
                "values": [],
            },
        }
        target_execution = {
            "status": "pass",
            "result_summary": {
                "rows": 11,
                "columns": ["time_bucket", "service", "requests"],
                "values": [],
            },
        }

        comparison = build_comparison_result(source_execution, target_execution, query_ir)
        self.assertEqual(comparison.status, "within_tolerance")
        self.assertEqual(comparison_gate_override(comparison.to_dict()), "Green")

    def test_single_value_material_drift_is_red(self):
        query_ir = {
            "output_shape": "single_value",
            "source_expression": "avg(system.cpu.user)",
            "target_query": "FROM metrics-* | STATS value = AVG(system_cpu_user)",
        }
        source_execution = {
            "status": "pass",
            "result_summary": {
                "rows": 1,
                "columns": ["value"],
                "values": [[100.0]],
            },
        }
        target_execution = {
            "status": "pass",
            "result_summary": {
                "rows": 1,
                "columns": ["value"],
                "values": [40.0],
            },
        }

        comparison = build_comparison_result(source_execution, target_execution, query_ir)
        self.assertEqual(comparison.status, "material_drift")
        self.assertEqual(comparison_gate_override(comparison.to_dict()), "Red")


if __name__ == "__main__":
    unittest.main()
