# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""The seeder must not generate documents its own index cannot accept.

A TSDS index caps ``index.look_back_time`` at 7 days, and
``_look_back_time`` already clamps to that — with a comment citing the limit.
``_contract_lookback_hours``, which decides how far back documents are
*generated*, did not clamp. A dashboard declaring a 14-day range therefore
produced 14 days of documents into a 7-day writable window, and Elasticsearch
rejected every document older than the window:

    the document timestamp [2026-09-08T08:43:15.000Z] is outside of ranges of
    currently writable indices [[2026-09-15T08:43:15.000Z, 2026-09-22...]]

Measured on the in-repo Datadog corpus: 125,532 metric documents generated,
72,276 indexed, **53,256 rejected** — 42% — from a command that reported only
a bare error count.

The 7-day ceiling is an Elasticsearch limit, not something this project can
lift, so the honest behavior is to generate exactly what fits and *say* that
the requested window was truncated, rather than silently failing half the
writes or silently shortening history.
"""

from __future__ import annotations

import datetime

import pytest

from observability_migration.core.telemetry_data import (
    MAX_TSDS_LOOKBACK_SECONDS,
    _contract_lookback_hours,
    _look_back_time,
    lookback_truncation_warning,
)


def _contract(minimum_lookback):
    return {"streams": {"metrics-*": {"fields": {}, "minimum_lookback": minimum_lookback}}}


def test_generation_window_is_capped_at_the_tsds_limit():
    assert _contract_lookback_hours(_contract("14 days")) == MAX_TSDS_LOOKBACK_SECONDS / 3600


@pytest.mark.parametrize("requested", ["8 days", "14 days", "30 days", "4 weeks"])
def test_no_request_can_exceed_what_the_index_will_accept(requested):
    """The generation window can never be wider than the index setting."""
    contract = _contract(requested)
    generated_seconds = _contract_lookback_hours(contract) * 3600
    setting = _look_back_time(contract["streams"]["metrics-*"])
    setting_seconds = int(setting.removesuffix("d")) * 24 * 3600
    assert generated_seconds <= setting_seconds, (generated_seconds, setting)


@pytest.mark.parametrize("requested", ["1 day", "6 days", "12h", "7 days"])
def test_a_request_within_the_limit_is_untouched(requested):
    contract = _contract(requested)
    hours = _contract_lookback_hours(contract)
    assert hours <= MAX_TSDS_LOOKBACK_SECONDS / 3600
    assert hours > 0


def test_an_empty_lookback_stays_zero():
    assert _contract_lookback_hours(_contract("")) == 0.0


# --- the operator has to be told -----------------------------------------


def test_truncation_is_reported_when_the_request_exceeds_the_limit():
    warning = lookback_truncation_warning(_contract("14 days"))
    assert warning
    assert "14" in warning and "7d" in warning


def test_no_warning_when_the_request_fits():
    assert lookback_truncation_warning(_contract("3 days")) is None
    assert lookback_truncation_warning(_contract("")) is None


def test_generated_timestamps_stay_inside_the_writable_window():
    """End to end: every emitted timestamp must be indexable."""
    from observability_migration.core.telemetry_data import generate_documents

    now = datetime.datetime(2026, 9, 22, 12, 0, tzinfo=datetime.UTC)
    contract = {
        "streams": {
            "metrics-*": {
                "minimum_lookback": "14 days",
                "fields": {"system.cpu.user": {"role": "metric"}},
            }
        }
    }
    oldest_allowed = now - datetime.timedelta(seconds=MAX_TSDS_LOOKBACK_SECONDS)
    seen = 0
    for _stream, doc in generate_documents(
        contract, now=now, data_hours=1, interval_sec=3600
    ):
        stamp = datetime.datetime.fromisoformat(doc["@timestamp"].replace("Z", "+00:00"))
        assert stamp >= oldest_allowed, f"{stamp} predates the writable window"
        seen += 1
    assert seen, "expected at least one document"


# ---------------------------------------------------------------------------
# An explicit --data-hours is bounded by the same ceiling as the contract
# ---------------------------------------------------------------------------

def test_an_explicit_data_hours_cannot_exceed_the_tsds_ceiling():
    """`--data-hours 240` generated documents 10 days old against a template
    that accepts 7, so Elasticsearch rejected them outright -- the same failure
    the contract clamp fixed, reached through the flag instead."""
    import datetime

    from observability_migration.core.telemetry_data import (
        MAX_TSDS_LOOKBACK_SECONDS,
        _document_timestamps,
    )

    now = datetime.datetime.now(datetime.UTC)
    stamps = _document_timestamps(now, data_hours=240, interval_sec=3600)
    outside = [t for t in stamps if (now - t).total_seconds() > MAX_TSDS_LOOKBACK_SECONDS]
    assert not outside, f"{len(outside)} documents fall outside the writable window"


def test_a_data_hours_within_the_ceiling_is_untouched():
    import datetime

    from observability_migration.core.telemetry_data import _document_timestamps

    now = datetime.datetime.now(datetime.UTC)
    stamps = _document_timestamps(now, data_hours=2, interval_sec=60)
    span_hours = (now - min(stamps)).total_seconds() / 3600
    assert 1.9 <= span_hours <= 2.1, span_hours


def test_the_truncation_warning_reports_an_over_ceiling_data_hours():
    """Silently halving what the operator asked for reads as missing data."""
    warning = lookback_truncation_warning({}, data_hours=240)
    assert warning
    assert "10" in warning or "240" in warning


def test_the_truncation_warning_stays_quiet_for_a_normal_data_hours():
    assert lookback_truncation_warning({}, data_hours=2) is None


def test_the_widest_of_contract_and_data_hours_is_reported():
    warning = lookback_truncation_warning(_contract("14 days"), data_hours=480)
    assert warning
    assert "20" in warning or "480" in warning, warning
