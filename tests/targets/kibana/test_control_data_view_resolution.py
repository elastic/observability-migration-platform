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
import unittest
from contextlib import redirect_stderr
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


if __name__ == "__main__":
    unittest.main()
