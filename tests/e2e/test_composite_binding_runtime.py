# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Read-only ES|QL counterexamples for physical binding instrumentation."""

import os

import pytest

from observability_migration.adapters.source.grafana.smoke import validate_esql

pytestmark = pytest.mark.skipif(not os.getenv("ES_URL"), reason="live ES_URL required")


@pytest.mark.parametrize("query, missing", [
    ('ROW instance=TO_STRING(null), x=1 | STATS used=AVG(x) BY instance | /* label | note */ EVAL legend=CONCAT(COALESCE(/* label */ TO_STRING(instance), ""), " - used") | KEEP legend,used', ["instance"]),
    ('ROW instance="known", x=1 | STATS used=AVG(x) BY instance | EVAL instance=TO_STRING(null), legend=CONCAT(COALESCE(TO_STRING(instance), ""), " - used") | KEEP legend,used', ["instance"]),
    ('ROW instance=TO_STRING(null), x=1 | STATS used=AVG(x) BY instance | EVAL instance="known", legend=CONCAT(COALESCE(TO_STRING(instance), ""), " - used") | KEEP legend,used', []),
    ('ROW instance=["known", ""], x=1 | MV_EXPAND instance | EVAL instance=CASE(instance=="", TO_STRING(null), instance) | STATS used=AVG(x) BY instance | EVAL legend=CONCAT(COALESCE(TO_STRING(instance), ""), " - used") | WHERE legend==" - used" | KEEP legend,used', ["instance"]),
    ('ROW instance="known", db=TO_STRING(null), x=1 | STATS used=AVG(x) BY instance,db | EVAL i=COALESCE(TO_STRING(instance), "") | EVAL d=COALESCE(TO_STRING(db), "") | EVAL legend=CONCAT(i,"/",d) | KEEP legend,used', ["db"]),
])
def test_physical_binding_matches_the_final_rendered_selection(query, missing):
    result = validate_esql(os.environ["ES_URL"], query, 30, es_api_key=os.getenv("ES_API_KEY", ""))
    assert result["status"] == "pass", result
    assert not result.get("binding_probe_error"), result
    assert result["null_columns"] == missing
