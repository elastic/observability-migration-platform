# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver


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
