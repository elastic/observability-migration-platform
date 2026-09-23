# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""A realistic corpus needs more mapping fields than Elasticsearch allows by default.

``index.mapping.total_fields.limit`` defaults to 1000. Seeding the 47
``DataDog/integrations-core`` dashboards produces a metrics stream with 1043
mapping properties, so ``seed-sample-data`` failed outright:

    Failed to create index template telemetry-data-metrics-generic-default:
    composable template [...] template after composition is invalid

Two problems there. The template did not raise its own field limit even though
it knows exactly how many properties it is about to declare; and the message
stopped at Elasticsearch's outermost wrapper, dropping the ``caused_by`` chain
whose leaf actually says ``Limit of total fields [1000] has been exceeded``.
Without that leaf the operator cannot tell a field-count ceiling from a type
conflict, and every panel is left unseeded.
"""

from __future__ import annotations

import pytest

from observability_migration.core.telemetry_data import plan_index_template


def _stream(n_metrics: int):
    return {"fields": {f"m{i}.value": {"role": "metric"} for i in range(n_metrics)}}


def _settings(stream, pattern="metrics-*"):
    return plan_index_template(pattern, stream)["template"]["settings"]["index"]


def test_a_small_stream_still_gets_a_limit_at_least_the_default():
    limit = int(_settings(_stream(10))["mapping.total_fields.limit"])
    assert limit >= 1000


def test_the_limit_scales_past_the_property_count():
    stream = _stream(1200)
    props = plan_index_template("metrics-*", stream)["template"]["mappings"]["properties"]
    limit = int(_settings(stream)["mapping.total_fields.limit"])
    assert limit > len(props), f"limit {limit} does not clear {len(props)} properties"


@pytest.mark.parametrize("n", [500, 1000, 1500, 3000])
def test_the_limit_always_clears_the_declared_properties(n):
    stream = _stream(n)
    props = plan_index_template("metrics-*", stream)["template"]["mappings"]["properties"]
    limit = int(_settings(stream)["mapping.total_fields.limit"])
    assert limit > len(props)


def test_logs_streams_get_the_limit_too():
    assert "mapping.total_fields.limit" in _settings(_stream(1200), pattern="logs-*")


# --- the error has to name the real cause --------------------------------


def test_template_failure_surfaces_the_root_cause_chain():
    """``template after composition is invalid`` alone is not actionable."""
    from observability_migration.core.telemetry_data import _raise_on_error

    result = {
        "error": {
            "type": "illegal_argument_exception",
            "reason": "composable template [x] template after composition is invalid",
            "caused_by": {
                "type": "illegal_argument_exception",
                "reason": "invalid composite mappings for [x]",
                "caused_by": {
                    "type": "illegal_argument_exception",
                    "reason": "Limit of total fields [1000] has been exceeded",
                },
            },
        }
    }
    with pytest.raises(RuntimeError) as excinfo:
        _raise_on_error(result, "create index template x")
    message = str(excinfo.value)
    assert "Limit of total fields [1000] has been exceeded" in message, message


def test_a_plain_error_without_a_chain_still_reads_cleanly():
    from observability_migration.core.telemetry_data import _raise_on_error

    with pytest.raises(RuntimeError) as excinfo:
        _raise_on_error({"error": {"reason": "boom"}}, "do a thing")
    assert "boom" in str(excinfo.value)


def test_no_error_does_not_raise():
    from observability_migration.core.telemetry_data import _raise_on_error

    _raise_on_error({"acknowledged": True}, "do a thing")
