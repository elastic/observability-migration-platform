# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana 15762 (Kubernetes / System / CoreDNS) curated pack plugin.

The job variable is ``label_values(coredns_build_info{cluster="$cluster"}, job)``.
When live field caps prove that metric absent, the general control translator
drops the scope so the dropdown is not empty. On a cluster that still has
``up`` and ``process_*`` for other exporters, Kibana then selects every scraped
job and the health, CPU, and memory panels plot those processes as CoreDNS.

This plugin scopes the control to a CoreDNS metric that is present instead
(info metrics are often dropped by relabeling while the request counters are
kept). Only when no CoreDNS metric is present does the control offer a single
value that matches no series.
"""

_PACK_NAME = "grafana_15762_k8s_system_coredns"
_SCOPE_METRIC = "coredns_build_info"
# CoreDNS metrics that carry the same job label, in preference order.
_SIBLING_METRICS = (
    "coredns_dns_requests_total",
    "coredns_dns_responses_total",
    "coredns_cache_entries",
)
# Not a Prometheus job name. Panel filters use MV_CONTAINS, so this selection
# matches no series and does not fall through the empty-selection "every job"
# branch.
_NO_JOB = "__no_matching_series__"


def _proven_absent(metric_name, resolver):
    from observability_migration.adapters.source.grafana.panels import (
        _resolve_control_scope_metric,
    )

    # "" means live field caps positively report the resolved field missing.
    return resolver is not None and not _resolve_control_scope_metric(metric_name, resolver, None)


def register(api):
    @api["variable_translators"].register("grafana_15762_coredns_job_scope", priority=5)
    def keep_job_scoped_to_coredns(context):
        pack = getattr(context, "rule_pack", None)
        if getattr(pack, "_curated_pack_name", "") != _PACK_NAME:
            return None
        variable = context.variable or {}
        if str(variable.get("name") or "") != "job":
            return None
        query_text = context.query_text or str(variable.get("query") or "")
        if _SCOPE_METRIC not in query_text.replace(" ", "").lower():
            return None
        resolver = context.resolver
        if not _proven_absent(_SCOPE_METRIC, resolver):
            return None

        for sibling in _SIBLING_METRICS:
            if _proven_absent(sibling, resolver):
                continue
            rewritten = query_text.replace(_SCOPE_METRIC, sibling)
            context.query_text = rewritten
            context.variable = dict(variable)
            context.variable["query"] = rewritten
            context.control_warnings.append(
                f"variable 'job' is scoped to coredns_build_info, which is not "
                f"present on the target; the control lists jobs from {sibling} instead"
            )
            return None

        from observability_migration.targets.kibana.emit.esql_utils import esql_identifier

        field_name = "job"
        resolve_label = getattr(resolver, "resolve_control_field", None)
        if resolve_label is not None:
            field_name = resolve_label("job") or field_name
        context.control = {
            "type": "esql",
            "label": variable.get("label") or "job",
            "variable_name": "job",
            "variable_type": "multi_values",
            "query": f'ROW {esql_identifier(field_name)} = "{_NO_JOB}"',
            "multiple": True,
            "default": [_NO_JOB],
        }
        context.control_warnings.append(
            "variable 'job' is scoped to CoreDNS metrics, none of which are "
            "present on the target; the control does not list other scraped jobs, "
            "so every job-filtered panel stays empty instead of plotting them"
        )
        context.handled = True
        return "job control has no coredns series"
