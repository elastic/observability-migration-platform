# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""A monitor tiered ``manual_required`` has to say why.

The dashboard path prints per-panel reasons, a "Not feasible / blocked panels"
list and a DATA READINESS section. The alerts path prints only::

    Total: 60
    By tier: {'automated': 20, 'manual_required': 30, 'draft_requires_review': 10}

Thirty monitors need hand-rebuilding and the run names none of them. Worse, 21
of those 30 carry no ``warnings`` at all in
``monitor_migration_results.json`` -- only generic ``losses`` about threshold
semantics, which are attached to translated monitors too and so do not explain
the tiering. The most common case is Datadog anomaly detection, which has no
ES|QL equivalent; nothing in the run says so.

Measured on 60 real ``DataDog/integrations-core`` monitors.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from observability_migration.adapters.source.datadog.report import (
    derive_manual_reason,
    summarize_manual_reasons,
)


def _ir(kind="datadog_metric", warnings=None, translated=""):
    return SimpleNamespace(
        kind=kind, warnings=list(warnings or []), translated_query=translated,
        name="a monitor",
    )


def test_an_anomaly_monitor_says_anomaly_detection_is_untranslatable():
    reason = derive_manual_reason(_ir(kind="datadog_anomaly_alert"))
    assert reason
    assert "anomaly" in reason.lower()


def test_a_monitor_with_no_translation_says_so():
    reason = derive_manual_reason(_ir(kind="datadog_metric"))
    assert reason
    assert "no" in reason.lower() and "translat" in reason.lower()


def test_an_existing_warning_is_preferred_over_a_derived_one():
    reason = derive_manual_reason(_ir(warnings=["formula monitor requires manual review"]))
    assert reason == "formula monitor requires manual review"


def test_a_translated_monitor_needs_no_reason():
    assert derive_manual_reason(_ir(translated="FROM metrics-* | STATS v = AVG(x)")) is None


@pytest.mark.parametrize("kind", ["datadog_metric", "datadog_log", "datadog_anomaly_alert"])
def test_every_kind_yields_some_reason(kind):
    assert derive_manual_reason(_ir(kind=kind))


# --- the console has to group and print them -----------------------------


def test_reasons_are_grouped_with_counts():
    irs = [
        _ir(kind="datadog_anomaly_alert"),
        _ir(kind="datadog_anomaly_alert"),
        _ir(warnings=["formula monitor requires manual review"]),
        _ir(translated="FROM metrics-*"),  # translated -> not counted
    ]
    grouped = summarize_manual_reasons(irs)
    assert sum(grouped.values()) == 3
    assert max(grouped.values()) == 2


def test_nothing_to_report_when_everything_translated():
    assert summarize_manual_reasons([_ir(translated="FROM metrics-*")]) == {}


def test_the_alerts_pipeline_prints_the_reasons(capsys):
    from observability_migration.adapters.source.datadog.report import (
        print_manual_monitor_reasons,
    )

    print_manual_monitor_reasons([
        _ir(kind="datadog_anomaly_alert"),
        _ir(kind="datadog_anomaly_alert"),
        _ir(warnings=["formula monitor requires manual review"]),
    ])
    out = capsys.readouterr().out
    assert "MONITORS NEEDING MANUAL WORK" in out
    assert "anomaly" in out.lower()
    assert "2" in out


def test_printing_is_silent_when_there_is_nothing_to_say(capsys):
    from observability_migration.adapters.source.datadog.report import (
        print_manual_monitor_reasons,
    )

    print_manual_monitor_reasons([_ir(translated="FROM metrics-*")])
    assert capsys.readouterr().out == ""


# --- every manual-only kind must name its own reason ---------------------


def test_every_manual_only_kind_has_a_specific_reason():
    """Guard against drift between the kind set and the reasons.

    ``core.mapping.MANUAL_ONLY_KINDS`` is the single source of truth for
    monitor kinds that can only ever be manual. Each one has a different
    cause -- a composite monitor references other monitors, a service check
    watches check status, an SLO alert wants an Elastic SLO -- so falling back
    to "no ES|QL translation was produced" for any of them wastes the one
    chance the run has to tell the operator what to build instead.
    """
    from observability_migration.adapters.source.datadog.report import (
        _GENERIC_MANUAL_REASON,
    )
    from observability_migration.core.mapping import MANUAL_ONLY_KINDS

    missing = []
    for kind in sorted(MANUAL_ONLY_KINDS):
        if not kind.startswith("datadog_"):
            continue
        reason = derive_manual_reason(_ir(kind=kind))
        if not reason or reason == _GENERIC_MANUAL_REASON:
            missing.append(kind)
    assert not missing, f"no specific reason for: {missing}"


@pytest.mark.parametrize(
    "kind,expect",
    [
        ("datadog_composite", "composite"),
        ("datadog_service_check", "check"),
        ("datadog_slo_alert", "slo"),
        ("datadog_synthetics_alert", "synthetic"),
        ("datadog_forecast", "forecast"),
        ("datadog_outlier", "outlier"),
        ("datadog_watchdog_alert", "watchdog"),
    ],
)
def test_the_reason_names_the_construct(kind, expect):
    assert expect in derive_manual_reason(_ir(kind=kind)).lower()


def test_an_unknown_kind_still_falls_back_rather_than_crashing():
    from observability_migration.adapters.source.datadog.report import (
        _GENERIC_MANUAL_REASON,
    )

    assert derive_manual_reason(_ir(kind="datadog_something_new")) == _GENERIC_MANUAL_REASON
