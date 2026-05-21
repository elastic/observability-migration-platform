# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Smoke test: the 8 newly-added DD integration dashboards normalize,
plan, translate, and generate YAML without raising.

Acts as the floor for the semantic accuracy suite — if a fixture
crashes here, the accuracy suite has nothing to assert against.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.generate import generate_dashboard_yaml
from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
from observability_migration.adapters.source.datadog.planner import plan_widget
from observability_migration.adapters.source.datadog.translate import translate_widget

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = REPO_ROOT / "infra" / "datadog" / "dashboards" / "integrations"


class TestNewFixturesSmoke(unittest.TestCase):
    def _run(self, filename: str) -> dict:
        raw = json.loads((DASHBOARD_DIR / filename).read_text(encoding="utf-8"))
        normalized = normalize_dashboard(raw)
        results = [
            translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
            for widget in normalized.widgets
        ]
        yaml_str = generate_dashboard_yaml(normalized, results)
        return yaml.safe_load(yaml_str)

    def test_mysql_translates(self):
        doc = self._run("mysql.json")
        self.assertIn("dashboards", doc)

    def test_apache_translates(self):
        doc = self._run("apache.json")
        self.assertIn("dashboards", doc)

    def test_haproxy_translates(self):
        doc = self._run("haproxy.json")
        self.assertIn("dashboards", doc)

    def test_kafka_translates(self):
        doc = self._run("kafka.json")
        self.assertIn("dashboards", doc)

    def test_mongodb_translates(self):
        doc = self._run("mongodb.json")
        self.assertIn("dashboards", doc)

    def test_rabbitmq_translates(self):
        doc = self._run("rabbitmq.json")
        self.assertIn("dashboards", doc)

    def test_consul_translates(self):
        doc = self._run("consul.json")
        self.assertIn("dashboards", doc)

    def test_celery_translates(self):
        doc = self._run("celery.json")
        self.assertIn("dashboards", doc)


if __name__ == "__main__":
    unittest.main()
