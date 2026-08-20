# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana 9628 (PostgreSQL Database) curated pack plugin.

Revision 1's Instance / Namespace / Release variables are Helm
``query_result()`` helpers. Kibana has no equivalent populate query, so the
engine skips them and the dashboard would have no Instance control. Rewrite
Instance to ``label_values(pg_up, instance)`` (the label the regex already
extracted) and drop the unused Helm cascade parents.
"""


_PACK_NAME = "grafana_9628_postgresql_database"


def register(api):
    @api["variable_translators"].register("grafana_9628_helm_query_result", priority=5)
    def rewrite_helm_query_result(context):
        pack = getattr(context, "rule_pack", None)
        if getattr(pack, "_curated_pack_name", "") != _PACK_NAME:
            return None
        variable = context.variable or {}
        name = str(variable.get("name") or "")
        query_text = context.query_text or str(variable.get("query") or "")
        if "query_result(" not in query_text.lower():
            return None
        if name in {"namespace", "release"}:
            context.handled = True
            return f"skipped helm-only query_result variable {name}"
        if name == "instance":
            rewritten = "label_values(pg_up, instance)"
            context.query_text = rewritten
            context.variable = dict(variable)
            context.variable["query"] = rewritten
            return None
        return None
