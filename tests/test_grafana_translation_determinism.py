# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Translation must not depend on PYTHONHASHSEED.

Found by running the 115-dashboard corpus under several hash seeds: "Loki stack
monitoring" grouped ``100 * rate(container_cpu…) / on(pod, container)
kube_pod_container_resource_limits_cpu_cores`` by [container, pod] in some runs
and by [pod] in others (merging every container of a pod), because the
dashboard-wide label inference iterated a set of metric names.
"""

import json
import os
import subprocess
import sys

from observability_migration.adapters.source.grafana.panels import _inferred_labels_for_target

_DASHBOARD = {
    "title": "Determinism",
    "uid": "det",
    "panels": [
        {
            "id": 1,
            "type": "timeseries",
            "title": "CPU vs limit",
            "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
            "datasource": {"type": "prometheus", "uid": "p"},
            "targets": [{"refId": "A", "expr": (
                '100 * rate(container_cpu_usage_seconds_total{container!=""}[1m]) '
                "/ on (pod, container) kube_pod_container_resource_limits_cpu_cores"
            )}],
        },
        {
            "id": 2,
            "type": "timeseries",
            "title": "CPU",
            "gridPos": {"x": 12, "y": 0, "w": 12, "h": 8},
            "datasource": {"type": "prometheus", "uid": "p"},
            "targets": [
                {"refId": "A", "expr": 'rate(container_cpu_usage_seconds_total{container!=""}[1m])',
                 "legendFormat": "{{pod}} {{container}}"},
                {"refId": "B", "expr": "min(kube_pod_container_resource_limits_cpu_cores{pod=~\"x.*\"}) by (pod)"},
            ],
        },
    ],
}

_SCRIPT = """
import json, sys
from observability_migration.adapters.source.grafana import panels
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
rp = RulePackConfig()
result = panels.translate_dashboard(json.loads(sys.argv[1]), rule_pack=rp, resolver=SchemaResolver(rp))
print(json.dumps([(p.title, p.esql_query) for p in result.panel_results]))
"""


def test_leftmost_operand_labels_win():
    labels = {
        "container_cpu_usage_seconds_total": ["container", "pod"],
        "kube_pod_container_resource_limits_cpu_cores": ["pod"],
    }
    target = {"expr": _DASHBOARD["panels"][0]["targets"][0]["expr"]}
    assert _inferred_labels_for_target(target, labels) == ["container", "pod"]


def test_translation_is_identical_across_hash_seeds():
    outputs = set()
    for seed in ("0", "1", "2", "5"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        run = subprocess.run(
            [sys.executable, "-c", _SCRIPT, json.dumps(_DASHBOARD)],
            capture_output=True, text=True, env=env, check=True,
        )
        outputs.add(run.stdout)
    assert len(outputs) == 1, outputs
