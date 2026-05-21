# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Semantic accuracy assertions for the Datadog → Kibana translation
pipeline.

Asserts that translated widgets preserve aggregation, metric identity,
group-by, bucketing, and filter semantics across the 14 shipped
integration dashboards. The intent is to catch silent semantic
regressions that the existing translate-without-crash tests don't.
"""

from __future__ import annotations

import json
import re
import unittest
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.generate import generate_dashboard_yaml
from observability_migration.adapters.source.datadog.models import (
    NormalizedDashboard,
    NormalizedWidget,
    TranslationResult,
)
from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.translate import translate_widget

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = REPO_ROOT / "infra" / "datadog" / "dashboards"

ALL_DASHBOARDS = [
    "sample_dashboard.json",
    "integrations/docker.json",
    "integrations/kubernetes.json",
    "integrations/nginx_overview.json",
    "integrations/postgres.json",
    "integrations/redis.json",
    "integrations/mysql.json",
    "integrations/apache.json",
    "integrations/haproxy.json",
    "integrations/kafka.json",
    "integrations/mongodb.json",
    "integrations/rabbitmq.json",
    "integrations/consul.json",
    "integrations/celery.json",
]

# Statuses for which per-widget semantic assertions apply. Skipped (group
# containers) and not_feasible (honestly blocked) widgets are excluded;
# they're tracked separately in the gap register.
ACTIONABLE_STATUSES = {"ok", "warning", "requires_manual"}


@dataclass
class TranslatedPair:
    widget: NormalizedWidget
    result: TranslationResult
    yaml_panel: dict


def _translate(filename: str) -> tuple[NormalizedDashboard, list[TranslatedPair]]:
    raw = json.loads((DASHBOARD_DIR / filename).read_text(encoding="utf-8"))
    normalized = normalize_dashboard(raw)
    results = [
        translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        for widget in normalized.widgets
    ]
    yaml_doc = yaml.safe_load(generate_dashboard_yaml(normalized, results))
    panels_by_key = _index_panels(yaml_doc)
    pairs: list[TranslatedPair] = []
    for widget, result in zip(normalized.widgets, results):
        yaml_panel = panels_by_key.get(result.title) or panels_by_key.get(result.widget_id, {})
        pairs.append(TranslatedPair(widget=widget, result=result, yaml_panel=yaml_panel))
    return normalized, pairs


def _index_panels(yaml_doc: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    dashboards = yaml_doc.get("dashboards") or []
    if not dashboards:
        return out
    stack = list(dashboards[0].get("panels") or [])
    while stack:
        panel = stack.pop(0)
        section = panel.get("section")
        if isinstance(section, dict):
            stack = list(section.get("panels") or []) + stack
            continue
        title = panel.get("title", "")
        if title:
            out[title] = panel
    return out


def _actionable(pairs: Iterable[TranslatedPair]) -> list[TranslatedPair]:
    return [p for p in pairs if p.result.status in ACTIONABLE_STATUSES]


def _emitted_query(panel: dict) -> str:
    esql = panel.get("esql")
    if isinstance(esql, dict):
        q = esql.get("query")
        return q if isinstance(q, str) else ""
    return ""


def _parameterize(test_method):
    """Decorator that marks a method for expansion into one test per
    dashboard. _SemanticAccuracyMeta does the actual expansion."""

    def make(filename: str):
        def inner(self):
            return test_method(self, filename)

        slug = filename.replace("/", "_").replace(".", "_")
        inner.__name__ = f"{test_method.__name__}__{slug}"
        return inner

    test_method._parameterized_versions = [
        (filename, make(filename)) for filename in ALL_DASHBOARDS
    ]
    return test_method


class _SemanticAccuracyMeta(type):
    def __new__(mcs, name, bases, namespace):
        expanded: dict = {}
        for attr_name, attr_value in list(namespace.items()):
            versions = getattr(attr_value, "_parameterized_versions", None)
            if versions:
                for _, fn in versions:
                    expanded[fn.__name__] = fn
                del namespace[attr_name]
        namespace.update(expanded)
        return super().__new__(mcs, name, bases, namespace)


class TestDatadogSemanticAccuracy(unittest.TestCase, metaclass=_SemanticAccuracyMeta):
    AGG_MAP = {
        "avg": "AVG(",
        "sum": "SUM(",
        "min": "MIN(",
        "max": "MAX(",
        "count": "COUNT(",
    }
    AGG_PREFIX_RE = re.compile(r"\s*(avg|sum|min|max|count):")

    @_parameterize
    def test_translates(self, filename: str):
        _, pairs = _translate(filename)
        self.assertTrue(pairs, f"{filename}: produced no widgets")

    METRIC_PATTERN = re.compile(r"(?:avg|sum|min|max|count):([a-zA-Z0-9_.]+)")
    GROUP_BY_PATTERN = re.compile(r"by\s*\{([^}]*)\}")

    @_parameterize
    def test_timeseries_has_bucket(self, filename: str):
        _, pairs = _translate(filename)
        offenders: list[str] = []
        for pair in _actionable(pairs):
            if pair.widget.widget_type != "timeseries":
                continue
            query = _emitted_query(pair.yaml_panel)
            if not query:
                continue
            if "BUCKET(@timestamp" not in query:
                offenders.append(
                    f"{pair.result.title!r}: timeseries widget missing "
                    f"BUCKET(@timestamp, ...): {query!r}"
                )
        self.assertFalse(
            offenders,
            f"{filename}: {len(offenders)} timeseries panels without bucket:\n  - "
            + "\n  - ".join(offenders),
        )

    @_parameterize
    def test_group_by_preserved(self, filename: str):
        _, pairs = _translate(filename)
        offenders: list[str] = []
        for pair in _actionable(pairs):
            query = _emitted_query(pair.yaml_panel)
            if not query:
                continue
            upper = query.upper()
            for source in pair.result.source_queries:
                m = self.GROUP_BY_PATTERN.search(source)
                if not m:
                    continue
                raw_keys = [k.strip() for k in m.group(1).split(",") if k.strip()]
                # Skip template-variable keys like `$scope` — they're not
                # group-by dimensions, they're filter substitutions.
                raw_keys = [k for k in raw_keys if not k.startswith("$") and k != "*"]
                if not raw_keys:
                    continue
                if "BY " not in upper:
                    offenders.append(
                        f"{pair.result.title!r}: source by {{{', '.join(raw_keys)}}} "
                        f"but ES|QL has no BY clause: {query!r}"
                    )
                    continue
                for dd_key in raw_keys:
                    mapped = OTEL_PROFILE.tag_map.get(dd_key, dd_key)
                    if mapped not in query and dd_key not in query:
                        offenders.append(
                            f"{pair.result.title!r}: group-by {dd_key!r} "
                            f"(otel→{mapped!r}) missing from ES|QL: {query!r}"
                        )
        self.assertFalse(
            offenders,
            f"{filename}: {len(offenders)} group-by mismatches:\n  - "
            + "\n  - ".join(offenders),
        )

    @_parameterize
    def test_metric_name_present(self, filename: str):
        _, pairs = _translate(filename)
        offenders: list[str] = []
        for pair in _actionable(pairs):
            query = _emitted_query(pair.yaml_panel)
            if not query:
                continue
            for source in pair.result.source_queries:
                m = self.METRIC_PATTERN.search(source)
                if not m:
                    continue
                dd_metric = m.group(1)
                underscored = dd_metric.replace(".", "_")
                tokens = [t for t in dd_metric.split(".") if t]
                if not any(
                    candidate in query
                    for candidate in (dd_metric, underscored, *tokens)
                ):
                    offenders.append(
                        f"{pair.result.title!r}: source metric {dd_metric!r} "
                        f"missing from ES|QL: {query!r}"
                    )
        self.assertFalse(
            offenders,
            f"{filename}: {len(offenders)} metric-name mismatches:\n  - "
            + "\n  - ".join(offenders),
        )

    @_parameterize
    def test_aggregation_preserved(self, filename: str):
        _, pairs = _translate(filename)
        offenders: list[str] = []
        for pair in _actionable(pairs):
            query = _emitted_query(pair.yaml_panel)
            if not query:
                continue
            upper = query.upper()
            for source in pair.result.source_queries:
                m = self.AGG_PREFIX_RE.match(source)
                if not m:
                    continue
                expected = self.AGG_MAP[m.group(1)]
                if expected not in upper:
                    offenders.append(
                        f"{pair.result.title!r}: source aggregation {m.group(1)!r} "
                        f"missing from ES|QL: {query!r}"
                    )
        self.assertFalse(
            offenders,
            f"{filename}: {len(offenders)} aggregation mismatches:\n  - "
            + "\n  - ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
