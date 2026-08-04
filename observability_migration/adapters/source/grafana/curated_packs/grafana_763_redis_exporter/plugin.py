# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Curated variable shaping for Grafana dashboard 763."""

from __future__ import annotations


def register(api):
    variable_translators = api["variable_translators"]

    @variable_translators.register("grafana_763_curated_variables", priority=5)
    def curated_redis_763_variable_cleanup(context):
        rule_pack = getattr(context, "rule_pack", None)
        if getattr(rule_pack, "_curated_pack_name", "") != "grafana_763_redis_exporter":
            return None
        variable = getattr(context, "variable", {}) or {}
        if variable.get("type") != "query":
            return None

        name = str(variable.get("name") or "")
        if name == "namespace":
            context.handled = True
            context.trace.append(
                "curated Redis 763: skipped scope-only namespace variable; "
                "dashboard panels bind only instance"
            )
            return "skipped scope-only namespace variable for curated Redis 763"

        if name == "instance":
            context.query_text = "label_values(redis_up, instance)"
            return "curated Redis 763 instance control uses direct label_values(redis_up, instance)"

        return None
