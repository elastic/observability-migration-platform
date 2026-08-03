# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import yaml
from conftest import ir_fixture_dir

from scripts import setup_telemetry_data


class _FakeSummary:
    def __init__(self, *, ok=5, errors=0, docs_per_stream=None, error_samples=None):
        self.ok = ok
        self.errors = errors
        self.docs_per_stream = docs_per_stream if docs_per_stream is not None else {"logs-generic-default": ok}
        self.error_samples = error_samples if error_samples is not None else []


class SetupTelemetryDataScriptTests(unittest.TestCase):
    def test_main_builds_contract_and_ingests_with_generic_engine(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            ir_dir = ir_fixture_dir(artifact_dir)
            (ir_dir / "dash.yaml").write_text(
                yaml.safe_dump(
                    {
                        "dashboards": [
                            {
                                "panels": [
                                    {
                                        "esql": {
                                            "query": (
                                                "FROM logs-*\n"
                                                "| WHERE log.level == \"error\"\n"
                                                "| STATS count = COUNT(*) BY service.name"
                                            )
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            calls = []

            def fake_request(method, path, body=None, content_type="application/json"):
                calls.append((method, path, body, content_type))
                if path == "/_bulk":
                    docs = [line for line in body.decode().splitlines() if line.startswith('{"create"')]
                    return {"items": [{"create": {}} for _ in docs]}
                return {"acknowledged": True}

            with mock.patch.object(setup_telemetry_data, "make_es_request", return_value=fake_request):
                exit_code = setup_telemetry_data.main(
                    [
                        str(artifact_dir),
                        "--es-endpoint",
                        "https://example.invalid",
                        "--api-key",
                        "secret",
                        "--data-hours",
                        "1",
                        "--interval-sec",
                        "3600",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertTrue(any(path.startswith("/_index_template/telemetry-data-") for _, path, _, _ in calls))
        self.assertTrue(any(path == "/_bulk" for _, path, _, _ in calls))

    def test_main_no_recreate_only_ingests_and_skips_template_mutations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            ir_dir = ir_fixture_dir(artifact_dir)
            (ir_dir / "dash.yaml").write_text(
                yaml.safe_dump(
                    {
                        "dashboards": [
                            {
                                "panels": [
                                    {
                                        "esql": {
                                            "query": (
                                                "FROM logs-*\n"
                                                "| WHERE log.level == \"error\"\n"
                                                "| STATS count = COUNT(*) BY service.name"
                                            )
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            calls: list[tuple[str, str]] = []

            def fake_request(method, path, body=None, content_type="application/json"):
                calls.append((method, path))
                if path == "/_bulk":
                    docs = [line for line in body.decode().splitlines() if line.startswith('{"create"')]
                    return {"items": [{"create": {}} for _ in docs]}
                return {"acknowledged": True}

            with mock.patch.object(setup_telemetry_data, "make_es_request", return_value=fake_request):
                exit_code = setup_telemetry_data.main(
                    [
                        str(artifact_dir),
                        "--es-endpoint",
                        "https://example.invalid",
                        "--api-key",
                        "secret",
                        "--no-recreate",
                        "--data-hours",
                        "1",
                        "--interval-sec",
                        "3600",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertFalse(any(method != "POST" or path != "/_bulk" for method, path in calls))

    def test_main_respects_max_combinations_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            artifact_dir.mkdir()

            captured = {}

            def fake_seed(artifact_dirs, request, **kwargs):
                captured.update(kwargs)
                return _FakeSummary()

            with (
                mock.patch.object(setup_telemetry_data, "make_es_request", return_value="REQ"),
                mock.patch.object(setup_telemetry_data, "seed_sample_data", side_effect=fake_seed),
            ):
                exit_code = setup_telemetry_data.main(
                    [
                        str(artifact_dir),
                        "--es-endpoint",
                        "https://example.invalid",
                        "--api-key",
                        "secret",
                        "--max-combinations",
                        "3",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["max_combinations"], 3)

    def test_main_handles_template_setup_failure_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            ir_dir = ir_fixture_dir(artifact_dir)
            (ir_dir / "dash.yaml").write_text(
                yaml.safe_dump(
                    {
                        "dashboards": [
                            {
                                "panels": [
                                    {
                                        "esql": {
                                            "query": (
                                                "FROM metrics-*\n"
                                                "| STATS value = SUM(http_requests_total) BY service.name"
                                            )
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            def fake_request(method, path, body=None, content_type="application/json"):
                if method == "PUT" and path.startswith("/_index_template/"):
                    return {"error": {"reason": "invalid composite mappings"}}
                return {"acknowledged": True}

            stdout = io.StringIO()
            with mock.patch.object(setup_telemetry_data, "make_es_request", return_value=fake_request), redirect_stdout(stdout):
                exit_code = setup_telemetry_data.main(
                    [
                        str(artifact_dir),
                        "--es-endpoint",
                        "https://example.invalid",
                        "--api-key",
                        "secret",
                        "--data-hours",
                        "1",
                        "--interval-sec",
                        "3600",
                    ]
                )

        output = stdout.getvalue()
        self.assertEqual(exit_code, 1)
        self.assertIn("Setup failed", output)
        self.assertIn("invalid composite mappings", output)

    def test_main_handles_network_failure_with_clear_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            artifact_dir.mkdir()

            stdout = io.StringIO()
            with (
                mock.patch.object(setup_telemetry_data, "make_es_request", return_value="REQ"),
                mock.patch.object(
                    setup_telemetry_data,
                    "seed_sample_data",
                    side_effect=setup_telemetry_data.NetworkError("connection refused"),
                ),
                redirect_stdout(stdout),
            ):
                exit_code = setup_telemetry_data.main(
                    [
                        str(artifact_dir),
                        "--es-endpoint",
                        "https://example.invalid",
                        "--api-key",
                        "secret",
                    ]
                )

        output = stdout.getvalue()
        self.assertEqual(exit_code, 1)
        self.assertIn("Setup failed", output)
        self.assertIn("connection refused", output)

    def test_main_rejects_missing_artifact_directory(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = setup_telemetry_data.main(
                [
                    "/nonexistent/path",
                    "--es-endpoint",
                    "https://example.invalid",
                    "--api-key",
                    "secret",
                ]
            )

        output = stdout.getvalue()
        self.assertEqual(exit_code, 1)
        self.assertIn("does not exist", output)

    def test_main_accepts_empty_api_key_for_local_no_auth_stack(self):
        # The local render-audit workflow seeds a security-disabled stack and
        # passes --api-key "". An empty key must be accepted (not rejected as
        # missing) and threaded through to make_es_request verbatim.
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            ir_dir = ir_fixture_dir(artifact_dir)
            (ir_dir / "dash.yaml").write_text(
                yaml.safe_dump({"dashboards": [{"panels": [{"esql": {
                    "query": "FROM logs-*\n| STATS count = COUNT(*) BY service.name"}}]}]}),
                encoding="utf-8",
            )

            captured: dict[str, str] = {}

            def fake_make_es_request(es_url, api_key, **_kw):
                captured["api_key"] = api_key

                def request(method, path, body=None, content_type="application/json"):
                    if path == "/_bulk":
                        docs = [line for line in body.decode().splitlines() if line.startswith('{"create"')]
                        return {"items": [{"create": {}} for _ in docs]}
                    return {"acknowledged": True}

                return request

            with mock.patch.object(
                setup_telemetry_data, "make_es_request", side_effect=fake_make_es_request
            ):
                exit_code = setup_telemetry_data.main(
                    [
                        str(artifact_dir),
                        "--es-endpoint", "http://localhost:9200",
                        "--api-key", "",
                        "--data-hours", "1",
                        "--interval-sec", "3600",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["api_key"], "")

    def test_main_rejects_missing_es_endpoint(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = setup_telemetry_data.main(
                ["/tmp", "--es-endpoint", "", "--api-key", "secret"]
            )
        self.assertEqual(exit_code, 1)
        self.assertIn("ELASTICSEARCH_ENDPOINT", stdout.getvalue())

    def test_main_rejects_invalid_data_hours_or_interval(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = setup_telemetry_data.main(
                [
                    "/tmp",
                    "--es-endpoint",
                    "https://example.invalid",
                    "--api-key",
                    "secret",
                    "--data-hours",
                    "0",
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("--data-hours must be greater than 0", stdout.getvalue())

    def test_main_logs_per_stream_doc_counts_and_ingest_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = Path(tmpdir) / "dashboards"
            ir_dir = ir_fixture_dir(artifact_dir)
            (ir_dir / "dash.yaml").write_text(
                yaml.safe_dump(
                    {
                        "dashboards": [
                            {
                                "panels": [
                                    {
                                        "esql": {
                                            "query": (
                                                "FROM logs-*\n"
                                                "| WHERE log.level == \"error\"\n"
                                                "| STATS count = COUNT(*) BY service.name"
                                            )
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            def fake_request(method, path, body=None, content_type="application/json"):
                if path == "/_bulk":
                    docs = [line for line in body.decode().splitlines() if line.startswith('{"create"')]
                    items = []
                    for index, _doc in enumerate(docs):
                        if index == 0:
                            items.append({"create": {"error": {"reason": "mapper_parsing_exception"}}})
                        else:
                            items.append({"create": {}})
                    return {"items": items}
                return {"acknowledged": True}

            stdout = io.StringIO()
            with mock.patch.object(setup_telemetry_data, "make_es_request", return_value=fake_request), redirect_stdout(stdout):
                exit_code = setup_telemetry_data.main(
                    [
                        str(artifact_dir),
                        "--es-endpoint",
                        "https://example.invalid",
                        "--api-key",
                        "secret",
                        "--data-hours",
                        "1",
                        "--interval-sec",
                        "3600",
                    ]
                )

        output = stdout.getvalue()
        self.assertEqual(exit_code, 1)
        self.assertIn("logs-generic-default:", output)
        self.assertIn("docs", output)
        self.assertIn("mapper_parsing_exception", output)

    def test_main_accepts_multiple_artifact_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            first_artifacts = Path(tmpdir) / "first"
            second_artifacts = Path(tmpdir) / "second"
            first = ir_fixture_dir(first_artifacts)
            second = ir_fixture_dir(second_artifacts)
            (first / "a.yaml").write_text(
                yaml.safe_dump({"dashboards": [{"panels": [{"esql": {"query": "FROM metrics-*\n| STATS value = SUM(first_metric)"}}]}]}),
                encoding="utf-8",
            )
            (second / "b.yaml").write_text(
                yaml.safe_dump({"dashboards": [{"panels": [{"esql": {"query": "FROM logs-*\n| WHERE log.level == \"error\"\n| STATS count = COUNT(*)"}}]}]}),
                encoding="utf-8",
            )
            calls = []

            def fake_request(method, path, body=None, content_type="application/json"):
                calls.append((method, path, body, content_type))
                if path == "/_bulk":
                    docs = [line for line in body.decode().splitlines() if line.startswith('{"create"')]
                    return {"items": [{"create": {}} for _ in docs]}
                return {"acknowledged": True}

            with mock.patch.object(setup_telemetry_data, "make_es_request", return_value=fake_request):
                exit_code = setup_telemetry_data.main(
                    [
                        str(first_artifacts),
                        str(second_artifacts),
                        "--es-endpoint",
                        "https://example.invalid",
                        "--api-key",
                        "secret",
                    ]
                )

        self.assertEqual(exit_code, 0)
        created_templates = [path for method, path, _, _ in calls if method == "PUT" and path.startswith("/_index_template/")]
        self.assertGreaterEqual(len(created_templates), 2)


class MetricKindOverrideLoadingTests(unittest.TestCase):
    def test_load_metric_kind_overrides_reads_rule_pack(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rules.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "query": {
                            "metric_kinds": {
                                "kube_pod_container_resource_requests": "gauge",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            overrides = setup_telemetry_data.load_metric_kind_overrides([str(path)])
        self.assertEqual(overrides["kube_pod_container_resource_requests"], "gauge")

    def test_load_metric_kind_overrides_empty_without_files(self):
        self.assertEqual(setup_telemetry_data.load_metric_kind_overrides([]), {})


if __name__ == "__main__":
    unittest.main()
