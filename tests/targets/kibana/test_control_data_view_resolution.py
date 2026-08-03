# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""A control ``data_view_id`` that cannot be resolved must not fall back silently.

Control ``data_view_id`` values are index patterns (``metrics-*``) that upload
rewrites to the real saved-object id Kibana assigned. On a lookup miss the raw
pattern is kept, and Kibana renders that control as "An error occurred" -- with
nothing in the run output pointing at it. ``ensure_migration_data_views`` is
supposed to make the lookup complete by construction, so a miss means ensuring
failed, which is what these tests force into the operator's view.

The two legitimate fallbacks must stay quiet: a value that is already a real
saved-object id, and a data view whose title *is* its id (which
``_data_view_id_lookup`` deliberately omits, because rewriting it would be a
no-op).
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from observability_migration.core.assets.native_dashboard import (
    NativeControl,
    NativeDashboard,
    NativeGrid,
    NativePanel,
)
from observability_migration.targets.kibana import dashboards_api as api
from observability_migration.targets.kibana.adapter import KibanaTargetAdapter

# What Kibana actually holds: one wildcard data view (id != title) and one
# concrete data view whose title is its own id.
LIVE_DATA_VIEWS = [
    {"id": "b4f1-uuid", "title": "metrics-*"},
    {"id": "logs-app", "title": "logs-app"},
]


def _control(data_view_id: str, title: str = "Service") -> NativeControl:
    return NativeControl(
        type="options_list_control",
        config={"title": title, "data_view_id": data_view_id, "field_name": "service.name"},
    )


def _dashboard(*controls: NativeControl) -> NativeDashboard:
    return NativeDashboard(
        title="Has Controls",
        dashboard_id="has-controls",
        items=[NativePanel(grid=NativeGrid(), type="vis", config={"type": "metric"})],
        controls=list(controls),
    )


class TestResolverReportsUnresolvedPatterns(unittest.TestCase):
    def test_an_unresolvable_pattern_is_reported_with_its_control(self):
        payload = {
            "pinned_panels": [
                {
                    "type": "options_list_control",
                    "config": {
                        "title": "Instance",
                        "data_view_id": "metrics-*.prometheus-*",
                        "field_name": "labels.instance",
                    },
                }
            ]
        }
        unresolved = api._resolve_pinned_panel_data_view_ids(
            payload,
            {"metrics-*": "b4f1-uuid"},
            data_view_inventory={"metrics-*", "b4f1-uuid", "logs-app"},
        )

        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0].data_view, "metrics-*.prometheus-*")
        self.assertEqual(unresolved[0].control, "Instance")
        # The value is left alone rather than blanked or guessed at.
        self.assertEqual(
            payload["pinned_panels"][0]["config"]["data_view_id"], "metrics-*.prometheus-*"
        )

    def test_an_already_resolved_saved_object_id_is_not_reported(self):
        payload = {
            "pinned_panels": [
                {
                    "type": "options_list_control",
                    "config": {"title": "Service", "data_view_id": "b4f1-uuid"},
                }
            ]
        }
        unresolved = api._resolve_pinned_panel_data_view_ids(
            payload,
            {"metrics-*": "b4f1-uuid"},
            data_view_inventory={"metrics-*", "b4f1-uuid", "logs-app"},
        )
        self.assertEqual(unresolved, [])

    def test_a_data_view_whose_title_is_its_id_is_not_reported(self):
        payload = {
            "pinned_panels": [
                {
                    "type": "options_list_control",
                    "config": {"title": "App", "data_view_id": "logs-app"},
                }
            ]
        }
        unresolved = api._resolve_pinned_panel_data_view_ids(
            payload,
            {"metrics-*": "b4f1-uuid"},
            data_view_inventory={"metrics-*", "b4f1-uuid", "logs-app"},
        )
        self.assertEqual(unresolved, [])

    def test_without_a_data_view_inventory_nothing_is_judged(self):
        payload = {
            "pinned_panels": [
                {
                    "type": "options_list_control",
                    "config": {"title": "Service", "data_view_id": "metrics-*"},
                }
            ]
        }
        self.assertEqual(api._resolve_pinned_panel_data_view_ids(payload, None), [])


class TestUploadCarriesUnresolvedDataViews(unittest.TestCase):
    def test_upload_result_records_the_unresolved_control(self):
        response = mock.Mock(status_code=201)
        response.json.return_value = {"id": "has-controls"}
        session = mock.Mock()
        session.put.return_value = response

        with mock.patch(
            "observability_migration.targets.kibana.dashboards_api._session",
            return_value=session,
        ):
            result = api.upload_native_dashboard(
                _dashboard(_control("metrics-*.prometheus-*", title="Instance")),
                "https://kibana.example",
                api_key="k",
                data_view_ids={"metrics-*": "b4f1-uuid"},
                data_view_inventory={"metrics-*", "b4f1-uuid", "logs-app"},
            )

        self.assertEqual(result.status, "created")
        self.assertEqual(
            [(item.data_view, item.control) for item in result.unresolved_data_views],
            [("metrics-*.prometheus-*", "Instance")],
        )


class TestAdapterWarnsOperatorVisibly(unittest.TestCase):
    """Only the ensured data views are passed to the resolver, so the adapter
    re-checks a reported fallback against every data view in the space before
    warning -- an operator-created data view this upload had no reason to ensure
    is not a defect."""

    def _upload(self, *controls, live_extra=(), stderr=None):
        response = mock.Mock(status_code=201)
        response.json.return_value = {"id": "has-controls"}
        session = mock.Mock()
        session.put.return_value = response
        with mock.patch(
            "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
            return_value=LIVE_DATA_VIEWS,
        ), mock.patch(
            "observability_migration.targets.kibana.adapter.list_data_views",
            return_value=[*LIVE_DATA_VIEWS, *live_extra],
        ), mock.patch(
            "observability_migration.targets.kibana.dashboards_api._session",
            return_value=session,
        ), redirect_stderr(stderr or io.StringIO()):
            return KibanaTargetAdapter().upload_dashboard(
                kibana_url="https://kibana.example",
                native_dashboard=_dashboard(*controls),
                artifact_label="has_controls",
            )

    def test_upload_dashboard_warns_naming_the_pattern_and_the_control(self):
        stderr = io.StringIO()
        record = self._upload(
            _control("metrics-*.prometheus-*", title="Instance"), stderr=stderr
        )

        warning = stderr.getvalue()
        self.assertIn("metrics-*.prometheus-*", warning)
        self.assertIn("Instance", warning)
        # A data-view gap is a warning, not an upload failure.
        self.assertTrue(record["success"])
        self.assertEqual(
            record["unresolved_data_views"],
            [{"data_view": "metrics-*.prometheus-*", "control": "Instance"}],
        )

    def test_resolvable_controls_do_not_warn(self):
        stderr = io.StringIO()
        self._upload(
            _control("metrics-*", title="Service"),
            _control("logs-app", title="App"),
            stderr=stderr,
        )
        self.assertEqual(stderr.getvalue(), "")

    def test_a_data_view_that_exists_but_was_not_ensured_does_not_warn(self):
        stderr = io.StringIO()
        record = self._upload(
            _control("metrics-redis.prometheus-default", title="Pod"),
            # Kibana holds it (title == id) even though this upload did not
            # ensure it, so the fallback is correct.
            live_extra=[
                {
                    "id": "metrics-redis.prometheus-default",
                    "title": "metrics-redis.prometheus-default",
                }
            ],
            stderr=stderr,
        )
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(record["unresolved_data_views"], [])


DEFAULT_PATTERNS = ["metrics-prometheus-*", "metrics-*", "logs-*"]


def _artifact_dir(tmpdir: str, *control_patterns: str, stem: str = "dash") -> Path:
    """A dashboard artifact root whose one artifact carries the given controls."""
    native_dir = Path(tmpdir) / "native"
    native_dir.mkdir(parents=True, exist_ok=True)
    native_dir.joinpath(f"{stem}.native.json").write_text(
        json.dumps(
            {
                "kind": "native_dashboard",
                "version": 1,
                "dashboard_id": f"obs-migrate-{stem}",
                "title": stem,
                "payload": {
                    "title": stem,
                    "panels": [
                        {
                            "grid": {"x": 0, "y": 0, "w": 24, "h": 8},
                            "type": "vis",
                            "config": {"type": "metric"},
                        }
                    ],
                    "pinned_panels": [
                        {
                            "type": "options_list_control",
                            "config": {
                                "title": f"control-{index}",
                                "data_view_id": pattern,
                                "field_name": "labels.instance",
                            },
                        }
                        for index, pattern in enumerate(control_patterns)
                    ],
                },
                "mapping": {
                    "mapped": 1,
                    "unmapped": 0,
                    "sections": 0,
                    "controls": len(control_patterns),
                    "reasons": {},
                },
            }
        ),
        encoding="utf-8",
    )
    return Path(tmpdir)


class TestBatchUploadEnsuresReferencedPatterns(unittest.TestCase):
    """``obs-migrate upload`` must ensure what the batch's artifacts reference.

    ``upload_dashboard`` (the migrate pipeline) already passes
    ``_referenced_data_view_patterns``; the standalone batch path ensured only
    the fixed defaults, so a reviewed artifact whose control names
    ``metrics-*.prometheus-*`` had no data view to resolve against and shipped
    pointing at the raw pattern.
    """

    def _upload(self, artifact_dir, *, ensure, upload_result=None, out=None, err=None):
        with mock.patch(
            "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
            side_effect=ensure,
        ) as ensure_mock, mock.patch(
            "observability_migration.targets.kibana.adapter.list_data_views",
            return_value=LIVE_DATA_VIEWS,
        ), mock.patch(
            "observability_migration.targets.kibana.adapter.dashboards_api.upload_native_artifact",
            return_value=upload_result
            or api.UploadResult(
                dashboard="dash", dashboard_id="obs-migrate-dash", status="created", mapped=1,
            ),
        ) as upload_mock, redirect_stdout(out or io.StringIO()), redirect_stderr(
            err or io.StringIO()
        ):
            payload = KibanaTargetAdapter().upload(
                artifact_dir,
                kibana_url="https://kibana.example",
                kibana_api_key="secret",
                space_id="shadow",
            )
        return payload, ensure_mock, upload_mock

    def test_a_non_default_pattern_the_batch_references_is_ensured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = _artifact_dir(tmpdir, "metrics-*.prometheus-*")
            _, ensure_mock, _ = self._upload(
                artifact_dir,
                ensure=lambda *a, **k: [{"id": "prom-uuid", "title": "metrics-*.prometheus-*"}],
            )

        ensure_mock.assert_called_once_with(
            "https://kibana.example",
            data_view_patterns=[*DEFAULT_PATTERNS, "metrics-*.prometheus-*"],
            api_key="secret",
            space_id="shadow",
            verify=True,
        )

    def test_one_pattern_shared_by_many_artifacts_is_ensured_once(self):
        # N artifacts naming the same pattern must not cost N ensure round-trips.
        with tempfile.TemporaryDirectory() as tmpdir:
            _artifact_dir(tmpdir, "metrics-*.prometheus-*", stem="alpha")
            _artifact_dir(tmpdir, "metrics-*.prometheus-*", stem="beta")
            artifact_dir = _artifact_dir(tmpdir, "metrics-*.prometheus-*", stem="gamma")
            _, ensure_mock, upload_mock = self._upload(
                artifact_dir,
                ensure=lambda *a, **k: [{"id": "prom-uuid", "title": "metrics-*.prometheus-*"}],
            )

        self.assertEqual(upload_mock.call_count, 3)
        ensure_mock.assert_called_once_with(
            "https://kibana.example",
            data_view_patterns=[*DEFAULT_PATTERNS, "metrics-*.prometheus-*"],
            api_key="secret",
            space_id="shadow",
            verify=True,
        )

    def test_a_defaults_only_batch_is_unchanged_and_silent(self):
        # The false-positive floor: nothing extra requested, nothing printed.
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = _artifact_dir(tmpdir, "metrics-*", "logs-*")
            payload, ensure_mock, _ = self._upload(
                artifact_dir,
                ensure=lambda *a, **k: LIVE_DATA_VIEWS,
                out=out,
                err=err,
            )

        ensure_mock.assert_called_once_with(
            "https://kibana.example",
            data_view_patterns=None,
            api_key="secret",
            space_id="shadow",
            verify=True,
        )
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(payload["summary"]["uploaded_ok"], 1)
        self.assertEqual(payload["records"][0]["status"], "created")


class TestBatchUploadResolvesTheControlToARealId(unittest.TestCase):
    """Ensuring is only worth anything if the control ends up on the real id."""

    @staticmethod
    def _ensure_only_what_was_asked_for(url, *, data_view_patterns=None, **kwargs):
        """Behave like Kibana: a data view exists only if it was requested."""
        assigned = {
            "metrics-*": "b4f1-uuid",
            "logs-app": "logs-app",
            "metrics-prometheus-*": "prom-metricbeat-uuid",
            "logs-*": "logs-uuid",
            "metrics-*.prometheus-*": "prom-uuid",
        }
        return [
            {"id": assigned.get(pattern, pattern), "title": pattern}
            for pattern in (data_view_patterns or DEFAULT_PATTERNS)
        ]

    def test_the_sent_control_carries_the_created_data_view_id(self):
        response = mock.Mock(status_code=201)
        response.json.return_value = {"id": "obs-migrate-dash"}
        session = mock.Mock()
        session.put.return_value = response

        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = _artifact_dir(tmpdir, "metrics-*.prometheus-*")
            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                side_effect=self._ensure_only_what_was_asked_for,
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.list_data_views",
                return_value=LIVE_DATA_VIEWS,
            ), mock.patch(
                "observability_migration.targets.kibana.dashboards_api._session",
                return_value=session,
            ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                payload = KibanaTargetAdapter().upload(
                    artifact_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                )

        sent = json.loads(session.put.call_args.kwargs["data"])
        self.assertEqual(
            sent["pinned_panels"][0]["config"]["data_view_id"], "prom-uuid",
        )
        self.assertEqual(payload["records"][0]["unresolved_data_views"], [])
        self.assertTrue(payload["records"][0]["success"])


class TestBatchUploadFailsOnAnUncreatableDataView(unittest.TestCase):
    """A pattern the target refuses is louder than a warning.

    Ensuring is now attempted for every referenced pattern, so a refusal means
    the data view does not exist and the control bound to it *will* render "An
    error occurred". That is a 2xx upload that is knowably incomplete -- the
    ``lossy`` case -- so it never counts toward ``uploaded_ok`` and the run
    exits non-zero, carrying the target's own reason.
    """

    @staticmethod
    def _ensure_refusing(bad_pattern: str):
        def _ensure(url, *, data_view_patterns=None, **kwargs):
            patterns = data_view_patterns or DEFAULT_PATTERNS
            if bad_pattern in patterns:
                raise RuntimeError(
                    "400 Client Error: Bad Request for url: "
                    "https://kibana.example/api/data_views/data_view"
                )
            return [dv for dv in LIVE_DATA_VIEWS if dv["title"] in patterns]

        return _ensure

    def test_the_record_fails_and_the_reason_reaches_the_operator(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_dir = _artifact_dir(tmpdir, "metrics-*.prometheus-*")
            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                side_effect=self._ensure_refusing("metrics-*.prometheus-*"),
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.list_data_views",
                return_value=LIVE_DATA_VIEWS,
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_native_artifact",
                return_value=api.UploadResult(
                    dashboard="dash",
                    dashboard_id="obs-migrate-dash",
                    status="created",
                    mapped=1,
                ),
            ), redirect_stdout(io.StringIO()), redirect_stderr(err):
                payload = KibanaTargetAdapter().upload(
                    artifact_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                )

        record = payload["records"][0]
        self.assertFalse(record["success"])
        self.assertEqual(record["status"], "data_view_unavailable")
        self.assertIn("metrics-*.prometheus-*", record["output"])
        self.assertIn("400", record["output"])
        # uploaded_ok < total is what makes ``obs-migrate upload`` exit 1.
        self.assertEqual(payload["summary"]["uploaded_ok"], 0)
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(
            list(payload["summary"]["data_views_unavailable"]), ["metrics-*.prometheus-*"],
        )
        message = err.getvalue()
        self.assertIn("metrics-*.prometheus-*", message)
        self.assertIn("400", message)

    def test_an_unrelated_artifact_in_the_same_batch_still_succeeds(self):
        # One refused pattern must not cost the operator the other dashboards.
        with tempfile.TemporaryDirectory() as tmpdir:
            _artifact_dir(tmpdir, "metrics-*.prometheus-*", stem="needs_prom")
            artifact_dir = _artifact_dir(tmpdir, "metrics-*", stem="plain")
            with mock.patch(
                "observability_migration.targets.kibana.adapter.ensure_migration_data_views",
                side_effect=self._ensure_refusing("metrics-*.prometheus-*"),
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.list_data_views",
                return_value=LIVE_DATA_VIEWS,
            ), mock.patch(
                "observability_migration.targets.kibana.adapter.dashboards_api.upload_native_artifact",
                side_effect=lambda artifact, *a, **k: api.UploadResult(
                    dashboard=str(artifact.get("title")),
                    dashboard_id=str(artifact.get("dashboard_id")),
                    status="created",
                    mapped=1,
                ),
            ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                payload = KibanaTargetAdapter().upload(
                    artifact_dir,
                    kibana_url="https://kibana.example",
                    kibana_api_key="secret",
                )

        by_artifact = {item["artifact"]: item for item in payload["records"]}
        self.assertTrue(by_artifact["plain.native.json"]["success"])
        self.assertFalse(by_artifact["needs_prom.native.json"]["success"])
        # The defaults were still ensured, so the plain dashboard resolves.
        self.assertEqual(payload["summary"]["uploaded_ok"], 1)
        self.assertEqual(payload["summary"]["total"], 2)


if __name__ == "__main__":
    unittest.main()
