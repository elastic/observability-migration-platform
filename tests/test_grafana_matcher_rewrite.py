# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Tests for binding-map-aware PromQL matcher rewriting (§6.1 rule table)."""

from observability_migration.adapters.source.grafana import promql
from observability_migration.core import variable_classifier as vc


class _Resolver:
    def resolve_label(self, label):
        return {"instance": "service.instance.id"}.get(label, label)


def _bm_single():
    return {"instance": vc.AcceptedBinding(
        field="service.instance.id", multi=False, options_query="FROM x"
    )}


def _bm_multi():
    return {"instance": vc.AcceptedBinding(
        field="service.instance.id", multi=True, options_query="FROM x"
    )}


def test_eq_single_value_emits_param():
    matcher = {"label": "instance", "op": "=", "value": "$instance"}
    out = promql._matcher_to_esql(matcher, _Resolver(), binding_map=_bm_single())
    assert out == "service.instance.id == ?instance"


def test_regex_single_value_no_meta_emits_param():
    matcher = {"label": "instance", "op": "=~", "value": "$instance"}
    out = promql._matcher_to_esql(matcher, _Resolver(), binding_map=_bm_single())
    assert out == "service.instance.id == ?instance"


def test_eq_multi_value_rejected_falls_back_to_none():
    matcher = {"label": "instance", "op": "=", "value": "$instance"}
    out = promql._matcher_to_esql(matcher, _Resolver(), binding_map=_bm_multi())
    assert out is None


def test_regex_multi_value_emits_mv_contains():
    matcher = {"label": "instance", "op": "=~", "value": "$instance"}
    out = promql._matcher_to_esql(matcher, _Resolver(), binding_map=_bm_multi())
    assert out == "MV_CONTAINS(?instance, service.instance.id)"


def test_neq_single_value_emits_param():
    matcher = {"label": "instance", "op": "!=", "value": "$instance"}
    out = promql._matcher_to_esql(matcher, _Resolver(), binding_map=_bm_single())
    assert out == "service.instance.id != ?instance"


def test_neg_regex_multi_value_emits_not_mv_contains():
    matcher = {"label": "instance", "op": "!~", "value": "$instance"}
    out = promql._matcher_to_esql(matcher, _Resolver(), binding_map=_bm_multi())
    assert out == "NOT MV_CONTAINS(?instance, service.instance.id)"


def test_no_binding_map_falls_through_to_legacy_behavior():
    matcher = {"label": "instance", "op": "=", "value": "$instance"}
    out = promql._matcher_to_esql(matcher, _Resolver(), binding_map=None)
    assert out is None


def test_rejected_binding_falls_through_to_legacy_behavior():
    bm = {"instance": vc.RejectedBinding(reason="include_all_unsupported")}
    matcher = {"label": "instance", "op": "=", "value": "$instance"}
    out = promql._matcher_to_esql(matcher, _Resolver(), binding_map=bm)
    assert out is None
