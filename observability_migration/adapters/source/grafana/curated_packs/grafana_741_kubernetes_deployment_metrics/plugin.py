# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana 741 (Kubernetes Deployment metrics) curated pack plugin.

Two control-populate rewrites that the general pipeline cannot infer:

* ``Deployment`` is a bare ``label_values(deployment)`` with no metric anchor,
  so there is nothing for the ES|QL control query to key ``labels.deployment``
  off. Anchor it on ``kube_deployment_status_replicas``.
* ``Node`` is ``label_values(kubernetes_io_hostname)``. That Heapster-era
  node label is not on a modern cAdvisor scrape; ``machine_cpu_cores`` carries
  ``instance`` (the pack rewrites ``kubernetes_io_hostname`` →
  ``labels.instance``), so populate from that instead of leaving the control
  empty.
"""


_PACK_NAME = "grafana_741_kubernetes_deployment_metrics"


def register(api):
    @api["variable_translators"].register("grafana_741_k8s_controls", priority=5)
    def rewrite_deployment_and_node_controls(context):
        pack = getattr(context, "rule_pack", None)
        if getattr(pack, "_curated_pack_name", "") != _PACK_NAME:
            return None
        variable = context.variable or {}
        name = str(variable.get("name") or "")
        lname = name.lower()
        query_text = context.query_text or str(variable.get("query") or "")
        compact = query_text.replace(" ", "").lower()

        if lname == "deployment" and "label_values(" in compact:
            rewritten = "label_values(kube_deployment_status_replicas, deployment)"
            context.query_text = rewritten
            context.variable = dict(variable)
            context.variable["query"] = rewritten
            return None

        if lname == "node" and "label_values(" in compact:
            rewritten = "label_values(machine_cpu_cores, instance)"
            context.query_text = rewritten
            context.variable = dict(variable)
            context.variable["query"] = rewritten
            return None

        return None
