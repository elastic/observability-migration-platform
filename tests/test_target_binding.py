# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Label-field provenance ("target binding") for migrated Grafana assets.

A Grafana Redis dashboard migrated without ``--es-url`` mapped ``instance`` to
the OTel ``service.instance.id``. On a target fed by Prometheus remote-write
(``instance`` / ``labels.instance``) every breakdown rendered one ``(null)``
series and the Instance control had no options, while the report said 13 Green.
These tests pin the provenance that now separates "translated" from "bound to
fields the target confirmed".
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from observability_migration.adapters.source.grafana import panels
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.runtime_features import (
    KIBANA_PROMQL_CONTROL_PARAMS,
    PROMQL_COMMAND_V0,
    PROMQL_LABEL_MATCHER_PARAMS,
    set_runtime_feature,
)
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.core import target_binding
from observability_migration.core.reporting.report import (
    MigrationResult,
    PanelResult,
    print_target_binding_summary,
    save_detailed_report,
)

_KEYWORD_CAPS = {"keyword": {"type": "keyword", "searchable": True, "aggregatable": True}}
_GAUGE_CAPS = {
    "double": {"type": "double", "searchable": True, "aggregatable": True, "time_series_metric": "gauge"}
}
# The native /_prometheus remote-write layout the failing customer target had.
_REMOTE_WRITE_FIELDS = {
    "instance": _KEYWORD_CAPS,
    "labels.instance": _KEYWORD_CAPS,
    "metrics.redis_up": _GAUGE_CAPS,
    "metrics.redis_memory_used_bytes": _GAUGE_CAPS,
    "metrics.redis_memory_max_bytes": _GAUGE_CAPS,
    "metrics.redis_connected_clients": _GAUGE_CAPS,
}


def _offline(field_profile=None, rule_pack=None):
    return SchemaResolver(rule_pack or RulePackConfig(), field_profile=field_profile)


def _live(fields, field_profile=None):
    resolver = SchemaResolver(RulePackConfig(), field_profile=field_profile)
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    if isinstance(fields, dict):
        resolver._field_cache = {name: dict(caps) for name, caps in fields.items()}
    else:
        resolver._field_cache = {name: dict(_KEYWORD_CAPS) for name in fields}
    return resolver


def _capture(resolver, label, metric_field=None):
    resolver.begin_binding_capture()
    field = resolver.resolve_label(label, metric_field=metric_field)
    entries = resolver.end_binding_capture()
    return field, entries


class ResolverBindingStatusTests(unittest.TestCase):
    def test_offline_default_profile_is_a_guess(self):
        field, entries = _capture(_offline(), "instance")
        self.assertEqual(field, "service.instance.id")
        self.assertEqual(
            entries,
            [{"label": "instance", "field": "service.instance.id", "basis": "otel_default", "status": "guessed"}],
        )

    def test_explicit_otel_profile_is_an_assertion_not_a_guess(self):
        _field, entries = _capture(_offline(field_profile="otel"), "instance")
        self.assertEqual(entries[0]["status"], "asserted")

    def test_explicit_named_profile_offline_is_asserted(self):
        field, entries = _capture(_offline(field_profile="prometheus_native"), "instance")
        self.assertEqual(field, "labels.instance")
        self.assertEqual((entries[0]["basis"], entries[0]["status"]), ("profile_plan", "asserted"))

    def test_live_caps_with_source_label_verify_it(self):
        field, entries = _capture(_live(["instance", "labels.instance"]), "instance")
        self.assertEqual(field, "instance")
        self.assertEqual((entries[0]["basis"], entries[0]["status"]), ("live_caps", "verified"))

    def test_live_caps_without_any_candidate_report_missing(self):
        field, entries = _capture(_live(["host.ip_unrelated"]), "instance")
        self.assertEqual(field, "service.instance.id")
        self.assertEqual(entries[0]["status"], "missing")

    def test_named_profile_field_absent_from_live_caps_is_missing(self):
        _field, entries = _capture(_live(["labels.job"], field_profile="prometheus_native"), "instance")
        self.assertEqual((entries[0]["field"], entries[0]["status"]), ("labels.instance", "missing"))

    def test_concrete_rule_pack_rewrite_is_asserted_offline(self):
        rule_pack = RulePackConfig()
        rule_pack.label_rewrites = {"instance": "my.custom.instance"}
        field, entries = _capture(_offline(rule_pack=rule_pack), "instance")
        self.assertEqual(field, "my.custom.instance")
        self.assertEqual((entries[0]["basis"], entries[0]["status"]), ("rule_pack", "asserted"))

    def test_ignored_label_records_nothing(self):
        rule_pack = RulePackConfig()
        rule_pack.ignored_labels = ["instance"]
        field, entries = _capture(_offline(rule_pack=rule_pack), "instance")
        self.assertIsNone(field)
        self.assertEqual(entries, [])

    def test_resolution_is_unchanged_by_capture(self):
        resolver = _offline()
        uncaptured = resolver.resolve_label("namespace")
        captured, _entries = _capture(resolver, "namespace")
        self.assertEqual(uncaptured, captured)

    def test_captures_nest_and_unbalanced_end_is_empty(self):
        resolver = _offline()
        self.assertEqual(resolver.end_binding_capture(), [])
        resolver.begin_binding_capture()
        resolver.resolve_label("instance")
        resolver.begin_binding_capture()
        resolver.resolve_label("pod")
        inner = resolver.end_binding_capture()
        outer = resolver.end_binding_capture()
        self.assertEqual([entry["label"] for entry in inner], ["pod"])
        self.assertEqual([entry["label"] for entry in outer], ["instance", "pod"])

    def test_native_promql_label_binding(self):
        self.assertEqual(_offline().native_promql_label_binding("instance")["status"], "guessed")
        self.assertEqual(
            _offline(field_profile="prometheus_native").native_promql_label_binding("instance")["status"],
            "asserted",
        )
        self.assertEqual(_live(["labels.instance"]).native_promql_label_binding("instance")["status"], "verified")
        self.assertEqual(_live(["service.instance.id"]).native_promql_label_binding("instance")["status"], "missing")


class SummarizeBindingTests(unittest.TestCase):
    def test_only_fields_in_the_emitted_query_count(self):
        entries = [
            {"label": "instance", "field": "service.instance.id", "status": "guessed"},
            {"label": "job", "field": "service.name", "status": "guessed"},
        ]
        binding = target_binding.summarize_binding(entries, "TS metrics-* | STATS v = AVG(x) BY `service.instance.id`")
        self.assertEqual([entry["label"] for entry in binding["fields"]], ["instance"])
        self.assertEqual(binding["status"], "guessed")

    def test_field_match_respects_identifier_boundaries(self):
        entry = [{"label": "instance", "field": "instance", "status": "verified"}]
        for text in ("BY labels.instance", "BY instance_id", "MV_CONTAINS(?instance, x)"):
            with self.subTest(text=text):
                self.assertEqual(target_binding.summarize_binding(entry, text)["status"], "none")
        self.assertEqual(target_binding.summarize_binding(entry, "BY time_bucket, instance")["status"], "verified")

    def test_worst_status_and_counts(self):
        self.assertEqual(target_binding.worst_status(["verified", "guessed", "asserted"]), "guessed")
        self.assertEqual(target_binding.worst_status([]), "none")
        counts = target_binding.count_statuses(
            [{"status": "verified"}, {"status": "missing"}, {}, None, {"status": "verified"}]
        )
        self.assertEqual(counts, {"missing": 1, "guessed": 0, "asserted": 0, "verified": 2, "none": 2})


def _redis_dashboard():
    """Minimal shape of Grafana 763's failing panels and Instance control."""
    return {
        "title": "Redis binding test",
        "uid": "redis-binding",
        "panels": [
            {
                "id": 1,
                "type": "timeseries",
                "title": "Total Memory Usage",
                "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
                "targets": [
                    {"refId": "A", "expr": 'redis_memory_used_bytes{instance=~"$instance"}', "legendFormat": "used, {{instance}}"},
                    {"refId": "B", "expr": 'redis_memory_max_bytes{instance=~"$instance"}', "legendFormat": "max, {{instance}}"},
                ],
            },
            {
                "id": 2,
                "type": "stat",
                "title": "Clients",
                "gridPos": {"x": 12, "y": 0, "w": 6, "h": 4},
                "targets": [{"refId": "A", "expr": 'sum(redis_connected_clients{instance=~"$instance"})'}],
            },
        ],
        "templating": {
            "list": [
                {
                    "name": "instance",
                    "type": "query",
                    "query": "label_values(redis_up, instance)",
                    "multi": True,
                    "includeAll": True,
                }
            ]
        },
    }


class TranslateDashboardBindingTests(unittest.TestCase):
    def _translate(self, resolver):
        # Mirror the CLI's offline defaults: native PROMQL with control params.
        rule_pack = resolver._rule_pack
        rule_pack.native_promql = True
        for feature in (PROMQL_COMMAND_V0, PROMQL_LABEL_MATCHER_PARAMS, KIBANA_PROMQL_CONTROL_PARAMS):
            set_runtime_feature(rule_pack, feature, supported=True, source="test", confidence="verified")
        return panels.translate_dashboard(
            _redis_dashboard(), datasource_index="metrics-*", esql_index="metrics-*",
            rule_pack=resolver._rule_pack, resolver=resolver,
        )

    def test_offline_default_reports_guessed_breakdown_and_control(self):
        result = self._translate(_offline())
        by_title = {pr.title: pr for pr in result.panel_results}
        memory = by_title["Total Memory Usage"].target_binding
        self.assertEqual(memory["status"], "guessed")
        self.assertIn(("instance", "service.instance.id"), {(e["label"], e["field"]) for e in memory["fields"]})
        # The native PROMQL stat panel keeps the bare label: still a guess.
        clients = by_title["Clients"]
        self.assertTrue(clients.esql_query.lstrip().startswith("PROMQL"), clients.esql_query)
        self.assertEqual(clients.target_binding["status"], "guessed")
        self.assertEqual(
            [(b["variable"], b["status"]) for b in result.control_bindings], [("instance", "guessed")]
        )

    def test_live_caps_report_verified(self):
        result = self._translate(_live(_REMOTE_WRITE_FIELDS))
        statuses = {pr.title: pr.target_binding["status"] for pr in result.panel_results}
        self.assertEqual(statuses, {"Total Memory Usage": "verified", "Clients": "verified"})
        self.assertEqual([b["status"] for b in result.control_bindings], ["verified"])


class ReportTests(unittest.TestCase):
    def _result(self):
        result = MigrationResult(dashboard_title="d", dashboard_uid="u")
        guessed = {
            "status": "guessed",
            "fields": [{"label": "instance", "field": "service.instance.id", "basis": "otel_default", "status": "guessed"}],
        }
        result.panel_results = [
            PanelResult("a", "timeseries", "line", "migrated", 1.0, target_binding=guessed),
            PanelResult("b", "text", "markdown", "migrated", 1.0, target_binding={"status": "none", "fields": []}),
        ]
        result.control_bindings = [{"variable": "instance", **guessed}]
        return result

    def test_summary_names_the_guessed_mapping_and_the_fix(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            print_target_binding_summary([self._result()])
        text = out.getvalue()
        self.assertIn("panels 0 verified / 1 guessed / 1 without label fields", text)
        self.assertIn("controls 0 verified / 1 guessed", text)
        self.assertIn("instance → service.instance.id (guessed)", text)
        self.assertIn("--es-url", text)

    def test_summary_is_silent_without_bindings(self):
        result = MigrationResult(dashboard_title="d", dashboard_uid="u")
        result.panel_results = [PanelResult("a", "timeseries", "line", "migrated", 1.0)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            print_target_binding_summary([result])
        self.assertEqual(out.getvalue(), "")

    def test_detailed_report_serializes_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            save_detailed_report([self._result()], path)
            dashboard = json.loads(path.read_text())["dashboards"][0]
        self.assertEqual(dashboard["panels"][0]["target_binding"]["status"], "guessed")
        self.assertEqual(dashboard["control_bindings"][0]["variable"], "instance")


if __name__ == "__main__":
    unittest.main()
