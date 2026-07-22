# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Registry of Datadog → ES|QL translator paths for the translation harness."""

from __future__ import annotations

DATADOG_ESQL_EMITTERS: tuple[str, ...] = (
    "metric_single_query",
    "metric_formula",
    "log_direct_esql",
    "log_kql_bridge",
)

EMITTER_HELPER_SYMBOLS: dict[str, str] = {
    "metric_single_query": "metric_single_query_rule",
    "metric_formula": "metric_formula_rule",
    "log_direct_esql": "log_direct_esql_rule",
    "log_kql_bridge": "log_kql_bridge_rule",
}

EMITTER_RULE_IDS: dict[str, str] = {
    "metric_single_query": "datadog.translate.metric_single_query",
    "metric_formula": "datadog.translate.metric_formula",
    "log_direct_esql": "datadog.translate.log_direct_esql",
    "log_kql_bridge": "datadog.translate.log_kql_bridge",
}
