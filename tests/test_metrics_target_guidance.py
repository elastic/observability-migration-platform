# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Operator guidance for --data-view / --esql-index selection (issue #284).

Covers both timelines:
- migrate-first (no --es-url yet): commit to a planned concrete target
- data-first (--es-url): warn on mixed wildcards / UI-vs-query divergence

and the quiet cases: a target that is already pinned must not produce a banner.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from observability_migration.adapters.source.grafana import cli as grafana_cli
from observability_migration.adapters.source.grafana import metrics_target_guidance as mtg

_PINNED = "metrics-prometheus-default"


class StubResolver:
    """Minimal stand-in for SchemaResolver's guidance-facing surface."""

    def __init__(self, streams=(), error="", conflicts=()):
        self._streams = list(streams)
        self._error = error
        self._conflicts = list(conflicts)

    def concrete_index_candidates(self):
        return list(self._streams)

    def concrete_index_error(self):
        return self._error

    def tsdb_conflict_fields(self):
        return list(self._conflicts)


class WildcardDetectionTests(unittest.TestCase):
    def test_detects_every_wildcard_token(self):
        self.assertTrue(mtg.is_wildcard_index("metrics-*"))
        self.assertTrue(mtg.is_wildcard_index("metrics-prod-?"))
        self.assertTrue(mtg.is_wildcard_index("metrics-a,metrics-b"))

    def test_concrete_and_empty_targets_are_not_wildcards(self):
        self.assertFalse(mtg.is_wildcard_index(_PINNED))
        self.assertFalse(mtg.is_wildcard_index(""))
        self.assertFalse(mtg.is_wildcard_index(None))


class BackendFamilyTests(unittest.TestCase):
    def test_classifies_common_metric_stream_families(self):
        self.assertEqual(mtg.backend_family("metrics-prometheus-default"), "prometheus")
        self.assertEqual(mtg.backend_family("metrics-alloy.prometheus-default"), "prometheus")
        self.assertEqual(mtg.backend_family("metrics-datadog-default"), "datadog")
        self.assertEqual(mtg.backend_family("metrics-otel-default"), "otel")
        self.assertEqual(mtg.backend_family("metrics-generic-default"), "generic")
        self.assertEqual(mtg.backend_family("metrics-foo-bar"), "other")


class MigrateFirstTests(unittest.TestCase):
    def test_wildcard_without_es_url_warns_planned_target(self):
        guidance = mtg.assess_metrics_target(
            data_view="metrics-*",
            esql_index="",
            es_url="",
            concrete_streams=None,
        )
        text = "\n".join(guidance.warnings)
        self.assertIn("migrate-first", text.lower())
        self.assertIn("metrics-*", text)
        self.assertIn("--es-url", text)
        self.assertIn("concrete", text.lower())

    def test_pinned_concrete_target_without_es_url_is_silent(self):
        """A run that already did the right thing must not get a banner.

        The generic "no --es-url so fields are unverified" case is already
        covered by the field-discovery warning; duplicating it here trains
        operators to skim past both.
        """
        guidance = mtg.assess_metrics_target(
            data_view=_PINNED,
            esql_index=_PINNED,
            es_url="",
        )
        self.assertEqual(guidance.warnings, [])
        self.assertEqual(guidance.notes, [])
        self.assertEqual(guidance.messages, [])


class DivergentFlagTests(unittest.TestCase):
    def test_narrower_query_target_is_a_note_not_a_warning(self):
        """`--data-view metrics-*` + a concrete `--esql-index` is the pattern
        docs/command-contract.md recommends, so it must not raise a banner."""
        guidance = mtg.assess_metrics_target(
            data_view="metrics-*",
            esql_index=_PINNED,
            es_url="https://es.example",
            concrete_streams=[_PINNED],
        )
        self.assertEqual(guidance.warnings, [])
        note = "\n".join(guidance.notes)
        self.assertIn(_PINNED, note)
        self.assertIn("metrics-*", note)
        self.assertRegex(note.lower(), r"data view|ui")

    def test_query_broader_than_ui_bind_warns(self):
        guidance = mtg.assess_metrics_target(
            data_view=_PINNED,
            esql_index="metrics-*",
            es_url="",
        )
        text = "\n".join(guidance.warnings)
        self.assertIn(_PINNED, text)
        self.assertIn("metrics-*", text)
        self.assertRegex(text.lower(), r"broader|spans")


class DataFirstWildcardTests(unittest.TestCase):
    def _warnings(self, streams, **kwargs):
        guidance = mtg.assess_metrics_target(
            data_view="metrics-*",
            esql_index="metrics-*",
            es_url="https://es.example",
            concrete_streams=streams,
            **kwargs,
        )
        return "\n".join(guidance.warnings)

    def test_mixed_backends_recommend_one_concrete_stream(self):
        text = self._warnings([_PINNED, "metrics-datadog-default", "metrics-otel-default"])
        self.assertIn(_PINNED, text)
        self.assertIn("metrics-datadog-default", text)
        self.assertRegex(text.lower(), r"mixed|multiple")
        self.assertIn("--esql-index", text)
        self.assertIn("--data-view", text)

    def test_single_concrete_stream_still_suggests_pinning(self):
        text = self._warnings([_PINNED])
        self.assertIn(_PINNED, text)
        self.assertRegex(text.lower(), r"pin|set both|concrete")

    def test_several_streams_of_one_family_still_warn(self):
        """A wildcard spanning two Prometheus streams is still a wildcard.

        Keying the warning off backend-family diversity alone left this silent
        while a *single*-stream wildcard got advice.
        """
        text = self._warnings([_PINNED, "metrics-prometheus-synthetic"])
        self.assertIn(_PINNED, text)
        self.assertIn("metrics-prometheus-synthetic", text)
        self.assertRegex(text.lower(), r"pin|spans")

    def test_several_unclassified_streams_still_warn(self):
        text = self._warnings(["metrics-kubernetes-default", "metrics-system-default"])
        self.assertIn("metrics-kubernetes-default", text)
        self.assertRegex(text.lower(), r"pin|spans")

    def test_empty_target_is_reported_as_empty(self):
        text = self._warnings([])
        self.assertRegex(text.lower(), r"no concrete data streams|has no concrete")
        self.assertNotIn("Could not list", text)

    def test_unreadable_target_is_not_reported_as_empty(self):
        """A 403 on _resolve/index must not be stated as "the target is empty"."""
        text = self._warnings([], stream_discovery_error="_resolve/index returned HTTP 403")
        self.assertIn("HTTP 403", text)
        self.assertRegex(text.lower(), r"could not list|unverified")
        self.assertNotRegex(text.lower(), r"has no concrete data streams")

    def test_pinned_concrete_target_emits_nothing(self):
        guidance = mtg.assess_metrics_target(
            data_view=_PINNED,
            esql_index=_PINNED,
            es_url="https://es.example",
            concrete_streams=[_PINNED],
        )
        self.assertEqual(guidance.messages, [])

    def test_tsdb_conflict_fields_warn_as_index_readiness(self):
        text = self._warnings(
            [_PINNED, "metrics-datadog-default"],
            tsdb_conflict_fields=["host.name", "service"],
        )
        self.assertIn("host.name", text)
        self.assertRegex(text.lower(), r"tsdb|dimension|metric|readiness|index")


class PrintAndCliWiringTests(unittest.TestCase):
    def _render(self, guidance):
        buf = io.StringIO()
        with redirect_stdout(buf):
            mtg.print_metrics_target_guidance(guidance)
        return buf.getvalue()

    def test_warnings_get_a_banner(self):
        out = self._render(
            mtg.MetricsTargetGuidance(
                query_index="metrics-*",
                data_view="metrics-*",
                warnings=["Pin --esql-index to a concrete stream."],
            )
        )
        self.assertIn("WARNING", out)
        self.assertIn("metrics target", out.lower())
        self.assertIn("Pin --esql-index", out)

    def test_notes_print_without_a_warning_banner(self):
        out = self._render(
            mtg.MetricsTargetGuidance(
                query_index=_PINNED,
                data_view="metrics-*",
                notes=["Queries read --esql-index."],
            )
        )
        self.assertIn("Queries read --esql-index.", out)
        self.assertNotIn("WARNING", out)

    def test_empty_guidance_prints_nothing(self):
        out = self._render(
            mtg.MetricsTargetGuidance(query_index=_PINNED, data_view=_PINNED)
        )
        self.assertEqual(out, "")

    def test_cli_helper_uses_resolver_candidates_when_es_url_set(self):
        args = SimpleNamespace(data_view="metrics-*", esql_index="", es_url="https://es.example")
        resolver = StubResolver(streams=[_PINNED, "metrics-datadog-default"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            grafana_cli._print_metrics_target_operator_guidance(args, resolver)
        out = buf.getvalue()
        self.assertIn(_PINNED, out)
        self.assertIn("metrics-datadog-default", out)

    def test_cli_helper_surfaces_stream_discovery_failure(self):
        args = SimpleNamespace(data_view="metrics-*", esql_index="", es_url="https://es.example")
        resolver = StubResolver(error="_resolve/index returned HTTP 403")
        buf = io.StringIO()
        with redirect_stdout(buf):
            grafana_cli._print_metrics_target_operator_guidance(args, resolver)
        self.assertIn("HTTP 403", buf.getvalue())


class OfflineMigrateStdoutTests(unittest.TestCase):
    _DASHBOARD = {
        "uid": "mtg-offline",
        "title": "Migrate First Fixture",
        "panels": [
            {
                "id": 1,
                "type": "timeseries",
                "title": "HTTP",
                "targets": [{"refId": "A", "expr": "sum(rate(http_requests_total[5m]))"}],
                "fieldConfig": {"defaults": {}, "overrides": []},
                "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
            }
        ],
        "templating": {"list": []},
        "time": {"from": "now-1h", "to": "now"},
    }

    def _migrate(self, *, data_view, esql_index=""):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            in_dir = root / "in"
            in_dir.mkdir()
            (in_dir / "d.json").write_text(json.dumps(self._DASHBOARD), encoding="utf-8")
            argv = [
                "grafana-migrate",
                "--source", "files",
                "--input-dir", str(in_dir),
                "--output-dir", str(root / "out"),
                "--assets", "dashboards",
                "--data-view", data_view,
                "--translation-mode", "native",
                "--field-profile", "otel",
            ]
            if esql_index:
                argv += ["--esql-index", esql_index]
            buf = io.StringIO()
            original = list(sys.argv)
            try:
                sys.argv = argv
                with redirect_stdout(buf):
                    grafana_cli.main()
            finally:
                sys.argv = original
            return buf.getvalue()

    def test_wildcard_target_prints_migrate_first_warning(self):
        out = self._migrate(data_view="metrics-*")
        self.assertIn("metrics target / data-plane readiness", out)
        self.assertIn("migrate-first", out.lower())
        self.assertIn("metrics-*", out)

    def test_pinned_target_does_not_print_a_second_readiness_banner(self):
        """Only the pre-existing field-discovery warning should fire here."""
        out = self._migrate(data_view=_PINNED, esql_index=_PINNED)
        self.assertNotIn("metrics target / data-plane readiness", out)
        # The unrelated, correctly-gated offline warning still does its job.
        self.assertIn("migrated panels may render empty", out)
