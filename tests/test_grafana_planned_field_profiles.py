# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import contextlib
import io
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from observability_migration.adapters.source.grafana import alert_pipeline, panels
from observability_migration.adapters.source.grafana import cli as grafana_cli
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


def test_auto_ambiguous_caps_falls_back_to_otel_and_warns():
    resolver = SchemaResolver(
        RulePackConfig(),
        es_url="https://es.example",
        field_profile="auto",
    )
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    # Caps that match neither Fleet nor native patterns:
    resolver._field_cache = {"host.name": {"keyword": {}}, "http_requests_total": {"double": {}}}
    summary = resolver.field_resolution_summary()  # or ensure_fields / discover hook used by CLI
    assert summary["field_profile"] in {"auto", "otel"}  # document chosen canonical
    # Effective emit is otel-like (bare or candidate), not prometheus.labels.*
    assert resolver.resolve_label("job") != "prometheus.labels.job"
    # Warning / note present — implement via summary key or list attribute:
    assert summary.get("auto_fallback") == "otel" or any(
        "otel" in w.lower() for w in getattr(resolver, "_profile_warnings", [])
    )


def test_planned_remote_write_keeps_emit_when_detected_native():
    resolver = SchemaResolver(
        RulePackConfig(),
        es_url="https://es.example",
        field_profile="prometheus_remote_write",
    )
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    resolver._field_cache = {
        "metrics.http_requests_total": {"double": {}},
        "labels.instance": {"keyword": {}},
    }
    assert resolver.resolve_label("instance") == "prometheus.labels.instance"
    summary = resolver.field_resolution_summary()
    assert summary.get("profile_mismatch") is True
    assert summary.get("detected_schema_profile") == "prometheus_native"
    assert summary.get("planned_schema_profile") == "prometheus_remote_write"


def test_cli_rejects_auto_without_es_url():
    args = SimpleNamespace(field_profile="auto", es_url="")
    with pytest.raises(SystemExit) as ei:
        grafana_cli._validate_field_profile(args)
    assert ei.value.code == 2


def test_cli_accepts_prometheus_remote_write():
    args = SimpleNamespace(field_profile="prometheus_remote_write", es_url="")
    grafana_cli._validate_field_profile(args)  # no raise


def test_cli_accepts_prometheus_native():
    args = SimpleNamespace(field_profile="prometheus_native", es_url="")
    grafana_cli._validate_field_profile(args)


def test_cli_accepts_auto_with_es_url():
    args = SimpleNamespace(field_profile="auto", es_url="https://es.example")
    grafana_cli._validate_field_profile(args)


def test_dashboard_resolver_threads_field_profile():
    args = SimpleNamespace(
        field_profile="prometheus_remote_write",
        es_url="https://es",
        esql_index="metrics-*",
        data_view="metrics-ui-*",
        es_api_key="key",
    )
    rule_pack = RulePackConfig()

    with patch.object(grafana_cli, "SchemaResolver") as resolver_class:
        grafana_cli._build_dashboard_schema_resolver(
            args,
            rule_pack,
            verify="/tmp/test-ca.pem",
        )

    resolver_class.assert_called_once_with(
        rule_pack,
        es_url="https://es",
        index_pattern="metrics-*",
        es_api_key="key",
        verify="/tmp/test-ca.pem",
        field_profile="prometheus_remote_write",
    )


def test_alert_resolver_threads_field_profile():
    args = SimpleNamespace(
        field_profile="prometheus_native",
        es_url="https://es",
        esql_index="metrics-*",
        data_view="metrics-*",
        es_api_key="key",
    )
    rule_pack = RulePackConfig()

    with (
        patch.object(
            grafana_cli,
            "_load_configured_rule_pack",
            return_value=rule_pack,
        ),
        patch.object(grafana_cli, "_apply_native_promql_to_rule_pack"),
        patch.object(grafana_cli, "_resolve_tls_from_args", return_value=False),
        patch(
            "observability_migration.adapters.source.grafana.schema.SchemaResolver",
        ) as resolver_class,
    ):
        alert_pipeline._build_alert_schema_resolver(args)

    resolver_class.assert_called_once_with(
        rule_pack,
        es_url="https://es",
        index_pattern="metrics-*",
        es_api_key="key",
        verify=False,
        field_profile="prometheus_native",
    )


def test_alternate_index_resolver_inherits_field_profile():
    rule_pack = RulePackConfig()
    parent = SchemaResolver(
        rule_pack,
        es_url="https://es",
        index_pattern="metrics-primary-*",
        es_api_key="key",
        verify="/tmp/test-ca.pem",
        field_profile="prometheus_remote_write",
    )

    alternate = panels._resolver_for_index(
        parent,
        rule_pack,
        "metrics-alternate-*",
    )

    assert alternate is not parent
    assert alternate._field_profile == "prometheus_remote_write"


def test_cli_discovery_status_shows_planned_detected_and_mismatch():
    resolver = _planned_resolver(
        "prometheus_remote_write",
        field_cache={
            "labels.instance": {"keyword": {"type": "keyword"}},
            "metrics.http_requests_total": {"double": {"type": "double"}},
        },
    )
    output = io.StringIO()

    with contextlib.redirect_stdout(output):
        grafana_cli._print_schema_discovery_status(
            resolver,
            field_profile="prometheus_remote_write",
        )

    message = output.getvalue()
    assert "planned_schema_profile=prometheus_remote_write" in message
    assert "detected_schema_profile=prometheus_native" in message
    assert "profile_mismatch=yes" in message


def test_cli_discovery_status_shows_auto_fallback():
    resolver = SchemaResolver(
        RulePackConfig(),
        es_url="https://es.example",
        field_profile="auto",
    )
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    resolver._field_cache = {"host.name": {"keyword": {}}, "http_requests_total": {"double": {}}}
    output = io.StringIO()

    with contextlib.redirect_stdout(output):
        grafana_cli._print_schema_discovery_status(resolver, field_profile="auto")

    message = output.getvalue()
    assert "field_profile=auto" in message
    assert "auto_fallback=otel" in message
