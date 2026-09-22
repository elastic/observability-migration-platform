# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""A Datadog run that can prove a metric is absent must say so.

``assess_field_usage`` has a ``capability is None`` branch whose whole job is to
warn "field '<name>' not found in target mapping" -- but the Datadog metric
path called it inside ``if metric_cap:``, so that branch was unreachable.
Group-by fields were worse: ``if not group_cap: continue``.

The consequence, reproduced against a live cluster holding OTLP-native dotted
fields (``shop.orders.active``) while the default ``otel`` profile flattens to
``shop_orders_active``::

    OK: 2  Warning: 0  Manual: 0  NF: 0
    Verification: Green=2  Yellow=0  Red=0
    Success rate: 100.0%
    Review queue:  Shop — Orders: risk=0 (G:2 Y:0 R:0)

...while ``target_readiness_contract.json`` from the same run recorded
``shop_orders_active -> status: missing``. The run knew, scored itself green,
and the operator met "Unknown column [shop_orders_active]" in Kibana instead.

The Grafana adapter already emits a panel reason containing
"missing from live schema discovery", which is what the shared reporter
(``core/reporting/report.py``) keys its DATA READINESS section on. Datadog
never emitted it, so that section could not fire for Datadog at all.

Absence is only provable when live caps were actually loaded: an offline run
(no ``--es-url``) or a cluster where discovery returned nothing must stay
silent rather than warn about every field.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from observability_migration.adapters.source.datadog.field_map import BUILTIN_PROFILES
from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.translate import translate_widget
from observability_migration.core.verification.field_capabilities import FieldCapability

READINESS_PHRASE = "missing from live schema discovery"


def _widget(query, title="Active orders", wtype="timeseries"):
    raw = {
        "title": "Shop",
        "widgets": [
            {
                "id": 1,
                "definition": {
                    "type": wtype,
                    "title": title,
                    "requests": [
                        {
                            "response_format": "timeseries",
                            "queries": [
                                {"data_source": "metrics", "name": "query1", "query": query}
                            ],
                            "formulas": [{"formula": "query1"}],
                        }
                    ],
                },
            }
        ],
    }
    return normalize_dashboard(raw).widgets[0]


def _profile(name="otel", caps=None):
    profile = deepcopy(BUILTIN_PROFILES[name])
    profile.metric_field_caps = {
        field: FieldCapability(name=field, type=field_type)
        for field, field_type in (caps or {}).items()
    }
    return profile


def _translate(query, profile):
    widget = _widget(query)
    return translate_widget(widget, plan_widget(widget), profile)


# The live target holds OTLP-native dotted names; `otel` flattens to underscores.
OTLP_TARGET = {
    "shop.orders.active": "double",
    "service.name": "keyword",
    "cloud.region": "keyword",
}


def test_absent_metric_field_is_warned_when_caps_prove_it():
    result = _translate("avg:shop.orders.active{*}", _profile("otel", OTLP_TARGET))
    assert any("shop_orders_active" in w for w in result.warnings), result.warnings


def test_absent_metric_field_emits_the_shared_readiness_reason():
    """This is what makes the DATA READINESS section fire in the reporter."""
    result = _translate("avg:shop.orders.active{*}", _profile("otel", OTLP_TARGET))
    assert any(READINESS_PHRASE in r for r in result.reasons), result.reasons
    assert any("shop_orders_active" in r for r in result.reasons), result.reasons


def test_absent_group_by_field_is_reported_too():
    result = _translate(
        "avg:shop.orders.active{*} by {nonexistent_tag}", _profile("otel", OTLP_TARGET)
    )
    joined = " ".join(result.warnings + result.reasons)
    assert "nonexistent_tag" in joined, joined


def test_a_present_field_produces_no_readiness_noise():
    """passthrough resolves the dotted name, so nothing is missing."""
    result = _translate("avg:shop.orders.active{*}", _profile("passthrough", OTLP_TARGET))
    assert not any(READINESS_PHRASE in r for r in result.reasons), result.reasons
    assert not any("not found in target mapping" in w for w in result.warnings), result.warnings


# --- absence must be provable, not assumed -------------------------------


def test_offline_run_stays_silent():
    """No --es-url means no caps: absence is unknowable, so do not warn."""
    result = _translate("avg:shop.orders.active{*}", _profile("otel", None))
    assert not any(READINESS_PHRASE in r for r in result.reasons), result.reasons
    assert not any("not found in target mapping" in w for w in result.warnings), result.warnings


def test_discovery_that_returned_nothing_stays_silent():
    """An index that does not exist yields empty caps -- also unknowable."""
    profile = _profile("otel", None)
    profile.metric_field_caps = {}
    profile.field_caps = {}
    result = _translate("avg:shop.orders.active{*}", profile)
    assert not any(READINESS_PHRASE in r for r in result.reasons), result.reasons


@pytest.mark.parametrize("profile_name", ["default", "otel", "elastic_agent"])
def test_every_flattening_profile_reports_the_gap(profile_name):
    result = _translate("avg:shop.orders.active{*}", _profile(profile_name, OTLP_TARGET))
    assert any(READINESS_PHRASE in r for r in result.reasons), (profile_name, result.reasons)


# --- both source reporters must print it ---------------------------------


def _fake_results(reason):
    from types import SimpleNamespace

    panel = SimpleNamespace(title="Active orders", reasons=[reason], warnings=[])
    return [SimpleNamespace(dashboard_title="Shop — Orders", panel_results=[panel])]


READINESS_REASON = (
    "Target field shop_orders_active is missing from live schema discovery "
    "(data readiness, not translation infeasibility)"
)


def test_the_shared_collector_extracts_the_field_name():
    from observability_migration.core.reporting.report import collect_data_readiness_gaps

    gaps = collect_data_readiness_gaps(_fake_results(READINESS_REASON))
    assert gaps == [("Shop — Orders", "Active orders", "shop_orders_active")], gaps


def test_the_shared_printer_names_the_field(capsys):
    from observability_migration.core.reporting.report import print_data_readiness

    print_data_readiness(_fake_results(READINESS_REASON))
    out = capsys.readouterr().out
    assert "DATA READINESS" in out
    assert "shop_orders_active" in out
    assert "absent from the target" in out


def test_the_shared_printer_stays_quiet_with_no_gaps(capsys):
    from observability_migration.core.reporting.report import print_data_readiness

    print_data_readiness(_fake_results("timeseries → esql XY panel"))
    assert capsys.readouterr().out == ""


def test_both_source_reporters_use_the_shared_printer():
    """The regression that let these two drift apart.

    Datadog and Grafana each have their own ``print_report``; the readiness
    section lived inline in Grafana's, so Datadog runs never printed it even
    though the same data was already in the readiness contract.
    """
    import inspect

    from observability_migration.adapters.source.datadog import report as dd_report
    from observability_migration.core.reporting import report as core_report

    assert "print_data_readiness" in inspect.getsource(dd_report.print_report)
    assert "print_data_readiness" in inspect.getsource(core_report.print_report)


# --- filter tags need the same treatment as metrics and group-bys --------


def test_absent_filter_tag_is_reported():
    """The passthrough-against-ECS failure mode.

    ``{service:shop-lab}`` under `passthrough` resolves to a bare ``service``
    that an OTel target does not have (it has ``service.name``). The emitted
    ``service == "shop-lab"`` then fails with "Unknown column [service]" while
    the run reported nothing: metrics and group-by fields were assessed, scope
    filters were not.
    """
    profile = _profile("passthrough", {"shop.orders.active": "double"})
    result = _translate("avg:shop.orders.active{service:shop-lab}", profile)
    assert any("service" in w for w in result.warnings), result.warnings
    assert any(READINESS_PHRASE in r and "service" in r for r in result.reasons), result.reasons


def test_absent_filter_tag_inside_a_boolean_group_is_reported():
    profile = _profile("passthrough", {"shop.orders.active": "double"})
    result = _translate(
        "avg:shop.orders.active{(service:a OR service:b)}", profile
    )
    assert any(READINESS_PHRASE in r and "service" in r for r in result.reasons), result.reasons


def test_a_present_filter_tag_is_not_reported():
    profile = _profile(
        "passthrough", {"shop.orders.active": "double", "service": "keyword"}
    )
    result = _translate("avg:shop.orders.active{service:shop-lab}", profile)
    assert not any(READINESS_PHRASE in r for r in result.reasons), result.reasons


def test_a_wildcard_scope_is_not_a_missing_field():
    """``{*}`` emits no clause, so there is nothing to be absent."""
    profile = _profile("passthrough", {"shop.orders.active": "double"})
    result = _translate("avg:shop.orders.active{*}", profile)
    assert not any(READINESS_PHRASE in r for r in result.reasons), result.reasons


def test_a_template_variable_filter_is_not_reported_as_missing():
    """``$var`` resolves at view time; its field cannot be judged now."""
    profile = _profile("passthrough", {"shop.orders.active": "double"})
    result = _translate("avg:shop.orders.active{service:$svc}", profile)
    assert not any(
        READINESS_PHRASE in r and "service" in r for r in result.reasons
    ), result.reasons


def test_offline_filter_tag_stays_silent():
    profile = _profile("passthrough", None)
    result = _translate("avg:shop.orders.active{service:shop-lab}", profile)
    assert not any(READINESS_PHRASE in r for r in result.reasons), result.reasons
