# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver


def _planned_resolver(field_profile, field_cache=None):
    """Resolver with a named plan and seeded live caps (otel-shaped by default)."""
    resolver = SchemaResolver(
        RulePackConfig(),
        es_url="https://es",
        index_pattern="metrics-*",
        field_profile=field_profile,
    )
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    resolver._field_cache = dict(field_cache or {})
    return resolver


def _otel_shaped_field_cache():
    return {
        "instance": {"keyword": {"type": "keyword"}},
        "service.instance.id": {"keyword": {"type": "keyword"}},
        "http_requests_total": {"long": {"type": "long", "time_series_metric": "counter"}},
    }


def test_offline_prometheus_remote_write_emits_namespaced_label_and_metric():
    resolver = SchemaResolver(RulePackConfig(), field_profile="prometheus_remote_write")
    assert resolver.resolve_label("instance") == "prometheus.labels.instance"
    # Use whatever public metric helper the file already uses in passthrough tests;
    # assert remote_write leaf shape, e.g. prometheus.<metric>.counter or .value.
    metric = resolver.resolve_metric_field("http_requests_total")
    assert metric.startswith("prometheus.http_requests_total.")


def test_offline_prometheus_native_emits_native_paths():
    resolver = SchemaResolver(RulePackConfig(), field_profile="prometheus_native")
    assert resolver.resolve_label("instance") == "labels.instance"
    assert resolver.resolve_metric_field("http_requests_total") == "metrics.http_requests_total"


def test_offline_otel_keeps_bare_or_candidate_without_es_url():
    resolver = SchemaResolver(RulePackConfig(), field_profile="otel")
    # No live caps: bare source label is acceptable for otel plan.
    assert resolver.resolve_label("instance") in {"instance", "service.instance.id", "host.name"}


def test_passthrough_kwarg_and_field_profile_agree():
    a = SchemaResolver(RulePackConfig(), field_profile="passthrough")
    b = SchemaResolver(RulePackConfig(), passthrough=True)
    assert a._passthrough and b._passthrough
    assert a.resolve_label("instance") == "instance"
    assert b.resolve_label("instance") == "instance"


def test_planned_prometheus_remote_write_wins_over_bare_otel_caps():
    resolver = _planned_resolver(
        "prometheus_remote_write",
        field_cache=_otel_shaped_field_cache(),
    )
    assert resolver.resolve_label("instance") == "prometheus.labels.instance"
    assert resolver.resolve_metric_field("http_requests_total").startswith(
        "prometheus.http_requests_total."
    )
    summary = resolver.field_resolution_summary()
    assert summary["planned_schema_profile"] == "prometheus_remote_write"
    assert summary["detected_schema_profile"] is None
    assert summary["profile_mismatch"] is False


def test_planned_prometheus_native_wins_over_bare_otel_caps():
    resolver = _planned_resolver(
        "prometheus_native",
        field_cache=_otel_shaped_field_cache(),
    )
    assert resolver.resolve_label("instance") == "labels.instance"
    assert resolver.resolve_metric_field("http_requests_total") == "metrics.http_requests_total"


def test_planned_remote_write_metric_scoped_wins_over_bare_otel_caps():
    resolver = _planned_resolver(
        "prometheus_remote_write",
        field_cache=_otel_shaped_field_cache(),
    )
    metric = resolver.resolve_metric_field("http_requests_total")
    # Scoped co-occurrence would pick bare `instance`; planned emit must win.
    resolver._cooccurrence_cache = {
        (metric, "instance"): True,
        (metric, "service.instance.id"): False,
        (metric, "prometheus.labels.instance"): False,
    }
    assert resolver.resolve_label("instance", metric_field=metric) == "prometheus.labels.instance"


def test_planned_prometheus_native_metric_scoped_wins_over_bare_otel_caps():
    resolver = _planned_resolver(
        "prometheus_native",
        field_cache=_otel_shaped_field_cache(),
    )
    metric = resolver.resolve_metric_field("http_requests_total")
    resolver._cooccurrence_cache = {
        (metric, "instance"): True,
        (metric, "labels.instance"): False,
    }
    assert resolver.resolve_label("instance", metric_field=metric) == "labels.instance"


def test_planned_remote_write_mismatch_when_native_detected():
    resolver = _planned_resolver(
        "prometheus_remote_write",
        field_cache={
            "labels.instance": {"keyword": {"type": "keyword"}},
            "metrics.http_requests_total": {"double": {"type": "double"}},
            "instance": {"keyword": {"type": "keyword"}},
            "http_requests_total": {"long": {"type": "long"}},
        },
    )
    assert resolver.resolve_label("instance") == "prometheus.labels.instance"
    assert resolver.resolve_metric_field("http_requests_total").startswith(
        "prometheus.http_requests_total."
    )
    summary = resolver.field_resolution_summary()
    assert summary["detected_schema_profile"] == "prometheus_native"
    assert summary["profile_mismatch"] is True
