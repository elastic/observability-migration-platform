# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana 747 (Kubernetes Pod Metrics) curated pack plugin.

``$Node`` is ``label_values(kubernetes_io_hostname)``. Populate from
``machine_cpu_cores`` / canonical ``instance`` instead (same bridge as 741 /
1621). ``$Pod`` is already ``label_values(kube_pod_info, pod)``.

Both are single-select with Grafana All (``.*``). Mark them multi-select so
first paint selects every concrete option instead of hydrating to the first
pod/node. Hidden ``$Pod_ip`` / ``$phase`` / ``$container`` stay skipped; the
pack replaces those markdown interpolations with a pod→IP / pod→container
datatable and a live phase metric tile.
"""


_PACK_NAME = "grafana_747_kubernetes_pod_metrics"
_NODE_QUERY = "label_values(machine_cpu_cores, instance)"
_MULTI = ("Node", "Pod")


def _mark_multi_select(pack):
    names = set(getattr(pack, "_multi_select_param_names", None) or ())
    names.update(_MULTI)
    setattr(pack, "_multi_select_param_names", names)


def register(api):
    @api["query_preprocessors"].register("grafana_747_k8s_multi_select", priority=1)
    def mark_pod_and_node_multi(context):
        pack = getattr(context, "rule_pack", None)
        if getattr(pack, "_curated_pack_name", "") != _PACK_NAME:
            return None
        _mark_multi_select(pack)
        return None

    @api["variable_translators"].register("grafana_747_k8s_controls", priority=5)
    def rewrite_node_and_pod_controls(context):
        pack = getattr(context, "rule_pack", None)
        if getattr(pack, "_curated_pack_name", "") != _PACK_NAME:
            return None
        variable = context.variable or {}
        name = str(variable.get("name") or "")
        query_text = context.query_text or str(variable.get("query") or "")
        compact = query_text.replace(" ", "").lower()
        if name not in _MULTI or "label_values(" not in compact:
            return None

        variable = dict(variable)
        if name == "Node":
            variable["query"] = _NODE_QUERY
            context.query_text = _NODE_QUERY
        variable["multi"] = True
        variable["includeAll"] = True
        variable["allValue"] = ".*"
        context.variable = variable
        _mark_multi_select(pack)
        return None
