# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana 15761 (Kubernetes / System / API Server) curated pack plugin.

The job variable is ``label_values(apiserver_request_total{cluster="$cluster"}, job)``.
When live field caps prove that metric absent, the general control translator
drops the scope so the dropdown is not empty. On a cluster that still has
``up`` and ``process_*`` for other exporters, Kibana then selects every scraped
job and the health, CPU, and memory panels plot those processes as the API
server. This plugin keeps the control from listing those jobs: its only option
is a sentinel that no real job label equals, so the panels stay empty.
"""

_PACK_NAME = "grafana_15761_k8s_system_apiserver"
_SCOPE_METRIC = "apiserver_request_total"
# Not a Prometheus job name. Panel filters use MV_CONTAINS, so this selection
# matches no series and does not fall through the empty-selection "every job"
# branch.
_NO_JOB = "__no_matching_series__"


def _identifier(name):
    if name.replace("_", "").isalnum() and not name[:1].isdigit():
        return name
    return "`" + name.replace("`", "``") + "`"


def _scope_metric_proven_absent(resolver):
    if resolver is None:
        return False
    field_exists = getattr(resolver, "field_exists", None)
    if field_exists is None:
        return False
    from observability_migration.adapters.source.grafana.panels import (
        _resolve_control_scope_metric,
    )

    if _resolve_control_scope_metric(_SCOPE_METRIC, resolver, None):
        return False
    resolved_name = _SCOPE_METRIC
    resolve = getattr(resolver, "resolve_metric_field", None)
    if resolve is not None:
        resolved = resolve(_SCOPE_METRIC)
        if resolved:
            resolved_name = resolved
    return field_exists(resolved_name) is False


def register(api):
    @api["variable_translators"].register("grafana_15761_apiserver_job_scope", priority=5)
    def keep_job_from_listing_unrelated_exporters(context):
        pack = getattr(context, "rule_pack", None)
        if getattr(pack, "_curated_pack_name", "") != _PACK_NAME:
            return None
        variable = context.variable or {}
        if str(variable.get("name") or "") != "job":
            return None
        query_text = context.query_text or str(variable.get("query") or "")
        if _SCOPE_METRIC not in query_text.replace(" ", "").lower():
            return None
        if not _scope_metric_proven_absent(context.resolver):
            return None

        resolver = context.resolver
        field_name = "job"
        resolve_label = getattr(resolver, "resolve_control_field", None)
        if resolve_label is not None:
            resolved = resolve_label("job")
            if resolved:
                field_name = resolved
        column = _identifier(field_name)
        index = context.data_view or "metrics-*"
        context.control = {
            "type": "esql",
            "label": variable.get("label") or "job",
            "variable_name": "job",
            "variable_type": "multi_values",
            "query": (
                f"FROM {index}"
                f" | STATS count = COUNT(*)"
                f' | EVAL {column} = "{_NO_JOB}"'
                f" | KEEP {column}"
                f" | LIMIT 1"
            ),
            "multiple": True,
            "default": [_NO_JOB],
        }
        context.control_warnings.append(
            "variable 'job' is scoped to apiserver_request_total, which is not "
            "present on the target; the control does not list other scraped jobs, "
            "so health, CPU, and memory stay empty instead of plotting them"
        )
        context.handled = True
        return "job control has no apiserver series"
