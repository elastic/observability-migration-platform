# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""A source dashboard with no panels is nothing to upload, not a failed upload.

Real Datadog orgs carry scratch dashboards with zero widgets -- three of the six
on the live account used to develop this. Migrating them produced
``UPLOAD FAILED``/``UPLOAD ERROR`` on the console and ``upload.status: "fail"``
in ``migration_report.json``, which sends an operator hunting for a breakage
that is not there and fails any CI gate keyed on the manifest.

The zero-leaf check those runs tripped exists for a real hazard -- a payload of
empty sections whose source panels were silently dropped -- so the two cases
have to stay distinguishable rather than the check being loosened.
"""
from __future__ import annotations

from observability_migration.targets.kibana.dashboards_api import (
    _upload_native_api_payload,
)


def _upload(payload, **kwargs):
    """Call the shared upload path. It returns before any HTTP when the payload
    has no leaf panels, so no Kibana is needed for these cases."""
    return _upload_native_api_payload(
        payload, title="D", kibana_url="http://kibana.invalid", **kwargs
    )


def test_wholly_empty_payload_reports_an_empty_source_not_a_failure():
    res = _upload({"title": "D", "panels": []}, mapped=0, unmapped=0)
    assert res.status == "source_empty"
    assert res.message


def test_missing_panels_key_is_treated_the_same_as_an_empty_one():
    res = _upload({"title": "D"}, mapped=0, unmapped=0)
    assert res.status == "source_empty"


def test_sections_with_no_leaves_still_fail_as_empty():
    """The silent-drop hazard: items exist, so panels went missing building them."""
    res = _upload(
        {"title": "D", "panels": [{"title": "Section A", "panels": []}]},
        mapped=0,
        unmapped=0,
    )
    assert res.status == "empty"


def test_a_payload_that_lost_panels_still_fails_even_with_nothing_left():
    """``unmapped`` records panels that did not survive, so this is not an
    empty source however bare the payload ends up."""
    res = _upload({"title": "D", "panels": []}, mapped=0, unmapped=4)
    assert res.status == "empty"


def test_mapped_panels_that_vanished_from_the_payload_still_fail():
    res = _upload({"title": "D", "panels": []}, mapped=7, unmapped=0)
    assert res.status == "empty"


def test_controls_without_panels_still_fail():
    """Controls filter nothing without panels, so this stays degenerate."""
    res = _upload(
        {"title": "D", "panels": [], "pinned_panels": [{"type": "esqlControl"}]},
        mapped=0,
        unmapped=0,
    )
    assert res.status == "empty"


def test_neither_outcome_counts_as_a_successful_upload():
    """``source_empty`` must not be mistaken for a dashboard that was written."""
    assert _upload({"title": "D", "panels": []}, mapped=0, unmapped=0).status not in {
        "created",
        "updated",
    }


def test_the_adapter_forwards_the_empty_source_signal_to_its_caller(monkeypatch):
    """``upload_dashboard`` rebuilds its own result dict rather than passing the
    inner record through, so the signal has to be carried across explicitly.
    Dropping it there is invisible to the checks above and turns the skip back
    into a reported failure."""
    from observability_migration.targets.kibana.adapter import KibanaTargetAdapter

    adapter = KibanaTargetAdapter()
    record = {
        "success": False,
        "nothing_to_upload": True,
        "output": "D: source_empty",
        "space_id": "default",
        "kibana_url": "http://kibana.invalid",
        "status": "source_empty",
        "mapped": 0,
        "unmapped": 0,
        "unmapped_reasons": {},
        "dashboard_ids": [],
    }
    monkeypatch.setattr(
        KibanaTargetAdapter, "_native_upload_file", lambda self, *a, **k: record
    )
    monkeypatch.setattr(
        KibanaTargetAdapter,
        "_ensure_data_views_for_upload",
        lambda self, *a, **k: ({}, []),
    )
    monkeypatch.setattr(
        "observability_migration.targets.kibana.adapter._referenced_data_view_patterns",
        lambda *a, **k: [],
    )

    result = adapter.upload_dashboard(
        kibana_url="http://kibana.invalid", native_dashboard=object()
    )
    assert result["nothing_to_upload"] is True
    assert result["success"] is False
