# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for pinned stratified benchmark corpus manifest builder."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "parity-rig"))

from verifier import corpus_manifest  # noqa: E402


def _catalog(n: int = 20):
    return [
        corpus_manifest.DashboardEntry(
            id=str(i),
            name=f"dash-{i}",
            downloads=1000 - i,
            tags=("tag",),
            org_slug="org",
        )
        for i in range(1, n + 1)
    ]


class TestCorpusManifest:
    def test_top_selects_by_downloads(self) -> None:
        manifest = corpus_manifest.build_manifest(_catalog(), top_count=3)
        assert [e.id for e in manifest.top] == ["1", "2", "3"]

    def test_long_tail_is_deterministic_and_rank_bounded(self) -> None:
        first = corpus_manifest.build_manifest(
            _catalog(100), long_tail_specs=["tail:20:40:5"], seed="s"
        )
        second = corpus_manifest.build_manifest(
            _catalog(100), long_tail_specs=["tail:20:40:5"], seed="s"
        )
        ids = [e.id for e in first.long_tail["tail"]]
        assert ids == [e.id for e in second.long_tail["tail"]]
        assert len(ids) == 5
        assert all(20 <= int(i) < 40 for i in ids)

    def test_datasource_quotas(self) -> None:
        ds_map = {
            "1": ["prometheus"],
            "2": ["prometheus", "loki"],
            "3": ["loki"],
            "4": ["cloudwatch"],
        }
        manifest = corpus_manifest.build_manifest(
            _catalog(6),
            datasource_map=ds_map,
            datasource_quotas=["prometheus=2", "loki=2", "cloudwatch=5"],
        )
        assert [e.id for e in manifest.datasource_strata["prometheus"]] == ["1", "2"]
        assert [e.id for e in manifest.datasource_strata["loki"]] == ["2", "3"]
        assert [e.id for e in manifest.datasource_strata["cloudwatch"]] == ["4"]

    def test_bug_seeds_are_in_all_ids_without_duplication(self) -> None:
        manifest = corpus_manifest.build_manifest(_catalog(5), top_count=2, bug_seeds=["2", "999"])
        assert manifest.all_ids() == ["1", "2", "999"]

    def test_to_jsonable_shape(self) -> None:
        manifest = corpus_manifest.build_manifest(
            _catalog(5),
            top_count=1,
            long_tail_specs=["3:5:1"],
            bug_seeds=["999"],
        )
        payload = manifest.to_jsonable()
        assert sorted(payload["grafana"].keys()) == [
            "all_ids",
            "bug_seeds",
            "datasource_strata",
            "long_tail",
            "top",
        ]
        assert payload["grafana"]["bug_seeds"] == ["999"]

    def test_load_catalog_and_datasource_map(self, tmp_path: Path) -> None:
        catalog_path = tmp_path / "dashboards.json"
        catalog_path.write_text(json.dumps([
            {"id": "10", "name": "n", "downloads": 7, "tags": ["a"], "orgSlug": "o"}
        ]))
        ds_path = tmp_path / "ds.json"
        ds_path.write_text(json.dumps({"10": ["Prometheus"]}))
        entries = corpus_manifest.load_catalog(catalog_path)
        assert entries[0].id == "10"
        assert entries[0].downloads == 7
        assert corpus_manifest.load_datasource_map(ds_path) == {"10": ["prometheus"]}

    def test_bug_seed_file_shapes(self, tmp_path: Path) -> None:
        list_path = tmp_path / "list.json"
        list_path.write_text(json.dumps(["1", "2"]))
        assert corpus_manifest._load_bug_seeds(list_path, ["0"]) == ["0", "1", "2"]

        obj_path = tmp_path / "obj.json"
        obj_path.write_text(json.dumps({"grafana": ["3"]}))
        assert corpus_manifest._load_bug_seeds(obj_path, []) == ["3"]

