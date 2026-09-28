# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression tests for GitHub issue #448.

On a non-Prometheus schema target (OTel Collector ``prometheusreceiver`` →
``elasticsearch`` exporter, ``mapping.mode: otel``, index
``metrics-prometheusreceiver.otel-default``), the native PROMQL path emitted
**bare Prometheus label names** in its matchers and GROK legend patterns.
Panels returned HTTP 200 / zero rows silently.

The ES|QL path namespaced the same labels through ``resolve_label`` (issue
#163, fixed).  This fix extends that to the native PROMQL path.

Live accept/reject matrix verified on metrics-prometheusreceiver.otel-default
(ES 9.5.0-SNAPSHOT, OTel Collector 0.149.0, mapping.mode: otel, 2026-09-23):

  PROMQL index=metrics-prometheusreceiver.otel-default step=1m
    value=(rate(node_disk_written_bytes_total{
      `service.instance.id`=~"node-exporter:9100"}[5m]))
      -> ACCEPT, rows > 0

  PROMQL index=metrics-prometheusreceiver.otel-default step=1m
    value=(sum by (`service.instance.id`)
      (rate(node_disk_written_bytes_total[5m])))
      -> ACCEPT, columns: ['value', 'step', 'service.instance.id']

  PROMQL index=metrics-prometheusreceiver.otel-default step=1m
    value=(rate(node_disk_written_bytes_total{instance=~"node-exporter:9100"}[5m]))
      -> ACCEPT (HTTP 200), rows = 0  <- the bug

  PROMQL ... | RENAME `service.instance.id` AS instance
      -> ACCEPT, column renamed to 'instance'

  PROMQL index=metrics-prometheusreceiver.otel-default step=1m
    value=(sum by (device) (rate(node_disk_written_bytes_total[5m])))
      -> ACCEPT, column = 'device', value = 'vda'  <- bare device works

_timeseries shape on mapping.mode: otel (live sample from 2026-09-23):
  {"attributes":{"device":"vda"},
   "resource":{"attributes":{
     "service.name":"node-exporter",
     "server.address":"node-exporter",
     "service.instance.id":"node-exporter:9100",
     "server.port":"9100",
     "url.scheme":"http"}},
   "scope":{"name":"github.com/open-telemetry/opentelemetry-collector-contrib/..."},
   "_metric_names_hash":"d1e2117fd39b59b1"}

Key asymmetry:
  - ``device`` is stored at ``attributes.device`` (top-level ``attributes``).
    bare matcher ``device=~"vdb"`` WORKS because ES PROMQL resolves names
    against datapoint ``attributes.*`` automatically.
  - ``service.instance.id`` is stored at ``resource.attributes.service.instance.id``.
    bare matcher ``instance=~"..."`` returns 0 rows because there is no top-level
    ``instance`` field and ES PROMQL does NOT resolve ``resource.attributes.*``.

Therefore the fix uses evidence-based resolution:
  - Resolve each label through ``resolve_label`` (already OTel-aware).
  - Only rewrite when ``discovery_status == 'ok'`` (requires ``--es-url``).
  - ``device`` resolves to itself -> no rewrite -> no change in behavior.
  - ``instance`` resolves to ``service.instance.id`` -> rewrite matcher key
    and grouping label, append ``| RENAME `service.instance.id` AS instance``.
"""

from __future__ import annotations

import re
import unittest
from unittest import mock

from observability_migration.adapters.source.grafana import panels
from observability_migration.adapters.source.grafana.panels import (
    _grok_label_extraction,
    build_native_promql_query,
)
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.runtime_features import (
    PROMQL_VECTOR_MATCHING,
)
from observability_migration.adapters.source.grafana.schema import SchemaResolver

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DISK_METRIC = "metrics.node_disk_written_bytes_total"  # after metric-field prefix


def _otel_live_resolver(*, field_profile="otel"):
    """Resolver seeded with the real OTel prometheusreceiver field layout.

    Mirrors what the engine would have after ``--es-url`` discovery on
    ``metrics-prometheusreceiver.otel-default``.  No network calls.
    """
    resolver = SchemaResolver(
        RulePackConfig(),
        es_url="https://es",
        index_pattern="metrics-prometheusreceiver.otel-default",
        field_profile=field_profile,
    )
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    # Simulate the field_caps for this index:
    #   - 'instance' is absent (no bare field)
    #   - 'service.instance.id' is present (top-level TSDB dimension)
    #   - 'device' is present bare (TSDB dimension)
    #   - 'attributes.device' and 'resource.attributes.service.instance.id' exist
    resolver._field_cache = {
        "service.instance.id": {"keyword": {"type": "keyword", "time_series_dimension": True}},
        "device": {"keyword": {"type": "keyword", "time_series_dimension": True}},
        "attributes.device": {"keyword": {"type": "keyword"}},
        "resource.attributes.service.instance.id": {"keyword": {"type": "keyword"}},
        "resource.attributes.service.name": {"keyword": {"type": "keyword"}},
        "service.name": {"keyword": {"type": "keyword"}},
        "metrics.node_disk_written_bytes_total": {"double": {"type": "double", "time_series_metric": "counter"}},
        "metrics.node_vmstat_oom_kill": {"double": {"type": "double", "time_series_metric": "counter"}},
    }
    # instance co-occurs with the metric under service.instance.id, not bare instance
    resolver._cooccurrence_cache = {
        (_DISK_METRIC, "instance"): False,
        (_DISK_METRIC, "service.instance.id"): True,
        (_DISK_METRIC, "host.name"): False,
        (_DISK_METRIC, "host.ip"): False,
        ("metrics.node_vmstat_oom_kill", "instance"): False,
        ("metrics.node_vmstat_oom_kill", "service.instance.id"): True,
    }
    return resolver


def _prometheus_live_resolver():
    """Resolver for a classic Prometheus flat target.

    Symmetric guard: labels land bare -- ``instance`` stays ``instance``.
    """
    resolver = SchemaResolver(
        RulePackConfig(),
        es_url="https://es",
        index_pattern="metrics-prometheus-*",
        field_profile="otel",
    )
    resolver._discovery_attempted = True
    resolver._discovery_status = "ok"
    resolver._field_cache = {
        "instance": {"keyword": {"type": "keyword"}},
        "device": {"keyword": {"type": "keyword"}},
        "node_disk_written_bytes_total": {"double": {"type": "double", "time_series_metric": "counter"}},
    }
    resolver._cooccurrence_cache = {
        ("node_disk_written_bytes_total", "instance"): True,
        ("node_disk_written_bytes_total", "service.instance.id"): False,
    }
    return resolver


def _offline_resolver():
    """Resolver with no live field-caps (offline / no --es-url)."""
    resolver = SchemaResolver(RulePackConfig(), field_profile="otel")
    return resolver


def _native_rp(resolver=None):
    rp = RulePackConfig()
    rp.native_promql = True
    return rp


def _translate(panel, rp, resolver):
    return panels.translate_panel(
        panel,
        datasource_index="metrics-prometheusreceiver.otel-default",
        esql_index="metrics-prometheusreceiver.otel-default",
        rule_pack=rp,
        resolver=resolver,
    )


def _make_panel(idx, expr, panel_type="timeseries", legend_format=""):
    return {
        "id": idx,
        "type": panel_type,
        "title": f"Panel {idx}",
        "targets": [
            {
                "expr": expr,
                "refId": "A",
                "legendFormat": legend_format,
                "datasource": {"type": "prometheus"},
            }
        ],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "gridPos": {"x": 0, "y": idx * 8, "w": 24, "h": 8},
    }


# ---------------------------------------------------------------------------
# A. _resolve_native_promql_label_fields — unit tests
# ---------------------------------------------------------------------------

class TestResolveLabelFieldsMatcher(unittest.TestCase):
    """Matcher-key rewrites inside {…}."""

    def setUp(self):
        from observability_migration.adapters.source.grafana.panels import (
            _resolve_native_promql_label_fields,
        )
        self._fn = _resolve_native_promql_label_fields

    def test_instance_matcher_rewritten_to_service_instance_id(self):
        resolver = _otel_live_resolver()
        expr = "rate(node_disk_written_bytes_total{instance=~?Node, device!=\"sr0\", device=~?Disk}[5m])"
        result, _renames = self._fn(expr, resolver)
        self.assertIn("`service.instance.id`=~?Node", result)
        self.assertNotIn("{instance=~", result)

    def test_device_stays_bare_because_it_resolves_to_itself(self):
        resolver = _otel_live_resolver()
        expr = "rate(node_disk_written_bytes_total{instance=~?Node, device!=\"sr0\", device=~?Disk}[5m])"
        result, _renames = self._fn(expr, resolver)
        self.assertIn("device!=\"sr0\"", result)
        self.assertIn("device=~?Disk", result)

    def test_param_binding_survives_rewrite_unchanged(self):
        resolver = _otel_live_resolver()
        expr = "rate(node_disk_written_bytes_total{instance=~?Node}[5m])"
        result, _renames = self._fn(expr, resolver)
        self.assertIn("=~?Node", result)

    def test_no_rewrite_when_discovery_offline(self):
        resolver = _offline_resolver()
        expr = "rate(node_disk_written_bytes_total{instance=~?Node, device!=\"sr0\"}[5m])"
        result, _renames = self._fn(expr, resolver)
        self.assertEqual(result, expr)

    def test_no_rewrite_on_prometheus_shaped_target(self):
        """On a Prometheus-shaped target instance stays bare — regression guard."""
        resolver = _prometheus_live_resolver()
        expr = "rate(node_disk_written_bytes_total{instance=~?Node, device!=\"sr0\"}[5m])"
        result, _renames = self._fn(expr, resolver)
        self.assertIn("instance=~?Node", result)
        self.assertNotIn("`service.instance.id`", result)

    def test_label_value_not_rewritten(self):
        """The value of a matcher (after the op) must not be treated as a label."""
        resolver = _otel_live_resolver()
        # Use a label key ('foo') that has no OTel mapping so it stays bare.
        # The VALUE 'device' (right-hand side) must never be rewritten even though
        # 'device' *is* a known label name.  Only the key position is a label.
        expr = "rate(node_disk_written_bytes_total{foo=\"device\"}[5m])"
        result, _renames = self._fn(expr, resolver)
        self.assertIn("foo=\"device\"", result)

    def test_grouping_syntax_inside_label_value_not_rewritten(self):
        """Grouping-like text inside a matcher value is data, not PromQL syntax."""
        resolver = _otel_live_resolver()
        expr = 'node_disk_written_bytes_total{description="by(instance)"}'
        result, renames = self._fn(expr, resolver)
        self.assertEqual(result, expr)
        self.assertEqual(renames, {})

    def test_name_matcher_not_treated_as_label(self):
        """__name__ matcher must not be rewritten as a label field."""
        resolver = _otel_live_resolver()
        expr = "rate(node_disk_written_bytes_total{__name__=\"node_disk_written_bytes_total\"}[5m])"
        result, _renames = self._fn(expr, resolver)
        self.assertIn("__name__=", result)

    def test_no_resolver_no_rewrite(self):
        from observability_migration.adapters.source.grafana.panels import (
            _resolve_native_promql_label_fields,
        )
        expr = "rate(M{instance=~?Node}[5m])"
        result, renames = _resolve_native_promql_label_fields(expr, None)
        self.assertEqual(result, expr)
        self.assertEqual(renames, {})


class TestResolveLabelFieldsGrouping(unittest.TestCase):
    """Grouping-label rewrites in by/without/on/ignoring parens."""

    def setUp(self):
        from observability_migration.adapters.source.grafana.panels import (
            _resolve_native_promql_label_fields,
        )
        self._fn = _resolve_native_promql_label_fields

    def test_by_instance_rewritten_to_service_instance_id(self):
        resolver = _otel_live_resolver()
        expr = "sum by (instance) (rate(node_disk_written_bytes_total[5m]))"
        result, renames = self._fn(expr, resolver)
        self.assertIn("by (`service.instance.id`)", result)
        self.assertEqual(renames, {"service.instance.id": "instance"})

    def test_by_device_stays_bare(self):
        resolver = _otel_live_resolver()
        expr = "sum by (device) (rate(node_disk_written_bytes_total[5m]))"
        result, renames = self._fn(expr, resolver)
        self.assertIn("by (device)", result)
        self.assertNotIn("`", result)
        self.assertEqual(renames, {})

    def test_by_multi_label_partial_rewrite(self):
        resolver = _otel_live_resolver()
        expr = "sum by (instance, device) (rate(node_disk_written_bytes_total[5m]))"
        result, renames = self._fn(expr, resolver)
        self.assertIn("`service.instance.id`", result)
        self.assertIn("device", result)
        self.assertEqual(renames.get("service.instance.id"), "instance")

    def test_rename_map_empty_when_no_grouping_rewrites(self):
        resolver = _otel_live_resolver()
        # No grouping label here — only matcher
        expr = "rate(node_disk_written_bytes_total{instance=~?Node}[5m])"
        _result, renames = self._fn(expr, resolver)
        self.assertEqual(renames, {})

    def test_without_clause_rewritten(self):
        resolver = _otel_live_resolver()
        expr = "sum without (instance) (rate(node_disk_written_bytes_total[5m]))"
        result, _renames = self._fn(expr, resolver)
        self.assertIn("without (`service.instance.id`)", result)


# ---------------------------------------------------------------------------
# B. build_native_promql_query — integration: RENAME pipe
# ---------------------------------------------------------------------------

class TestBuildNativePromqlQueryRenames(unittest.TestCase):
    """RENAME pipe appended when grouping labels were resolved to dotted fields."""

    def test_grouped_by_instance_appends_rename_pipe(self):
        resolver = _otel_live_resolver()
        expr = "sum by (instance) (rate(node_disk_written_bytes_total[5m]))"
        query = build_native_promql_query(
            expr,
            index="metrics-prometheusreceiver.otel-default",
            resolver=resolver,
            adaptive_step=True,
        )
        self.assertIn("| RENAME `service.instance.id` AS instance", query)

    def test_grouped_by_device_no_rename_pipe(self):
        resolver = _otel_live_resolver()
        expr = "sum by (device) (rate(node_disk_written_bytes_total[5m]))"
        query = build_native_promql_query(
            expr,
            index="metrics-prometheusreceiver.otel-default",
            resolver=resolver,
            adaptive_step=True,
        )
        self.assertNotIn("RENAME", query)

    def test_rename_position_before_keep(self):
        resolver = _otel_live_resolver()
        expr = "sum by (instance) (rate(node_disk_written_bytes_total[5m]))"
        query = build_native_promql_query(
            expr,
            index="metrics-prometheusreceiver.otel-default",
            resolver=resolver,
            adaptive_step=True,
        )
        rename_pos = query.find("RENAME")
        keep_pos = query.find("KEEP")
        if keep_pos != -1 and rename_pos != -1:
            self.assertLess(rename_pos, keep_pos)

    def test_prometheus_target_no_rename(self):
        resolver = _prometheus_live_resolver()
        expr = "sum by (instance) (rate(node_disk_written_bytes_total[5m]))"
        query = build_native_promql_query(
            expr,
            index="metrics-prometheus-*",
            resolver=resolver,
            adaptive_step=True,
        )
        self.assertNotIn("RENAME", query)

    def test_offline_resolver_no_rename(self):
        resolver = _offline_resolver()
        expr = "sum by (instance) (rate(node_disk_written_bytes_total[5m]))"
        query = build_native_promql_query(
            expr,
            index="metrics-*",
            resolver=resolver,
            adaptive_step=True,
        )
        self.assertNotIn("RENAME", query)

    def test_matcher_rewrite_in_emitted_query(self):
        """The PROMQL value=(...) clause rewrites the matcher key."""
        resolver = _otel_live_resolver()
        expr = "rate(node_disk_written_bytes_total{instance=~?Node, device!=\"sr0\"}[5m])"
        query = build_native_promql_query(
            expr,
            index="metrics-prometheusreceiver.otel-default",
            resolver=resolver,
            adaptive_step=True,
        )
        self.assertIn("`service.instance.id`=~?Node", query)
        self.assertNotIn("{instance=~", query)
        # device stays bare
        self.assertIn("device!=\"sr0\"", query)


# ---------------------------------------------------------------------------
# C. GROK legend patterns — _grok_label_extraction and _timeseries_json_path
# ---------------------------------------------------------------------------

class TestTimeseriesJsonPath(unittest.TestCase):
    """_timeseries_json_path returns the right storage path for each label."""

    def setUp(self):
        from observability_migration.adapters.source.grafana.panels import (
            _timeseries_json_path,
        )
        self._fn = _timeseries_json_path

    def test_device_returns_attributes_path(self):
        resolver = _otel_live_resolver()
        path = self._fn("device", resolver)
        self.assertEqual(path, ("attributes", "device"))

    def test_instance_returns_resource_attributes_path(self):
        resolver = _otel_live_resolver()
        path = self._fn("instance", resolver)
        self.assertEqual(path, ("resource.attributes", "service.instance.id"))

    def test_offline_resolver_returns_none(self):
        resolver = _offline_resolver()
        path = self._fn("device", resolver)
        self.assertIsNone(path)

    def test_no_resolver_returns_none(self):
        path = self._fn("device", None)
        self.assertIsNone(path)

    def test_label_not_in_otel_layout_returns_none(self):
        """A label with no known nested path falls back to flat pattern."""
        resolver = _otel_live_resolver()
        # 'unknownlabel' has no attributes.unknownlabel or resource.attributes.* entry
        path = self._fn("unknownlabel", resolver)
        self.assertIsNone(path)


class TestGrokLabelExtraction(unittest.TestCase):
    """_grok_label_extraction emits correct patterns for each storage path."""

    # Real OTel _timeseries sample from the live lab
    OTEL_TS = (
        '{"attributes":{"device":"vda"},'
        '"resource":{"attributes":{'
        '"service.name":"node-exporter",'
        '"server.address":"node-exporter",'
        '"service.instance.id":"node-exporter:9100",'
        '"server.port":"9100",'
        '"url.scheme":"http"}},'
        '"scope":{"name":"github.com/otelcol"},'
        '"_metric_names_hash":"d1e2117fd39b59b1"}'
    )

    def _apply_grok(self, pattern_str, timeseries_str):
        """Simulate GROK by replacing %{DATA:name} with (?P<name>.*?).

        Also normalises Java/GROK regex escapes that are invalid in Python 3.12+:
        \\{ and \\} (literal braces in Java regex) and \\" (literal double-quote).
        """
        py_pat = pattern_str.replace(r'\{', '{').replace(r'\}', '}').replace(r'\"', '"')
        py_pat = re.sub(r"%\{DATA:(\w+)\}", r"(?P<\1>.*?)", py_pat)
        return re.search(py_pat, timeseries_str)

    def _extract_grok_pattern(self, grok_pipe):
        """Extract just the regex inside GROK ... \"\"\"...\"\"\""""
        m = re.match(r'\| GROK _timeseries """(.+)"""$', grok_pipe, re.DOTALL)
        self.assertIsNotNone(m, f"Cannot parse GROK pipe: {grok_pipe!r}")
        return m.group(1)

    def test_attributes_device_new_pattern(self):
        """attributes.device path → nested anchor, captures 'device' column."""
        pipe = _grok_label_extraction("device", json_path=("attributes", "device"))
        pat = self._extract_grok_pattern(pipe)
        m = self._apply_grok(pat, self.OTEL_TS)
        self.assertIsNotNone(m, f"Pattern did not match. pattern={pat!r}")
        self.assertEqual(m.group("device"), "vda")

    def test_resource_attributes_instance_new_pattern(self):
        """resource.attributes path → two-level anchor, captures 'instance' column."""
        pipe = _grok_label_extraction("instance", json_path=("resource.attributes", "service.instance.id"))
        pat = self._extract_grok_pattern(pipe)
        m = self._apply_grok(pat, self.OTEL_TS)
        self.assertIsNotNone(m, f"Pattern did not match. pattern={pat!r}")
        self.assertEqual(m.group("instance"), "node-exporter:9100")

    def test_flat_pattern_unchanged_when_no_json_path(self):
        """No json_path → today's flat/labels.* pattern is returned byte-for-byte."""
        pipe_new = _grok_label_extraction("device", json_path=None)
        pipe_today = _grok_label_extraction("device")
        self.assertEqual(pipe_new, pipe_today)

    def test_flat_pattern_does_not_match_otel_ts(self):
        """Today's flat pattern yields NULL on OTel _timeseries — confirms the bug."""
        pipe = _grok_label_extraction("device")
        pat = self._extract_grok_pattern(pipe)
        m = self._apply_grok(pat, self.OTEL_TS)
        self.assertIsNone(m, "Today's pattern should NOT match OTel _timeseries")

    def test_attributes_pattern_does_not_match_resource_attributes(self):
        """attributes.X pattern must not pick up resource.attributes.X instead."""
        # Blob where device only exists inside resource.attributes, not attributes
        ts = '{"resource":{"attributes":{"device":"fake"}},"attributes":{"other":"value"}}'
        pipe = _grok_label_extraction("device", json_path=("attributes", "device"))
        pat = self._extract_grok_pattern(pipe)
        m = self._apply_grok(pat, ts)
        self.assertIsNone(m, "Pattern must not match device inside resource.attributes")

    def test_resource_attributes_pattern_does_not_match_wrong_key(self):
        """resource.attributes pattern for service.instance.id must not bind service.name."""
        pipe = _grok_label_extraction("instance", json_path=("resource.attributes", "service.instance.id"))
        pat = self._extract_grok_pattern(pipe)
        ts = '{"resource":{"attributes":{"service.name":"node-exporter"}}}'
        m = self._apply_grok(pat, ts)
        self.assertIsNone(m, "Pattern must not match service.name when looking for service.instance.id")


# ---------------------------------------------------------------------------
# D. build_native_promql_query — GROK pipes use resolved paths
# ---------------------------------------------------------------------------

class TestBuildNativePromqlQueryGrok(unittest.TestCase):
    """GROK patterns in build_native_promql_query use resolved _timeseries paths."""

    # Real OTel _timeseries sample
    OTEL_TS = (
        '{"attributes":{"device":"vda"},'
        '"resource":{"attributes":{'
        '"service.name":"node-exporter",'
        '"service.instance.id":"node-exporter:9100"}},'
        '"scope":{}}'
    )

    def _apply_grok(self, pattern_str, timeseries_str):
        # Convert Java/GROK regex escapes that are not valid Python regex:
        # \{ and \} are Java escapes for literal braces; in Python 3.12+ they
        # raise PatternError.  \" is a Java escape for literal "; Python regex
        # doesn't recognise it.  Replace all with the unescaped character.
        py_pat = pattern_str.replace(r'\{', '{').replace(r'\}', '}').replace(r'\"', '"')
        py_pat = re.sub(r"%\{DATA:(\w+)\}", r"(?P<\1>.*?)", py_pat)
        return re.search(py_pat, timeseries_str)

    def _extract_grok_patterns(self, query):
        # Use greedy (.+) without re.DOTALL so the match stays on one line —
        # this correctly includes the trailing \" that ends each GROK pattern
        # (the lazy .+? version cuts off before the final " in \").
        return re.findall(r'\| GROK _timeseries """(.+)"""', query)

    def test_device_grok_pattern_matches_otel_timeseries(self):
        """build_native_promql_query emits a GROK pattern that finds device in OTel."""
        resolver = _otel_live_resolver()
        expr = "rate(node_disk_written_bytes_total[5m])"
        query = build_native_promql_query(
            expr,
            index="metrics-prometheusreceiver.otel-default",
            legend_labels=["device"],
            resolver=resolver,
            adaptive_step=True,
        )
        patterns = self._extract_grok_patterns(query)
        self.assertTrue(len(patterns) >= 1, "Expected at least one GROK pipe")
        m = self._apply_grok(patterns[0], self.OTEL_TS)
        self.assertIsNotNone(m)
        self.assertEqual(m.group("device"), "vda")

    def test_instance_grok_pattern_matches_otel_timeseries(self):
        """build_native_promql_query emits a GROK pattern that finds instance in OTel."""
        resolver = _otel_live_resolver()
        expr = "rate(node_disk_written_bytes_total[5m])"
        query = build_native_promql_query(
            expr,
            index="metrics-prometheusreceiver.otel-default",
            legend_labels=["instance"],
            resolver=resolver,
            adaptive_step=True,
        )
        patterns = self._extract_grok_patterns(query)
        self.assertTrue(len(patterns) >= 1)
        m = self._apply_grok(patterns[0], self.OTEL_TS)
        self.assertIsNotNone(m)
        self.assertEqual(m.group("instance"), "node-exporter:9100")

    def test_offline_resolver_emits_flat_grok_pattern(self):
        """Without live caps, falls back to today's flat pattern (no regression)."""
        resolver = _offline_resolver()
        expr = "rate(node_disk_written_bytes_total[5m])"
        query = build_native_promql_query(
            expr,
            index="metrics-*",
            legend_labels=["instance"],
            resolver=resolver,
            adaptive_step=True,
        )
        patterns = self._extract_grok_patterns(query)
        self.assertTrue(len(patterns) >= 1)
        # Today's flat anchor
        self.assertIn(r"\A\{", patterns[0])

    def test_prometheus_target_emits_flat_grok_pattern(self):
        """Prometheus target (bare instance field) keeps flat GROK pattern."""
        resolver = _prometheus_live_resolver()
        expr = "rate(node_disk_written_bytes_total{instance=~?Node}[5m])"
        query = build_native_promql_query(
            expr,
            index="metrics-prometheus-*",
            legend_labels=["instance"],
            resolver=resolver,
            adaptive_step=True,
        )
        patterns = self._extract_grok_patterns(query)
        self.assertTrue(len(patterns) >= 1)
        # Should still use flat/labels pattern (instance exists bare on Prometheus target)
        self.assertIn(r"\A\{", patterns[0])


# ---------------------------------------------------------------------------
# D2. Full panel translation via translate_panel
# ---------------------------------------------------------------------------

class TestPanelTranslationOtelTarget(unittest.TestCase):
    """translate_panel emits correct native PROMQL for an OTel target."""

    def test_disk_panel_matcher_key_rewritten(self):
        """The Write bandwidth panel (9852) emits service.instance.id in native PROMQL."""
        resolver = _otel_live_resolver()
        rp = _native_rp()
        panel = _make_panel(
            1,
            "rate(node_disk_written_bytes_total{instance=~?Node, device!=\"sr0\", device=~?Disk}[5m])",
            legend_format="{{instance}} {{device}}",
        )
        yaml_panel, _result = _translate(panel, rp, resolver)
        query = (yaml_panel.get("esql") or {}).get("query", "")
        self.assertTrue(query.startswith("PROMQL"), f"Expected native PROMQL, got: {query[:100]}")
        self.assertIn("`service.instance.id`=~?Node", query)
        self.assertNotIn("{instance=~", query)

    def test_disk_panel_grok_matches_otel_timeseries(self):
        """The GROK pipes in the emitted panel query find device+instance in OTel format."""
        resolver = _otel_live_resolver()
        rp = _native_rp()
        panel = _make_panel(
            2,
            "rate(node_disk_written_bytes_total[5m])",
            legend_format="{{device}}",
        )
        yaml_panel, _result = _translate(panel, rp, resolver)
        query = (yaml_panel.get("esql") or {}).get("query", "")
        # Use greedy (.+) without DOTALL to correctly capture patterns ending in \"
        grok_patterns = re.findall(r'\| GROK _timeseries """(.+)"""', query)
        self.assertTrue(grok_patterns, "Expected GROK pattern in query")
        otel_ts = (
            '{"attributes":{"device":"vda"},'
            '"resource":{"attributes":{"service.instance.id":"node-exporter:9100"}}}'
        )
        # Normalise Java/GROK regex escapes before using as Python regex
        py_pat = grok_patterns[0].replace(r'\{', '{').replace(r'\}', '}').replace(r'\"', '"')
        py_pat = re.sub(r"%\{DATA:(\w+)\}", r"(?P<\1>.*?)", py_pat)
        m = re.search(py_pat, otel_ts)
        self.assertIsNotNone(m, f"GROK pattern should match OTel _timeseries, pattern={grok_patterns[0]!r}")

    def test_prometheus_panel_unchanged(self):
        """On a Prometheus-shaped target, existing behavior is preserved."""
        resolver = _prometheus_live_resolver()
        rp = _native_rp()
        panel = _make_panel(
            3,
            "rate(node_disk_written_bytes_total{instance=~?Node}[5m])",
        )
        yaml_panel, _result = _translate(panel, rp, resolver)
        query = (yaml_panel.get("esql") or {}).get("query", "")
        if query.startswith("PROMQL"):
            # Prometheus target: instance stays bare
            self.assertIn("instance=~", query)
            self.assertNotIn("`service.instance.id`", query)


# ---------------------------------------------------------------------------
# E. Safety net — decline native when resolved field is absent from live caps
# ---------------------------------------------------------------------------

class TestDeclineNativeOnAbsentResolvedField(unittest.TestCase):
    """When resolve_label returns a field that live caps prove absent, decline native."""

    def _resolver_with_absent_resolved(self):
        """Resolver where instance resolves to service.instance.id but it is absent."""
        resolver = SchemaResolver(
            RulePackConfig(),
            es_url="https://es",
            index_pattern="metrics-weird.otel-default",
            field_profile="otel",
        )
        resolver._discovery_attempted = True
        resolver._discovery_status = "ok"
        resolver._field_cache = {
            # No 'instance', no 'service.instance.id' — both absent
            "device": {"keyword": {"type": "keyword"}},
            "metrics.node_disk_written_bytes_total": {"double": {"type": "double"}},
        }
        resolver._cooccurrence_cache = {}
        return resolver

    def test_panel_degrades_with_skip_note(self):
        """When the resolved matcher field is absent, panel degrades to ES|QL with a note."""
        resolver = self._resolver_with_absent_resolved()
        rp = _native_rp()
        panel = _make_panel(
            1,
            "rate(node_disk_written_bytes_total{instance=~?Node}[5m])",
        )
        yaml_panel, result = _translate(panel, rp, resolver)
        query = (yaml_panel.get("esql") or {}).get("query", "")
        # Should have degraded to ES|QL (not native PROMQL) with a note
        notes_and_reasons = list(result.notes or []) + list(result.reasons or [])
        has_skip_note = any("Native PROMQL skipped" in (n or "") for n in notes_and_reasons)
        self.assertTrue(
            has_skip_note and not query.startswith("PROMQL"),
            f"Expected degrade note or ES|QL fallback. query={query[:80]!r}, notes={result.notes}",
        )

    def test_grouping_only_label_with_absent_resolved_field_degrades(self):
        """A missing resolved group-by field must not silently collapse all series."""
        resolver = self._resolver_with_absent_resolved()
        rp = _native_rp()
        panel = _make_panel(
            2,
            "sum by(instance) (node_disk_written_bytes_total)",
        )
        yaml_panel, result = _translate(panel, rp, resolver)
        query = (yaml_panel.get("esql") or {}).get("query", "")
        notes_and_reasons = list(result.notes or []) + list(result.reasons or [])
        self.assertFalse(query.startswith("PROMQL"), query)
        self.assertTrue(
            any("native matchers/groupings" in (note or "") for note in notes_and_reasons),
            notes_and_reasons,
        )


class TestDeclineNativeOnConflictingMetricScopedLabels(unittest.TestCase):
    """Multi-metric expressions must not pick one metric's label mapping arbitrarily."""

    def _resolver_with_conflicting_scopes(self):
        resolver = SchemaResolver(
            RulePackConfig(),
            es_url="https://es",
            index_pattern="metrics-mixed.otel-default",
            field_profile="otel",
        )
        resolver._discovery_attempted = True
        resolver._discovery_status = "ok"
        resolver._field_cache = {
            "instance": {"keyword": {"type": "keyword"}},
            "service.instance.id": {"keyword": {"type": "keyword"}},
            "host.name": {"keyword": {"type": "keyword"}},
            "metrics.foo_total": {"double": {"type": "double"}},
            "metrics.bar_total": {"double": {"type": "double"}},
        }
        resolver._cooccurrence_cache = {
            ("metrics.foo_total", "instance"): False,
            ("metrics.foo_total", "service.instance.id"): True,
            ("metrics.foo_total", "host.name"): False,
            ("metrics.bar_total", "instance"): False,
            ("metrics.bar_total", "service.instance.id"): False,
            ("metrics.bar_total", "host.name"): True,
        }
        return resolver

    def test_conflicting_group_label_resolution_degrades_to_esql(self):
        resolver = self._resolver_with_conflicting_scopes()
        rp = _native_rp()
        rp.runtime_features = {PROMQL_VECTOR_MATCHING: True}
        expr = (
            'sum by(instance) (foo_total{instance="node-a"}) '
            '/ on(instance) sum by(instance) (bar_total{instance="node-a"})'
        )
        yaml_panel, result = _translate(_make_panel(3, expr), rp, resolver)
        query = (yaml_panel.get("esql") or {}).get("query", "")
        notes_and_reasons = list(result.notes or []) + list(result.reasons or [])
        self.assertFalse(query.startswith("PROMQL"), query)
        self.assertTrue(
            any("different target fields" in (note or "") for note in notes_and_reasons),
            notes_and_reasons,
        )


# ---------------------------------------------------------------------------
# F. Alerts path — core/mapping.py guard
#
# The alert generator calls ``build_native_promql_query`` directly, so it gets
# the label rewrite whether or not it also carries the panel path's gates. The
# dashboard path pre-checks conflicts and absent fields in
# ``_translate_panel_native_promql``; these tests pin the equivalent decisions
# on the rule path, where a wrong query is worse than an empty panel — the rule
# either never fires or fails evaluation.
# ---------------------------------------------------------------------------

_OTEL_INDEX = "metrics-prometheusreceiver.otel-default"


def _unified_rule(expr, *, instant=False, threshold=0.9):
    """A minimal Grafana unified rule whose query is *expr* with a threshold."""
    return {
        "uid": "rule-448",
        "title": "Disk write pressure",
        "ruleGroup": "resource-alerts",
        "folderUID": "folder-1",
        "condition": "C",
        "for": "5m",
        "noDataState": "NoData",
        "execErrState": "Error",
        "isPaused": False,
        "labels": {},
        "annotations": {},
        "data": [
            {
                "refId": "A",
                "datasourceUid": "prometheus",
                "relativeTimeRange": {"from": 300, "to": 0},
                "model": {
                    "expr": expr,
                    "instant": instant,
                    "datasource": {"type": "prometheus"},
                },
            },
            {
                "refId": "C",
                "datasourceUid": "-100",
                "relativeTimeRange": {"from": 0, "to": 0},
                "model": {
                    "type": "threshold",
                    "conditions": [
                        {"evaluator": {"type": "gt", "params": [threshold]}}
                    ],
                },
            },
        ],
    }


def _alert_ir(expr, *, instant=False):
    from observability_migration.adapters.source.grafana.alert_pipeline import (
        build_unified_alert_irs,
    )

    return build_unified_alert_irs(
        {
            "alert_rules": [_unified_rule(expr, instant=instant)],
            "rule_groups": [
                {"folderUid": "folder-1", "title": "resource-alerts", "interval": 300}
            ],
        }
    )[0]


def _alert_query(expr, resolver, *, instant=False, ir=None):
    from observability_migration.core.mapping import _generate_esql_for_alert

    ir = ir if ir is not None else _alert_ir(expr, instant=instant)
    return _generate_esql_for_alert(ir, _OTEL_INDEX, resolver=resolver)


class TestAlertLabelFieldResolution(unittest.TestCase):
    """The rule path resolves label names through the resolver, like panels do."""

    def test_alert_matcher_key_is_resolved(self):
        query = _alert_query(
            'rate(node_disk_written_bytes_total{instance=~"node-exporter:9100"}[5m]) > 0.9',
            _otel_live_resolver(),
        )
        self.assertIn("PROMQL index=", query)
        self.assertIn("`service.instance.id`=~", query)
        self.assertNotIn("{instance=~", query)

    def test_grouped_alert_renames_resolved_column_back(self):
        """A ``by (…)`` rule must carry the RENAME, not just the rewritten key.

        ``kibana_type="metric"`` used to return before the RENAME pipes, so the
        query emitted a ``service.instance.id`` column while every downstream
        reference still said ``instance``.
        """
        query = _alert_query(
            "sum by (instance) (rate(node_disk_written_bytes_total[5m])) > 0.9",
            _otel_live_resolver(),
        )
        self.assertIn("sum by (`service.instance.id`)", query)
        self.assertIn("| RENAME `service.instance.id` AS instance", query)

    def test_exact_topk_alert_reduction_references_an_emitted_column(self):
        """``| STATS … BY instance`` needs the RENAME or it is an unknown column.

        The ``topk``/``bottomk`` rule builds its base query with
        ``kibana_type="metric"`` and then appends a reduction whose ``BY`` list
        comes from the *source* expression, so the two must agree on the column
        name. ``ir.group_by`` is written from the same list and feeds the
        migrated rule's grouping, so it has to match too.
        """
        from observability_migration.core.mapping import (
            _grafana_unified_exact_topk_bottomk_spec,
        )

        expr = "topk(5, sum by (instance) (rate(node_disk_written_bytes_total[5m])))"
        ir = _alert_ir(expr, instant=True)
        spec = _grafana_unified_exact_topk_bottomk_spec(ir)
        self.assertIsNotNone(spec, "fixture must exercise the exact-rank branch")
        self.assertEqual(spec["group_cols"], ["instance"])

        query = _alert_query(expr, _otel_live_resolver(), ir=ir)
        self.assertIn("| STATS value = LAST(value, step) BY instance", query)
        self.assertIn("| RENAME `service.instance.id` AS instance", query)
        # The RENAME has to come before the reduction that consumes the column.
        self.assertLess(
            query.index("| RENAME `service.instance.id` AS instance"),
            query.index("| STATS value = LAST(value, step) BY instance"),
        )
        self.assertEqual(list(ir.group_by), ["instance"])

    def test_offline_alert_keeps_bare_labels(self):
        """No ``--es-url`` means no rewrite — unchanged behavior."""
        query = _alert_query(
            'rate(node_disk_written_bytes_total{instance=~"node-exporter:9100"}[5m]) > 0.9',
            _offline_resolver(),
        )
        self.assertIn("{instance=~", query)
        self.assertNotIn("service.instance.id", query)


class TestAlertDegradesInsteadOfRaising(unittest.TestCase):
    """The rule path owns the same two #448 degrade gates as the panel path.

    Without them a conflicting label set raised out of
    ``build_native_promql_query`` — through ``build_es_query_rule_params`` and
    ``map_alert_to_kibana_payload``, neither of which catches it — and took the
    whole ``map_alerts_batch`` run down with it.
    """

    def _resolver_with_conflicting_scopes(self):
        resolver = SchemaResolver(
            RulePackConfig(),
            es_url="https://es",
            index_pattern="metrics-mixed.otel-default",
            field_profile="otel",
        )
        resolver._discovery_attempted = True
        resolver._discovery_status = "ok"
        resolver._field_cache = {
            "instance": {"keyword": {"type": "keyword"}},
            "service.instance.id": {"keyword": {"type": "keyword"}},
            "host.name": {"keyword": {"type": "keyword"}},
            "metrics.foo_total": {"double": {"type": "double"}},
            "metrics.bar_total": {"double": {"type": "double"}},
        }
        resolver._cooccurrence_cache = {
            ("metrics.foo_total", "instance"): False,
            ("metrics.foo_total", "service.instance.id"): True,
            ("metrics.foo_total", "host.name"): False,
            ("metrics.bar_total", "instance"): False,
            ("metrics.bar_total", "service.instance.id"): False,
            ("metrics.bar_total", "host.name"): True,
        }
        return resolver

    def _resolver_with_absent_resolved(self):
        resolver = SchemaResolver(
            RulePackConfig(),
            es_url="https://es",
            index_pattern="metrics-weird.otel-default",
            field_profile="otel",
        )
        resolver._discovery_attempted = True
        resolver._discovery_status = "ok"
        resolver._field_cache = {
            # Neither ``instance`` nor ``service.instance.id`` exists here.
            "device": {"keyword": {"type": "keyword"}},
            "metrics.node_disk_written_bytes_total": {"double": {"type": "double"}},
        }
        resolver._cooccurrence_cache = {}
        return resolver

    _CONFLICTING_EXPR = (
        'sum by(instance) (foo_total{instance="node-a"}) '
        '/ sum by(instance) (bar_total{instance="node-a"}) > 0.9'
    )

    def test_conflicting_labels_still_raise_from_the_builder(self):
        """Pin the builder contract the rule path has to absorb."""
        with self.assertRaises(ValueError):
            build_native_promql_query(
                self._CONFLICTING_EXPR,
                index="metrics-mixed.otel-default",
                kibana_type="metric",
                resolver=self._resolver_with_conflicting_scopes(),
            )

    def test_conflicting_labels_do_not_propagate_out_of_the_alert_path(self):
        query = _alert_query(
            self._CONFLICTING_EXPR, self._resolver_with_conflicting_scopes()
        )
        self.assertNotIn("PROMQL index=", query)

    def test_conflicting_labels_do_not_abort_the_batch(self):
        """One unmappable rule must not take its whole batch down."""
        from observability_migration.core.mapping import map_alerts_batch

        resolver = self._resolver_with_conflicting_scopes()
        irs = [
            _alert_ir(self._CONFLICTING_EXPR),
            _alert_ir("sum by (instance) (rate(foo_total[5m])) > 0.9"),
        ]
        results = map_alerts_batch(
            irs, data_view="metrics-mixed.otel-default", resolver=resolver
        )
        self.assertEqual(len(results), 2)

    def test_absent_resolved_field_does_not_emit_a_native_matcher(self):
        """A matcher on a provably absent field is a rule that never fires."""
        query = _alert_query(
            'rate(node_disk_written_bytes_total{instance=~"node-a"}[5m]) > 0.9',
            self._resolver_with_absent_resolved(),
        )
        self.assertNotIn("PROMQL index=", query)

    def test_absent_resolved_group_label_does_not_emit_a_native_grouping(self):
        query = _alert_query(
            "sum by (instance) (node_disk_written_bytes_total) > 0.9",
            self._resolver_with_absent_resolved(),
        )
        self.assertNotIn("PROMQL index=", query)


# ---------------------------------------------------------------------------
# G. Prometheus-namespaced profiles — the rewrite must stay off
# ---------------------------------------------------------------------------

class TestNamedPrometheusProfilesKeepBareLabels(unittest.TestCase):
    """``labels.*`` targets are resolved by the PROMQL command itself.

    ``_unresolvable_native_promql_labels`` always skipped these profiles for
    that reason, which also disabled the absent-field safety net there — so if
    the rewrite fires anyway nothing degrades it, and every panel on a
    remote-write / native-endpoint target that worked with bare names emits a
    namespaced matcher key next to a bare metric name instead.
    """

    def _resolver(self, field_profile):
        resolver = SchemaResolver(
            RulePackConfig(),
            es_url="https://es",
            index_pattern="metrics-prometheus-*",
            field_profile=field_profile,
        )
        resolver._discovery_attempted = True
        resolver._discovery_status = "ok"
        resolver._field_cache = {
            "labels.instance": {"keyword": {"type": "keyword"}},
            "labels.device": {"keyword": {"type": "keyword"}},
            "prometheus.labels.instance": {"keyword": {"type": "keyword"}},
            "node_disk_written_bytes_total": {"double": {"type": "double"}},
        }
        resolver._cooccurrence_cache = {}
        return resolver

    def test_resolve_label_does_namespace_on_these_profiles(self):
        """Guard the premise: without a gate the rewrite has something to do."""
        for profile in ("prometheus_native", "prometheus_remote_write", "prometheus_metrics"):
            with self.subTest(profile=profile):
                resolved = self._resolver(profile).resolve_label("instance")
                self.assertNotEqual(resolved, "instance")
                self.assertIn("labels.instance", resolved)

    def test_matcher_keys_stay_bare(self):
        for profile in ("prometheus_native", "prometheus_remote_write", "prometheus_metrics"):
            with self.subTest(profile=profile):
                query = build_native_promql_query(
                    'rate(node_disk_written_bytes_total{instance=~"node-a"}[5m])',
                    index="metrics-prometheus-*",
                    resolver=self._resolver(profile),
                )
                self.assertIn("{instance=~", query)
                self.assertNotIn("labels.instance", query)

    def test_grouping_labels_stay_bare_and_emit_no_rename(self):
        for profile in ("prometheus_native", "prometheus_remote_write", "prometheus_metrics"):
            with self.subTest(profile=profile):
                query = build_native_promql_query(
                    "sum by (instance) (rate(node_disk_written_bytes_total[5m]))",
                    index="metrics-prometheus-*",
                    resolver=self._resolver(profile),
                )
                self.assertIn("sum by (instance)", query)
                self.assertNotIn("RENAME", query)

    def test_otel_profile_on_the_same_caps_still_rewrites(self):
        """Symmetry check: the plan gates the rewrite on its own.

        These caps match no named layout (``labels.*`` but no ``metrics.*``),
        so only the plan can turn the rewrite off — see section K for the caps
        that turn it off under the default ``otel`` plan.
        """
        resolver = self._resolver("prometheus_native")
        resolver._field_profile = "otel"
        resolver._field_cache["service.instance.id"] = {"keyword": {"type": "keyword"}}
        resolver._cooccurrence_cache = {
            ("node_disk_written_bytes_total", "instance"): False,
            ("node_disk_written_bytes_total", "service.instance.id"): True,
        }
        query = build_native_promql_query(
            'rate(node_disk_written_bytes_total{instance=~"node-a"}[5m])',
            index="metrics-prometheus-*",
            resolver=resolver,
        )
        self.assertIn("`service.instance.id`=~", query)


# ---------------------------------------------------------------------------
# H. RENAME scoping — only labels that are really result columns
# ---------------------------------------------------------------------------

class TestRenameMapCoversOnlyResultColumns(unittest.TestCase):
    """``| RENAME <resolved> AS <prom>`` on a non-column is a hard failure.

    Worse than a hard failure, actually: the resulting ``verification_exception``
    is not in ``_NATIVE_PARSE_REJECTION_SIGNALS``, so
    ``_native_promql_query_survives_validation`` reads it as a data/field gap
    and *keeps* the native path — shipping a panel that can never render.
    """

    def _rename_targets(self, query):
        return re.findall(r"\|\s*RENAME\s+(\S+)\s+AS\s+(\S+)", query)

    def test_vector_matching_modifier_label_is_not_renamed(self):
        """``on(instance)`` constrains pairing; it is not an output column."""
        resolver = _otel_live_resolver()
        query = build_native_promql_query(
            "sum by (device) (rate(node_disk_written_bytes_total[5m])) "
            "/ on(instance) group_left sum by (device) (rate(node_vmstat_oom_kill[5m]))",
            index=_OTEL_INDEX,
            runtime_features={PROMQL_VECTOR_MATCHING: True},
            resolver=resolver,
        )
        # The modifier label is still rewritten in the expression — the engine
        # needs the real field name there …
        self.assertIn("on(`service.instance.id`)", query)
        # … but nothing renames a column the result does not carry.
        self.assertNotIn("AS instance", query)

    def test_ignoring_modifier_label_is_not_renamed(self):
        """``ignoring(instance)`` leaves the result on the ``_timeseries`` shape."""
        resolver = _otel_live_resolver()
        expr = (
            "sum by (device, instance) (rate(node_disk_written_bytes_total[5m])) "
            "/ ignoring(instance) sum by (device) (rate(node_vmstat_oom_kill[5m]))"
        )
        _, group_cols = panels._native_promql_result_shape(expr)
        self.assertEqual(group_cols, ["_timeseries"])
        query = build_native_promql_query(
            expr,
            index=_OTEL_INDEX,
            runtime_features={PROMQL_VECTOR_MATCHING: True},
            resolver=resolver,
        )
        self.assertIn("ignoring(`service.instance.id`)", query)
        self.assertNotIn("RENAME", query)

    def test_nested_aggregation_never_reaches_the_builder(self):
        """Why the nested-``by`` shape needs no RENAME handling of its own.

        ``sum(sum by (instance)(…))`` would put a rewritten grouping label in
        ``rename_map`` that the outer aggregation removes from the result, but
        ``can_use_native_promql`` declines nested aggregations first, so the
        builder never sees one. Pin that ordering: if the eligibility gate ever
        admits them, the result-column filter in ``build_native_promql_query``
        is what keeps the RENAME honest.
        """
        from observability_migration.adapters.source.grafana.panels import (
            can_use_native_promql,
        )

        expr = "sum(sum by (instance) (rate(node_disk_written_bytes_total[5m])))"
        self.assertFalse(can_use_native_promql(expr))
        _, group_cols = panels._native_promql_result_shape(expr)
        self.assertNotIn("instance", group_cols)

    def test_top_level_grouping_label_is_still_renamed(self):
        """The case the RENAME exists for keeps working."""
        query = build_native_promql_query(
            "sum by (instance) (rate(node_disk_written_bytes_total[5m]))",
            index=_OTEL_INDEX,
            resolver=_otel_live_resolver(),
        )
        self.assertEqual(
            self._rename_targets(query), [("`service.instance.id`", "instance")]
        )

    def test_every_rename_target_is_a_declared_result_column(self):
        """Invariant sweep across grouping shapes."""
        resolver = _otel_live_resolver()
        exprs = [
            "sum by (instance) (rate(node_disk_written_bytes_total[5m]))",
            "sum by (instance, device) (rate(node_disk_written_bytes_total[5m]))",
            "sum without (instance) (rate(node_disk_written_bytes_total[5m]))",
            "sum by (device) (rate(node_disk_written_bytes_total[5m])) "
            "/ on(instance) group_left sum by (device) (rate(node_vmstat_oom_kill[5m]))",
            "sum by (device, instance) (rate(node_disk_written_bytes_total[5m])) "
            "/ ignoring(instance) sum by (device) (rate(node_vmstat_oom_kill[5m]))",
        ]
        for expr in exprs:
            for kibana_type in (None, "metric"):
                with self.subTest(expr=expr, kibana_type=kibana_type):
                    query = build_native_promql_query(
                        expr,
                        index=_OTEL_INDEX,
                        kibana_type=kibana_type,
                        runtime_features={PROMQL_VECTOR_MATCHING: True},
                        resolver=resolver,
                    )
                    _, group_cols = panels._native_promql_result_shape(expr)
                    for _resolved, prom_label in self._rename_targets(query):
                        self.assertIn(prom_label, group_cols)


# ---------------------------------------------------------------------------
# I. Primary metric scope — source order, not alphabetical order
# ---------------------------------------------------------------------------

class TestPrimaryMetricScopeFollowsSourceOrder(unittest.TestCase):
    """``_metric_fields[0]`` is the scope every label resolves against.

    The AST path already answers in source order. Its ``except Exception``
    fallback reads from ``_metrics_in_expr``, which returns an unordered *set* —
    so the fallback has to impose order itself, and sorting made the scope
    whichever operand happened to sort first rather than the leading one. These
    tests force the fallback, since no expression found so far defeats the AST.
    """

    def _fallback_metrics(self, expr):
        with mock.patch.object(
            panels, "_collect_source_metrics", side_effect=RuntimeError("no AST")
        ):
            return panels._native_promql_source_metrics(expr)

    def test_ast_path_answers_in_source_order(self):
        self.assertEqual(
            panels._native_promql_source_metrics(
                "zzz_last_metric - aaa_first_metric"
            ),
            ["zzz_last_metric", "aaa_first_metric"],
        )

    def test_fallback_orders_metrics_by_first_appearance(self):
        self.assertEqual(
            self._fallback_metrics("zzz_last_metric - aaa_first_metric"),
            ["zzz_last_metric", "aaa_first_metric"],
        )

    def test_fallback_agrees_with_the_ast_path_it_replaces(self):
        expr = "node_zfs_arc_size / node_memory_MemTotal_bytes"
        self.assertEqual(
            self._fallback_metrics(expr),
            panels._native_promql_source_metrics(expr),
        )

    def test_fallback_still_excludes_grouping_labels(self):
        """The scanner cannot tell a label from a metric; the filter must."""
        metrics = self._fallback_metrics(
            "sum by (instance) (rate(node_disk_written_bytes_total[5m]))"
        )
        self.assertNotIn("instance", metrics)
        self.assertIn("node_disk_written_bytes_total", metrics)


# ---------------------------------------------------------------------------
# J. ``_timeseries`` GROK anchor — same gate as the rest of #448
# ---------------------------------------------------------------------------

class TestTimeseriesJsonPathGate(unittest.TestCase):
    """A ``partial`` cache is positive-only, not an authoritative inventory.

    ``field_exists`` answers ``True`` for anything a ``--control-schema`` merge
    seeded, so gating only on ``field_exists`` let a seeded hint flip the GROK
    anchor to the nested form while the label rewrite and both degrade gates
    stayed off. If the real ``_timeseries`` blob is flat, the legend column
    comes back NULL where the flat anchor would have matched.
    """

    def _partial_resolver(self):
        resolver = SchemaResolver(
            RulePackConfig(),
            es_url="https://es",
            index_pattern=_OTEL_INDEX,
            field_profile="otel",
        )
        resolver._discovery_attempted = True
        resolver._discovery_status = "partial"
        resolver._field_cache = {
            "attributes.device": {"keyword": {"type": "keyword"}},
        }
        resolver._cooccurrence_cache = {}
        return resolver

    def test_partial_discovery_keeps_the_flat_anchor(self):
        resolver = self._partial_resolver()
        # Premise: the seeded field does read as present.
        self.assertIs(resolver.field_exists("attributes.device"), True)
        self.assertIsNone(panels._timeseries_json_path("device", resolver))

    def test_partial_discovery_query_uses_the_flat_grok_pattern(self):
        query = build_native_promql_query(
            "rate(node_disk_written_bytes_total[5m])",
            index=_OTEL_INDEX,
            legend_labels=["device"],
            resolver=self._partial_resolver(),
        )
        self.assertNotIn("attributes", query)

    def test_live_discovery_still_uses_the_nested_anchor(self):
        self.assertEqual(
            panels._timeseries_json_path("device", _otel_live_resolver()),
            ("attributes", "device"),
        )

    def test_named_prometheus_profile_keeps_the_flat_anchor(self):
        """``labels.*`` targets have no OTel ``attributes`` blob to anchor to."""
        resolver = SchemaResolver(
            RulePackConfig(),
            es_url="https://es",
            index_pattern="metrics-prometheus-*",
            field_profile="prometheus_native",
        )
        resolver._discovery_attempted = True
        resolver._discovery_status = "ok"
        resolver._field_cache = {
            "attributes.device": {"keyword": {"type": "keyword"}},
        }
        resolver._cooccurrence_cache = {}
        self.assertIsNone(panels._timeseries_json_path("device", resolver))


# ---------------------------------------------------------------------------
# K. Detected named-Prometheus layouts — the gate is the plan *and* the caps
# ---------------------------------------------------------------------------

class TestDetectedNamedPrometheusLayoutKeepsBareLabels(unittest.TestCase):
    """The default ``otel`` plan on ``labels.*`` caps must keep bare labels.

    ``_effective_schema_profile()`` returns ``None`` for the default ``otel``
    plan, so gating on the *planned* profile alone turned the rewrite on for
    targets whose live caps are unmistakably ``labels.*`` / ``metrics.*``.
    ``resolve_label`` then guesses OTel names (``instance`` ->
    ``service.instance.id``), ``field_exists`` proves them absent, and every
    ``instance`` / ``job`` panel falls off the native path — even though
    ``_prefix_native_metric_fields`` (#270) still emits ``metrics.up`` and the
    PROMQL command resolves the bare keys against ``labels.*`` itself. That is
    the exact combination this target supports today, and
    ``_maybe_warn_otel_plan_vs_named_layout`` only *warns* about it.
    """

    _NATIVE_CAPS = {
        "labels.instance": {"keyword": {"type": "keyword"}},
        "labels.job": {"keyword": {"type": "keyword"}},
        "metrics.up": {"double": {"type": "double"}},
    }
    _REMOTE_WRITE_CAPS = {
        "prometheus.labels.instance": {"keyword": {"type": "keyword"}},
        "prometheus.up.counter": {"double": {"type": "double"}},
    }
    _METRICS_CAPS = {
        "prometheus.labels.instance": {"keyword": {"type": "keyword"}},
        "prometheus.metrics.up": {"double": {"type": "double"}},
    }

    def _resolver(self, caps=None, field_profile="otel"):
        resolver = SchemaResolver(
            RulePackConfig(),
            es_url="https://es",
            index_pattern="metrics-prom-native-*",
            field_profile=field_profile,
        )
        resolver._discovery_attempted = True
        resolver._discovery_status = "ok"
        resolver._field_cache = dict(caps or self._NATIVE_CAPS)
        resolver._cooccurrence_cache = {}
        return resolver

    def test_caps_detect_the_named_layout(self):
        """Guard the premise: these caps really are a named Prometheus layout."""
        for expected, caps in (
            ("prometheus_native", self._NATIVE_CAPS),
            ("prometheus_remote_write", self._REMOTE_WRITE_CAPS),
            ("prometheus_metrics", self._METRICS_CAPS),
        ):
            with self.subTest(layout=expected):
                resolver = self._resolver(caps)
                self.assertIsNone(resolver._effective_schema_profile())
                self.assertEqual(resolver._current_schema_profile(), expected)

    def test_gate_is_off_for_every_detected_named_layout(self):
        for caps in (self._NATIVE_CAPS, self._REMOTE_WRITE_CAPS, self._METRICS_CAPS):
            with self.subTest(caps=sorted(caps)):
                self.assertFalse(
                    panels._native_promql_label_rewrite_applies(self._resolver(caps))
                )

    def test_grouped_panel_stays_native_with_bare_labels(self):
        query = build_native_promql_query(
            'sum by (instance) (up{job="api"})',
            index="metrics-prom-native-*",
            resolver=self._resolver(),
        )
        self.assertIn("sum by (instance)", query)
        self.assertIn("{job=", query)
        self.assertIn("metrics.up", query)
        self.assertNotIn("RENAME", query)
        self.assertNotIn("service.instance.id", query)

    def test_absent_field_safety_net_stays_off(self):
        """The degrade that pushed these panels to ES|QL must not fire."""
        self.assertEqual(
            panels._unresolvable_native_promql_labels(
                'sum by (instance) (up{job="api"})', self._resolver()
            ),
            [],
        )

    def test_otel_caps_under_the_otel_plan_still_rewrite(self):
        """Symmetry: caps that match no named layout keep the #448 rewrite on."""
        self.assertTrue(
            panels._native_promql_label_rewrite_applies(_otel_live_resolver())
        )


if __name__ == "__main__":
    unittest.main()
