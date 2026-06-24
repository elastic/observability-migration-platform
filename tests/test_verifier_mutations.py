# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for verifier mutation testing harness."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parity-rig"))

from verifier import mutations  # noqa: E402


def _clean_report() -> dict:
    query = (
        "TS metrics-* "
        "| STATS value = AVG(x) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), service.name "
        "| EVAL legend = CONCAT(service.name) "
        "| KEEP time_bucket, value, legend"
    )
    return {
        "dashboards": [
            {
                "title": "D",
                "panels": [
                    {
                        "title": "P",
                        "status": "migrated",
                        "grafana_type": "timeseries",
                        "reasons": [],
                        "post_validation_action": "",
                        "query_ir": {
                            "output_shape": "time_series",
                            "output_group_fields": ["time_bucket", "service.name"],
                            "warnings": [],
                            "semantic_losses": [],
                        },
                        "visual_ir": {
                            "presentation": {
                                "kind": "esql",
                                "config": {
                                    "type": "line",
                                    "query": query,
                                    "dimension": {"field": "time_bucket"},
                                    "metrics": [{"field": "value"}],
                                    "breakdown": {"field": "legend"},
                                },
                            }
                        },
                    }
                ],
            }
        ]
    }


class TestMutations:
    def test_each_mutation_trips_expected_category(self) -> None:
        results = mutations.run_invariant_mutations(_clean_report())
        assert results
        assert all(result.passed for result in results)
        observed = {result.mutation: result.expected_category for result in results}
        assert observed == {
            "break_accessor": "ACCESSOR_BROKEN",
            "break_composite_legend": "BREAKDOWN_LEGEND_MISMATCH",
            "silent_placeholder": "PLACEHOLDER_DROPPED",
        }

    def test_summarize_reports_failed_mutations(self) -> None:
        results = [
            mutations.MutationResult("m1", "CAT", True, ["CAT"]),
            mutations.MutationResult("m2", "OTHER", False, ["CAT"]),
        ]
        summary = mutations.summarize(results)
        assert summary["total"] == 2
        assert summary["passed"] == 1
        assert summary["failed"][0]["mutation"] == "m2"

    def test_mutations_work_on_native_promql_only_reports(self) -> None:
        report = {
            "dashboards": [
                {
                    "title": "D",
                    "panels": [
                        {
                            "title": "native",
                            "status": "migrated",
                            "grafana_type": "timeseries",
                            "reasons": [],
                            "visual_ir": {
                                "presentation": {
                                    "kind": "esql",
                                    "config": {
                                        "type": "line",
                                        "query": "PROMQL index=metrics-* step=1m value=(up)",
                                        "dimension": {"field": "step"},
                                        "metrics": [{"field": "value"}],
                                    },
                                }
                            },
                        }
                    ],
                }
            ]
        }
        results = mutations.run_invariant_mutations(report)
        assert all(result.passed for result in results)

