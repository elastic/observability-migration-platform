# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""A non-zero seed error count has to say *why*.

``_record_bulk_result`` already captures up to three rejection reasons into
``IngestSummary.error_samples``, but nothing ever read that field: the
``seed-sample-data`` report printed only ``ingested`` / ``errors`` /
``docs_per_stream``. On a real run that meant::

    {"ingested": 98412, "errors": 53256, "docs_per_stream": {...}}

— 35% of the documents silently dropped, with no way to tell a TSDS dimension
collision from a mapping conflict from a rejected timestamp, and the command
exits 1 telling the operator only that *something* failed. The samples are the
whole diagnosis and they were being discarded.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from observability_migration.core.telemetry_data import IngestSummary


def _run_cli(summary, capsys):
    from observability_migration.app import cli

    args = SimpleNamespace(
        artifact_dir=["."], es_url="http://es", api_key="k",
        data_hours=1, interval_sec=300, batch_docs=100, max_combinations=10,
        no_recreate=False, purge_foreign_streams=False, rules_file=None,
        prometheus_url=None, quiet=True, ca_cert=None, insecure=False,
    )
    with patch.object(cli, "seed_sample_data", return_value=summary), \
            patch.object(cli, "_build_request_context", create=True, return_value=object()):
        code = cli._run_seed_sample_data(args)
    return code, json.loads(capsys.readouterr().out)


def test_error_samples_reach_the_report(capsys):
    summary = IngestSummary()
    summary.ok = 98412
    summary.errors = 53256
    summary.docs_per_stream = {"metrics-generic-default": 125532}
    summary.error_samples = [
        "[1][metrics-generic-default] TSDS documents must have a unique _tsid/@timestamp",
        "failed to parse field [system.load] of type [double]",
    ]
    code, payload = _run_cli(summary, capsys)
    assert code == 1
    assert payload["errors"] == 53256
    assert payload["error_samples"] == summary.error_samples, payload


def test_a_clean_run_reports_no_error_samples_key_noise(capsys):
    summary = IngestSummary()
    summary.ok = 1000
    summary.docs_per_stream = {"metrics-generic-default": 1000}
    code, payload = _run_cli(summary, capsys)
    assert code == 0
    assert payload["errors"] == 0
    assert payload.get("error_samples", []) == []
