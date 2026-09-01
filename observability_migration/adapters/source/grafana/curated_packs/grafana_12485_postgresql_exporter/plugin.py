# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana 12485 (PostgreSQL Exporter) curated pack plugin.

Three control-populate rewrites that the general pipeline cannot infer:

* ``Instance`` is ``label_values({job="postgres-exporter"}, instance)``. The
  ``postgres-exporter`` job filter never matches an Elastic prometheus_native
  scrape (the job label there is ``postgres_exporter``/whatever the collector
  sets), so the control would populate empty. Rewrite it to
  ``label_values(pg_up, instance)`` — the same source-faithful ``instance``
  label, anchored on a metric that always exists per server.
* ``Database`` is a bare ``label_values(datname)`` with no metric anchor, so
  there is nothing for the ES|QL control query to key the canonical ``datname`` off.
  Anchor it on a per-database gauge (``pg_stat_database_numbackends``).
* ``Interval`` is a Grafana *interval* variable (rate-window helper), not a
  query variable. It must never become a Kibana control.
"""


_PACK_NAME = "grafana_12485_postgresql_exporter"


def register(api):
    @api["variable_translators"].register("grafana_12485_pg_controls", priority=5)
    def rewrite_pg_controls(context):
        pack = getattr(context, "rule_pack", None)
        if getattr(pack, "_curated_pack_name", "") != _PACK_NAME:
            return None
        variable = context.variable or {}
        name = str(variable.get("name") or "")
        lname = name.lower()
        var_type = str(variable.get("type") or "").lower()
        query_text = context.query_text or str(variable.get("query") or "")
        compact = query_text.replace(" ", "").lower()

        # Interval helper variable — drop it, no panel binds it as a control.
        if var_type == "interval" or lname == "interval":
            context.handled = True
            return f"skipped Grafana interval variable {name}"

        if lname == "instance" and "label_values(" in compact:
            rewritten = "label_values(pg_up, instance)"
            context.query_text = rewritten
            context.variable = dict(variable)
            context.variable["query"] = rewritten
            return None

        if lname in {"database", "datname"} and "label_values(" in compact:
            rewritten = "label_values(pg_stat_database_numbackends, datname)"
            context.query_text = rewritten
            context.variable = dict(variable)
            context.variable["query"] = rewritten
            return None

        return None
