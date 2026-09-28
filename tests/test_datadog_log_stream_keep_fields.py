# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""A log-stream panel's display columns must follow the field profile.

The ``log_stream`` / ``list_stream`` branch emitted a hardcoded ECS column
list::

    | KEEP @timestamp, message, log.level, service.name, host.name

Six of the seven Datadog profiles map ``status``/``service``/``host`` to
exactly those ECS names, so the hardcoding was invisible. ``passthrough``
keeps the Datadog spellings (``status``, ``service``, ``host``), so every log
panel failed against a correctly seeded target::

    Unknown column [log.level]
    Unknown column [service.name]
    Unknown column [host.name]

Measured with the full migrate -> seed -> ``verifier.live_validate`` cycle on
the in-repo Datadog corpus: 11 of 261 queries, across 8 dashboards, and the
only profile affected was the one the customer demo used.

These columns must resolve through ``map_tag(..., context="log")`` like every
other field, so the profile decides the spelling.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from observability_migration.adapters.source.datadog.field_map import BUILTIN_PROFILES
from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.translate import translate_widget

ECS_PROFILES = [
    "default",
    "otel",
    "prometheus",
    "prometheus_metrics",
    "prometheus_native",
    "elastic_agent",
]


def _log_stream_query(profile_name):
    raw = {
        "title": "Log panel",
        "widgets": [
            {
                "id": 1,
                "definition": {
                    "type": "log_stream",
                    "title": "Log Events",
                    "query": "source:apache",
                    "columns": ["host", "service"],
                },
            }
        ],
    }
    dashboard = normalize_dashboard(raw)
    widget = dashboard.widgets[0]
    profile = deepcopy(BUILTIN_PROFILES[profile_name])
    result = translate_widget(widget, plan_widget(widget), profile)
    return result.esql_query or ""


def _keep_columns(query):
    for line in query.splitlines():
        if line.strip().startswith("| KEEP"):
            return [c.strip() for c in line.split("KEEP", 1)[1].split(",")]
    return []


@pytest.mark.parametrize("profile_name", ECS_PROFILES)
def test_ecs_profiles_keep_the_ecs_column_names(profile_name):
    """Unchanged behavior for every profile that maps to ECS."""
    columns = _keep_columns(_log_stream_query(profile_name))
    assert columns == ["@timestamp", "message", "log.level", "service.name", "host.name"], columns


def test_passthrough_keeps_the_datadog_column_names():
    """The profile that was broken: Datadog spellings, not ECS."""
    columns = _keep_columns(_log_stream_query("passthrough"))
    assert columns == ["@timestamp", "message", "status", "service", "host"], columns


@pytest.mark.parametrize("profile_name", [*ECS_PROFILES, "passthrough"])
def test_no_profile_emits_a_column_it_does_not_map(profile_name):
    """Every non-structural column must be something the profile resolves to."""
    profile = deepcopy(BUILTIN_PROFILES[profile_name])
    resolvable = {
        profile.map_tag(tag, context="log")
        for tag in ("status", "service", "host", "source", "message")
    }
    for column in _keep_columns(_log_stream_query(profile_name)):
        if column in ("@timestamp", "message"):
            continue
        assert column in resolvable, f"{profile_name}: {column} not mapped by the profile"


def test_a_custom_log_tag_map_is_honoured():
    """An operator profile that renames the level field must be respected."""
    profile = deepcopy(BUILTIN_PROFILES["passthrough"])
    profile.log_tag_map = {"status": "severity", "service": "svc", "host": "hostname"}
    raw = {
        "title": "Log panel",
        "widgets": [
            {"id": 1, "definition": {"type": "log_stream", "title": "L", "query": "*"}}
        ],
    }
    dashboard = normalize_dashboard(raw)
    widget = dashboard.widgets[0]
    query = (translate_widget(widget, plan_widget(widget), profile).esql_query or "")
    columns = _keep_columns(query)
    assert columns == ["@timestamp", "message", "severity", "svc", "hostname"], columns
