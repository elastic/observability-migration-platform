# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Curated variable shaping for Grafana dashboard 11835."""

from __future__ import annotations


def register(api):
    variable_translators = api["variable_translators"]

    @variable_translators.register("grafana_11835_curated_variables", priority=5)
    def curated_redis_11835_variable_cleanup(context):
        rule_pack = getattr(context, "rule_pack", None)
        if getattr(rule_pack, "_curated_pack_name", "") != "grafana_11835_redis_exporter_helm":
            return None
        # Chained label_values() scoping now binds through named ES|QL params, so
        # the generic translator can preserve the source variable graph without
        # curated flattening.
        return None
