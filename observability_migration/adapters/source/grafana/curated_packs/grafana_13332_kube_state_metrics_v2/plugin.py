# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana 13332 display corrections that a query override cannot express.

Cluster Mem Capacity plots byte gauges (allocatable, capacity, requested,
limits) but the source axis unit is ``bits``, so Kibana would render memory
about 8x too small. Bytes match the metric.
"""

_PACK_NAME = "grafana_13332_kube_state_metrics_v2"


def register(api):
    @api["panel_translators"].register("grafana_13332_memory_bytes", priority=1)
    def show_memory_capacity_as_bytes(context):
        pack = getattr(getattr(context, "translation", None), "rule_pack", None)
        if getattr(pack, "_curated_pack_name", "") != _PACK_NAME:
            return None
        if str(getattr(context, "title", "") or "").strip() != "Cluster Mem Capacity":
            return None
        for axis in (context.panel.get("yaxes") or []):
            if isinstance(axis, dict) and axis.get("format") == "bits":
                axis["format"] = "bytes"
        return None
