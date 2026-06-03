# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
import yaml

from observability_migration.core import sample_data


def _write_artifact(root: Path, query: str) -> Path:
    yaml_dir = root / "yaml"
    yaml_dir.mkdir(parents=True)
    (yaml_dir / "dash.yaml").write_text(
        yaml.safe_dump({"dashboards": [{"panels": [{"esql": {"query": query}}]}]}),
        encoding="utf-8",
    )
    return root


class SeedSampleDataTests(unittest.TestCase):
    def test_seed_builds_templates_and_ingests(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = _write_artifact(
                Path(tmp) / "dashboards",
                "FROM logs-*\n| WHERE log.level == \"error\"\n| STATS count = COUNT(*) BY service.name",
            )
            calls: list[tuple[str, str]] = []

            def request(method, path, body=None, content_type="application/json"):
                calls.append((method, path))
                if path == "/_bulk":
                    docs = [ln for ln in body.decode().splitlines() if ln.startswith('{"create"')]
                    return {"items": [{"create": {}} for _ in docs]}
                return {"acknowledged": True}

            summary = sample_data.seed_sample_data(
                [artifact], request, data_hours=1, interval_sec=3600,
                batch_docs=5000, max_combinations=12,
            )

        self.assertTrue(any(p.startswith("/_index_template/telemetry-data-") for _, p in calls))
        self.assertTrue(any(p == "/_bulk" for _, p in calls))
        self.assertEqual(summary.errors, 0)
        self.assertGreater(summary.ok, 0)

    def test_load_metric_kind_overrides_empty_without_files(self):
        self.assertEqual(sample_data.load_metric_kind_overrides([]), {})


class MakeEsRequestTests(unittest.TestCase):
    def _resp(self, status=200, text='{"acknowledged": true}'):
        resp = mock.Mock()
        resp.status_code = status
        resp.ok = 200 <= status < 300
        resp.text = text
        return resp

    def test_delete_404_is_idempotent_ack_and_threads_verify(self):
        with mock.patch(
            "observability_migration.core.sample_data.requests.request",
            return_value=self._resp(404, ""),
        ) as req:
            request = sample_data.make_es_request("https://es", "k", verify=False)
            self.assertEqual(request("DELETE", "/_data_stream/x"), {"acknowledged": True})
        self.assertEqual(req.call_args.kwargs["verify"], False)

    def test_empty_success_body_is_ack(self):
        with mock.patch(
            "observability_migration.core.sample_data.requests.request",
            return_value=self._resp(200, ""),
        ):
            request = sample_data.make_es_request("https://es", "k")
            self.assertEqual(request("PUT", "/_data_stream/x"), {"acknowledged": True})

    def test_non_2xx_empty_body_is_error(self):
        with mock.patch(
            "observability_migration.core.sample_data.requests.request",
            return_value=self._resp(400, ""),
        ):
            request = sample_data.make_es_request("https://es", "k")
            self.assertEqual(request("GET", "/x").get("error", {}).get("status"), 400)

    def test_http_error_json_body_passes_through(self):
        with mock.patch(
            "observability_migration.core.sample_data.requests.request",
            return_value=self._resp(400, '{"error": {"reason": "bad"}}'),
        ):
            request = sample_data.make_es_request("https://es", "k")
            self.assertEqual(request("GET", "/x")["error"]["reason"], "bad")

    def test_bytes_body_passthrough_and_content_type(self):
        with mock.patch(
            "observability_migration.core.sample_data.requests.request",
            return_value=self._resp(200, '{"items": []}'),
        ) as req:
            request = sample_data.make_es_request("https://es", "k")
            request("POST", "/_bulk", b'{"create":{}}\n', "application/x-ndjson")
        self.assertEqual(req.call_args.kwargs["data"], b'{"create":{}}\n')
        self.assertEqual(req.call_args.kwargs["headers"]["Content-Type"], "application/x-ndjson")

    def test_network_error_on_requests_exception(self):
        with mock.patch(
            "observability_migration.core.sample_data.requests.request",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            request = sample_data.make_es_request("https://es", "k")
            with self.assertRaises(sample_data.NetworkError):
                request("GET", "/x")


class SeedOrchestrationEdgeTests(unittest.TestCase):
    def test_seed_raises_on_empty_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "dashboards" / "yaml"
            empty.mkdir(parents=True)  # no yaml files -> no streams
            with self.assertRaises(RuntimeError):
                sample_data.seed_sample_data(
                    [Path(tmp) / "dashboards"],
                    lambda *a, **k: {"acknowledged": True},
                    data_hours=1, interval_sec=3600, batch_docs=10, max_combinations=2,
                )

    def test_no_recreate_skips_template_creation_but_still_ingests(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = _write_artifact(
                Path(tmp) / "dashboards",
                "FROM logs-*\n| STATS count = COUNT(*) BY service.name",
            )
            calls: list[tuple[str, str]] = []

            def request(method, path, body=None, content_type="application/json"):
                calls.append((method, path))
                if path == "/_bulk":
                    docs = [ln for ln in body.decode().splitlines() if ln.startswith('{"create"')]
                    return {"items": [{"create": {}} for _ in docs]}
                return {"acknowledged": True}

            sample_data.seed_sample_data(
                [artifact], request, data_hours=1, interval_sec=3600,
                batch_docs=5000, max_combinations=12, no_recreate=True,
            )

        self.assertFalse(any(p.startswith("/_index_template/") for _, p in calls))
        self.assertTrue(any(p == "/_bulk" for _, p in calls))


if __name__ == "__main__":
    unittest.main()
