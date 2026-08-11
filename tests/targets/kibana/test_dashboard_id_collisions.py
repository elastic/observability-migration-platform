# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Two dashboards that share a title must not share a Kibana dashboard id.

The API dashboard id is the *upsert key*: uploading two dashboards under one id
leaves Kibana holding only the second, with no error anywhere in the run. These
tests pin both halves of the contract:

* a dashboard whose title is unique in the run keeps the id it has always had,
  byte-for-byte (changing it would orphan every already-uploaded copy), and
* a title collision produces distinct ids, is reported to the operator, and --
  if two payloads still reach one id -- fails the upload loudly instead of
  reporting two successes.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from observability_migration.core.assets.dashboard import DashboardIR
from observability_migration.core.assets.native_dashboard import (
    NativeDashboard,
    NativeGrid,
    NativePanel,
)
from observability_migration.targets.kibana import dashboards_api as api
from observability_migration.targets.kibana.adapter import KibanaTargetAdapter

# Every dashboard id the two canonical corpora produced *before* collision
# handling existed, keyed by title. These are the live upsert keys of dashboards
# operators have already uploaded: a change here orphans them.
#
# Note the last entry. Grafana's artifact stem truncates the title at 60
# characters, so slugifying the *stem* would yield
# ``obs-migrate-redis-dashboard-for-prometheus-redis-exporter-helm-stable-r`` --
# a different dashboard. The id is derived from the title plus an explicit
# collision token precisely so stem truncation cannot rename anything.
CORPUS_DASHBOARD_IDS = {
    "Apache - Overview": "obs-migrate-apache-overview",
    "Celery Overview": "obs-migrate-celery-overview",
    "Consul Overview": "obs-migrate-consul-overview",
    "Docker - Overview": "obs-migrate-docker-overview",
    "HAProxy - Overview": "obs-migrate-haproxy-overview",
    "Kafka, Zookeeper and Kafka Consumer Overview": (
        "obs-migrate-kafka-zookeeper-and-kafka-consumer-overview"
    ),
    "Kubernetes - Overview": "obs-migrate-kubernetes-overview",
    "MongoDB - Overview": "obs-migrate-mongodb-overview",
    "MySQL - Overview": "obs-migrate-mysql-overview",
    "NGINX - Overview": "obs-migrate-nginx-overview",
    "Postgres - Metrics": "obs-migrate-postgres-metrics",
    "RabbitMQ Overview (OpenMetrics Version)": (
        "obs-migrate-rabbitmq-overview-openmetrics-version"
    ),
    "Redis - Overview": "obs-migrate-redis-overview",
    "Diverse Panel Types Test": "obs-migrate-diverse-panel-types-test",
    "Express Prometheus Middleware": "obs-migrate-express-prometheus-middleware",
    "Home - Migration Test Lab": "obs-migrate-home-migration-test-lab",
    "Kitchen Sink Panel Canary": "obs-migrate-kitchen-sink-panel-canary",
    "Kubernetes / Views / Global": "obs-migrate-kubernetes-views-global",
    "Multi Pattern Coverage": "obs-migrate-multi-pattern-coverage",
    "Node Exporter Full": "obs-migrate-node-exporter-full",
    "Prometheus 2.0 (by FUSAKLA)": "obs-migrate-prometheus-2-0-by-fusakla",
    "Redis Dashboard for Prometheus Redis Exporter (helm stable/redis-ha)": (
        "obs-migrate-redis-dashboard-for-prometheus-redis-exporter-helm-stable-redis-ha"
    ),
}


def _one_panel() -> list:
    return [NativePanel(grid=NativeGrid(), type="vis", config={"type": "metric"})]


def _native_artifact(dashboard_id: str, title: str) -> str:
    return json.dumps(
        {
            "kind": "native_dashboard",
            "version": 1,
            "dashboard_id": dashboard_id,
            "title": title,
            "payload": {
                "title": title,
                "panels": [
                    {
                        "grid": {"x": 0, "y": 0, "w": 24, "h": 8},
                        "type": "vis",
                        "config": {"type": "metric"},
                    }
                ],
            },
            "mapping": {"mapped": 1, "unmapped": 0, "sections": 0, "controls": 0, "reasons": {}},
        }
    )


class TestDashboardIdDerivation(unittest.TestCase):
    def test_unique_title_keeps_its_current_dashboard_id(self):
        """No disambiguator -> byte-identical to the pre-fix id, for both corpora."""
        for title, expected in CORPUS_DASHBOARD_IDS.items():
            with self.subTest(title=title):
                ir = DashboardIR(title=title)
                self.assertEqual(api._stable_dashboard_id_from_ir(ir), expected)
                native, _counts = api.native_dashboard_from_ir(ir)
                self.assertEqual(native.dashboard_id, expected)

    def test_same_title_dashboards_get_distinct_dashboard_ids(self):
        first = DashboardIR(title="Shared Title", uid="dash-alpha")
        second = DashboardIR(
            title="Shared Title", uid="dash-beta", id_disambiguator="dash-beta"
        )

        first_id = api._stable_dashboard_id_from_ir(first)
        second_id = api._stable_dashboard_id_from_ir(second)

        self.assertEqual(first_id, "obs-migrate-shared-title")
        self.assertEqual(second_id, "obs-migrate-shared-title-dash-beta")
        self.assertNotEqual(first_id, second_id)

    def test_native_payloads_for_a_title_collision_carry_the_distinct_ids(self):
        first, _ = api.native_dashboard_from_ir(DashboardIR(title="Shared Title"))
        second, _ = api.native_dashboard_from_ir(
            DashboardIR(title="Shared Title", id_disambiguator="dash-beta")
        )
        self.assertNotEqual(first.dashboard_id, second.dashboard_id)

    def test_disambiguator_is_slugified_into_the_id(self):
        ir = DashboardIR(title="Shared Title", id_disambiguator="Dash Beta/2")
        self.assertEqual(
            api._stable_dashboard_id_from_ir(ir), "obs-migrate-shared-title-dash-beta-2"
        )


class TestDisambiguationIsReported(unittest.TestCase):
    def test_a_disambiguated_dashboard_id_is_reported(self):
        note = api.dashboard_id_disambiguation_note(
            DashboardIR(title="Shared Title", id_disambiguator="dash-beta")
        )
        self.assertIn("Shared Title", note)
        self.assertIn("obs-migrate-shared-title-dash-beta", note)
        # The plain title slug is named too: that is what someone will search for.
        self.assertIn("obs-migrate-shared-title", note)

    def test_a_unique_title_reports_nothing(self):
        self.assertEqual(
            api.dashboard_id_disambiguation_note(DashboardIR(title="Solo Title")), ""
        )


class TestCollisionTokenSurvivesTheRun(unittest.TestCase):
    """The token has to survive every stage that rebuilds the IR or payload."""

    def test_the_yaml_rebuild_carries_the_disambiguator(self):
        # ``sync_result_queries_to_ir`` and ``apply_metadata_polish`` rebuild the
        # IR from its lossy YAML dict shape and re-derive the native payload from
        # it. Dropping the token there would silently restore the plain title
        # slug -- and the overwrite -- on any ``--validate`` or
        # ``--polish-metadata`` run.
        from observability_migration.targets.kibana.compile import (
            carry_over_non_yaml_ir_fields,
        )

        original = DashboardIR(
            title="Shared Title", uid="dash-beta", id_disambiguator="dash-beta"
        )
        rebuilt = DashboardIR.from_yaml_dict(original.to_yaml_dict(), source_adapter="grafana")
        carry_over_non_yaml_ir_fields(rebuilt, original, fallback_source_adapter="grafana")

        self.assertEqual(rebuilt.id_disambiguator, "dash-beta")
        self.assertEqual(
            api._stable_dashboard_id_from_ir(rebuilt),
            api._stable_dashboard_id_from_ir(original),
        )

    def test_the_persisted_ir_artifact_round_trips_the_disambiguator(self):
        original = DashboardIR(title="Shared Title", id_disambiguator="dash-beta")
        restored = DashboardIR.from_dict(original.to_dict())
        self.assertEqual(restored.id_disambiguator, "dash-beta")

    def test_both_sources_report_the_token_that_disambiguated_the_stem(self):
        """Artifact stem and dashboard id must be disambiguated by one decision."""
        from observability_migration.adapters.source.datadog import cli as datadog_cli
        from observability_migration.adapters.source.grafana import cli as grafana_cli

        used: set[str] = set()
        self.assertEqual(
            grafana_cli._allocate_dashboard_output_stem(
                title="Shared Title", dashboard_uid="dash-alpha", used_stems=used
            ),
            ("shared_title", ""),
        )
        self.assertEqual(
            grafana_cli._allocate_dashboard_output_stem(
                title="Shared Title", dashboard_uid="dash-beta", used_stems=used
            ),
            ("shared_title_dash-beta", "dash-beta"),
        )

        used = set()
        datadog_cli._allocate_artifact_stem("Shared Title", "dash-alpha", used)
        stem, token = datadog_cli._allocate_artifact_stem("Shared Title", "dash-beta", used)
        self.assertEqual((stem, token), ("shared_title_dash-beta", "dash-beta"))
        # The stem and the id say the same thing about the same dashboard.
        self.assertEqual(
            api._stable_dashboard_id_from_ir(
                DashboardIR(title="Shared Title", id_disambiguator=token)
            ),
            "obs-migrate-shared-title-dash-beta",
        )


class TestBatchDuplicateIdFailsLoudly(unittest.TestCase):
    """Defence in depth: one id reached twice in a batch is a hard failure."""

    def test_second_payload_with_the_same_id_is_not_uploaded(self):
        seen: set[str] = set()
        response = mock.Mock(status_code=201)
        response.json.return_value = {"id": "obs-migrate-shared-title"}
        session = mock.Mock()
        session.put.return_value = response

        payloads = [
            NativeDashboard(
                title="Shared Title", dashboard_id="obs-migrate-shared-title", items=_one_panel()
            ),
            NativeDashboard(
                title="Shared Title", dashboard_id="obs-migrate-shared-title", items=_one_panel()
            ),
        ]
        with mock.patch(
            "observability_migration.targets.kibana.dashboards_api._session",
            return_value=session,
        ):
            results = [
                api.upload_native_dashboard(
                    payload, "https://kibana.example", api_key="k", seen_dashboard_ids=seen
                )
                for payload in payloads
            ]

        self.assertEqual(results[0].status, "created")
        self.assertEqual(results[1].status, "duplicate_id")
        self.assertIn("obs-migrate-shared-title", results[1].message)
        # The overwrite never left the process.
        self.assertEqual(session.put.call_count, 1)

    def test_adapter_batch_reports_a_duplicate_id_as_a_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            native_dir = Path(tmpdir) / "native"
            native_dir.mkdir(parents=True)
            native_dir.joinpath("shared_title.native.json").write_text(
                _native_artifact("obs-migrate-shared-title", "Shared Title"), encoding="utf-8"
            )
            native_dir.joinpath("shared_title_dash-beta.native.json").write_text(
                _native_artifact("obs-migrate-shared-title", "Shared Title"), encoding="utf-8"
            )

            response = mock.Mock(status_code=201)
            response.json.return_value = {"id": "obs-migrate-shared-title"}
            session = mock.Mock()
            session.put.return_value = response

            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                return_value=[{"id": "metrics-*", "title": "metrics-*"}],
            ), mock.patch(
                "observability_migration.targets.kibana.dashboards_api._session",
                return_value=session,
            ):
                payload = KibanaTargetAdapter().upload(
                    Path(tmpdir),
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                )

        statuses = sorted(record["status"] for record in payload["records"])
        self.assertEqual(statuses, ["created", "duplicate_id"])
        # Not "two successes": the batch must not report a clean run.
        self.assertEqual(payload["summary"]["uploaded_ok"], 1)
        self.assertEqual(payload["summary"]["total"], 2)
        self.assertEqual(session.put.call_count, 1)


if __name__ == "__main__":
    unittest.main()
