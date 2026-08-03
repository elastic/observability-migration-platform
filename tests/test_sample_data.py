# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
from conftest import write_dashboard_ir_artifact

from observability_migration.core import sample_data


def _write_artifact(root: Path, query: str, controls: list[dict] | None = None) -> Path:
    """Write one dashboard IR artifact -- what the telemetry contract reads.

    The fixture is described in the kb-dashboard-core ``dashboards[]`` shape
    and converted with ``DashboardIR.from_yaml_dict``, the same conversion the
    translator runs before writing ``ir/<stem>.ir.json``.
    """
    dashboard: dict = {"panels": [{"esql": {"query": query}}]}
    if controls is not None:
        dashboard["controls"] = controls
    write_dashboard_ir_artifact(root, dashboard)
    return root


def _write_native_control_count(root: Path, controls: int) -> None:
    """Declare ``controls`` dashboard controls the way the migration does."""
    native_dir = root / "native"
    native_dir.mkdir(parents=True, exist_ok=True)
    (native_dir / "dash.native.json").write_text(
        json.dumps({"title": "dash", "payload": {}, "mapping": {"controls": controls}}),
        encoding="utf-8",
    )


def _noop_request(method, path, body=None, content_type="application/json"):
    if path == "/_bulk":
        docs = [ln for ln in (body or b"").decode().splitlines() if ln.startswith('{"create"')]
        return {"items": [{"create": {}} for _ in docs]}
    return {"acknowledged": True}


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

    def test_seed_forwards_on_progress_to_ingest(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = _write_artifact(
                Path(tmp) / "dashboards",
                "FROM logs-*\n| STATS count = COUNT(*) BY service.name",
            )

            def request(method, path, body=None, content_type="application/json"):
                if path == "/_bulk":
                    docs = [ln for ln in body.decode().splitlines() if ln.startswith('{"create"')]
                    return {"items": [{"create": {}} for _ in docs]}
                return {"acknowledged": True}

            messages: list[str] = []
            sample_data.seed_sample_data(
                [artifact], request, data_hours=1, interval_sec=3600,
                batch_docs=5000, max_combinations=12, on_progress=messages.append,
            )

        self.assertTrue(any("ingested" in m for m in messages), messages)

    def test_load_metric_kind_overrides_empty_without_files(self):
        self.assertEqual(sample_data.load_metric_kind_overrides([]), {})


class ControlFieldSeedingGuardTests(unittest.TestCase):
    """Declared controls that produce zero ``control_fields`` are fatal.

    When the control-carrying artifact is unreadable, ``control_fields``
    collapses from N to 0 while ``streams`` stays non-empty, so the
    ``no telemetry requirements discovered`` guard does not fire. The seeded
    documents then match no control selection and every filtered panel
    renders empty — but the seed reports success.
    """

    _QUERY = "FROM logs-*\n| STATS count = COUNT(*) BY service.name"
    _CONTROLS = [
        {
            "type": "options",
            "label": "env",
            "data_view": "logs-*",
            "field": "deployment.environment",
        }
    ]

    def test_raises_when_declared_controls_produce_no_control_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = _write_artifact(Path(tmp) / "dashboards", self._QUERY)
            # The native artifacts declare 2 controls; the YAML declares none.
            _write_native_control_count(artifact, 2)

            with self.assertRaises(RuntimeError) as ctx:
                sample_data.seed_sample_data(
                    [artifact], _noop_request, data_hours=1, interval_sec=3600,
                    batch_docs=5000, max_combinations=12,
                )

        message = str(ctx.exception)
        self.assertIn("no control fields discovered", message)
        self.assertIn("2 dashboard control(s) are declared", message)
        self.assertIn("native/*.native.json", message)

    def test_passes_when_declared_controls_reach_the_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = _write_artifact(
                Path(tmp) / "dashboards", self._QUERY, controls=self._CONTROLS
            )
            _write_native_control_count(artifact, 1)

            summary = sample_data.seed_sample_data(
                [artifact], _noop_request, data_hours=1, interval_sec=3600,
                batch_docs=5000, max_combinations=12,
            )

        self.assertEqual(summary.errors, 0)
        self.assertGreater(summary.ok, 0)

    def test_no_declared_controls_means_no_assertion(self):
        """Absence of evidence is not evidence: a dashboard genuinely
        without controls must still seed."""
        with tempfile.TemporaryDirectory() as tmp:
            artifact = _write_artifact(Path(tmp) / "dashboards", self._QUERY)
            _write_native_control_count(artifact, 0)

            summary = sample_data.seed_sample_data(
                [artifact], _noop_request, data_hours=1, interval_sec=3600,
                batch_docs=5000, max_combinations=12,
            )

        self.assertGreater(summary.ok, 0)


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

    def test_api_key_header_sent_when_key_present(self):
        with mock.patch(
            "observability_migration.core.sample_data.requests.request",
            return_value=self._resp(200, ""),
        ) as req:
            request = sample_data.make_es_request("https://es", "abc123")
            request("GET", "/x")
        self.assertEqual(req.call_args.kwargs["headers"]["Authorization"], "ApiKey abc123")

    def test_no_auth_header_when_key_empty(self):
        # A security-disabled local stack has no API key; sending an empty
        # "ApiKey " header is wrong. Omit Authorization entirely.
        with mock.patch(
            "observability_migration.core.sample_data.requests.request",
            return_value=self._resp(200, ""),
        ) as req:
            request = sample_data.make_es_request("http://localhost:9200", "")
            request("GET", "/x")
        self.assertNotIn("Authorization", req.call_args.kwargs["headers"])

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
            empty = Path(tmp) / "dashboards" / "ir"
            empty.mkdir(parents=True)  # no IR artifacts -> no streams
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


class BulkIngestTests(unittest.TestCase):
    def test_version_conflicts_count_as_successful_idempotent_reseed(self):
        from observability_migration.core.telemetry_data import IngestSummary, _flush_into_summary

        summary = IngestSummary()
        _flush_into_summary(
            ['{"create":{}}', "{}"],
            lambda *_args, **_kwargs: {
                "items": [
                    {
                        "create": {
                            "error": {
                                "type": "version_conflict_engine_exception",
                                "reason": "document already exists",
                            }
                        }
                    }
                ]
            },
            summary,
        )
        self.assertEqual(summary.ok, 1)
        self.assertEqual(summary.errors, 0)


if __name__ == "__main__":
    unittest.main()
