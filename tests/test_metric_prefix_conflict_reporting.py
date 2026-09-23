# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""A metric that is also another metric's prefix is mappable — with subobjects off.

Datadog has both ``redis.keys`` and ``redis.keys.evicted``. Under the *default*
object mapping Elasticsearch refuses the pair::

    can't merge a non object mapping [redis.keys] with an object mapping

which is why the contract used to drop the parent. That drop was treating an
Elasticsearch *default* as an Elasticsearch *limit*. Verified against
Elasticsearch 9.6.0-SNAPSHOT:

    default mapping      -> rejected
    subobjects: false    -> accepted; both indexed; ES|QL returns both values
    subobjects: "auto"   -> rejected ("unknown subobjects value: auto")

``subobjects: false`` stores a dotted name as a literal leaf instead of an
object path, and it composes with ``index.mode: time_series`` — template
accepted, document indexed, ES|QL read back. Elastic's own OTel metrics
mapping reaches the same place by declaring ``metrics`` and ``attributes`` as
``passthrough`` objects, which is how an OTLP target can hold
``redis.keys`` and ``redis.keys.evicted`` side by side.

The seeder already emits flat dotted document keys, which is exactly what
``subobjects: false`` requires, so nothing else had to change. Same fix also
lets ``service`` and ``service.name`` coexist.
"""

from __future__ import annotations

import pytest

from observability_migration.core.telemetry_data import (
    plan_index_template,
    unmappable_field_names,
)


def _stream(*names, role="metric"):
    return {"fields": {n: {"role": role} for n in names}}


def _props(stream, pattern="metrics-*"):
    return plan_index_template(pattern, stream)["template"]["mappings"]["properties"]


def _mappings(stream, pattern="metrics-*"):
    return plan_index_template(pattern, stream)["template"]["mappings"]


def test_the_template_turns_subobjects_off():
    assert _mappings(_stream("a.b"))["subobjects"] is False


def test_logs_templates_also_turn_subobjects_off():
    assert _mappings(_stream("a.b"), pattern="logs-*")["subobjects"] is False


def test_a_parent_metric_and_its_child_both_survive():
    props = _props(_stream("redis.keys", "redis.keys.evicted", "redis.keys.expired"))
    for name in ("redis.keys", "redis.keys.evicted", "redis.keys.expired"):
        assert name in props, name


@pytest.mark.parametrize(
    "parent,child",
    [
        ("redis.keys", "redis.keys.evicted"),
        ("rabbitmq.queues", "rabbitmq.queues.created.count"),
        ("rabbitmq.queue.messages", "rabbitmq.queue.messages.acked.count"),
    ],
)
def test_every_corpus_collision_is_now_mappable(parent, child):
    """The three pairs that cost `passthrough` six runnable queries."""
    props = _props(_stream(parent, child))
    assert parent in props and child in props


def test_a_dimension_and_its_dotted_child_both_survive():
    """The Grafana ``service`` / ``service.name`` pair."""
    stream = {
        "fields": {
            "m": {"role": "metric"},
            "service": {"role": "dimension"},
            "service.name": {"role": "dimension"},
        }
    }
    props = _props(stream)
    assert "service" in props and "service.name" in props


def test_nothing_is_reported_as_unmappable_for_a_prefix_pair():
    assert unmappable_field_names(_stream("redis.keys", "redis.keys.evicted")) == frozenset()


# --- the genuinely unmappable case is still caught ------------------------


@pytest.mark.parametrize("bad", ["trailing.dot.", ".leading", "double..dot", "."])
def test_an_empty_path_segment_is_still_unmappable(bad):
    """``subobjects: false`` does not make an empty segment legal."""
    assert bad in unmappable_field_names(_stream(bad, "ok.field"))
    assert "ok.field" not in unmappable_field_names(_stream(bad, "ok.field"))


def test_documents_still_omit_an_unmappable_name():
    from observability_migration.core.telemetry_data import generate_documents

    contract = {"streams": {"metrics-*": _stream("m", "broken.name.")}}
    for _name, doc in generate_documents(contract, data_hours=1, interval_sec=3600):
        for key in doc:
            assert not key.endswith("."), key
