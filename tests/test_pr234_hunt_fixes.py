# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression tests for the adversarial bug-hunt findings on PR #234."""

from __future__ import annotations

import argparse
import unittest


class TestServer5xxStatusToken(unittest.TestCase):
    # #5: only a whitespace-delimited 5xx STATUS is a server error; a 500-599
    # substring in a URL/id/port must not be misclassified.
    def test_only_status_token_5xx_counts(self):
        from observability_migration.targets.kibana.render_audit import _server_5xx
        # 403 status, but '500' appears inside the dashboard id -> NOT a server 5xx
        self.assertEqual(
            _server_5xx(["GET /api/saved_objects/dashboard/abc-500-def 403"]), []
        )
        # genuine 5xx with a trailing reason phrase (non-trailing status) -> flagged
        self.assertEqual(
            _server_5xx(["POST /api/dashboards 503 Service Unavailable"]),
            ["POST /api/dashboards 503 Service Unavailable"],
        )
        # trailing 5xx -> flagged
        self.assertEqual(_server_5xx(["GET /api/x 503"]), ["GET /api/x 503"])
        # bare status form
        self.assertEqual(_server_5xx(["500"]), ["500"])
        # a 5xx-looking substring in a port but a 200 status -> not flagged
        self.assertEqual(_server_5xx(["GET http://h:5001/x 200"]), [])


class TestProvenancePlaceholderStatuses(unittest.TestCase):
    # #6: requires_manual / skipped ship markdown placeholders (no live query),
    # so they must classify as PLACEHOLDER, not "ES|QL translated".
    def test_placeholder_statuses(self):
        from observability_migration.core.reporting.summary_md import (
            PanelProvenance,
            classify_panel_provenance,
        )
        for st in ("not_feasible", "requires_manual", "skipped"):
            self.assertEqual(
                classify_panel_provenance(status=st, query="", query_ir={}),
                PanelProvenance.PLACEHOLDER,
                msg=f"{st} must be a placeholder",
            )
        # a real migrated ES|QL panel still classifies as ES|QL translated
        self.assertEqual(
            classify_panel_provenance(
                status="migrated", query="FROM metrics-* | STATS x=AVG(v)", query_ir={}
            ),
            PanelProvenance.ESQL,
        )


class TestForcedNativeSkipsValidator(unittest.TestCase):
    # #7: --translation-mode native must NOT attach the degrading validator, so
    # forced-native queries are emitted (and error at render time) per contract.
    def _apply(self, mode, command_absent):
        from observability_migration.adapters.source.grafana import cli
        from observability_migration.adapters.source.grafana.rules import RulePackConfig
        from observability_migration.adapters.source.grafana.runtime_features import (
            PROMQL_COMMAND_V0,
        )
        supported = not command_absent
        cli._detect_target_runtime_features = lambda *a, **k: {
            PROMQL_COMMAND_V0: {
                "supported": supported, "confidence": "verified",
                "source": "probe", "reason": "test",
            }
        }
        cli._detect_esql_named_param_binding = lambda *a, **k: {
            "supported": True, "confidence": "verified", "source": "probe", "reason": "t",
        }
        rp = RulePackConfig()
        args = argparse.Namespace(
            translation_mode=mode, es_url="http://es", es_api_key="k",
            dataset_filter="", ca_cert="", insecure=True,
        )
        cli._apply_native_promql_to_rule_pack(rp, args)
        return rp

    def test_forced_native_does_not_attach_validator(self):
        rp = self._apply("native", command_absent=True)
        self.assertTrue(rp.native_promql)
        self.assertIsNone(
            getattr(rp, "native_promql_validator", None),
            "forced --translation-mode native must not attach the degrading validator",
        )

    def test_auto_mode_still_attaches_validator(self):
        rp = self._apply("auto", command_absent=False)
        self.assertTrue(rp.native_promql)
        self.assertIsNotNone(getattr(rp, "native_promql_validator", None))


class TestVerifyBlocksOnAuthErrors(unittest.TestCase):
    # #3: a run where the cluster refused every query (auth/security/quota) must
    # not exit 0 / report PASS.
    def test_classify_and_exit_code(self):
        from observability_migration.app import verify as v
        self.assertEqual(
            v.classify_validation(False, "action [indices:data/read/esql] is unauthorized for API key"),
            "blocked",
        )
        self.assertEqual(
            v.classify_validation(False, "circuit_breaking_exception: [parent] Data too large"),
            "blocked",
        )
        acc = {
            "total": 5,
            "counts": {"ok": 0, "real_bug": 0, "data_gap": 0, "blocked": 5, "other": 0, "unreachable": 0},
            "results": [], "unreachable": False,
        }
        report = v.build_report(acceptance=acc, compare=None)
        self.assertEqual(report["verdict"], "BLOCKED")
        self.assertEqual(v.exit_code_for(report), 2)


class TestVerifyThreadsTls(unittest.TestCase):
    # Review B#1: the acceptance gate + reachability probe must pass the resolved
    # TLS verify value to the validator (honor --ca-cert / --insecure), not just
    # the optional compare runner.
    @staticmethod
    def _recording_validator(seen):
        def fake(query, es_url, index_pattern="metrics-*", es_api_key=None, verify=True):
            seen.append(verify)
            return (True, "")
        return fake

    def test_acceptance_gate_passes_verify_to_validator(self):
        from observability_migration.app import verify as v
        seen: list = []
        v.run_acceptance_gate(
            [("d", "p", "FROM x")], es_url="http://es", api_key="k",
            index="metrics-*", validator=self._recording_validator(seen),
            verify="/tmp/ca.pem")
        self.assertEqual(seen, ["/tmp/ca.pem"])

    def test_cluster_reachable_passes_verify_to_validator(self):
        from observability_migration.app import verify as v
        seen: list = []
        v.cluster_reachable(
            "http://es", "k", validator=self._recording_validator(seen), verify=False)
        self.assertEqual(seen, [False])


class TestAgentBrowserCapturesFromSession(unittest.TestCase):
    # Review B#2: --agent-browser must capture the a11y snapshot from the
    # activated agent-browser tab (the logged-in session), not a separate
    # headless Chrome (dump_dom).
    def test_capture_via_session_not_dump_dom(self):
        import json as _json
        import types

        from observability_migration.targets.kibana import render_audit_driver as rad
        kib = "https://kb.example.com"
        calls = []

        def tab_driver(argv):
            calls.append(tuple(argv))
            if argv[:2] == ["tab", "list"]:
                return _json.dumps(
                    {"data": {"tabs": [{"tabId": "t1", "url": f"{kib}/app/dashboards#/view/d1"}]}})
            if argv == ["snapshot"]:
                return 'StaticText "panel" line chart rendered instance_1'
            return ""

        orig = rad.dump_dom
        dump_calls = []
        rad.dump_dom = lambda *a, **k: (dump_calls.append(1) or "DUMP")
        try:
            args = types.SimpleNamespace(
                kibana_url=kib, dashboard_id="d1", space="", user_data_dir="",
                time_from="now-1h", time_to="now", fail_on_error=False, elements=False,
                migration_out="", es_url="", es_api_key="", insecure=False, agent_browser=True)
            rc = rad.run_audit_cli(args, tab_driver=tab_driver)
        finally:
            rad.dump_dom = orig
        self.assertIn(("snapshot",), calls)   # captured from the agent-browser session
        self.assertEqual(dump_calls, [])       # dump_dom (separate Chrome) NOT used
        self.assertEqual(rc, 0)


class TestSecondHuntFixes(unittest.TestCase):
    # Bugs the session-fixes hunt found in this session's OWN fixes.
    def test_scorecard_shows_blocked_line(self):
        # Gate-1 breakdown must reconcile with the total: the blocked bucket
        # was added to verdict/exit but not to render_scorecard.
        from observability_migration.app import verify as v
        acc = {"total": 2,
               "counts": {"ok": 1, "real_bug": 0, "data_gap": 0, "blocked": 1,
                          "other": 0, "unreachable": 0},
               "results": [], "unreachable": False}
        card = v.render_scorecard(v.build_report(acceptance=acc, compare=None, artifact_dir="x"))
        self.assertIn("blocked   : 1", card)

    def test_query_less_migrated_panel_is_placeholder(self):
        # A migrated panel with no executable query (static text/markdown) must
        # not inflate "ES|QL translated".
        from observability_migration.core.reporting.summary_md import (
            PanelProvenance,
            classify_panel_provenance,
        )
        self.assertEqual(
            classify_panel_provenance(status="migrated", query="", query_ir={}),
            PanelProvenance.PLACEHOLDER)
        self.assertEqual(
            classify_panel_provenance(status="migrated", query="FROM x | STATS y=AVG(z)", query_ir={}),
            PanelProvenance.ESQL)

    def test_agent_browser_navigates_to_target_and_settles(self):
        # --agent-browser must navigate to the TARGET dashboard (even when a
        # different Kibana tab is already open) and settle async panels before
        # capture — not snapshot whatever tab is active, mid-load.
        import json as _json
        import types

        from observability_migration.targets.kibana import render_audit_driver as rad
        kib = "https://kb.example.com"
        calls = []

        def tab_driver(argv):
            calls.append(tuple(argv))
            if argv[:2] == ["tab", "list"]:
                # a Kibana tab is open, but it is NOT the target dashboard
                return _json.dumps({"data": {"tabs": [{"tabId": "t2", "url": f"{kib}/app/home"}]}})
            if argv[:1] == ["snapshot"]:
                return 'StaticText "x" line chart rendered'
            return ""

        orig = rad.dump_dom
        dump = []
        rad.dump_dom = lambda *a, **k: (dump.append(1) or "DUMP")
        try:
            args = types.SimpleNamespace(
                kibana_url=kib, dashboard_id="d1", space="", user_data_dir="",
                time_from="now-1h", time_to="now", fail_on_error=True, elements=False,
                migration_out="", es_url="", es_api_key="", insecure=False, agent_browser=True)
            rad.run_audit_cli(args, tab_driver=tab_driver)
        finally:
            rad.dump_dom = orig
        names = [c[0] for c in calls]
        # navigated to the TARGET dashboard, waited, then snapshotted — in order
        self.assertTrue(any(c[0] == "open" and "d1" in c[-1] for c in calls), calls)
        self.assertIn("wait", names)
        self.assertIn("snapshot", names)
        self.assertLess(names.index("open"), names.index("snapshot"))
        self.assertLess(names.index("wait"), names.index("snapshot"))
        self.assertEqual(dump, [])  # not the separate headless Chrome


if __name__ == "__main__":
    unittest.main()
