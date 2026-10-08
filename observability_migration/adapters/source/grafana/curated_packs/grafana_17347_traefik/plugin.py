# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana 17347 (Traefik Official Kubernetes Dashboard) curated pack plugin.

The ``service`` variable is ``label_values(traefik_service_requests_total,
service)`` with the regex ``/([^@]+)@.*/``, so Grafana lists bare service
names such as ``default-whoami-80`` rather than ``default-whoami-80@kubernetes``.
The generic control query lists raw label values, so this rewrite drops the
``@provider`` suffix before the distinct-value STATS. Panels match the chosen
name as a prefix, as in ``service=~"$service.*"``.
"""

import re

_PACK_NAME = "grafana_17347_traefik"
_VALUES_STATS_RE = re.compile(r" \| STATS count = COUNT\(\*\) BY (\S+)")


def register(api):
    # The generic query_variable rule (priority 10) marks the variable handled,
    # which ends the chain, so run it here and adjust the control it builds.
    @api["variable_translators"].register("grafana_17347_service_control", priority=9)
    def strip_service_provider_suffix(context):
        pack = getattr(context, "rule_pack", None)
        if getattr(pack, "_curated_pack_name", "") != _PACK_NAME:
            return None
        variable = context.variable or {}
        if str(variable.get("name") or "") != "service":
            return None
        from observability_migration.adapters.source.grafana.panels import query_variable_rule

        detail = query_variable_rule(context)
        control = context.control
        if not isinstance(control, dict) or control.get("type") != "esql":
            return detail
        query = str(control.get("query") or "")
        match = _VALUES_STATS_RE.search(query)
        if not match:
            return detail
        field = match.group(1)
        strip = f' | EVAL {field} = MV_FIRST(SPLIT(TO_STRING({field}), "@"))'
        control["query"] = query[: match.start()] + strip + query[match.start():]
        return f"{detail}; service control drops the @provider suffix"
