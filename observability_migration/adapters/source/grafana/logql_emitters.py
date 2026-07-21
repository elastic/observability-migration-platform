# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Registered LogQL → ES|QL translator routes for the PR3 emitter matrix."""

from __future__ import annotations

GRAFANA_LOGQL_EMITTERS: tuple[str, ...] = (
    "logql_stream",
    "logql_count",
)

EMITTER_HELPER_SYMBOLS: dict[str, str] = {
    "logql_stream": "logql_stream_family_rule",
    "logql_count": "logql_count_family_rule",
}

__all__ = ["EMITTER_HELPER_SYMBOLS", "GRAFANA_LOGQL_EMITTERS"]
