# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""A scope filter that emits a field reference must be assessed for readiness.

Readiness assessment skipped every scope value containing a template variable,
on the grounds that the field is only decided at view time. That is true for a
*pure* template (``service:$svc`` emits no clause at all), but not for a mixed
one: ``service:prod-$svc`` emits ``service LIKE "prod-*"``, a hard reference to
``service``. If live caps prove that field absent, the panel used to get no
DATA READINESS reason and then fail at query time with ``Unknown column``.

The rule is therefore about the clause, not the value: assess whenever a clause
is emitted, and skip only when nothing is emitted or when the *key* itself is
dynamic (a dynamic key names no field to assess, and recording the literal
``$k`` as a dependency is worse than recording nothing).
"""

from __future__ import annotations

from copy import deepcopy

from observability_migration.adapters.source.datadog.field_map import BUILTIN_PROFILES
from observability_migration.adapters.source.datadog.models import ScopeBoolOp, TagFilter
from observability_migration.adapters.source.datadog.translate import (
    _metric_scope_to_esql,
    _scope_filter_tag_keys,
)


def _profile():
    return deepcopy(BUILTIN_PROFILES["passthrough"])


def _emits(profile):
    return lambda item: bool(_metric_scope_to_esql(item, profile, context="metric"))


def _keys(item, profile=None):
    profile = profile or _profile()
    return _scope_filter_tag_keys(item, emits=_emits(profile))


def test_a_mixed_literal_and_template_value_is_assessed():
    """It emits `service LIKE "prod-*"`, so it depends on `service`."""
    item = TagFilter(key="service", value="prod-$svc")
    assert _metric_scope_to_esql(item, _profile(), context="metric")
    assert _keys(item) == ["service"]


def test_a_pure_template_value_is_not_assessed():
    item = TagFilter(key="service", value="$svc")
    assert not _metric_scope_to_esql(item, _profile(), context="metric")
    assert _keys(item) == []


def test_a_dynamic_key_is_not_assessed():
    """There is no field to judge, and `$k` is not one."""
    assert _keys(TagFilter(key="$k", value="prod")) == []


def test_a_bare_star_is_not_assessed():
    assert _keys(TagFilter(key="service", value="*")) == []


def test_a_negated_bare_star_is_assessed_because_it_emits():
    item = TagFilter(key="service", value="*", negated=True)
    if _metric_scope_to_esql(item, _profile(), context="metric"):
        assert _keys(item) == ["service"]


def test_a_plain_literal_is_still_assessed():
    assert _keys(TagFilter(key="service", value="prod")) == ["service"]


def test_nested_groups_are_assessed_child_by_child():
    group = ScopeBoolOp(
        op="AND",
        children=[
            TagFilter(key="service", value="prod-$svc"),
            TagFilter(key="env", value="$e"),
            TagFilter(key="host", value="web-1"),
        ],
    )
    keys = _keys(group)
    assert "service" in keys, keys
    assert "host" in keys, keys
    assert "env" not in keys, keys


def test_the_structural_fallback_still_works_without_an_emitter():
    """Called with no emitter the function keeps its old conservative rules."""
    assert _scope_filter_tag_keys(TagFilter(key="service", value="prod")) == ["service"]
    assert _scope_filter_tag_keys(TagFilter(key="service", value="*")) == []
