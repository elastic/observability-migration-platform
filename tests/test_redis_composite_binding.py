# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Exercise smoke against the Redis query after native multi-metric folding."""

import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from observability_migration.adapters.source.grafana import panels, smoke
from observability_migration.adapters.source.grafana.verification import _semantic_gate
from tests.test_smoke_binding_gaps import _FakeResponse, _lens_panel
from tests.test_target_binding import _offline, _redis_dashboard


def _query_strings(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "query" and isinstance(child, str):
                yield child
            else:
                yield from _query_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _query_strings(child)


def _emitted_redis_query():
    source = _redis_dashboard()
    source["panels"] = source["panels"][:1]
    resolver = _offline()
    result = panels.translate_dashboard(source, rule_pack=resolver._rule_pack, resolver=resolver)
    query = next(_query_strings(result.native_dashboard.to_api_payload()))
    assert 'COALESCE(TO_STRING(service.instance.id), "")' in query
    return query


def _inspect(query, *, populated=False, probe_error=False, blank_legend=False):
    queries = []
    values = [["2026-01-01T00:00:00Z", " - used", 1024.0], ["2026-01-01T00:00:00Z", " - max", 2048.0]]
    if populated and not blank_legend:
        values = [["2026-01-01T00:00:00Z", f"{instance} - {metric}", value] for instance, used, maximum in
                  [("redis-a:9121", 1024.0, 4096.0), ("redis-b:9121", 2048.0, 8192.0)]
                  for metric, value in [("used", used), ("max", maximum)]]

    def post(url, **kwargs):
        sent = kwargs["json"]["query"]
        queries.append(sent)
        if "_binding_rows" in sent:
            if probe_error:
                return _FakeResponse({"error": {"reason": "probe unavailable"}}, status_code=503)
            return _FakeResponse({
                "columns": [{"name": "_binding_rows", "type": "long"}, {"name": "_binding_0", "type": "long"}],
                "values": [[len(values), len(values) if populated else 0]],
            })
        return _FakeResponse({
            "columns": [{"name": "time_bucket", "type": "date"}, {"name": "_series", "type": "keyword"}, {"name": "_value", "type": "double"}],
            "values": values,
        })

    saved = {"id": "redis", "attributes": {
        "title": "Redis", "panelsJSON": json.dumps([_lens_panel(0, "Total Memory Usage", query)]),
    }}
    session = Mock()
    session.post.side_effect = post
    with patch.object(smoke.requests, "Session", return_value=session), \
            patch.object(smoke.requests, "post", side_effect=post), \
            patch.object(smoke.requests, "get", return_value=_FakeResponse({"fields": {}})):
        return smoke.inspect_dashboard(saved, "http://es", timeout=30), queries


def test_emitted_redis_composite_reports_the_missing_physical_instance():
    result, queries = _inspect(_emitted_redis_query())
    assert result["status"] == "has_binding_gaps"
    assert result["null_series_panels"][0]["null_columns"] == ["service.instance.id"]
    assert any("_binding_rows" in query for query in queries)


def test_composite_text_alone_does_not_prove_a_binding_gap():
    # A real label can contain punctuation; require physical-field evidence.
    result, _ = _inspect(_emitted_redis_query(), populated=True, blank_legend=True)
    assert result["status"] == "clean"


def test_multiple_instance_result_is_not_collapsed_by_binding_validation():
    result, _ = _inspect(_emitted_redis_query(), populated=True)
    assert result["status"] == "clean"
    assert result["panels"][0]["rows"] == 4


def test_failed_physical_binding_probe_is_not_a_clean_panel():
    result, _ = _inspect(_emitted_redis_query(), probe_error=True)
    assert result["status"] == "has_runtime_gaps"
    assert result["panels"][0]["status"] == "not_runtime_checked"
    assert "probe unavailable" in result["panels"][0]["binding_probe_error"]
    summary = smoke.build_summary([result])
    assert summary["unverified_binding_panels"] == 1
    assert summary["dashboards_with_binding_gaps"] == 1


def test_standalone_smoke_entrypoint_can_import():
    subprocess.run([sys.executable, "-c", "import observability_migration.adapters.source.grafana.smoke"], check=True)


def test_composite_probe_preserves_scope_and_checks_each_physical_label():
    query = '''FROM metrics-redis
        | WHERE redis_memory_used_bytes IS NOT NULL AND job == "redis|exporter"
        | STATS used = AVG(redis_memory_used_bytes) BY labels.instance, labels.db
        | EVAL series = CONCAT(COALESCE(TO_STRING(`labels.instance`), ""), "/", COALESCE(TO_STRING(labels.db), ""))
        | KEEP series, used'''
    probe, fields = smoke._composite_binding_probe(query)
    assert fields == ["labels.instance", "labels.db"]
    assert 'job == "redis|exporter"' in probe
    assert "WHERE redis_memory_used_bytes IS NOT NULL" in probe
    assert "_binding_present_0 = CASE(labels.instance IS NOT NULL, 1, 0)" in probe
    assert "_binding_present_1 = CASE(labels.db IS NOT NULL, 1, 0)" in probe
    assert "_binding_0 = SUM(_binding_present_0)" in probe
    assert "CONCAT" in probe


def test_plain_metric_labels_do_not_need_a_physical_binding_probe():
    assert smoke._composite_binding_probe('FROM metrics-* | STATS used = AVG(x) BY instance') == ("", [])


def test_hint_does_not_recommend_an_alternative_whose_binding_probe_failed():
    outcome = {"status": "pass", "rows": 2, "null_columns": [], "binding_probe_error": "unavailable"}
    with patch.object(smoke, "_existing_fields", return_value=["labels.instance"]), \
            patch.object(smoke, "validate_esql", return_value=outcome):
        hint = smoke.binding_gap_hint("service.instance.id", _emitted_redis_query(), "http://es", 30, kind="panel")
    assert "same query returns values with" not in hint
    assert "could not be verified" in hint
    assert "--field-profile" not in hint


def test_literal_text_does_not_hide_a_later_executable_conversion():
    query = '''FROM metrics-* | STATS used = AVG(x) BY instance, db
        | EVAL note = """COALESCE(TO_STRING(instance), "")"""
        | EVAL legend = CONCAT(COALESCE(TO_STRING(db), ""), " - used")
        | KEEP legend, used'''
    _, fields = smoke._composite_binding_probe(query)
    assert fields == ["db"]


@pytest.mark.parametrize("rows, count", [(2, None), (2, "0"), (2, -1), (2, 3), (2, True), (1, 1), (999, 999)])
def test_malformed_physical_count_never_verifies_the_binding(rows, count):
    response = _FakeResponse({
        "columns": [{"name": "_binding_rows", "type": "long"}, {"name": "_binding_0", "type": "long"}],
        "values": [[rows, count]],
    })
    primary = _FakeResponse({
        "columns": [{"name": "_series", "type": "keyword"}, {"name": "_value", "type": "double"}],
        "values": [[" - used", 1.0], [" - max", 2.0]],
    })
    session = Mock()
    session.post.side_effect = [primary, response]
    result = smoke.validate_esql("http://es", _emitted_redis_query(), 30, session=session)
    assert result["binding_probe_error"]


@pytest.mark.parametrize("status, expected", [
    ("guessed", "Yellow"), ("missing", "Yellow"),
    ("asserted", "Green"), ("verified", "Green"), ("none", "Green"),
])
def test_required_label_binding_affects_readiness(status, expected):
    panel = SimpleNamespace(status="migrated", target_binding={"status": status})
    assert _semantic_gate(panel, None, [], []) == expected
