# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""One unmappable field name must not cost the whole stream.

``plan_index_template`` expands dotted field names into an object path, so a
name Elasticsearch cannot map takes down the *entire* template -- and with it
every panel's data, because ``obs-migrate seed-sample-data`` aborts the stream:

    composable template [...] template after composition is invalid
    caused_by: can't merge a non object mapping [x.y] with an object mapping

Two shapes do that:

* An empty path segment (``a.b.`` / ``.a`` / ``a..b``), which makes the parent
  both a leaf and an object.
* A dotted leaf that is also the prefix of a deeper dotted field (``a.b`` as a
  metric alongside ``a.b.c``). The existing guard covered only the *flat*
  version of this (``a`` alongside ``a.b``); the dotted version is the same
  conflict.

The contract should not produce either (see
``tests/test_telemetry_contract_quoted_identifiers.py`` for the extractor bug
that did), but the template planner is the layer that decides whether a bad
name degrades one field or all of them.
"""

from __future__ import annotations

import pytest

from observability_migration.core.telemetry_data import plan_index_template


def _stream(*field_names, role="metric"):
    return {"fields": {name: {"role": role} for name in field_names}}


def _props(stream, pattern="metrics-*"):
    return plan_index_template(pattern, stream)["template"]["mappings"]["properties"]


@pytest.mark.parametrize(
    "bad_name",
    [
        "consul.raft.leader.lastContact.",
        "haproxy.backend.response.",
        ".leading",
        "double..dot",
        ".",
    ],
)
def test_a_field_name_with_an_empty_segment_is_skipped(bad_name):
    props = _props(_stream(bad_name, "system.cpu.user"))
    assert bad_name not in props
    assert "system.cpu.user" in props, "the good field must survive"


def test_a_dotted_leaf_that_is_also_a_parent_is_kept():
    """Both survive: ``subobjects: false`` makes each a literal leaf.

    Under the default object mapping Elasticsearch refuses the pair, which is
    why this used to assert the parent was skipped. The template now sets
    ``subobjects: false`` (verified accepted on 9.6.0, including with
    ``index.mode: time_series``), so dropping the parent would lose a real
    metric for no reason.
    """
    props = _props(_stream("haproxy.backend.response", "haproxy.backend.response.5xx"))
    assert "haproxy.backend.response" in props
    assert "haproxy.backend.response.5xx" in props


def test_a_flat_name_and_its_dotted_children_both_survive():
    props = _props(_stream("system", "system.cpu.user"))
    assert "system" in props
    assert "system.cpu.user" in props


def test_no_planned_property_name_has_an_empty_segment():
    """The invariant, stated over a mixed stream."""
    props = _props(
        _stream(
            "system.load.",
            "system.load.1",
            "redis.slowlog.micros.",
            "redis.slowlog.micros.95percentile",
            "ok.field",
        )
    )
    for name in props:
        assert not name.startswith("."), name
        assert not name.endswith("."), name
        assert ".." not in name, name
    assert "system.load.1" in props
    assert "redis.slowlog.micros.95percentile" in props


def test_a_generated_dimension_with_an_empty_segment_is_also_skipped():
    """``group_fields`` becomes a generated dimension, so it needs the guard too."""
    stream = {
        "fields": {"m": {"role": "metric"}},
        "group_fields": ["good.dim", "bad.dim."],
    }
    props = _props(stream)
    assert "good.dim" in props
    assert "bad.dim." not in props
    for name in props:
        assert not name.endswith("."), name


def test_a_generated_dimension_deeper_than_a_contract_metric_coexists():
    """A metric and a deeper generated dimension are both literal leaves now."""
    stream = {
        "fields": {"pool.size": {"role": "metric"}},
        "group_fields": ["pool.size.tier"],
    }
    props = _props(stream)
    assert "pool.size" in props
    assert "pool.size.tier" in props


# --- documents must agree with the template ------------------------------


def _docs(contract):
    from observability_migration.core.telemetry_data import generate_documents

    return list(generate_documents(contract, data_hours=1, interval_sec=3600))


def test_service_and_service_name_now_coexist():
    """The Grafana pair that cost 2,684 of 51,545 documents.

    ``service`` as a dimension alongside ``service.name`` used to force
    ``service`` to be an object, so the template skipped the leaf and any
    document still carrying it was rejected in full ("object mapping for
    [service] tried to parse field [service] as object"). With
    ``subobjects: false`` both are literal leaves and both are kept --
    verified against Elasticsearch 9.6.0, which indexes the pair and returns
    both values from ES|QL.
    """
    contract = {
        "streams": {
            "metrics-*": {
                "fields": {
                    "m": {"role": "metric"},
                    "service": {"role": "dimension"},
                    "service.name": {"role": "dimension"},
                }
            }
        }
    }
    props = _props(contract["streams"]["metrics-*"])
    assert "service" in props and "service.name" in props

    for _stream, doc in _docs(contract):
        for key in doc:
            assert key in props, f"{key} is emitted but unmapped"


def test_documents_omit_an_unmappable_field_name():
    contract = {
        "streams": {
            "metrics-*": {
                "fields": {
                    "m": {"role": "metric"},
                    "redis.slowlog.micros.": {"role": "metric"},
                }
            }
        }
    }
    for _stream, doc in _docs(contract):
        for name in doc:
            assert not name.endswith("."), name


def test_every_document_key_exists_in_the_planned_template():
    """The invariant: documents and mapping cannot disagree."""
    stream = {
        "fields": {
            "m": {"role": "metric"},
            "service": {"role": "dimension"},
            "service.name": {"role": "dimension"},
            "pool.size": {"role": "metric"},
            "pool.size.tier": {"role": "dimension"},
        }
    }
    contract = {"streams": {"metrics-*": stream}}
    props = set(_props(stream))
    for _name, doc in _docs(contract):
        for key in doc:
            assert key in props, f"{key} is emitted but unmapped"


# --- the operator must be told what was dropped --------------------------


def test_an_unmappable_name_is_reported_not_silently_dropped():
    """Excluding a field is sometimes right, but hiding it is not.

    Only an empty path segment is unmappable now; a prefix pair is fine. The
    operator still has to be told about the one that is dropped, or the panel
    shows "Unknown column" with nothing connecting it to a seeding decision.
    """
    from observability_migration.core.telemetry_data import unmappable_field_report

    stream = {
        "fields": {
            "m": {"role": "metric"},
            "service": {"role": "dimension"},
            "service.name": {"role": "dimension"},
            "broken.name.": {"role": "metric"},
        }
    }
    report = unmappable_field_report({"streams": {"metrics-*": stream}})
    joined = " ".join(report)
    assert "broken.name." in joined, report
    assert "service" not in joined, "a prefix pair is mappable and must not be reported"


def test_no_report_when_nothing_is_skipped():
    from observability_migration.core.telemetry_data import unmappable_field_report

    stream = {"fields": {"m": {"role": "metric"}, "host.name": {"role": "dimension"}}}
    assert unmappable_field_report({"streams": {"metrics-*": stream}}) == []
