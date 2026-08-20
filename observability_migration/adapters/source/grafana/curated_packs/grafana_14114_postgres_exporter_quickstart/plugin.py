# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana 14114 (PostgreSQL Exporter Quickstart) curated pack plugin.

Revision 1's Instance variable is ``label_values(up{job=~"postgres.*"}, instance)``.
Prometheus ``up`` is not stored for postgres_exporter on typical Elastic
prometheus_native scrapes (``pg_up`` is). The unused ``job`` template never
appears in panel PromQL, so emitting it only adds an incompatible default
(``postgres`` vs ``postgres_exporter``).
"""


_PACK_NAME = "grafana_14114_postgres_exporter_quickstart"


def register(api):
    @api["variable_translators"].register("grafana_14114_pg_up_instance", priority=5)
    def rewrite_instance_and_drop_job(context):
        pack = getattr(context, "rule_pack", None)
        if getattr(pack, "_curated_pack_name", "") != _PACK_NAME:
            return None
        variable = context.variable or {}
        name = str(variable.get("name") or "")
        query_text = context.query_text or str(variable.get("query") or "")
        compact = query_text.replace(" ", "").lower()
        if name == "job":
            context.handled = True
            return f"skipped unused job variable {name}"
        if name == "instance" and "label_values(up" in compact:
            rewritten = "label_values(pg_up, instance)"
            context.query_text = rewritten
            context.variable = dict(variable)
            context.variable["query"] = rewritten
            return None
        return None
