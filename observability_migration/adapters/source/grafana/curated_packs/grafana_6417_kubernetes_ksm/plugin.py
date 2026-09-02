# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana 6417 (Kubernetes Cluster / kube-state-metrics) curated pack plugin.

``$node`` and ``$namespace`` are Grafana *constant* variables whose only value
is ``.*`` (match-all regex). The engine still synthesizes VALUES_FROM_QUERY
controls from the ``?node`` / ``?namespace`` panel params, then live-upload
hydration picks the first concrete option — so the dashboard opens filtered
to one namespace and one node instead of Grafana's all-match default.

Rewrite the constants to multi-select ``label_values()`` query variables
anchored on metrics this dashboard always queries. Multi-select is the
Kibana-safe replacement for Grafana's ``.*`` all-sentinel: upload hydration
pre-selects every concrete option, and ES|QL panel filters use ``MV_CONTAINS``.

``_collect_multi_select_param_names`` runs *before* variable translators, so a
query preprocessor also marks these params multi-select on the pack before
any panel query is emitted.
"""


_PACK_NAME = "grafana_6417_kubernetes_ksm"

_QUERY = {
    "node": "label_values(kube_node_info, node)",
    "namespace": "label_values(kube_pod_info, namespace)",
}


def _mark_multi_select(pack):
    names = set(getattr(pack, "_multi_select_param_names", None) or ())
    names.update(_QUERY)
    setattr(pack, "_multi_select_param_names", names)


def register(api):
    @api["query_preprocessors"].register("grafana_6417_k8s_multi_select", priority=1)
    def mark_k8s_controls_multi(context):
        pack = getattr(context, "rule_pack", None)
        if getattr(pack, "_curated_pack_name", "") != _PACK_NAME:
            return None
        _mark_multi_select(pack)
        return None

    @api["variable_translators"].register("grafana_6417_k8s_controls", priority=5)
    def rewrite_constant_all_controls(context):
        pack = getattr(context, "rule_pack", None)
        if getattr(pack, "_curated_pack_name", "") != _PACK_NAME:
            return None
        variable = context.variable or {}
        name = str(variable.get("name") or "").lower()
        if name not in _QUERY:
            return None
        rewritten = _QUERY[name]
        # Mutate in place so later ``_ensure_param_controls`` / multi-select
        # collection on the same templating list sees query + multi.
        variable["type"] = "query"
        variable["query"] = rewritten
        variable["multi"] = True
        variable["includeAll"] = True
        variable["allValue"] = ".*"
        context.variable = variable
        context.query_text = rewritten
        _mark_multi_select(pack)
        return None
