# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Post-upload smoke evidence for labels bound to fields the data lacks.

Reproduces the Grafana 763 Redis failure on a Prometheus remote-write target:
``service.instance.id`` is mapped on ``metrics-*`` (another stream has it) but
null on the Redis documents, which carry ``instance`` / ``labels.instance``.
Before, smoke reported such a dashboard ``clean``: queries ran and returned
rows. Now the all-null breakdown, the option-less control and the native
PROMQL panels that break on its null parameter are each reported, with a hint
proven by re-running the query with the working spelling.
"""

import json
import re
import unittest
from types import SimpleNamespace
from unittest import mock

from observability_migration.adapters.source.grafana import smoke
from observability_migration.adapters.source.grafana.verification import _semantic_gate
from observability_migration.targets.kibana.smoke_integration import merge_smoke_into_results

_TS = "2026-10-29T11:00:00.000Z"
_KEYWORD = {"keyword": {"type": "keyword", "searchable": True, "aggregatable": True}}


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


def _label_column(query):
    """The label field the query groups or keeps, as the fake target sees it."""
    for name in ("labels.instance", "service.instance.id", "instance"):
        if re.search(r"(?<![\w.?])`?" + re.escape(name) + r"`?(?![\w.])", query):
            return name
    return ""


class _RemoteWriteTarget:
    """Fake ES for the customer layout: ``instance`` has values, the OTel field does not."""

    def __init__(self, existing=("instance", "labels.instance", "service.instance.id")):
        self.existing = set(existing)
        self.queries = []

    def post(self, url, params=None, json=None, headers=None, timeout=None, **_kwargs):
        query = json["query"]
        self.queries.append(query)
        label = _label_column(query)
        value = None if label in ("", "service.instance.id") else "redis-a:9121"
        if query.lstrip().startswith("PROMQL"):
            return _FakeResponse({"columns": [{"name": "value", "type": "double"}], "values": [[3.0]]})
        if "MV_APPEND" in query:  # control options query
            values = [[".*"]] + ([[value]] if value else [])
            return _FakeResponse({"columns": [{"name": label, "type": "keyword"}], "values": values})
        return _FakeResponse(
            {
                "columns": [
                    {"name": "time_bucket", "type": "date"},
                    {"name": label, "type": "keyword"},
                    {"name": "used", "type": "double"},
                ],
                "values": [[_TS, value, 1048576.0], [_TS, value, 1050000.0]],
            }
        )

    def get(self, url, params=None, headers=None, timeout=None, verify=None):
        requested = params["fields"].split(",")
        return _FakeResponse({"fields": {name: _KEYWORD for name in requested if name in self.existing}})


def _lens_panel(index, title, query):
    return {
        "panelIndex": f"p{index}",
        "type": "lens",
        # Dashboards API uploads carry the title here, not on the attributes.
        "embeddableConfig": {"title": title, "attributes": {"state": {"query": {"esql": query}}}},
        "gridData": {"x": 0, "y": index * 8, "w": 24, "h": 8},
    }


_MEMORY_QUERY = (
    "TS metrics-* | WHERE (MV_COUNT(?instance) == 0 OR MV_CONTAINS(TO_STRING(?instance), service.instance.id)) "
    "| STATS used = AVG(LAST_OVER_TIME(redis_memory_used_bytes)) BY time_bucket = TBUCKET(75, ?_tstart, ?_tend), "
    "service.instance.id | KEEP time_bucket, `service.instance.id`, used"
)
_CLIENTS_QUERY = (
    "PROMQL index=metrics-* start=?_tstart end=?_tend buckets=50 "
    "value=(sum(redis_connected_clients{instance=~?instance}))"
)
_CONTROL_QUERY = (
    "FROM metrics-* | WHERE redis_up IS NOT NULL AND `service.instance.id` IS NOT NULL "
    "| STATS count = COUNT(*) BY `service.instance.id` | EVAL options = MV_APPEND(\".*\", `service.instance.id`) "
    "| MV_EXPAND options | STATS count = COUNT(*) BY options | KEEP options "
    "| RENAME options AS `service.instance.id`"
)


def _saved_object():
    return {
        "id": "redis",
        "attributes": {
            "title": "Redis",
            "panelsJSON": json.dumps(
                [_lens_panel(0, "Total Memory Usage", _MEMORY_QUERY), _lens_panel(1, "Clients", _CLIENTS_QUERY)]
            ),
            "pinned_panels": {
                "panels": {
                    "c1": {
                        "type": "esql_control",
                        "config": {"title": "instance", "variable_name": "instance", "esql_query": _CONTROL_QUERY},
                    }
                }
            },
        },
    }


def _inspect(target):
    session = mock.Mock()
    session.post.side_effect = target.post
    with mock.patch.object(smoke.requests, "Session", return_value=session), \
            mock.patch.object(smoke.requests, "post", side_effect=target.post), \
            mock.patch.object(smoke.requests, "get", side_effect=target.get):
        return smoke.inspect_dashboard(_saved_object(), "http://es", timeout=30)


class InspectDashboardBindingGapTests(unittest.TestCase):
    def test_reports_null_series_and_control_without_options(self):
        result = _inspect(_RemoteWriteTarget())

        self.assertEqual(result["status"], "has_binding_gaps")
        [memory] = result["null_series_panels"]
        self.assertEqual(memory["panel"], "Total Memory Usage")
        self.assertEqual(memory["null_columns"], ["service.instance.id"])
        [control] = result["empty_controls"]
        self.assertEqual((control["variable"], control["field"], control["options"]), ("instance", "service.instance.id", 0))
        self.assertEqual(control["dependent_panels"], ["Total Memory Usage", "Clients"])
        self.assertEqual(control["native_promql_dependents"], ["Clients"])

    def test_hint_names_only_spellings_proven_to_return_values(self):
        result = _inspect(_RemoteWriteTarget())

        [hint] = result["null_series_panels"][0]["hints"]
        self.assertIn("`instance`, `labels.instance`", hint)
        self.assertIn("--field-profile prometheus_native", hint)
        self.assertIn("`instance`", result["empty_controls"][0]["hint"])

    def test_hint_reports_a_data_gap_when_no_spelling_has_values(self):
        # Only the OTel field is mapped: no alternative exists to prove.
        result = _inspect(_RemoteWriteTarget(existing=("service.instance.id",)))

        [hint] = result["null_series_panels"][0]["hints"]
        self.assertIn("data gap, not a field-mapping error", hint)

    def test_control_whose_options_query_fails_is_reported_with_a_hint(self):
        # Kibana 9.7 rig: no stream maps the guessed field, so the options query
        # errors instead of returning nothing; the control is just as unusable.
        target = _RemoteWriteTarget()
        real_post = target.post

        def post(url, params=None, json=None, **kwargs):
            if "MV_APPEND" in json["query"] and "service.instance.id" in json["query"]:
                return _FakeResponse(
                    {"error": {"reason": "Found 1 problem\nline 1:46: Unknown column [service.instance.id]"}},
                    status_code=400,
                )
            return real_post(url, params=params, json=json, **kwargs)

        target.post = post
        result = _inspect(target)

        [control] = result["empty_controls"]
        self.assertEqual((control["status"], control["field"]), ("fail", "service.instance.id"))
        self.assertIn("`instance`", control["hint"])
        self.assertEqual(result["status"], "has_binding_gaps")

    def test_correctly_bound_dashboard_is_clean(self):
        target = _RemoteWriteTarget()
        saved = _saved_object()
        attrs = saved["attributes"]
        attrs["panelsJSON"] = attrs["panelsJSON"].replace("service.instance.id", "instance")
        control = attrs["pinned_panels"]["panels"]["c1"]["config"]
        control["esql_query"] = control["esql_query"].replace("service.instance.id", "instance")
        session = mock.Mock()
        session.post.side_effect = target.post
        with mock.patch.object(smoke.requests, "Session", return_value=session), \
                mock.patch.object(smoke.requests, "post", side_effect=target.post), \
                mock.patch.object(smoke.requests, "get", side_effect=target.get):
            result = smoke.inspect_dashboard(saved, "http://es", timeout=30)

        self.assertEqual(result["status"], "clean")
        self.assertEqual(result["controls"][0]["options"], 1)
        self.assertEqual(result["null_series_panels"], [])


class HelperTests(unittest.TestCase):
    def test_null_dimension_columns(self):
        columns = [{"name": "t", "type": "date"}, {"name": "host", "type": "keyword"}, {"name": "v", "type": "double"}]
        self.assertEqual(smoke._null_dimension_columns(columns, [[_TS, None, 1.0], [_TS, None, 2.0]]), ["host"])
        # Some rows labelled: a real breakdown with a gap, not a binding error.
        self.assertEqual(smoke._null_dimension_columns(columns, [[_TS, None, 1.0], [_TS, "a", 2.0]]), [])
        # No measure values: that is an empty panel, reported as such elsewhere.
        self.assertEqual(smoke._null_dimension_columns(columns, [[_TS, None, None]]), [])
        self.assertEqual(smoke._null_dimension_columns(columns, []), [])

    def test_all_measures_null(self):
        columns = [{"name": "value", "type": "double"}, {"name": "host", "type": "keyword"}]
        self.assertTrue(smoke._all_measures_null(columns, [[None, "a"]]))
        self.assertFalse(smoke._all_measures_null(columns, [[None, "a"], [0.0, "b"]]))
        self.assertFalse(smoke._all_measures_null(columns, []))
        self.assertFalse(smoke._all_measures_null([{"name": "host", "type": "keyword"}], [["a"]]))

    def test_rows_of_null_values_are_an_empty_panel_with_a_reason(self):
        # Grafana 763 "Memory Usage" with Redis maxmemory 0: used / max is null,
        # Kibana shows "No results found", but the query returns one row (#482).
        saved = {
            "id": "d",
            "attributes": {
                "title": "D",
                "panelsJSON": json.dumps(
                    [_lens_panel(0, "Memory Usage", "TS metrics-* | EVAL value = used / max | KEEP value")]
                ),
            },
        }
        session = mock.Mock()
        session.post.return_value = _FakeResponse(
            {"columns": [{"name": "value", "type": "double"}], "values": [[None]]}
        )
        with mock.patch.object(smoke.requests, "Session", return_value=session):
            result = smoke.inspect_dashboard(saved, "http://es", timeout=30)

        self.assertEqual(result["status"], "has_empty_panels")
        [panel] = result["empty_panels"]
        self.assertEqual((panel["panel"], panel["rows"]), ("Memory Usage", 1))
        self.assertIn("division by zero", panel["empty_reason"])

    def test_extract_dashboard_controls_reads_both_storage_shapes(self):
        pinned = {"panels": {"a": {"type": "esql_control", "config": {"variable_name": "x", "esql_query": "FROM m"}}}}
        legacy = {"b": {"type": "esqlControl", "explicitInput": {"variableName": "y", "esqlQuery": "FROM n", "title": "Y"}}}
        controls = smoke.extract_dashboard_controls(
            {"pinned_panels": json.dumps(pinned), "controlGroupInput": {"panelsJSON": json.dumps(legacy)}}
        )
        self.assertEqual(
            [(c["variable"], c["title"], c["query"]) for c in controls], [("x", "x", "FROM m"), ("y", "Y", "FROM n")]
        )
        self.assertEqual(smoke.extract_dashboard_controls({}), [])

    def test_panel_title_prefers_embeddable_title_over_panel_index(self):
        self.assertEqual(smoke._panel_title({"panelIndex": "uuid", "embeddableConfig": {"title": "Clients"}}), "Clients")
        self.assertEqual(smoke._panel_title({"panelIndex": "uuid"}), "uuid")

    def test_panels_binding_variable_ignores_identifier_params(self):
        bound, native = smoke._panels_binding_variable(
            "instance",
            [
                ("es", ["FROM m | WHERE x == ?instance"]),
                ("field", ["FROM m | STATS BY ??instance"]),
                ("promql", ["PROMQL index=m value=(up{instance=~?instance})"]),
                ("other", ["FROM m | WHERE x == ?instance_name"]),
            ],
        )
        self.assertEqual((bound, native), (["es", "promql"], ["promql"]))

    def test_substitute_field_respects_boundaries(self):
        query = "FROM m | WHERE MV_CONTAINS(?instance, service.instance.id) | KEEP `service.instance.id`, labels.instance"
        self.assertEqual(
            smoke._substitute_field(query, "service.instance.id", "labels.instance"),
            "FROM m | WHERE MV_CONTAINS(?instance, `labels.instance`) | KEEP `labels.instance`, labels.instance",
        )
        self.assertEqual(smoke._substitute_field("BY instance, labels.instance", "instance", "host"), "BY host, labels.instance")

    def test_alternative_label_fields(self):
        self.assertEqual(
            smoke._alternative_label_fields("service.instance.id")[:3], ["instance", "labels.instance", "prometheus.labels.instance"]
        )
        self.assertIn("instance", smoke._alternative_label_fields("labels.instance"))
        self.assertIn("service.instance.id", smoke._alternative_label_fields("instance"))


def _duration_panel(title, decimals, field="v"):
    """Lens state as Kibana 9.6+ stores the Dashboards API ``duration`` format."""
    params = {"compact": True, "fromUnit": "seconds", "toUnit": "humanizePrecise"}
    if decimals is not None:
        params["decimals"] = decimals
    panel = _lens_panel(0, title, f"TS metrics-* | KEEP t, {field}")
    panel["embeddableConfig"]["attributes"]["state"]["datasourceStates"] = {
        "textBased": {
            "layers": {
                "l0": {
                    "columns": [
                        {"fieldName": "t", "meta": {"type": "date"}},
                        {"fieldName": field, "params": {"format": {"id": "duration", "params": params}}},
                    ]
                }
            }
        }
    }
    return panel


class ControlSelectionBindingTests(unittest.TestCase):
    """Smoke must bind ``?var`` to the control's stored selection, as Kibana does.

    K8s Views Global: ``MV_COUNT(?job) == 0 OR MV_CONTAINS(?job, job)`` matched
    nothing when smoke sent the string ``.*``, so panels with data were "empty".
    """

    def test_control_param_values(self):
        controls = [
            {"variable": "job", "selected": ["node", "kubelet"], "multi": True},
            {"variable": "cluster", "selected": ["prod"], "multi": False},
            {"variable": "pod", "selected": [], "multi": True},
        ]
        self.assertEqual(smoke.control_param_values(controls), {"job": ["node", "kubelet"], "cluster": "prod"})

    def test_panel_queries_use_the_stored_selection(self):
        query = "TS metrics-* | WHERE MV_COUNT(?job) == 0 OR MV_CONTAINS(?job, job) | STATS v = SUM(x) BY t"
        saved = {
            "id": "d",
            "attributes": {
                "title": "D",
                "panelsJSON": json.dumps([_lens_panel(0, "CPU", query)]),
                "pinned_panels": {
                    "panels": {
                        "c": {
                            "type": "esql_control",
                            "config": {
                                "variable_name": "job", "variable_type": "multi_values",
                                "esql_query": "FROM m | STATS BY job", "selected_options": ["node"],
                            },
                        }
                    }
                },
            },
        }

        def post(url, params=None, json=None, **kwargs):
            bound = {k: v for p in json.get("params", []) for k, v in p.items()}
            if json["query"].startswith("FROM m"):
                return _FakeResponse({"columns": [{"name": "job", "type": "keyword"}], "values": [["node"]]})
            value = 1.0 if bound.get("job") == ["node"] else None
            return _FakeResponse({"columns": [{"name": "v", "type": "double"}], "values": [[value]]})

        session = mock.Mock()
        session.post.side_effect = post
        with mock.patch.object(smoke.requests, "Session", return_value=session):
            result = smoke.inspect_dashboard(saved, "http://es", timeout=30)

        self.assertEqual(result["status"], "clean")
        self.assertEqual(result["empty_panels"], [])


class TimeWindowTests(unittest.TestCase):
    """Smoke queries the window a viewer sees on load, not a fixed hour.

    Node Exporter disk graphs (saved ``now-15m``) peaked at 3 ms in its own
    window but 0.5 ms in smoke's hour, so smoke flagged an axis Kibana draws fine.
    """

    def test_relative_time_window(self):
        self.assertEqual(smoke.relative_time_window("now-15m", "now"), smoke.timedelta(minutes=15))
        self.assertEqual(smoke.relative_time_window("now-7d"), smoke.timedelta(days=7))
        self.assertIsNone(smoke.relative_time_window("now-1h", "now-5m"))
        self.assertIsNone(smoke.relative_time_window("2026-10-01T00:00:00Z"))

    def test_panels_use_their_override_then_the_saved_dashboard_range(self):
        dashboard_panel = _lens_panel(0, "Disk", "TS m | WHERE @timestamp >= ?_tstart | STATS v = MAX(x)")
        override_panel = _lens_panel(1, "Clients", "PROMQL index=m start=?_tstart end=?_tend value=(up)")
        override_panel["embeddableConfig"]["timeRange"] = {"from": "now-1m", "to": "now"}
        saved = {
            "id": "d",
            "attributes": {
                "title": "D", "timeRestore": True, "timeFrom": "now-15m", "timeTo": "now",
                "panelsJSON": json.dumps([dashboard_panel, override_panel]),
            },
        }
        seen = []

        def post(url, params=None, json=None, **kwargs):
            seen.append(json)
            return _FakeResponse({"columns": [{"name": "v", "type": "double"}], "values": [[1.0]]})

        session = mock.Mock()
        session.post.side_effect = post
        with mock.patch.object(smoke.requests, "Session", return_value=session):
            smoke.inspect_dashboard(saved, "http://es", timeout=30)

        esql, promql = seen
        self.assertIn("NOW() - 900 seconds", esql["query"])
        bounds = {k: v for p in promql["params"] for k, v in p.items()}
        start = smoke.datetime.fromisoformat(bounds["_tstart"].replace("Z", "+00:00"))
        end = smoke.datetime.fromisoformat(bounds["_tend"].replace("Z", "+00:00"))
        self.assertEqual(end - start, smoke.timedelta(minutes=1))


class UnreadableAxisTests(unittest.TestCase):
    """#480: a 0.2 ms series with Kibana 9.6+'s ``decimals: 0`` duration reads "0 ms" on every tick."""

    def _inspect(self, panel, values):
        saved = {"id": "d", "attributes": {"title": "D", "panelsJSON": json.dumps([panel])}}
        session = mock.Mock()
        session.post.return_value = _FakeResponse(
            {
                "columns": [{"name": "t", "type": "date"}, {"name": "v", "type": "double"}],
                "values": [[_TS, value] for value in values],
            }
        )
        with mock.patch.object(smoke.requests, "Session", return_value=session):
            return smoke.inspect_dashboard(saved, "http://es", timeout=30)

    def test_sub_millisecond_series_with_zero_decimals_is_flagged(self):
        result = self._inspect(_duration_panel("Time Spent", 0), [0.00012, 0.00028])

        self.assertEqual(result["status"], "has_unreadable_axes")
        [panel] = result["unreadable_axis_panels"]
        self.assertEqual(panel["unreadable_axes"], [{"column": "v", "peak_seconds": 0.00028, "decimals": 0}])

    def test_readable_cases_are_not_flagged(self):
        cases = {
            "series reaches 1 ms": (_duration_panel("T", 0), [0.0002, 0.006]),
            "format keeps decimals (Kibana 9.5 style)": (_duration_panel("T", None), [0.0002]),
            "two decimals show 0.28 ms": (_duration_panel("T", 2), [0.00028]),
            "all zero": (_duration_panel("T", 0), [0.0, 0.0]),
        }
        for name, (panel, values) in cases.items():
            with self.subTest(name):
                result = self._inspect(panel, values)
                self.assertEqual(result["unreadable_axis_panels"], [])
                self.assertEqual(result["status"], "clean")


class MergeAndGateTests(unittest.TestCase):
    def test_rollups_reach_the_verification_gate(self):
        smoke_report = {
            "dashboards": [
                {
                    "id": "redis",
                    "title": "Redis",
                    "panels": [
                        {"panel": "Total Memory Usage", "status": "null_series"},
                        {"panel": "Clients", "status": "pass"},
                        {"panel": "Uptime", "status": "pass"},
                    ],
                    "empty_controls": [
                        {"dependent_panels": ["Total Memory Usage", "Clients"], "native_promql_dependents": ["Clients"]}
                    ],
                    "unreadable_axis_panels": [{"panel": "Uptime"}],
                }
            ]
        }
        panels = [SimpleNamespace(title=t, status="migrated", runtime_rollups=[]) for t in ("Total Memory Usage", "Clients", "Uptime")]
        result = SimpleNamespace(dashboard_title="Redis", kibana_saved_object_id="redis", panel_results=panels)

        summary = merge_smoke_into_results([result], smoke_report)

        self.assertEqual(
            (summary["null_series"], summary["control_unbound"], summary["control_null_param"],
             summary["unreadable_axis"]),
            (1, 2, 1, 1),
        )
        memory, clients, uptime = panels
        self.assertEqual(memory.runtime_rollups, ["null_series", "control_unbound"])
        self.assertEqual(clients.runtime_rollups, ["control_unbound", "control_null_param"])
        self.assertEqual(uptime.runtime_rollups, ["unreadable_axis"])
        gates = [_semantic_gate(p, None, [], p.runtime_rollups) for p in panels]
        self.assertEqual(gates, ["Yellow", "Red", "Yellow"])


if __name__ == "__main__":
    unittest.main()
