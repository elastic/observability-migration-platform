# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""The canonical Datadog monitor *asset* format must be readable.

``extract_monitors_from_files`` accepted a bare monitor object only when it
carried a top-level ``id`` **and** ``type``. Datadog does not publish monitors
that way: an integration asset — and the payload the Datadog UI exports —
wraps the monitor in ``definition``::

    {"version": 2, "title": "...", "tags": [...], "description": "...",
     "definition": {"name": ..., "type": "query alert", "query": "...", ...}}

All 60 monitor assets sampled from ``DataDog/integrations-core`` are that
shape, so every one was skipped and the run reported zero monitors to migrate.
21 of those 60 also carry no ``id`` at all, so requiring one would still drop a
third of them after unwrapping — a monitor without a source id is still a
monitor.

Types present in that sample: 44 ``query alert``, 16 ``log alert``.
"""

from __future__ import annotations

import json

import pytest

from observability_migration.adapters.source.datadog.extract import (
    extract_monitors_from_files,
)

ASSET = {
    "version": 2,
    "created_at": "2021-04-20",
    "title": "LDAP binding duration is high",
    "description": "Tracks binding duration.",
    "tags": ["integration:active-directory"],
    "definition": {
        "name": "[Active Directory] Elevated LDAP binding duration",
        "type": "query alert",
        "query": "avg(last_5m):avg:active_directory.ldap.bind_time{*} > 30",
        "message": "binding slow",
        "options": {"thresholds": {"critical": 30}},
    },
}


def _write(tmp_path, name, payload):
    (tmp_path / name).write_text(json.dumps(payload))
    return tmp_path


def test_the_asset_wrapper_is_unwrapped(tmp_path):
    monitors = extract_monitors_from_files(str(_write(tmp_path, "a.json", ASSET)))
    assert len(monitors) == 1, monitors
    assert monitors[0]["type"] == "query alert"
    assert monitors[0]["query"].startswith("avg(last_5m)")


def test_an_asset_without_an_id_is_still_a_monitor(tmp_path):
    assert "id" not in ASSET["definition"]
    monitors = extract_monitors_from_files(str(_write(tmp_path, "a.json", ASSET)))
    assert len(monitors) == 1


def test_the_outer_title_is_kept_when_the_definition_has_no_name(tmp_path):
    payload = json.loads(json.dumps(ASSET))
    payload["definition"].pop("name")
    monitors = extract_monitors_from_files(str(_write(tmp_path, "a.json", payload)))
    assert monitors[0].get("name") == "LDAP binding duration is high"


def test_a_log_alert_asset_is_accepted(tmp_path):
    payload = json.loads(json.dumps(ASSET))
    payload["definition"]["type"] = "log alert"
    monitors = extract_monitors_from_files(str(_write(tmp_path, "a.json", payload)))
    assert monitors[0]["type"] == "log alert"


def test_the_source_file_is_recorded(tmp_path):
    monitors = extract_monitors_from_files(str(_write(tmp_path, "a.json", ASSET)))
    assert monitors[0]["_source_file"].endswith("a.json")


# --- the shapes that already worked must keep working --------------------


def test_a_bare_monitor_with_id_and_type_still_works(tmp_path):
    payload = {"id": 1, "type": "query alert", "query": "avg(last_5m):avg:x{*} > 1"}
    monitors = extract_monitors_from_files(str(_write(tmp_path, "a.json", payload)))
    assert len(monitors) == 1 and monitors[0]["id"] == 1


def test_a_monitors_array_wrapper_still_works(tmp_path):
    payload = {"monitors": [{"id": 1, "type": "query alert", "query": "q"},
                            {"id": 2, "type": "log alert", "query": "q"}]}
    assert len(extract_monitors_from_files(str(_write(tmp_path, "a.json", payload)))) == 2


def test_a_top_level_list_still_works(tmp_path):
    payload = [{"id": 1, "type": "query alert", "query": "q"}]
    assert len(extract_monitors_from_files(str(_write(tmp_path, "a.json", payload)))) == 1


@pytest.mark.parametrize("payload", [
    {"not": "a monitor"},
    {"definition": {"name": "no type or query"}},
    {"definition": "not an object"},
])
def test_a_non_monitor_is_still_skipped(tmp_path, payload):
    assert extract_monitors_from_files(str(_write(tmp_path, "a.json", payload))) == []
