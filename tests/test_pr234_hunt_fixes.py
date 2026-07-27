# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression tests for the adversarial bug-hunt findings on PR #234."""

from __future__ import annotations

import argparse
import os
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


class TestVerifyClassifiesAuthAsOther(unittest.TestCase):
    # De-scoped (hunt #3): the dedicated 'blocked' bucket was removed. Its regex
    # (`\b429\b`) misclassified real ES|QL errors (e.g. `line 1:429:`) as blocked,
    # and its exit-code precedence let a transient quota error mask real bugs.
    # Auth/security/quota errors now fall through to 'other' (a warn) -- the
    # simpler, robust contract. A fully auth-blocked run is an accepted
    # limitation, not a hard fail.
    def test_auth_and_quota_classify_as_other(self):
        from observability_migration.app import verify as v
        self.assertEqual(
            v.classify_validation(False, "action [indices:data/read/esql] is unauthorized for API key"),
            "other",
        )
        self.assertEqual(
            v.classify_validation(False, "circuit_breaking_exception: [parent] Data too large"),
            "other",
        )

    def test_429_in_error_column_is_not_misclassified(self):
        # The root false_gate: a real ES|QL error whose column position is 429
        # must classify by its actual content, never as an infra/quota signal
        # (the removed 'blocked' bucket matched the bare '429' in 'line 1:429:').
        from observability_migration.app import verify as v
        # content -> data_gap (an unknown column), NOT 'blocked'
        self.assertEqual(
            v.classify_validation(False, "line 1:429: Unknown column [foo]"),
            "data_gap",
        )
        # content -> real_bug (a parse error), NOT 'blocked'
        self.assertEqual(
            v.classify_validation(False, "line 1:429: mismatched input 'FROM'"),
            "real_bug",
        )

    def test_real_bug_exit_code_not_demoted_by_quota(self):
        # A coexisting quota/'other' result must not demote a real_bug from the
        # hard-fail exit code (1).
        from observability_migration.app import verify as v
        acc = {
            "total": 5,
            "counts": {"ok": 2, "real_bug": 1, "data_gap": 0, "other": 2, "unreachable": 0},
            "results": [], "unreachable": False,
        }
        report = v.build_report(acceptance=acc, compare=None)
        self.assertEqual(v.exit_code_for(report), 1)


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


class TestProvenanceEmptyQueryIsEsql(unittest.TestCase):
    # De-scoped (hunt #3): the empty-query -> PLACEHOLDER branch was reverted.
    # It mis-classified successfully-migrated Datadog Lens panels (whose query
    # lives off the classify input) as PLACEHOLDER, deflating the ES|QL count and
    # inflating "not migrated". A migrated panel with a blank query string now
    # classifies as ES|QL; only status (not_feasible/requires_manual/skipped) or
    # a native-PROMQL marker change the bucket.
    def test_migrated_blank_query_is_esql_not_placeholder(self):
        from observability_migration.core.reporting.summary_md import (
            PanelProvenance,
            classify_panel_provenance,
        )
        self.assertEqual(
            classify_panel_provenance(status="migrated", query="", query_ir={}),
            PanelProvenance.ESQL)
        self.assertEqual(
            classify_panel_provenance(status="migrated", query="FROM x | STATS y=AVG(z)", query_ir={}),
            PanelProvenance.ESQL)
        # status-based placeholders still win regardless of query
        self.assertEqual(
            classify_panel_provenance(status="not_feasible", query="", query_ir={}),
            PanelProvenance.PLACEHOLDER)


class TestAgentBrowserUsesHeadlessCapture(unittest.TestCase):
    # De-scoped (hunt #3): --agent-browser is now a tab-selection helper only;
    # DOM capture always goes through the headless dump_dom path (which reads
    # HTML, so CSS-class markers like embPanel__error are visible, and navigates
    # to the exact target URL). The fragile a11y-snapshot capture was removed.
    def test_agent_browser_activates_tab_then_captures_via_dump_dom(self):
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
        names = [c[0] for c in calls]
        self.assertIn(("tab", "list", "--json"), calls)  # tab selection still runs
        self.assertEqual(dump_calls, [1])                # captured via headless dump_dom
        self.assertNotIn("snapshot", names)              # no fragile a11y capture
        self.assertEqual(rc, 0)


class TestHunt4MidSweepDrop(unittest.TestCase):
    # #1: a cluster that drops mid-sweep must NOT demote an already-found
    # real_bug from exit 1 to exit 2 (CI reads 2 as 'infra flaky, non-fatal').
    def _validator(self, query, es_url, index_pattern="metrics-*", es_api_key=None, verify=True):
        if "ROW" in query:
            return (True, "")          # reachability preflight passes
        if query == "FROM broken":
            return (False, "line 1:1: mismatched input '('")   # real_bug
        if query == "FROM drops":
            return (None, "Connection reset by peer")          # mid-sweep drop
        return (True, "")

    def _artifact(self):
        import json as _json
        import tempfile
        d = tempfile.mkdtemp()
        packets = {"packets": [
            {"dashboard": "D", "panel": "p1", "translated_query": "FROM broken"},
            {"dashboard": "D", "panel": "p2", "translated_query": "FROM drops"},
        ]}
        with open(os.path.join(d, "verification_packets.json"), "w") as fh:
            _json.dump(packets, fh)
        return d

    def test_real_bug_survives_mid_sweep_drop(self):
        from observability_migration.app import verify as v
        rc = v.run_verify(
            artifact_dir=self._artifact(), es_url="http://es", api_key="k",
            report_out=None, validator=self._validator,
        )
        self.assertEqual(rc, 1, "a real_bug must produce exit 1 even if the cluster dropped later")

    def test_verdict_consistent_with_counts_on_mid_sweep_drop(self):
        # The scorecard verdict must not read UNREACHABLE while showing a real_bug.
        from observability_migration.app import verify as v
        acc = {"total": 2, "unreachable": True,
               "counts": {"ok": 0, "real_bug": 1, "data_gap": 0, "other": 0, "unreachable": 1},
               "results": []}
        report = v.build_report(acceptance=acc, compare=None)
        self.assertEqual(report["verdict"], "ATTENTION")
        self.assertEqual(v.exit_code_for(report), 1)
        # pure unreachable (no real_bug) still reads UNREACHABLE / exit 2
        acc2 = {"total": 1, "unreachable": True,
                "counts": {"ok": 0, "real_bug": 0, "data_gap": 0, "other": 0, "unreachable": 1},
                "results": []}
        report2 = v.build_report(acceptance=acc2, compare=None)
        self.assertEqual(report2["verdict"], "UNREACHABLE")
        self.assertEqual(v.exit_code_for(report2), 2)


class TestHunt4Provenance(unittest.TestCase):
    # #2 + #11: a 'blocked' panel and a non-data visual (markdown/text/image/
    # iframe) carry no executable query and must NOT inflate "ES|QL translated".
    def test_blocked_status_is_placeholder(self):
        from observability_migration.core.reporting.summary_md import (
            PanelProvenance,
            classify_panel_provenance,
        )
        self.assertEqual(
            classify_panel_provenance(status="blocked", query="", query_ir={}),
            PanelProvenance.PLACEHOLDER)

    def test_migrated_markdown_widget_is_placeholder(self):
        from observability_migration.core.reporting.summary_md import (
            PanelProvenance,
            classify_panel_provenance,
        )
        # a successfully-migrated note/markdown widget (no live query)
        self.assertEqual(
            classify_panel_provenance(
                status="ok", query="", query_ir={}, kibana_type="markdown"),
            PanelProvenance.PLACEHOLDER)
        # a real Lens data panel (blank query at this layer) stays ES|QL — the
        # de-scope guarantee that we don't mis-placeholder Datadog Lens panels
        self.assertEqual(
            classify_panel_provenance(
                status="ok", query="", query_ir={}, kibana_type="xy"),
            PanelProvenance.ESQL)

    def test_datadog_migrated_markdown_widget_counted_as_placeholder(self):
        from observability_migration.adapters.source.datadog.models import (
            DashboardResult,
            TranslationResult,
        )
        from observability_migration.adapters.source.datadog.report import (
            build_summary_view,
        )
        ok = TranslationResult(title="Req", kibana_type="xy", status="ok",
                               esql_query="FROM m | STATS sum(v)", query_ir={"family": "range_agg"})
        note = TranslationResult(title="Note", kibana_type="markdown", status="ok")
        blk = TranslationResult(title="Blk", kibana_type="metric", status="blocked")
        dr = DashboardResult(dashboard_id="d", dashboard_title="D")
        dr.panel_results = [ok, note, blk]
        view = build_summary_view([dr], run_id="d")
        self.assertEqual(view.totals.esql_translated, 1)   # only the xy panel
        self.assertEqual(view.totals.placeholder, 2)       # note + blocked


class TestHunt4RenderClassifier(unittest.TestCase):
    # #3/#6: a render marker is only downgraded to field_gap (warn) when the
    # panel text actually names an absent column/field. A translator/ES|QL bug
    # marker stays a hard render_error even if a breakdown field happens to be
    # absent from the target.
    def test_translator_bug_marker_stays_render_error(self):
        from observability_migration.targets.kibana.render_audit import classify_panel
        r = classify_panel(
            "ts", "embPanel__error is not yet implemented: histogram_quantile",
            breakdown_fields=["method"], available_fields=["instance"],
        )
        self.assertEqual(r.error_class, "render_error")
        r2 = classify_panel(
            "ts", "embPanel__error verification_exception: Output has changed from",
            breakdown_fields=["method"], available_fields=["instance"],
        )
        self.assertEqual(r2.error_class, "render_error")

    def test_column_absence_marker_with_missing_breakdown_is_field_gap(self):
        from observability_migration.targets.kibana.render_audit import classify_panel
        r = classify_panel(
            "ts", "embPanel__error Unknown column [method]",
            breakdown_fields=["method"], available_fields=["instance"],
        )
        self.assertEqual(r.error_class, "field_gap")
        self.assertEqual(r.missing_fields, ["method"])

    def test_malformed_migration_report_does_not_crash(self):
        # #5: a malformed migration_report.json must not crash run_audit_cli; it
        # degrades to the whole-dashboard render classification.
        import tempfile
        import types

        from observability_migration.targets.kibana import render_audit_driver as rad
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "migration_report.json"), "w") as fh:
            fh.write("{ not valid json")
        args = types.SimpleNamespace(
            kibana_url="http://k", dashboard_id="x", space="", user_data_dir="",
            time_from="now-1h", time_to="now", fail_on_error=False, elements=False,
            migration_out=d, es_url="", es_api_key="", insecure=False, agent_browser=False)
        rc = rad.run_audit_cli(args, dom_fetcher=lambda u: "<div>ok</div>")
        self.assertEqual(rc, 0)


class TestHunt4CoreEngine(unittest.TestCase):
    # Hunt #4: three core-engine translation bugs that silently emit invalid
    # ES|QL / unsupported native PROMQL (reproduced by hand before fixing).

    def test_default_step_subquery_not_native_eligible(self):
        # #8: a default-step subquery `[range:]` is unsupported by Elastic native
        # PROMQL and must degrade to ES|QL, like the explicit-step `[range:step]`.
        from observability_migration.adapters.source.grafana.panels import (
            can_use_native_promql,
        )
        self.assertFalse(can_use_native_promql("max_over_time(rate(http_requests_total[5m])[30m:])"))
        self.assertFalse(can_use_native_promql("avg_over_time(node_load1[1h:])"))
        # explicit-step subquery stays blocked; a plain range selector stays eligible
        self.assertFalse(can_use_native_promql("max_over_time(rate(http_requests_total[5m])[30m:1m])"))
        self.assertTrue(can_use_native_promql("rate(http_requests_total[5m])"))

    def test_bare_range_func_filter_splits_window_out_of_case(self):
        # #9: a top-level counter range function (RATE/IRATE/INCREASE/DELTA/DERIV)
        # with a per-operand filter must keep the window as the function's 2nd
        # argument, not fold it into the CASE.
        from observability_migration.adapters.source.grafana import (
            rules,
            schema,
            translate,
        )
        rp = rules.RulePackConfig()
        res = schema.SchemaResolver(rp)
        r = translate.translate_promql_to_esql(
            'rate(a_total{code="200"}[5m]) + rate(b_total{code="500"}[5m])',
            esql_index="metrics-*", panel_type="timeseries", rule_pack=rp, resolver=res,
        )
        # Outer CASE around RATE — never RATE(CASE(...)) which ClassCasts on ES.
        # Window stays the 2nd arg of RATE, not folded into CASE.
        self.assertNotIn("5m, NULL", r.esql_query)
        self.assertNotIn("RATE(CASE(", r.esql_query)
        self.assertIn('CASE((code == "200"), RATE(a_total, 5m), NULL)', r.esql_query)
        self.assertIn('CASE((code == "500"), RATE(b_total, 5m), NULL)', r.esql_query)

    def test_topk_bare_counter_uses_ts_source(self):
        # #10: topk on a bare counter auto-rates the metric, so it must run under
        # the TS command (RATE is invalid under FROM).
        from observability_migration.adapters.source.grafana import (
            rules,
            schema,
            translate,
        )
        rp = rules.RulePackConfig()
        res = schema.SchemaResolver(rp)
        r = translate.translate_promql_to_esql(
            "topk(10, container_cpu_usage_seconds_total)",
            esql_index="metrics-*", panel_type="graph", rule_pack=rp, resolver=res,
        )
        self.assertTrue(r.esql_query.startswith("TS metrics-*"), r.esql_query)
        self.assertIn("RATE(", r.esql_query)
        self.assertNotIn("FROM metrics-*", r.esql_query)
        # a bare gauge stays under FROM (no auto-rate)
        rg = translate.translate_promql_to_esql(
            "topk(10, node_load1)",
            esql_index="metrics-*", panel_type="graph", rule_pack=rp, resolver=res,
        )
        self.assertTrue(rg.esql_query.startswith("FROM metrics-*"), rg.esql_query)


if __name__ == "__main__":
    unittest.main()
