# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

""""No dashboards found" has to give advice for the mode actually in use.

Both empty-extraction branches printed the same files-mode sentence::

    ERROR: no Datadog dashboards found under .. Point --input-dir at a
    directory of Datadog dashboard JSON exports (each with a top-level
    'widgets' key).

Running against the live Datadog API with a mistyped ``--dashboard-ids`` shows
that verbatim -- interpolating an unset ``--input-dir`` as ``..`` and telling
the operator to fix a flag they never passed. The actionable advice there is
about the id, the selectors, or the API credentials.

Reproduced against a real Datadog org:
``migrate --source datadog --input-mode api --dashboard-ids does-not-exist``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from observability_migration.adapters.source.datadog.cli import (
    empty_extraction_message,
)


def _args(**kw):
    base = dict(input_mode="files", input_dir="/some/dir", dashboard_ids="", monitor_ids="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_files_mode_points_at_the_input_dir():
    msg = empty_extraction_message(_args(input_mode="files", input_dir="/some/dir"))
    assert "--input-dir" in msg
    assert "/some/dir" in msg


def test_api_mode_does_not_mention_input_dir():
    msg = empty_extraction_message(_args(input_mode="api", input_dir=None))
    assert "--input-dir" not in msg, msg
    assert ".." not in msg, msg


def test_api_mode_names_the_ids_that_matched_nothing():
    msg = empty_extraction_message(_args(input_mode="api", input_dir=None,
                                         dashboard_ids="does-not-exist"))
    assert "does-not-exist" in msg


def test_api_mode_without_ids_points_at_credentials_or_selectors():
    msg = empty_extraction_message(_args(input_mode="api", input_dir=None))
    lowered = msg.lower()
    assert "credential" in lowered or "--select" in lowered or "api" in lowered


@pytest.mark.parametrize("mode", ["files", "api"])
def test_the_message_always_says_what_was_missing(mode):
    msg = empty_extraction_message(_args(input_mode=mode, input_dir="/d"))
    assert "no Datadog dashboards" in msg
