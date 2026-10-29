# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Dashboard ids must be unique across a run, not just artifact stems.

Curated packs 12660 ("Kubernetes / Persistent Volumes") and 13646
("kubernetes-persistent-volumes") got distinct artifact stems but one Kibana id,
so the second upload was refused as ``duplicate_id``.
"""

from observability_migration.adapters.source.grafana.cli import _allocate_dashboard_output_stem
from observability_migration.targets.kibana.dashboards_api import dashboard_id_for_title


def test_titles_that_slug_to_one_id_are_disambiguated():
    used_stems: set[str] = set()
    used_ids: set[str] = set()
    first = _allocate_dashboard_output_stem(
        title="Kubernetes / Persistent Volumes", dashboard_uid="pv-a", used_stems=used_stems, used_ids=used_ids,
    )
    second = _allocate_dashboard_output_stem(
        title="kubernetes-persistent-volumes", dashboard_uid="pv-b", used_stems=used_stems, used_ids=used_ids,
    )
    assert first[1] == ""
    assert second[1] == "pv-b"
    first_id = dashboard_id_for_title("Kubernetes / Persistent Volumes", first[1])
    second_id = dashboard_id_for_title("kubernetes-persistent-volumes", second[1])
    assert first_id == "obs-migrate-kubernetes-persistent-volumes"
    assert second_id == "obs-migrate-kubernetes-persistent-volumes-pv-b"


def test_unique_titles_keep_their_plain_id():
    used_stems: set[str] = set()
    used_ids: set[str] = set()
    for title in ("Redis", "MySQL Overview"):
        _stem, token = _allocate_dashboard_output_stem(title=title, dashboard_uid="u", used_stems=used_stems, used_ids=used_ids)
        assert token == ""
    assert used_ids == {"obs-migrate-redis", "obs-migrate-mysql-overview"}
