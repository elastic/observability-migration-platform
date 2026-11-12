# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Read-only runtime counterexamples for the parity oracle's reference buckets."""

import os

import pytest

from observability_migration.core.sample_data import make_es_request
from observability_migration.core.verification.parity_oracle import _bucket_native_reference

pytestmark = pytest.mark.skipif(not os.getenv("ES_URL"), reason="live ES_URL required")


def test_reference_bucket_retains_latest_point_per_instance():
    request = make_es_request(os.environ["ES_URL"], os.getenv("ES_API_KEY", ""))
    query = (
        "ROW n=[0,1,2,3,4,5,6,7] | MV_EXPAND n "
        '| EVAL step=TO_DATETIME(1767225615000 + n * 15000), value=TO_DOUBLE(n), instance=CASE(n % 2 == 0, "a", "b") '
        "| KEEP step,value,instance"
    )
    raw = request("POST", "/_query", {"query": query})
    assert not raw.get("error"), raw
    result = _bucket_native_reference(
        request, query, raw, "BUCKET(step, 1 minute)",
        "2026-01-01T00:00:00Z", "2026-01-01T00:03:00Z",
    )
    assert not result.get("error"), result
    names = [column["name"] for column in result["columns"]]
    rows = [dict(zip(names, row, strict=True)) for row in result["values"]]
    assert {(row["step"], row["instance"]): row["value"] for row in rows} == {
        ("2026-01-01T00:00:00.000Z", "a"): 2,
        ("2026-01-01T00:00:00.000Z", "b"): 1,
        ("2026-01-01T00:01:00.000Z", "a"): 6,
        ("2026-01-01T00:01:00.000Z", "b"): 5,
        ("2026-01-01T00:02:00.000Z", "b"): 7,
    }
