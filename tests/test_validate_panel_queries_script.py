# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import contextlib
import importlib.util
import io
import os
import pathlib
import tempfile
import unittest
from unittest import mock


def _load_validate_panel_queries():
    script_path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "validate_panel_queries.py"
    spec = importlib.util.spec_from_file_location("validate_panel_queries_script", script_path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        os.environ,
        {
            "ELASTICSEARCH_ENDPOINT": "http://localhost:9200",
            "KEY": "dummy",
        },
        clear=False,
    ):
        spec.loader.exec_module(module)
    return module


validate_panel_queries = _load_validate_panel_queries()


class ValidatePanelQueriesScriptTests(unittest.TestCase):
    def test_extract_query_fields_handles_rlike_without_fake_field(self):
        index_pattern, fields = validate_panel_queries._extract_query_fields(
            'FROM metrics-prometheus-*\n'
            '| WHERE NOT (device RLIKE "rootfs")\n'
            '| STATS value = AVG(node_filesystem_avail_bytes)'
        )

        self.assertEqual(index_pattern, "metrics-prometheus-*")
        self.assertIn("device", fields)
        self.assertIn("node_filesystem_avail_bytes", fields)
        self.assertNotIn("R", fields)

    def test_extract_query_fields_ignores_derived_aliases(self):
        index_pattern, fields = validate_panel_queries._extract_query_fields(
            "FROM metrics-prometheus-*\n"
            "| STATS inner_val = COUNT(node_cpu_seconds_total) BY cpu\n"
            "| STATS node_cpu_seconds_total_count = COUNT(inner_val)"
        )

        self.assertEqual(index_pattern, "metrics-prometheus-*")
        self.assertIn("node_cpu_seconds_total", fields)
        self.assertIn("cpu", fields)
        self.assertNotIn("inner_val", fields)
        self.assertNotIn("node_cpu_seconds_total_count", fields)

    def test_phase2_validate_executes_native_promql_queries(self):
        query = 'PROMQL index=metrics-prometheus-* step=1m value=(sum(rate(process_cpu_seconds_total[5m])))'

        with mock.patch.object(
            validate_panel_queries,
            "_es_request",
            return_value={"columns": [], "values": []},
        ) as es_request:
            status, detail, rows = validate_panel_queries.phase2_validate(query)

        self.assertEqual(status, "OK")
        self.assertEqual(detail, "valid")
        self.assertEqual(rows, 0)
        es_request.assert_called_once_with("POST", "/_query", {"query": query})


class EmptyCorpusIsFatalTests(unittest.TestCase):
    """An empty corpus must never print VALIDATION PASSED.

    ``broken_pct`` used to be ``0`` whenever the denominator was zero, and
    ``0 > MAX_BROKEN_PCT`` is false, so a corpus with no panels reported a
    clean pass and exited 0.
    """

    def test_broken_percentage_rejects_zero_denominator(self):
        with self.assertRaises(validate_panel_queries.EmptyCorpusError) as ctx:
            validate_panel_queries.broken_percentage(0, 0, 0)
        self.assertIn("0 panels reached a verdict", str(ctx.exception))

    def test_broken_percentage_computes_normally(self):
        self.assertEqual(validate_panel_queries.broken_percentage(8, 2, 0), 20.0)

    def test_main_fails_on_directory_without_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = validate_panel_queries.main(
                    [tmp, "--es-endpoint", "http://localhost:9200", "--api-key", "k"]
                )
            output = buf.getvalue()

        self.assertEqual(rc, 1)
        self.assertNotIn("VALIDATION PASSED", output)
        self.assertIn("no panel queries discovered", output)
        # The message must name the directory it searched.
        self.assertIn(tmp, output)

    def test_main_fails_when_yaml_has_no_queryable_panels(self):
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, "dash.yaml").write_text(
                "dashboards:\n"
                "  - name: Empty\n"
                "    panels:\n"
                "      - markdown:\n"
                "          content: nothing to validate\n",
                encoding="utf-8",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = validate_panel_queries.main(
                    [tmp, "--es-endpoint", "http://localhost:9200", "--api-key", "k"]
                )
            output = buf.getvalue()

        self.assertEqual(rc, 1)
        self.assertNotIn("VALIDATION PASSED", output)
        self.assertIn("no panel queries discovered", output)


if __name__ == "__main__":
    unittest.main()
