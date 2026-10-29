# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""The render audit must attribute dashboards that share a title.

Grafana 315 and 1621 are both "Kubernetes cluster monitoring (via Prometheus)".
Their ids differ only by a disambiguator, but every report key (title, title
slug) was shared, so neither could be attributed ("per-panel attribution
unavailable").
"""

import json

from observability_migration.targets.kibana.render_audit import report_dashboards_by_key
from observability_migration.targets.kibana.render_audit_driver import native_dashboard_id_aliases

_TITLE = "Kubernetes cluster monitoring (via Prometheus)"
_REPORT = {
    "dashboards": [
        {"title": _TITLE, "artifact_stem": "k8s_mon", "panels": [{"title": "A"}]},
        {"title": _TITLE, "artifact_stem": "k8s_mon_2", "panels": [{"title": "B"}]},
    ]
}


def test_index_aliases_carry_the_artifact_stem(tmp_path):
    native = tmp_path / "native"
    native.mkdir()
    (native / "index.json").write_text(json.dumps({"dashboards": [
        {"stem": "k8s_mon", "title": _TITLE, "dashboard_id": "obs-migrate-k8s-mon"},
        {"stem": "k8s_mon_2", "title": _TITLE, "dashboard_id": "obs-migrate-k8s-mon-2"},
    ]}))
    assert native_dashboard_id_aliases(tmp_path) == {
        "obs-migrate-k8s-mon": "k8s_mon", "obs-migrate-k8s-mon-2": "k8s_mon_2",
    }


def test_same_titled_dashboards_are_attributed_by_stem():
    aliases = {
        # The plain title slug: also derived (ambiguously) from both titles.
        "obs-migrate-kubernetes-cluster-monitoring-via-prometheus": "k8s_mon",
        "obs-migrate-kubernetes-cluster-monitoring-via-prometheus-2": "k8s_mon_2",
    }
    by_key = report_dashboards_by_key(_REPORT, id_aliases=aliases)

    first = by_key["obs-migrate-kubernetes-cluster-monitoring-via-prometheus"]
    second = by_key["obs-migrate-kubernetes-cluster-monitoring-via-prometheus-2"]
    assert first["panels"][0]["title"] == "A"
    assert second["panels"][0]["title"] == "B"
    # The shared title itself stays unattributable rather than guessed.
    assert _TITLE not in by_key


def test_older_title_aliases_still_work_when_titles_are_unique():
    report = {"dashboards": [{"title": "Redis", "panels": []}]}
    assert "obs-migrate-redis-x" in report_dashboards_by_key(report, id_aliases={"obs-migrate-redis-x": "Redis"})
