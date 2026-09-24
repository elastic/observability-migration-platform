# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Keep the node uptime tile a plain duration.

Grafana colors that stat's value green, yellow, or red. Kibana's metric
palette paints the whole tile, which reads as a solid bar next to the other
numbers. ``colorMode: none`` leaves the duration in the same style as CPU and
memory used.
"""

_PACK_NAME = "grafana_15759_k8s_views_nodes"


def register(api):
    @api["panel_translators"].register("grafana_15759_plain_uptime", priority=1)
    def plain_uptime(context):
        pack = getattr(getattr(context, "translation", None), "rule_pack", None)
        if getattr(pack, "_curated_pack_name", "") != _PACK_NAME:
            return None
        if str(getattr(context, "title", "") or "").strip() != "uptime":
            return None
        options = context.panel.get("options")
        if not isinstance(options, dict):
            options = {}
            context.panel["options"] = options
        options["colorMode"] = "none"
        return None
