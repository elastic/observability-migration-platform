# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from observability_migration.adapters.source.grafana.local_ai import (  # noqa: E402
    parse_json_response_content,
    render_json_user_message,
    resolve_task_model,
)


class LocalAITests(unittest.TestCase):
    def test_parse_json_response_content_strips_think_and_fences(self):
        content = '<think>reasoning</think>\n```json\n{"summary":"ok","suggested_checks":[],"notes":[]}\n```'
        parsed = parse_json_response_content(content)
        self.assertEqual(parsed["summary"], "ok")
        self.assertEqual(parsed["suggested_checks"], [])

    def test_parse_json_response_content_extracts_object_from_extra_text(self):
        content = 'Here is the result:\n{"dashboard_title":"Node Exporter","panel_titles":{},"control_labels":{},"notes":[]}\nDone.'
        parsed = parse_json_response_content(content)
        self.assertEqual(parsed["dashboard_title"], "Node Exporter")

    def test_render_json_user_message_adds_no_think_for_qwen(self):
        message = render_json_user_message({"foo": "bar"}, "qwen3:30b")
        self.assertTrue(message.startswith("/no_think\n"))
        self.assertIn('"foo":"bar"', message)

    def test_resolve_task_model_prefers_lighter_polish_model(self):
        with mock.patch("observability_migration.adapters.source.grafana.local_ai.available_ollama_models", return_value={"qwen3.5:35b", "qwen3.5:27b"}):
            resolved = resolve_task_model("polish", "http://localhost:11434/v1", "qwen3.5:35b")
        self.assertEqual(resolved, "qwen3.5:27b")


if __name__ == "__main__":
    unittest.main()
