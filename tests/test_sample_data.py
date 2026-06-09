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

    def test_request_uses_connect_read_timeout_tuple(self):
        # A scalar requests timeout is per-read, not a total deadline: a server
        # that trickles bytes resets the read timer forever and the bulk hangs.
        # The adapter must pass a (connect, read) tuple so the read deadline is
        # bounded and deterministic.
        with mock.patch(
            "observability_migration.core.sample_data.requests.request",
            return_value=self._resp(200, '{"items": []}'),
        ) as req:
            request = sample_data.make_es_request("https://es", "k")
            request("POST", "/_bulk", b'{"create":{}}\n', "application/x-ndjson")
        timeout = req.call_args.kwargs["timeout"]
        self.assertIsInstance(timeout, tuple)
        self.assertEqual(len(timeout), 2)
        connect_timeout, read_timeout = timeout
        self.assertGreater(connect_timeout, 0)
        self.assertGreater(read_timeout, 0)

    def test_transient_failure_is_retried_then_raises(self):
        # A stalled/dropped bulk surfaces as a Timeout/ConnectionError. The
        # adapter must retry a bounded number of times (so a single transient
        # blip doesn't fail the whole seed) and then raise NetworkError rather
        # than hang or retry forever.
        attempts = {"n": 0}

        def always_timeout(*_args, **_kwargs):
            attempts["n"] += 1
            raise requests.exceptions.ReadTimeout("read timed out")

        with mock.patch(
            "observability_migration.core.sample_data.requests.request",
            side_effect=always_timeout,
        ), mock.patch("observability_migration.core.sample_data.time.sleep"):
            request = sample_data.make_es_request("https://es", "k", max_retries=3)
            with self.assertRaises(sample_data.NetworkError):
                request("POST", "/_bulk", b'{"create":{}}\n', "application/x-ndjson")
        # 1 initial try + 3 retries == 4 attempts, then give up.
        self.assertEqual(attempts["n"], 4)

    def test_transient_failure_then_success_recovers(self):
        # If a retry succeeds, the call returns normally and does not raise.
        seq = [
            requests.exceptions.ConnectionError("reset"),
            self._resp(200, '{"items": []}'),
        ]

        def flaky(*_args, **_kwargs):
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with mock.patch(
            "observability_migration.core.sample_data.requests.request",
            side_effect=flaky,
        ), mock.patch("observability_migration.core.sample_data.time.sleep"):
            request = sample_data.make_es_request("https://es", "k", max_retries=3)
            result = request("POST", "/_bulk", b'{"create":{}}\n', "application/x-ndjson")
        self.assertEqual(result, {"items": []})


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


class RemoveSampleDataTests(unittest.TestCase):
    def _artifact(self, tmp):
        return _write_artifact(
            Path(tmp) / "dashboards",
            "FROM logs-*\n| WHERE log.level == \"error\"\n| STATS count = COUNT(*) BY service.name",
        )

    def test_dry_run_performs_no_writes_and_reports_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = self._artifact(tmp)
            calls: list[tuple[str, str]] = []

            def request(method, path, body=None, content_type="application/json"):
                calls.append((method, path))
                if method == "GET":
                    name = path.rsplit("/", 1)[-1]
                    return {"data_streams": [{"name": name, "template": "telemetry-data-" + name}]}
                return {"acknowledged": True}

            summary = sample_data.remove_sample_data([artifact], request, dry_run=True)

        self.assertTrue(summary.dry_run)
        self.assertTrue(summary.deleted_streams)  # would-delete plan, non-empty
        self.assertFalse(any(m == "DELETE" for m, _ in calls))

    def test_confirm_deletes_only_seeder_owned_streams(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = self._artifact(tmp)
            deletes: list[str] = []

            def request(method, path, body=None, content_type="application/json"):
                if method == "GET":
                    name = path.rsplit("/", 1)[-1]
                    # The concrete stream is NOT seeder-owned (real data).
                    return {"data_streams": [{"name": name, "template": "logs"}]}
                if method == "DELETE":
                    deletes.append(path)
                return {"acknowledged": True}

            summary = sample_data.remove_sample_data([artifact], request, dry_run=False)

        self.assertEqual(summary.deleted_streams, [])
        self.assertTrue(summary.skipped_not_owned)
        self.assertFalse(any("/_data_stream/" in p for p in deletes))

    def test_confirm_deletes_owned_stream_and_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = self._artifact(tmp)
            deletes: list[str] = []

            def request(method, path, body=None, content_type="application/json"):
                if method == "GET":
                    name = path.rsplit("/", 1)[-1]
                    return {"data_streams": [{"name": name, "template": "telemetry-data-" + name}]}
                if method == "DELETE":
                    deletes.append(path)
                return {"acknowledged": True}

            summary = sample_data.remove_sample_data([artifact], request, dry_run=False)

        self.assertTrue(summary.deleted_streams)
        self.assertTrue(any(p.startswith("/_data_stream/") for p in deletes))
        self.assertTrue(any(p.startswith("/_index_template/telemetry-data-") for p in deletes))

    def test_get_error_is_unverifiable_and_deletes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = self._artifact(tmp)
            deletes: list[str] = []

            def request(method, path, body=None, content_type="application/json"):
                if method == "GET":
                    return {"error": {"type": "security_exception"}, "status": 403}
                if method == "DELETE":
                    deletes.append(path)
                return {"acknowledged": True}

            summary = sample_data.remove_sample_data([artifact], request, dry_run=False)

        self.assertEqual(summary.deleted_streams, [])
        self.assertEqual(deletes, [])  # fail closed: nothing deleted on unreadable GET
        self.assertTrue(summary.errors)

    def test_absent_stream_cleans_orphan_template_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = self._artifact(tmp)
            deletes: list[str] = []

            def request(method, path, body=None, content_type="application/json"):
                if method == "GET":
                    return {"error": {"type": "resource_not_found_exception"}, "status": 404}
                if method == "DELETE":
                    deletes.append(path)
                return {"acknowledged": True}

            summary = sample_data.remove_sample_data([artifact], request, dry_run=False)

        self.assertEqual(summary.deleted_streams, [])  # stream was absent; not "deleted"
        self.assertTrue(summary.deleted_templates)
        self.assertTrue(any(p.startswith("/_index_template/telemetry-data-") for p in deletes))
        self.assertFalse(any(p.startswith("/_data_stream/") for p in deletes))  # never delete an absent stream by name

    def test_foreign_skip_does_not_plan_template_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = self._artifact(tmp)

            def request(method, path, body=None, content_type="application/json"):
                if method == "GET":
                    name = path.rsplit("/", 1)[-1]
                    return {"data_streams": [{"name": name, "template": "logs"}]}
                return {"acknowledged": True}

            summary = sample_data.remove_sample_data([artifact], request, dry_run=True)

        self.assertEqual(summary.deleted_templates, [])
        self.assertTrue(summary.skipped_not_owned)


if __name__ == "__main__":
    unittest.main()
