# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Unit tests for the package-native ``obs-migrate verify`` orchestrator.

The verify orchestrator is unit-testable without a live cluster: it accepts an
injected ES|QL validator (a ``(query) -> (ok, error)`` callable in the shape of
``esql_validate.validate_esql``) and an injected compare runner, so the
classification, scorecard aggregation, exit codes, and coverage-honesty section
can all be exercised offline.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from observability_migration.app import verify as verify_mod

# ------------------------------------------------------------------ #
# Classification of a single emitted-query validation result.
# ------------------------------------------------------------------ #


class TestClassifyValidation(unittest.TestCase):
    def test_ok_when_query_accepted(self):
        self.assertEqual(verify_mod.classify_validation(True, ""), "ok")

    def test_data_gap_unknown_column(self):
        self.assertEqual(
            verify_mod.classify_validation(False, "Unknown column [nginx_foo]"),
            "data_gap",
        )

    def test_data_gap_unknown_index(self):
        self.assertEqual(
            verify_mod.classify_validation(False, "Unknown index [metrics-*]"),
            "data_gap",
        )

    def test_data_gap_no_such_index(self):
        self.assertEqual(
            verify_mod.classify_validation(False, "no such index [foo]"),
            "data_gap",
        )

    def test_real_bug_parsing_exception(self):
        self.assertEqual(
            verify_mod.classify_validation(False, "line 1:23: mismatched input '('"),
            "real_bug",
        )

    def test_real_bug_argument_error(self):
        self.assertEqual(
            verify_mod.classify_validation(
                False, "error building [PERCENTILE]: expects exactly two arguments"
            ),
            "real_bug",
        )

    def test_data_gap_wins_over_real_bug_signal(self):
        # Unknown column is the dominant explanation: a data gap, not a bug,
        # even if a real-bug-looking phrase co-occurs.
        text = "Unknown column [x], line 1:5: something"
        self.assertEqual(verify_mod.classify_validation(False, text), "data_gap")

    def test_unreachable_when_ok_is_none(self):
        # ok is None == transport error (the validate_esql convention).
        self.assertEqual(
            verify_mod.classify_validation(None, "Connection refused"),
            "unreachable",
        )

    def test_other_for_unrecognized_error(self):
        self.assertEqual(
            verify_mod.classify_validation(False, "HTTP 503"),
            "other",
        )


# ------------------------------------------------------------------ #
# Emitted-query extraction from artifacts.
# ------------------------------------------------------------------ #


def _write_packets(d: Path, packets: list[dict]) -> None:
    (d / "verification_packets.json").write_text(
        json.dumps({"summary": {}, "packets": packets}), encoding="utf-8"
    )


def _write_report(d: Path, dashboards: list[dict]) -> None:
    (d / "migration_report.json").write_text(
        json.dumps({"tool": "t", "dashboards": dashboards}), encoding="utf-8"
    )


class TestExtractQueries(unittest.TestCase):
    def test_reads_translated_query_from_packets(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_packets(
                d,
                [
                    {"dashboard": "D", "panel": "P", "translated_query": "FROM a | LIMIT 1"},
                    {"dashboard": "D", "panel": "Q", "translated_query": ""},
                ],
            )
            items = verify_mod.collect_emitted_queries(d)
            self.assertEqual(items, [("D", "P", "FROM a | LIMIT 1")])

    def test_falls_back_to_report_esql_query(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_report(
                d,
                [
                    {
                        "title": "Dash",
                        "panels": [
                            {"title": "Pan", "esql_query": "FROM b | LIMIT 1"},
                            {"title": "Empty", "esql_query": ""},
                        ],
                    }
                ],
            )
            items = verify_mod.collect_emitted_queries(d)
            self.assertEqual(items, [("Dash", "Pan", "FROM b | LIMIT 1")])

    def test_merges_packets_and_report_dedup_by_query(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_packets(d, [{"dashboard": "D", "panel": "P", "translated_query": "FROM a"}])
            _write_report(
                d,
                [{"title": "D", "panels": [{"title": "P", "esql_query": "FROM a"}]}],
            )
            items = verify_mod.collect_emitted_queries(d)
            # Same query text appears in both sources; collect keeps both
            # occurrences but the acceptance gate dedups by query when running.
            self.assertIn(("D", "P", "FROM a"), items)


# ------------------------------------------------------------------ #
# Acceptance gate: runs queries through an injected validator and dedups.
# ------------------------------------------------------------------ #


class _FakeValidator:
    """Records calls; returns scripted (ok, error) by query text."""

    def __init__(self, responses: dict[str, tuple]):
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, query, es_url, index_pattern="metrics-*", es_api_key=None, verify=True):
        self.calls.append(query)
        return self.responses.get(query, (True, ""))


class TestAcceptanceGate(unittest.TestCase):
    def test_dedups_identical_queries(self):
        v = _FakeValidator({"FROM a": (True, "")})
        items = [("D", "P1", "FROM a"), ("D", "P2", "FROM a"), ("D", "P3", "FROM a")]
        gate = verify_mod.run_acceptance_gate(
            items, es_url="http://es", api_key="k", index="metrics-*", validator=v
        )
        self.assertEqual(len(v.calls), 1)  # deduped
        self.assertEqual(gate["counts"]["ok"], 1)
        self.assertEqual(gate["total"], 1)

    def test_classifies_each_result(self):
        v = _FakeValidator(
            {
                "FROM ok": (True, ""),
                "FROM bug": (False, "line 1:1: mismatched input"),
                "FROM gap": (False, "Unknown column [x]"),
            }
        )
        items = [
            ("D", "ok", "FROM ok"),
            ("D", "bug", "FROM bug"),
            ("D", "gap", "FROM gap"),
        ]
        gate = verify_mod.run_acceptance_gate(
            items, es_url="http://es", api_key="k", index="metrics-*", validator=v
        )
        self.assertEqual(gate["counts"]["ok"], 1)
        self.assertEqual(gate["counts"]["real_bug"], 1)
        self.assertEqual(gate["counts"]["data_gap"], 1)
        bugs = [r for r in gate["results"] if r["classification"] == "real_bug"]
        self.assertEqual(bugs[0]["panel"], "bug")

    def test_unreachable_detected_and_flagged(self):
        v = _FakeValidator({"FROM x": (None, "Connection refused")})
        gate = verify_mod.run_acceptance_gate(
            [("D", "P", "FROM x")],
            es_url="http://es",
            api_key="k",
            index="metrics-*",
            validator=v,
        )
        self.assertTrue(gate["unreachable"])
        self.assertEqual(gate["counts"]["unreachable"], 1)


# ------------------------------------------------------------------ #
# Reachability preflight.
# ------------------------------------------------------------------ #


class TestReachability(unittest.TestCase):
    def test_reachable_when_probe_ok(self):
        v = _FakeValidator({})  # default returns (True, "")
        self.assertTrue(
            verify_mod.cluster_reachable("http://es", "k", validator=v)
        )

    def test_unreachable_when_probe_transport_error(self):
        def v(query, es_url, index_pattern="metrics-*", es_api_key=None, verify=True):
            return (None, "Max retries exceeded")

        self.assertFalse(verify_mod.cluster_reachable("http://es", "k", validator=v))

    def test_reachable_even_when_probe_returns_http_error(self):
        # A 4xx/5xx HTTP error (ok is False) still proves we reached the cluster.
        def v(query, es_url, index_pattern="metrics-*", es_api_key=None, verify=True):
            return (False, "HTTP 400")

        self.assertTrue(verify_mod.cluster_reachable("http://es", "k", validator=v))


# ------------------------------------------------------------------ #
# Scorecard verdict + exit code.
# ------------------------------------------------------------------ #


class TestVerdictAndExit(unittest.TestCase):
    def _gate(self, counts, unreachable=False):
        return {
            "total": sum(counts.values()),
            "counts": {"ok": 0, "real_bug": 0, "data_gap": 0, "other": 0, "unreachable": 0, **counts},
            "results": [],
            "unreachable": unreachable,
        }

    def test_pass_when_all_ok(self):
        report = verify_mod.build_report(
            acceptance=self._gate({"ok": 5}), compare=None
        )
        self.assertEqual(report["verdict"], "PASS")
        self.assertEqual(verify_mod.exit_code_for(report), 0)

    def test_attention_on_data_gap_but_no_bug(self):
        report = verify_mod.build_report(
            acceptance=self._gate({"ok": 3, "data_gap": 2}), compare=None
        )
        self.assertEqual(report["verdict"], "ATTENTION")
        # data gaps are a warn, not a fail -> exit 0
        self.assertEqual(verify_mod.exit_code_for(report), 0)

    def test_fail_exit_on_real_bug(self):
        report = verify_mod.build_report(
            acceptance=self._gate({"ok": 3, "real_bug": 1}), compare=None
        )
        self.assertEqual(report["verdict"], "ATTENTION")
        self.assertEqual(verify_mod.exit_code_for(report), 1)

    def test_unreachable_exit_2(self):
        report = verify_mod.build_report(
            acceptance=self._gate({}, unreachable=True), compare=None
        )
        self.assertEqual(verify_mod.exit_code_for(report), 2)

    def test_compare_fail_drives_exit_1(self):
        compare = {"ran": True, "summary": {"panels": 4, "FAIL": 1, "STRICT_PASS": 3}}
        report = verify_mod.build_report(
            acceptance=self._gate({"ok": 4}), compare=compare
        )
        self.assertEqual(verify_mod.exit_code_for(report), 1)

    def test_compare_error_drives_exit_1(self):
        compare = {"ran": True, "summary": {"panels": 4, "ERROR": 1, "STRICT_PASS": 3}}
        report = verify_mod.build_report(
            acceptance=self._gate({"ok": 4}), compare=compare
        )
        self.assertEqual(verify_mod.exit_code_for(report), 1)

    def test_compare_not_run_does_not_fail(self):
        compare = {"ran": False, "reason": "no data on either side"}
        report = verify_mod.build_report(
            acceptance=self._gate({"ok": 4}), compare=compare
        )
        self.assertEqual(verify_mod.exit_code_for(report), 0)


# ------------------------------------------------------------------ #
# Coverage-honesty section.
# ------------------------------------------------------------------ #


class TestCoverageHonesty(unittest.TestCase):
    def test_lists_repo_only_gates_not_run(self):
        gates = verify_mod.uncovered_gates()
        names = " ".join(g["gate"] for g in gates).lower()
        self.assertIn("dashboards_api", names)
        self.assertIn("render", names)
        # Each entry points at the exact command to run it.
        for g in gates:
            self.assertTrue(g["command"])
            self.assertIn("parity-rig", g["command"])

    def test_report_embeds_coverage_section(self):
        report = verify_mod.build_report(
            acceptance={"total": 1, "counts": {"ok": 1}, "results": [], "unreachable": False},
            compare=None,
        )
        self.assertIn("not_run_gates", report)
        self.assertTrue(report["not_run_gates"])


# ------------------------------------------------------------------ #
# Scorecard rendering (human-readable).
# ------------------------------------------------------------------ #


class TestScorecardRendering(unittest.TestCase):
    def test_render_includes_counts_and_verdict_and_honesty(self):
        report = verify_mod.build_report(
            acceptance={
                "total": 3,
                "counts": {"ok": 2, "real_bug": 1, "data_gap": 0, "other": 0, "unreachable": 0},
                "results": [
                    {"dashboard": "D", "panel": "Bad", "classification": "real_bug",
                     "error": "line 1:1: mismatched input", "query": "FROM x"},
                ],
                "unreachable": False,
            },
            compare={"ran": True, "summary": {"panels": 3, "STRICT_PASS": 3}},
        )
        text = verify_mod.render_scorecard(report)
        self.assertIn("Emitted-query acceptance", text)
        self.assertIn("real_bug", text)
        self.assertIn("Numeric parity", text)
        self.assertIn("Gates NOT run", text)
        self.assertIn("dashboards_api", text)
        # The verdict line is present.
        self.assertIn(report["verdict"], text)


# ------------------------------------------------------------------ #
# End-to-end orchestration via run_verify with injected seams.
# ------------------------------------------------------------------ #


class TestRunVerify(unittest.TestCase):
    def test_missing_artifact_dir_exit_2(self):
        code = verify_mod.run_verify(
            artifact_dir="/does/not/exist",
            es_url="http://es",
            api_key="k",
            index="metrics-*",
            report_out=None,
            run_compare=False,
            validator=_FakeValidator({}),
            compare_runner=None,
        )
        self.assertEqual(code, 2)

    def test_missing_credentials_exit_2(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_packets(d, [{"dashboard": "D", "panel": "P", "translated_query": "FROM a"}])
            code = verify_mod.run_verify(
                artifact_dir=str(d),
                es_url="",
                api_key="",
                index="metrics-*",
                report_out=None,
                run_compare=False,
                validator=_FakeValidator({}),
                compare_runner=None,
            )
            self.assertEqual(code, 2)

    def test_clean_run_exit_0_and_writes_report(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_packets(
                d,
                [
                    {"dashboard": "D", "panel": "P1", "translated_query": "FROM a"},
                    {"dashboard": "D", "panel": "P2", "translated_query": "FROM b"},
                ],
            )
            out = Path(tmp) / "verify_report.json"
            v = _FakeValidator({"FROM a": (True, ""), "FROM b": (True, "")})
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = verify_mod.run_verify(
                    artifact_dir=str(d),
                    es_url="http://es",
                    api_key="k",
                    index="metrics-*",
                    report_out=str(out),
                    run_compare=False,
                    validator=v,
                    compare_runner=None,
                )
            self.assertEqual(code, 0)
            self.assertTrue(out.exists())
            written = json.loads(out.read_text())
            self.assertEqual(written["verdict"], "PASS")
            self.assertIn("Emitted-query acceptance", buf.getvalue())

    def test_real_bug_exit_1(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_packets(
                d, [{"dashboard": "D", "panel": "P", "translated_query": "FROM broken"}]
            )
            v = _FakeValidator({"FROM broken": (False, "line 1:1: mismatched input '('")})
            with redirect_stdout(io.StringIO()):
                code = verify_mod.run_verify(
                    artifact_dir=str(d),
                    es_url="http://es",
                    api_key="k",
                    index="metrics-*",
                    report_out=None,
                    run_compare=False,
                    validator=v,
                    compare_runner=None,
                )
            self.assertEqual(code, 1)

    def test_unreachable_exit_2_via_run(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_packets(d, [{"dashboard": "D", "panel": "P", "translated_query": "FROM a"}])

            def v(query, es_url, index_pattern="metrics-*", es_api_key=None, verify=True):
                return (None, "Connection refused")

            with redirect_stdout(io.StringIO()):
                code = verify_mod.run_verify(
                    artifact_dir=str(d),
                    es_url="http://es",
                    api_key="k",
                    index="metrics-*",
                    report_out=None,
                    run_compare=False,
                    validator=v,
                    compare_runner=None,
                )
            self.assertEqual(code, 2)

    def test_compare_runner_invoked_and_surfaced(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_packets(d, [{"dashboard": "D", "panel": "P", "translated_query": "FROM a"}])

            def compare_runner(**kwargs):
                return {"ran": True, "summary": {"panels": 1, "STRICT_PASS": 1}}

            with redirect_stdout(io.StringIO()):
                code = verify_mod.run_verify(
                    artifact_dir=str(d),
                    es_url="http://es",
                    api_key="k",
                    index="metrics-*",
                    report_out=str(Path(tmp) / "r.json"),
                    run_compare=True,
                    validator=_FakeValidator({"FROM a": (True, "")}),
                    compare_runner=compare_runner,
                )
            self.assertEqual(code, 0)
            written = json.loads((Path(tmp) / "r.json").read_text())
            self.assertTrue(written["compare"]["ran"])

    def test_compare_fail_exit_1_via_run(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_packets(d, [{"dashboard": "D", "panel": "P", "translated_query": "FROM a"}])

            def compare_runner(**kwargs):
                return {"ran": True, "summary": {"panels": 1, "FAIL": 1}}

            with redirect_stdout(io.StringIO()):
                code = verify_mod.run_verify(
                    artifact_dir=str(d),
                    es_url="http://es",
                    api_key="k",
                    index="metrics-*",
                    report_out=None,
                    run_compare=True,
                    validator=_FakeValidator({"FROM a": (True, "")}),
                    compare_runner=compare_runner,
                )
            self.assertEqual(code, 1)


# ------------------------------------------------------------------ #
# CLI wiring (subparser exists and dispatches).
# ------------------------------------------------------------------ #


class TestCliWiring(unittest.TestCase):
    def test_verify_subparser_parses(self):
        from observability_migration.app import cli as app_cli

        parser = app_cli._build_parser()
        args = parser.parse_args(
            [
                "verify",
                "--artifact-dir",
                "/tmp/out/dashboards",
                "--es-url",
                "http://es",
                "--api-key",
                "k",
            ]
        )
        self.assertEqual(args.command, "verify")
        self.assertEqual(args.artifact_dir, "/tmp/out/dashboards")
        self.assertEqual(args.es_url, "http://es")
        self.assertEqual(args.api_key, "k")


if __name__ == "__main__":
    unittest.main()
