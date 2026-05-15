# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import unittest

from observability_migration.adapters.source.grafana.contract_evaluator import (
    evaluate_target_query_contract,
)
from observability_migration.adapters.source.grafana.fulfillment_planner import (
    plan_contract_fulfillment,
)
from observability_migration.core.assets.target_query_contract import (
    FieldRequirement,
    TargetEnvironmentSnapshot,
    TargetQueryContract,
)
from observability_migration.core.verification.field_capabilities import FieldCapability


class TestGrafanaContractEvaluator(unittest.TestCase):
    def test_all_tsds_numeric_counter_is_exact_now(self):
        contract = TargetQueryContract(
            canonical_target="ts",
            exactness_class="exact_if_contract_met",
            target_shape={"required_index_patterns": ["metrics-*"], "target_mode": "all_tsds"},
            field_requirements=[
                FieldRequirement(name="http_requests_total", role="metric", type_family="numeric", metric_kind="counter"),
            ],
            runtime_requirements={"source_command": "TS", "functions": ["RATE", "TBUCKET"]},
            degradation_policy={"fallback": "explicit_only"},
        )
        snapshot = TargetEnvironmentSnapshot(
            target_patterns={"metrics-*": {"all_tsds": True}},
            field_capabilities={
                "http_requests_total": FieldCapability(
                    name="http_requests_total",
                    type="long",
                    type_family="numeric",
                    time_series_metric_kind="counter",
                )
            },
            runtime_capabilities={"TS": True, "RATE": True, "TBUCKET": True},
        )

        evaluation = evaluate_target_query_contract(contract, snapshot)

        self.assertEqual(evaluation.status, "exact_now")
        self.assertIn("metrics-* is all-TSDS", evaluation.satisfied)
        self.assertIn("TS runtime is available", evaluation.satisfied)
        self.assertIn("RATE runtime is available", evaluation.satisfied)
        self.assertIn("http_requests_total has type_family numeric", evaluation.satisfied)
        self.assertIn("http_requests_total is marked as counter", evaluation.satisfied)

    def test_satisfied_reasons_omit_unspecified_field_requirements(self):
        contract = TargetQueryContract(
            canonical_target="ts",
            exactness_class="exact_if_contract_met",
            target_shape={"required_index_patterns": ["metrics-*"], "target_mode": "all_tsds"},
            field_requirements=[
                FieldRequirement(name="http_requests_total", role="metric"),
            ],
            runtime_requirements={"source_command": "TS"},
            degradation_policy={"fallback": "explicit_only"},
        )
        snapshot = TargetEnvironmentSnapshot(
            target_patterns={"metrics-*": {"all_tsds": True}},
            field_capabilities={
                "http_requests_total": FieldCapability(
                    name="http_requests_total",
                    type="long",
                    type_family="numeric",
                    time_series_metric_kind="counter",
                )
            },
            runtime_capabilities={"TS": True},
        )

        evaluation = evaluate_target_query_contract(contract, snapshot)

        self.assertEqual(evaluation.status, "exact_now")
        self.assertIn("metrics-* is all-TSDS", evaluation.satisfied)
        self.assertIn("TS runtime is available", evaluation.satisfied)
        for reason in evaluation.satisfied:
            self.assertNotIn("http_requests_total has type_family", reason)
            self.assertNotIn("http_requests_total is marked as", reason)

    def test_mixed_pattern_becomes_exact_after_fulfillment(self):
        contract = TargetQueryContract(
            canonical_target="ts",
            exactness_class="exact_if_contract_met",
            target_shape={"required_index_patterns": ["metrics-*"], "target_mode": "all_tsds", "mixed_forbidden": True},
            field_requirements=[],
            runtime_requirements={"source_command": "TS"},
            degradation_policy={"fallback": "explicit_only"},
            fulfillment_hints={"allow_index_narrowing": True},
        )
        snapshot = TargetEnvironmentSnapshot(
            target_patterns={"metrics-*": {"all_tsds": False}},
            runtime_capabilities={"TS": True},
        )

        evaluation = evaluate_target_query_contract(contract, snapshot)
        fulfillment = plan_contract_fulfillment(contract, evaluation)

        self.assertEqual(evaluation.status, "exact_after_fulfillment")
        self.assertEqual(fulfillment.status, "required")
        self.assertEqual(fulfillment.actions[0].kind, "narrow_index_pattern")

    def test_missing_field_is_degraded_not_fulfillable(self):
        contract = TargetQueryContract(
            canonical_target="ts",
            exactness_class="exact_if_contract_met",
            target_shape={"required_index_patterns": ["metrics-*"], "target_mode": "all_tsds"},
            field_requirements=[
                FieldRequirement(name="missing_counter", role="metric", type_family="numeric", metric_kind="counter"),
            ],
            runtime_requirements={"source_command": "TS"},
            degradation_policy={"fallback": "explicit_only"},
            fulfillment_hints={"allow_index_narrowing": True},
        )
        snapshot = TargetEnvironmentSnapshot(
            target_patterns={"metrics-*": {"all_tsds": True}},
            field_capabilities={},
            runtime_capabilities={"TS": True},
        )

        evaluation = evaluate_target_query_contract(contract, snapshot)

        self.assertEqual(evaluation.status, "degraded_if_forced")

    def test_fulfillment_actions_only_cover_unsatisfied_requirements(self):
        contract = TargetQueryContract(
            canonical_target="ts",
            exactness_class="exact_if_contract_met",
            target_shape={"required_index_patterns": ["metrics-*"], "target_mode": "all_tsds", "mixed_forbidden": True},
            field_requirements=[
                FieldRequirement(name="http_requests_total", role="metric", type_family="numeric", metric_kind="counter"),
            ],
            runtime_requirements={"source_command": "TS"},
            degradation_policy={"fallback": "explicit_only"},
            fulfillment_hints={"allow_index_narrowing": True},
        )
        snapshot = TargetEnvironmentSnapshot(
            target_patterns={"metrics-*": {"all_tsds": False}},
            field_capabilities={
                "http_requests_total": FieldCapability(
                    name="http_requests_total",
                    type="long",
                    type_family="numeric",
                    time_series_metric_kind="counter",
                )
            },
            runtime_capabilities={"TS": True},
        )

        evaluation = evaluate_target_query_contract(contract, snapshot)
        fulfillment = plan_contract_fulfillment(contract, evaluation)

        self.assertEqual(evaluation.status, "exact_after_fulfillment")
        self.assertEqual([action.kind for action in fulfillment.actions], ["narrow_index_pattern"])

    def test_missing_promql_runtime_is_blocked(self):
        contract = TargetQueryContract(
            canonical_target="promql",
            exactness_class="exact_if_contract_met",
            target_shape={"required_index_patterns": ["metrics-*"]},
            runtime_requirements={"source_command": "PROMQL"},
            degradation_policy={"fallback": "forbidden"},
        )
        snapshot = TargetEnvironmentSnapshot(runtime_capabilities={"PROMQL": False})

        evaluation = evaluate_target_query_contract(contract, snapshot)

        self.assertEqual(evaluation.status, "blocked")
