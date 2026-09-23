# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""An `object` node is a path, not a queryable field.

`_field_caps` reports the intermediate nodes of a dotted path as their own
entries. An OTLP target holding `service.name` therefore answers with **both**:

    service        ['object']
    service.name   ['keyword']

The readiness contract set `status: confirmed` for any non-``None``
capability, so a `passthrough` run resolving the Datadog tag `service` against
that target recorded `service -> confirmed` — while the emitted query fails at
runtime with `Unknown column [service], did you mean [service.name]?`.

`target_readiness_contract.json` is the artifact an operator reads to answer
"is my target ready", so calling an object container confirmed is the one
answer it must not give. Verified against Elasticsearch 9.6.0.
"""

from __future__ import annotations

import pytest

from observability_migration.core.verification.field_capabilities import (
    FieldCapability,
    is_object_container_field,
)


@pytest.mark.parametrize("container_type", ["object", "nested"])
def test_container_types_are_recognised(container_type):
    cap = FieldCapability(name="service", type=container_type)
    assert is_object_container_field(cap)


@pytest.mark.parametrize(
    "field_type", ["keyword", "long", "double", "date", "boolean", "ip", "text"]
)
def test_real_field_types_are_not_containers(field_type):
    assert not is_object_container_field(FieldCapability(name="x", type=field_type))


def test_none_is_not_a_container():
    assert not is_object_container_field(None)


def test_a_container_is_not_reported_as_confirmed():
    """End-to-end: the contract must not call an object node confirmed."""
    from copy import deepcopy

    from observability_migration.adapters.source.datadog.field_map import BUILTIN_PROFILES
    from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
    from observability_migration.adapters.source.datadog.preflight import (
        build_target_readiness_contract,
    )

    raw = {
        "title": "Shop",
        "widgets": [
            {
                "id": 1,
                "definition": {
                    "type": "timeseries",
                    "title": "Active orders",
                    "requests": [
                        {
                            "response_format": "timeseries",
                            "queries": [
                                {
                                    "data_source": "metrics",
                                    "name": "query1",
                                    "query": "avg:shop.orders.active{service:shop-lab}",
                                }
                            ],
                            "formulas": [{"formula": "query1"}],
                        }
                    ],
                },
            }
        ],
    }
    dashboard = normalize_dashboard(raw)
    profile = deepcopy(BUILTIN_PROFILES["passthrough"])
    profile.metric_field_caps = {
        "service": FieldCapability(name="service", type="object"),
        "service.name": FieldCapability(name="service.name", type="keyword"),
        "shop.orders.active": FieldCapability(name="shop.orders.active", type="double"),
    }
    contract = build_target_readiness_contract([dashboard], profile)
    by_name = contract["required_fields"]
    assert by_name["shop.orders.active"]["status"] == "confirmed"
    assert by_name["service"]["status"] == "missing", by_name.get("service")
