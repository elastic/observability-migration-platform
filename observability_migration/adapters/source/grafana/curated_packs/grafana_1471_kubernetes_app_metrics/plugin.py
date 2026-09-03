# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana 1471 (Kubernetes App Metrics) curated pack plugin.

The dashboard's ``$namespace`` / ``$container`` populate queries still use the
pre-1.16 cAdvisor label ``container_name``. Rewrite them onto the canonical
``container`` / ``namespace`` labels so the Kibana controls fill from a modern
scrape. Panel PromQL already goes through ``label_rewrites``; only the
templating queries need this hook.
"""


_PACK_NAME = "grafana_1471_kubernetes_app_metrics"


def register(api):
    @api["variable_translators"].register("grafana_1471_k8s_controls", priority=5)
    def rewrite_namespace_and_container_controls(context):
        pack = getattr(context, "rule_pack", None)
        if getattr(pack, "_curated_pack_name", "") != _PACK_NAME:
            return None
        variable = context.variable or {}
        name = str(variable.get("name") or "")
        lname = name.lower()
        query_text = context.query_text or str(variable.get("query") or "")
        compact = query_text.replace(" ", "").lower()

        if lname == "namespace" and "label_values(" in compact:
            rewritten = (
                'label_values(container_memory_usage_bytes{container!="POD"}, namespace)'
            )
            context.query_text = rewritten
            context.variable = dict(variable)
            context.variable["query"] = rewritten
            return None

        if lname == "container" and "label_values(" in compact:
            rewritten = (
                'label_values(container_memory_usage_bytes'
                '{namespace=~"$namespace",container!="POD"}, container)'
            )
            context.query_text = rewritten
            context.variable = dict(variable)
            context.variable["query"] = rewritten
            return None

        return None
