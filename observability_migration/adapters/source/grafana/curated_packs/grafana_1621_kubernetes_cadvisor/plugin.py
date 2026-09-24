# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana 1621 (Kubernetes cluster monitoring via Prometheus) curated pack plugin.

``$Node`` is ``label_values(kubernetes_io_hostname)``. That Heapster-era node
label is not on a modern cAdvisor scrape; ``machine_cpu_cores`` carries
``instance`` (the pack rewrites ``kubernetes_io_hostname`` → canonical
``instance``), so populate from that instead of leaving the control empty.

The source variable is single-select with ``includeAll`` / ``allValue: .*``.
Kibana live-hydration of a single-select VALUES_FROM_QUERY control picks the
first concrete node, so first paint would be one node instead of Grafana All.
Mark it multi-select so upload hydration pre-selects every option (Kibana's
stand-in for Grafana All); panel ES|QL uses ``MV_CONTAINS``.
"""


_PACK_NAME = "grafana_1621_kubernetes_cadvisor"
_NODE_QUERY = "label_values(machine_cpu_cores, instance)"


def _mark_multi_select(pack):
    names = set(getattr(pack, "_multi_select_param_names", None) or ())
    names.add("Node")
    setattr(pack, "_multi_select_param_names", names)


def register(api):
    @api["query_preprocessors"].register("grafana_1621_k8s_multi_select", priority=1)
    def mark_node_multi(context):
        pack = getattr(context, "rule_pack", None)
        if getattr(pack, "_curated_pack_name", "") != _PACK_NAME:
            return None
        _mark_multi_select(pack)
        return None

    @api["variable_translators"].register("grafana_1621_k8s_controls", priority=5)
    def rewrite_node_control(context):
        pack = getattr(context, "rule_pack", None)
        if getattr(pack, "_curated_pack_name", "") != _PACK_NAME:
            return None
        variable = context.variable or {}
        name = str(variable.get("name") or "")
        query_text = context.query_text or str(variable.get("query") or "")
        compact = query_text.replace(" ", "").lower()

        if name != "Node" or "label_values(" not in compact:
            return None

        variable = dict(variable)
        variable["query"] = _NODE_QUERY
        variable["multi"] = True
        variable["includeAll"] = True
        variable["allValue"] = ".*"
        context.variable = variable
        context.query_text = _NODE_QUERY
        _mark_multi_select(pack)
        return None
