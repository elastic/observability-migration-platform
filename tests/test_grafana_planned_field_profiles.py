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


def test_offline_prometheus_metrics_emits_nested_metricbeat_paths():
    """Classic Metricbeat use_types=false — Grafana twin of Datadog `prometheus`."""
    resolver = SchemaResolver(RulePackConfig(), field_profile="prometheus_metrics")
    assert resolver.resolve_label("instance") == "prometheus.labels.instance"
    assert (
        resolver.resolve_metric_field("http_requests_total")
        == "prometheus.metrics.http_requests_total"
    )


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
    assert resolver.resolve_metric_field("http_requests_total") == "http_requests_total"


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
    assert summary["operator_guidance"]["suggested_field_profile"] == "prometheus_native"


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
    assert "explicit --field-profile" in summary["operator_guidance"]["next_step"]


def _remote_write_field_caps():
    return {
        "prometheus.labels.instance": {"keyword": {"type": "keyword"}},
        "prometheus.http_requests_total.counter": {
            "long": {"type": "long", "time_series_metric": "counter"}
        },
    }


def _native_field_caps():
    return {
        "metrics.http_requests_total": {
            "double": {"type": "double", "time_series_metric": "counter"}
        },
        "labels.instance": {"keyword": {"type": "keyword"}},
    }


def _metrics_nested_field_caps():
    """Classic Metricbeat nested layout (Datadog `prometheus` twin)."""
    return {
        "prometheus.labels.instance": {"keyword": {"type": "keyword"}},
        "prometheus.metrics.http_requests_total": {
            "double": {"type": "double", "time_series_metric": "counter"}
        },
    }


def test_auto_detects_prometheus_remote_write_and_emits_namespaced():
    resolver = SchemaResolver(
        RulePackConfig(),
        es_url="https://es.example",
        field_profile="auto",
    )
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    resolver._field_cache = _remote_write_field_caps()
    summary = resolver.field_resolution_summary()
    assert summary["field_profile"] == "auto"
    assert summary["planned_schema_profile"] == "prometheus_remote_write"
    assert summary.get("auto_fallback") is None
    assert not summary.get("profile_warnings")
    assert resolver.resolve_label("instance") == "prometheus.labels.instance"
    assert resolver.resolve_metric_field("http_requests_total").startswith(
        "prometheus.http_requests_total."
    )


def test_auto_detects_prometheus_native_and_emits_metrics_prefix():
    resolver = SchemaResolver(
        RulePackConfig(),
        es_url="https://es.example",
        field_profile="auto",
    )
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    resolver._field_cache = _native_field_caps()
    summary = resolver.field_resolution_summary()
    assert summary["field_profile"] == "auto"
    assert summary["planned_schema_profile"] == "prometheus_native"
    assert summary.get("auto_fallback") is None
    assert not summary.get("profile_warnings")
    assert resolver.resolve_label("instance") == "labels.instance"
    assert resolver.resolve_metric_field("http_requests_total") == "metrics.http_requests_total"


def test_auto_detects_prometheus_metrics_and_emits_nested():
    resolver = SchemaResolver(
        RulePackConfig(),
        es_url="https://es.example",
        field_profile="auto",
    )
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    resolver._field_cache = _metrics_nested_field_caps()
    summary = resolver.field_resolution_summary()
    assert summary["field_profile"] == "auto"
    assert summary["planned_schema_profile"] == "prometheus_metrics"
    assert summary.get("auto_fallback") is None
    assert not summary.get("profile_warnings")
    assert resolver.resolve_label("instance") == "prometheus.labels.instance"
    assert (
        resolver.resolve_metric_field("http_requests_total")
        == "prometheus.metrics.http_requests_total"
    )


def test_summary_separates_mapping_from_automatic_profile_selection():
    otel = SchemaResolver(RulePackConfig(), field_profile="otel").field_resolution_summary()
    automatic = SchemaResolver(
        RulePackConfig(),
        es_url="https://es.example",
        field_profile="auto",
    )
    automatic._discovery_attempted = True
    automatic._discovery_status = "ok"
    automatic._field_cache = _native_field_caps()
    auto_summary = automatic.field_resolution_summary()
    passthrough = SchemaResolver(
        RulePackConfig(),
        field_profile="passthrough",
    ).field_resolution_summary()

    assert otel["automatic_mapping"] is True
    assert otel["automatic_profile_selection"] is False
    assert auto_summary["automatic_mapping"] is True
    assert auto_summary["automatic_profile_selection"] is True
    assert passthrough["automatic_mapping"] is False
    assert passthrough["automatic_profile_selection"] is False


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


def test_cli_accepts_prometheus_metrics():
    args = SimpleNamespace(field_profile="prometheus_metrics", es_url="")
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
    assert "suggested_field_profile=prometheus_native" in message
    assert "Next step:" in message


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


def test_auto_empty_caps_after_discovery_falls_back_to_otel_and_warns():
    """Empty/errored caps with --es-url must not silently skip auto resolution."""
    resolver = SchemaResolver(
        RulePackConfig(),
        es_url="https://es.example",
        field_profile="auto",
    )
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    resolver._field_cache = {}
    summary = resolver.field_resolution_summary()
    assert summary["field_profile"] == "auto"
    assert summary.get("auto_fallback") == "otel"
    assert summary.get("planned_schema_profile") is None
    assert any("falling back to otel" in w for w in summary.get("profile_warnings", []))
    assert resolver.resolve_label("instance") != "prometheus.labels.instance"
    assert not resolver.resolve_metric_field("http_requests_total").startswith("prometheus.")


def test_otel_plan_warns_when_live_caps_look_like_remote_write():
    """Default otel keeps emit, but surfaces a layout hint when Fleet caps are clear."""
    resolver = _planned_resolver("otel", field_cache=_remote_write_field_caps())
    summary = resolver.field_resolution_summary()
    assert summary["field_profile"] == "otel"
    assert summary["detected_schema_profile"] == "prometheus_remote_write"
    assert summary["profile_mismatch"] is False  # otel planned emit is None, not a named mismatch
    assert resolver.resolve_label("instance") != "prometheus.labels.instance"
    assert any(
        "prometheus_remote_write" in w and "otel" in w.lower()
        for w in summary.get("profile_warnings", [])
    )


def test_otel_plan_warning_is_idempotent_across_summary_calls():
    resolver = _planned_resolver("otel", field_cache=_remote_write_field_caps())

    first = resolver.field_resolution_summary()
    second = resolver.field_resolution_summary()

    assert len(first.get("profile_warnings", [])) == 1
    assert second.get("profile_warnings") == first.get("profile_warnings")


def test_otel_plan_warns_when_live_caps_look_like_prometheus_metrics():
    resolver = _planned_resolver("otel", field_cache=_metrics_nested_field_caps())
    summary = resolver.field_resolution_summary()
    assert summary["field_profile"] == "otel"
    assert summary["detected_schema_profile"] == "prometheus_metrics"
    assert resolver.resolve_metric_field("http_requests_total") != (
        "prometheus.metrics.http_requests_total"
    )
    assert any(
        "prometheus_metrics" in w and "otel" in w.lower()
        for w in summary.get("profile_warnings", [])
    )


def test_typed_fleet_detection_wins_over_nested_when_both_present():
    cache = {
        **_remote_write_field_caps(),
        **_metrics_nested_field_caps(),
    }
    assert SchemaResolver._compute_schema_profile(cache) == "prometheus_remote_write"


def test_otel_issue270_metrics_prefix_without_profile_flip():
    resolver = _planned_resolver(
        "otel",
        field_cache={
            "service.instance.id": {"keyword": {"type": "keyword"}},
            "metrics.http_requests_total": {
                "double": {"type": "double", "time_series_metric": "counter"}
            },
        },
    )
    assert resolver.resolve_metric_field("http_requests_total") == "metrics.http_requests_total"
    summary = resolver.field_resolution_summary()
    assert summary["field_profile"] == "otel"
    assert summary["planned_schema_profile"] is None
    assert summary["detected_schema_profile"] is None  # needs labels.* too for native


@pytest.mark.parametrize(
    ("profile", "label", "metric_prefix"),
    [
        ("otel", None, None),  # otel: candidate or bare; metric not prometheus./metrics.
        ("prometheus_remote_write", "prometheus.labels.instance", "prometheus.http_requests_total."),
        ("prometheus_metrics", "prometheus.labels.instance", "prometheus.metrics.http_requests_total"),
        ("prometheus_native", "labels.instance", "metrics.http_requests_total"),
        ("passthrough", "instance", "http_requests_total"),
    ],
)
def test_offline_emit_matrix_per_profile(profile, label, metric_prefix):
    resolver = SchemaResolver(RulePackConfig(), field_profile=profile)
    resolved_label = resolver.resolve_label("instance")
    resolved_metric = resolver.resolve_metric_field("http_requests_total")
    if label is None:
        assert resolved_label in {"instance", "service.instance.id", "host.name"}
        assert not resolved_metric.startswith("prometheus.")
        assert resolved_metric != "metrics.http_requests_total"
    else:
        assert resolved_label == label
        if metric_prefix.endswith("."):
            assert resolved_metric.startswith(metric_prefix)
        else:
            assert resolved_metric == metric_prefix


@pytest.mark.parametrize(
    ("profile", "caps", "expect_label", "expect_metric_prefix", "expect_detected", "expect_mismatch"),
    [
        (
            "prometheus_remote_write",
            "fleet",
            "prometheus.labels.instance",
            "prometheus.http_requests_total.",
            "prometheus_remote_write",
            False,
        ),
        (
            "prometheus_metrics",
            "nested",
            "prometheus.labels.instance",
            "prometheus.metrics.http_requests_total",
            "prometheus_metrics",
            False,
        ),
        (
            "prometheus_native",
            "native",
            "labels.instance",
            "metrics.http_requests_total",
            "prometheus_native",
            False,
        ),
        (
            "prometheus_remote_write",
            "native",
            "prometheus.labels.instance",
            "prometheus.http_requests_total.",
            "prometheus_native",
            True,
        ),
        (
            "prometheus_metrics",
            "fleet",
            "prometheus.labels.instance",
            "prometheus.metrics.http_requests_total",
            "prometheus_remote_write",
            True,
        ),
        (
            "prometheus_native",
            "fleet",
            "labels.instance",
            "metrics.http_requests_total",
            "prometheus_remote_write",
            True,
        ),
        (
            "auto",
            "fleet",
            "prometheus.labels.instance",
            "prometheus.http_requests_total.",
            "prometheus_remote_write",
            False,
        ),
        (
            "auto",
            "nested",
            "prometheus.labels.instance",
            "prometheus.metrics.http_requests_total",
            "prometheus_metrics",
            False,
        ),
        (
            "auto",
            "native",
            "labels.instance",
            "metrics.http_requests_total",
            "prometheus_native",
            False,
        ),
        (
            "passthrough",
            "fleet",
            "instance",
            "http_requests_total",
            None,
            False,
        ),
    ],
)
def test_live_emit_matrix_plan_vs_caps(
    profile,
    caps,
    expect_label,
    expect_metric_prefix,
    expect_detected,
    expect_mismatch,
):
    caches = {
        "fleet": _remote_write_field_caps(),
        "native": _native_field_caps(),
        "nested": _metrics_nested_field_caps(),
    }
    resolver = _planned_resolver(profile, field_cache=caches[caps])
    assert resolver.resolve_label("instance") == expect_label
    metric = resolver.resolve_metric_field("http_requests_total")
    if expect_metric_prefix.endswith("."):
        assert metric.startswith(expect_metric_prefix)
    else:
        assert metric == expect_metric_prefix
    summary = resolver.field_resolution_summary()
    assert summary["field_profile"] == profile
    assert summary["detected_schema_profile"] == expect_detected
    assert summary["profile_mismatch"] is expect_mismatch
    if profile == "auto":
        assert summary["planned_schema_profile"] == expect_detected
        assert summary.get("auto_fallback") is None
