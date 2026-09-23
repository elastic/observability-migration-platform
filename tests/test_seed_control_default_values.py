# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Seeded data must contain the values a dashboard control pre-selects.

A Datadog template variable with a default (``{'name': 'env', 'default':
'prod'}``) migrates faithfully into a Kibana options-list control with
``selected_options: ['prod']``. The translation is correct. But the telemetry
contract is built from *query text* -- ``_extract_required_filters`` picks up a
literal like ``host == "web01"`` -- and a control's pre-selection never appears
in any query, so the seeder never learns about ``prod``.

It then invents generic values (``production``/``staging``/``development``),
the control filters on ``prod``, and **every panel on the dashboard renders
"No results found" on first open**. Observed in a real browser against the
in-repo ``sample_dashboard.json``: 7 of 9 query panels blank, and
``env == "prod"`` matched 0 of 34,946 seeded documents.

``_require_control_fields`` was written for exactly this symptom -- its
docstring says "the seeded documents then match no control selection" -- but it
only asserts that the control *field* reached the contract. ``env`` does reach
it, so the guard passes while the dashboard is still blank. The field is not
enough; the pre-selected *value* has to be seeded too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from observability_migration.core.telemetry_contract import control_selection_values


def _write_ir(tmp_path: Path, controls: list[dict]) -> Path:
    ir = tmp_path / "ir"
    ir.mkdir(parents=True, exist_ok=True)
    (ir / "dash.ir.json").write_text(
        json.dumps({"kind": "dashboard", "dashboard_ir": {"controls": controls}})
    )
    return tmp_path


def test_a_pre_selected_control_value_is_discovered(tmp_path):
    d = _write_ir(tmp_path, [
        {"field_name": "env", "data_view": "metrics-*", "selected_options": ["prod"],
         "available_options": ["prod", "staging", "dev"]},
    ])
    assert control_selection_values(d) == {"metrics-*": {"env": ["prod", "staging", "dev"]}}


def test_selected_value_comes_first_so_it_is_never_dropped(tmp_path):
    """Cardinality caps truncate; the pre-selection must survive truncation."""
    d = _write_ir(tmp_path, [
        {"field_name": "env", "data_view": "metrics-*", "selected_options": ["prod"],
         "available_options": ["a", "b", "c", "prod"]},
    ])
    assert control_selection_values(d)["metrics-*"]["env"][0] == "prod"


def test_a_control_with_no_selection_still_offers_its_options(tmp_path):
    d = _write_ir(tmp_path, [
        {"field_name": "host", "data_view": "metrics-*", "selected_options": [],
         "available_options": ["web01", "web02"]},
    ])
    assert control_selection_values(d) == {"metrics-*": {"host": ["web01", "web02"]}}


def test_a_control_with_nothing_at_all_is_skipped(tmp_path):
    d = _write_ir(tmp_path, [
        {"field_name": "host", "data_view": "metrics-*", "selected_options": [],
         "available_options": []},
    ])
    assert control_selection_values(d) == {}


def test_controls_are_grouped_by_their_data_view(tmp_path):
    d = _write_ir(tmp_path, [
        {"field_name": "env", "data_view": "metrics-*", "selected_options": ["prod"]},
        {"field_name": "service", "data_view": "logs-*", "selected_options": ["web"]},
    ])
    got = control_selection_values(d)
    assert got["metrics-*"] == {"env": ["prod"]}
    assert got["logs-*"] == {"service": ["web"]}


def test_no_ir_directory_is_not_an_error(tmp_path):
    assert control_selection_values(tmp_path) == {}


@pytest.mark.parametrize("bad", [{"field_name": "", "selected_options": ["x"]}, {"selected_options": ["x"]}])
def test_a_control_without_a_field_is_skipped(tmp_path, bad):
    assert control_selection_values(_write_ir(tmp_path, [bad])) == {}


# --- the values must reach the contract the seeder reads -----------------


def test_control_values_reach_every_stream_that_has_the_field(tmp_path):
    """A control filters every panel, including ones on another data view.

    The ``env`` control binds ``metrics-*``, but Kibana applies it to the
    ``logs-*`` ES|QL panels too. Merging its values only into the stream named
    by the control's own ``data_view`` left ``logs-*`` with generic invented
    values, so ``env == "prod"`` matched 0 of 22,836 log documents and both
    log panels rendered "No results found".
    """
    from observability_migration.core.telemetry_contract import (
        merge_control_selection_values,
    )

    contract = {
        "streams": {
            "metrics-*": {"fields": {}, "control_fields": ["env"], "required_values": {}},
            "logs-*": {"fields": {}, "control_fields": ["env"], "required_values": {}},
        }
    }
    merge_control_selection_values(contract, {"metrics-*": {"env": ["prod"]}})
    assert contract["streams"]["metrics-*"]["required_values"]["env"] == ["prod"]
    assert contract["streams"]["logs-*"]["required_values"]["env"] == ["prod"], (
        "the control also filters the logs panels"
    )


def test_a_stream_without_the_control_field_is_untouched():
    from observability_migration.core.telemetry_contract import (
        merge_control_selection_values,
    )

    contract = {
        "streams": {
            "metrics-*": {"fields": {}, "control_fields": ["env"], "required_values": {}},
            "traces-*": {"fields": {}, "control_fields": [], "required_values": {}},
        }
    }
    merge_control_selection_values(contract, {"metrics-*": {"env": ["prod"]}})
    assert contract["streams"]["traces-*"]["required_values"] == {}


def test_control_values_are_merged_into_required_values(tmp_path):
    from observability_migration.core.telemetry_contract import (
        merge_control_selection_values,
    )

    contract = {
        "streams": {
            "metrics-*": {
                "fields": {"m": {"role": "metric"}, "env": {"role": "dimension"}},
                "control_fields": ["env"],
                "required_values": {},
            }
        }
    }
    merge_control_selection_values(contract, {"metrics-*": {"env": ["prod", "staging"]}})
    assert contract["streams"]["metrics-*"]["required_values"]["env"][0] == "prod"


def test_merging_does_not_clobber_values_already_required_by_a_query(tmp_path):
    from observability_migration.core.telemetry_contract import (
        merge_control_selection_values,
    )

    contract = {
        "streams": {
            "metrics-*": {
                "fields": {"host": {"role": "dimension"}},
                "control_fields": ["host"],
                "required_values": {"host": ["web01"]},
            }
        }
    }
    merge_control_selection_values(contract, {"metrics-*": {"host": ["web02"]}})
    got = contract["streams"]["metrics-*"]["required_values"]["host"]
    assert "web01" in got and "web02" in got


# --- a dashboard control filters every panel, so every doc needs the field ---


def test_control_fields_travel_with_every_metric():
    """A dashboard-level control applies to all panels, not just one query.

    ``_metric_families`` unions dimensions **per requirement**. A dashboard
    control is not a query dimension, so when some *other* panel's query
    mentions it the field is known to the stream, yet the family holding an
    unrelated metric excludes it. The seeded ``system.cpu.user`` documents
    then carry ``env = null``, the control filters ``env == "prod"``, and the
    charts draw axes with ``(null)`` values.

    Measured on the in-repo Datadog corpus: 90 of 1077 requirements mention
    ``env``, none of the 20 for ``system.cpu.user`` do, and 0 documents ended
    up carrying both.
    """
    from observability_migration.core.telemetry_data import _metric_families

    stream = {
        "fields": {
            "a.metric": {"role": "metric"},
            "b.metric": {"role": "metric"},
            "env": {"role": "dimension"},
            "host": {"role": "dimension"},
        },
        "control_fields": ["env"],
        "required_values": {"env": ["prod"], "host": ["web01"]},
        "requirements": [
            # a.metric's own query never mentions env ...
            {"metrics": ["a.metric"], "dimensions": ["host", "timestamp"]},
            # ... but another panel's does, which is what excluded it.
            {"metrics": ["b.metric"], "dimensions": ["host", "timestamp", "env"]},
        ],
    }
    metric_fields = {k: v for k, v in stream["fields"].items() if v.get("role") == "metric"}
    families = _metric_families(stream, metric_fields, max_combinations=12)
    for metrics, combos, _le in families:
        if "a.metric" not in metrics:
            continue
        dims = {key for combo in combos for key in combo}
        assert "env" in dims, f"control field missing from the family: {sorted(dims)}"
        return
    raise AssertionError("a.metric appeared in no family")


def test_a_non_control_dimension_is_still_scoped_per_query():
    """The co-occurrence scoping must not collapse into 'everything everywhere'."""
    from observability_migration.core.telemetry_data import generate_documents

    contract = {
        "streams": {
            "metrics-*": {
                "fields": {
                    "a.metric": {"role": "metric"},
                    "b.metric": {"role": "metric"},
                    "only_for_b": {"role": "dimension"},
                },
                "control_fields": [],
                "required_values": {"only_for_b": ["x"]},
                "requirements": [
                    {"metrics": ["a.metric"], "dimensions": []},
                    {"metrics": ["b.metric"], "dimensions": ["only_for_b"]},
                ],
            }
        }
    }
    leaked = 0
    for _stream, doc in generate_documents(contract, data_hours=1, interval_sec=3600):
        if doc.get("a.metric") is not None and doc.get("only_for_b") is not None:
            leaked += 1
    assert leaked == 0, "a query-scoped dimension must not attach to an unrelated metric"
