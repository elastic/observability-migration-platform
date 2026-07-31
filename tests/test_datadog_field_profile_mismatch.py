# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Choosing a field profile the target does not use must not fail silently."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from types import SimpleNamespace

from observability_migration.adapters.source.datadog.cli import (
    _warn_on_field_profile_mismatch,
)


def _profile(name, prefix, caps, index="metrics-redis.prometheus-default"):
    return SimpleNamespace(
        name=name, metric_prefix=prefix, metric_field_caps=dict.fromkeys(caps, object()),
        metric_index=index,
    )


def _warn(profile):
    buf = io.StringIO()
    with redirect_stdout(buf):
        _warn_on_field_profile_mismatch(profile)
    return buf.getvalue()


def test_prometheus_profile_against_a_native_index_warns_and_names_the_fix():
    """`prometheus` expects prometheus.metrics.*; the native layout is metrics.*.

    Picking wrong migrates, reports success and uploads, then every panel fails in
    Kibana with "Unknown column" -- nothing had compared the profile against the
    index it was pointed at.
    """
    out = _warn(_profile("prometheus", "prometheus.metrics.",
                         ["metrics.redis_up", "labels.instance"]))
    assert "WARNING" in out
    assert "prometheus.metrics." in out
    assert "--field-profile prometheus_native" in out


def test_native_profile_against_a_prometheus_integration_index_names_the_fix():
    out = _warn(_profile("prometheus_native", "metrics.",
                         ["prometheus.metrics.redis_up", "prometheus.labels.instance"]))
    assert "--field-profile prometheus" in out


def test_matching_profile_is_silent():
    assert _warn(_profile("prometheus_native", "metrics.",
                          ["metrics.redis_up", "labels.instance"])) == ""


def test_prefixless_profile_is_silent():
    """otel / elastic_agent / passthrough declare no prefix; nothing to check."""
    assert _warn(_profile("otel", "", ["system.cpu.utilization"])) == ""


def test_offline_discovery_is_silent():
    """With no discovered fields there is no evidence either way."""
    assert _warn(_profile("prometheus", "prometheus.metrics.", [])) == ""
