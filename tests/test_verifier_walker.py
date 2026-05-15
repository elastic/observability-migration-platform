# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for parity-rig.verifier.walker.

Covers the parts of the walker that are testable without a live
``agent-browser`` daemon:

* fingerprint extraction
* HAR parsing + panel-to-HAR correlation
* the merge-mode overlay (walker evidence onto an existing verifier
  JSON, preserving the existing verdict)
* import-safety when ``agent-browser`` is not on PATH

The walker's actual browser orchestration (``Walker.run``) is exercised
only via mocked subprocess to confirm the call sequence; it is *not*
run against a real browser because that requires a SAML-completed
bootstrap.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
VERIFIER_PARENT = ROOT / "parity-rig"
sys.path.insert(0, str(VERIFIER_PARENT))

from verifier import walker  # noqa: E402
from verifier.records import PanelRecord, Verdict  # noqa: E402

# --------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------- #


def _make_har(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap a list of synthetic HAR entries in a valid HAR 1.2 envelope."""
    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "test", "version": "1.0"},
            "entries": entries,
        }
    }


def _query_entry(
    *,
    url: str = "https://kibana.example.com/internal/esql/_query",
    method: str = "POST",
    request_body: str = "",
    response_body: str = "",
    status: int = 200,
    started_at: str = "2026-05-12T10:00:00.000Z",
    encoding: str | None = None,
) -> dict[str, Any]:
    """Build one synthetic HAR entry that the walker should match."""
    content: dict[str, Any] = {
        "size": len(response_body),
        "mimeType": "application/json",
        "text": response_body,
    }
    if encoding:
        content["encoding"] = encoding
    return {
        "startedDateTime": started_at,
        "time": 12.3,
        "request": {
            "method": method,
            "url": url,
            "httpVersion": "HTTP/1.1",
            "headers": [],
            "queryString": [],
            "cookies": [],
            "headersSize": -1,
            "bodySize": len(request_body),
            "postData": {
                "mimeType": "application/json",
                "text": request_body,
            },
        },
        "response": {
            "status": status,
            "statusText": "OK" if status < 400 else "ERR",
            "httpVersion": "HTTP/1.1",
            "headers": [],
            "cookies": [],
            "content": content,
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": len(response_body),
        },
        "cache": {},
        "timings": {"send": 0, "wait": 0, "receive": 0},
    }


def _write_har(tmp_path: Path, entries: list[dict[str, Any]]) -> Path:
    p = tmp_path / "run.har"
    p.write_text(json.dumps(_make_har(entries)), encoding="utf-8")
    return p


# --------------------------------------------------------------------- #
# Fingerprint extraction
# --------------------------------------------------------------------- #


class TestExtractFingerprint:
    def test_first_60_non_whitespace_chars(self):
        esql = "FROM metrics-* | STATS x = COUNT(*) BY host | LIMIT 100"
        fp = walker.extract_fingerprint(esql)
        # 60 chars max, no whitespace.
        assert " " not in fp
        assert len(fp) <= 60
        # Order-preserving compression of the input.
        assert fp.startswith("FROMmetrics-*|STATSx=COUNT(*)BYhost|LIMIT100")

    def test_longer_inputs_truncate(self):
        esql = "FROM " + ("a" * 200)
        fp = walker.extract_fingerprint(esql)
        assert len(fp) == walker.FINGERPRINT_CHAR_BUDGET
        assert fp.startswith("FROMaaaa")

    def test_whitespace_only_input_yields_empty(self):
        assert walker.extract_fingerprint("    \n   \t") == ""

    def test_empty_input_yields_empty(self):
        assert walker.extract_fingerprint("") == ""

    def test_short_inputs_returned_intact(self):
        assert walker.extract_fingerprint("FROM x | LIMIT 1") == "FROMx|LIMIT1"

    def test_budget_override(self):
        fp = walker.extract_fingerprint("FROM metrics-* | LIMIT 1", budget=10)
        assert fp == "FROMmetric"


# --------------------------------------------------------------------- #
# HAR parsing
# --------------------------------------------------------------------- #


class TestParseHar:
    def test_extracts_only_query_entries(self, tmp_path):
        har_path = _write_har(
            tmp_path,
            [
                _query_entry(
                    url="https://kib.example.com/internal/esql/_query",
                    request_body=json.dumps({"query": "FROM metrics-* | LIMIT 1"}),
                ),
                _query_entry(
                    url="https://kib.example.com/api/spaces/space/default",
                    request_body="",
                ),
                _query_entry(
                    url="https://kib.example.com/api/esql/async_query",
                    request_body=json.dumps({"query": "FROM other-* | LIMIT 1"}),
                ),
            ],
        )
        entries = walker.parse_har_for_query_entries(har_path)
        assert len(entries) == 1
        assert entries[0].url.endswith("/_query")
        assert "metrics-*" in entries[0].request_body

    def test_missing_har_returns_empty_list(self, tmp_path):
        assert walker.parse_har_for_query_entries(tmp_path / "does-not-exist.har") == []

    def test_malformed_har_returns_empty_list(self, tmp_path):
        p = tmp_path / "bad.har"
        p.write_text("not json", encoding="utf-8")
        assert walker.parse_har_for_query_entries(p) == []

    def test_base64_response_text_dropped(self, tmp_path):
        har_path = _write_har(
            tmp_path,
            [
                _query_entry(
                    request_body=json.dumps({"query": "FROM x"}),
                    response_body="aGVsbG8=",
                    encoding="base64",
                ),
            ],
        )
        entries = walker.parse_har_for_query_entries(har_path)
        assert entries[0].response_body == ""

    def test_response_status_captured(self, tmp_path):
        har_path = _write_har(
            tmp_path,
            [
                _query_entry(
                    request_body=json.dumps({"query": "FROM x"}),
                    response_body=json.dumps({"columns": [], "values": []}),
                    status=200,
                ),
            ],
        )
        entries = walker.parse_har_for_query_entries(har_path)
        assert entries[0].response_status == 200

    def test_query_string_in_url_still_matches(self, tmp_path):
        # Some Kibana paths append ?drop_null_columns=true; the path
        # suffix should still match.
        har_path = _write_har(
            tmp_path,
            [
                _query_entry(
                    url="https://kib.example.com/internal/esql/_query?drop_null_columns=true",
                    request_body=json.dumps({"query": "FROM x"}),
                ),
            ],
        )
        assert len(walker.parse_har_for_query_entries(har_path)) == 1


# --------------------------------------------------------------------- #
# Fingerprint -> HAR matching
# --------------------------------------------------------------------- #


class TestMatchHarToPanels:
    def test_matching_panel_finds_its_entry(self, tmp_path):
        matching_query = "FROM metrics-* | STATS x = COUNT(*) BY host"
        nonmatching_query = "FROM logs-* | STATS y = COUNT(*) BY service"
        entries = walker.parse_har_for_query_entries(
            _write_har(
                tmp_path,
                [
                    _query_entry(request_body=json.dumps({"query": nonmatching_query})),
                    _query_entry(request_body=json.dumps({"query": matching_query})),
                ],
            )
        )
        fingerprints = {"P1": walker.extract_fingerprint(matching_query)}
        matched = walker.match_har_to_panels(entries, fingerprints)
        assert set(matched.keys()) == {"P1"}
        assert "metrics-*" in matched["P1"].request_body

    def test_panel_with_no_har_match_is_omitted(self, tmp_path):
        entries = walker.parse_har_for_query_entries(
            _write_har(
                tmp_path,
                [
                    _query_entry(
                        request_body=json.dumps({"query": "FROM unrelated-* | LIMIT 1"})
                    ),
                ],
            )
        )
        fingerprints = {"P1": walker.extract_fingerprint("FROM metrics-* | LIMIT 1")}
        matched = walker.match_har_to_panels(entries, fingerprints)
        assert matched == {}

    def test_empty_fingerprint_dropped(self, tmp_path):
        entries = walker.parse_har_for_query_entries(
            _write_har(
                tmp_path,
                [_query_entry(request_body=json.dumps({"query": "FROM x"}))],
            )
        )
        matched = walker.match_har_to_panels(entries, {"P1": ""})
        assert matched == {}

    def test_first_matching_entry_wins(self, tmp_path):
        query = "FROM metrics-* | LIMIT 1"
        entries = walker.parse_har_for_query_entries(
            _write_har(
                tmp_path,
                [
                    _query_entry(
                        request_body=json.dumps({"query": query}),
                        started_at="2026-05-12T10:00:00.000Z",
                    ),
                    _query_entry(
                        request_body=json.dumps({"query": query}),
                        started_at="2026-05-12T10:00:05.000Z",
                    ),
                ],
            )
        )
        fingerprints = {"P1": walker.extract_fingerprint(query)}
        matched = walker.match_har_to_panels(entries, fingerprints)
        assert matched["P1"].started_at == "2026-05-12T10:00:00.000Z"


# --------------------------------------------------------------------- #
# build_panel_evidence — the integration of HAR + fingerprint + screenshot
# --------------------------------------------------------------------- #


class TestBuildPanelEvidence:
    def test_panel_with_matched_har_records_query_and_response_shape(self, tmp_path):
        query = "FROM metrics-* | STATS x = COUNT(*)"
        response_body = json.dumps(
            {
                "columns": [{"name": "x", "type": "long"}],
                "values": [[42]],
            }
        )
        entry = walker.HarQueryEntry(
            url="https://kib.example.com/internal/esql/_query",
            method="POST",
            request_body=json.dumps({"query": query}),
            response_body=response_body,
            response_status=200,
            started_at="2026-05-12T10:00:00.000Z",
        )
        screenshot = tmp_path / "panel.png"
        screenshot.write_bytes(b"\x89PNG\r\n")
        har_path = tmp_path / "run.har"
        har_path.write_text("{}", encoding="utf-8")
        evidence = walker.build_panel_evidence(
            panel_id="p1",
            title="My Panel",
            fingerprint=walker.extract_fingerprint(query),
            har_path=har_path,
            har_entry=entry,
            screenshot_path=screenshot,
            suspense_status="ok",
        )
        assert evidence.t4_cluster_esql == query
        assert evidence.t5_response_status == 200
        assert evidence.t5_response_columns == ["x"]
        assert evidence.t5_response_row_count == 1
        assert evidence.har_path == str(har_path)
        assert evidence.kibana_screenshot_path == str(screenshot)
        assert evidence.suspense_status == "ok"

    def test_panel_with_no_har_entry_records_notes(self, tmp_path):
        evidence = walker.build_panel_evidence(
            panel_id="p1",
            title="No HAR Panel",
            fingerprint="FROMx|LIMIT1",
            har_path=tmp_path / "run.har",
            har_entry=None,
            screenshot_path=None,
            suspense_status="",
        )
        assert evidence.t4_cluster_esql == ""
        assert "no HAR entry matched fingerprint" in "; ".join(evidence.notes)

    def test_panel_with_no_fingerprint_records_explanatory_note(self):
        evidence = walker.build_panel_evidence(
            panel_id="p1",
            title="No NDJSON Panel",
            fingerprint="",
            har_path=None,
            har_entry=None,
            screenshot_path=None,
            suspense_status="",
        )
        assert "no NDJSON ES|QL fingerprint available" in "; ".join(evidence.notes)


# --------------------------------------------------------------------- #
# Merge mode
# --------------------------------------------------------------------- #


def _verifier_payload_with_one_panel(
    *,
    title: str = "My Panel",
    t3: str = "FROM metrics-* | LIMIT 1",
    verdict: Verdict = Verdict.PASS,
) -> dict[str, Any]:
    record = PanelRecord(
        panel_id="p1",
        title=title,
        dashboard_uid="dash-1",
        dashboard_title="Dash 1",
        t1_translator_esql=t3,
        t2_yaml_esql=t3,
        t3_ndjson_esql=t3,
        t4_cluster_esql=t3,
        verdict=verdict,
    )
    return {
        "dashboard_id": "dash-1",
        "dashboard_title": "Dash 1",
        "verdict_counts": {verdict.value: 1},
        "drift_axis_counts": {},
        "panels": [record.to_jsonable()],
    }


class TestMergeWalkerIntoVerifier:
    def test_merge_populates_har_screenshot_and_t5_fields(self, tmp_path):
        verifier = _verifier_payload_with_one_panel()
        har_path = tmp_path / "run.har"
        har_path.write_text("{}", encoding="utf-8")
        screenshot = tmp_path / "panel.png"
        screenshot.write_bytes(b"\x89PNG\r\n")
        evidence = walker.WalkerPanelEvidence(
            panel_id="p1",
            title="My Panel",
            fingerprint=walker.extract_fingerprint("FROM metrics-* | LIMIT 1"),
            har_path=str(har_path),
            kibana_screenshot_path=str(screenshot),
            suspense_status="ok",
            t4_cluster_esql="FROM metrics-* | LIMIT 1",
            t5_live_query_body=json.dumps({"query": "FROM metrics-* | LIMIT 1"}),
            t5_response_status=200,
            t5_response_columns=["count"],
            t5_response_row_count=42,
        )
        merged = walker.merge_walker_into_verifier(verifier, [evidence], har_path)
        panel = merged["panels"][0]
        assert panel["browser"]["har_path"] == str(har_path)
        assert panel["visual"]["kibana_screenshot"] == str(screenshot)
        assert panel["browser"]["suspense_status"] == "ok"
        assert panel["live"]["response_status"] == 200
        assert panel["live"]["response_columns"] == ["count"]
        assert panel["live"]["response_row_count"] == 42

    def test_merge_preserves_existing_verdict(self, tmp_path):
        # The walker is additive — it never reclassifies a panel.
        verifier = _verifier_payload_with_one_panel(verdict=Verdict.PASS)
        evidence = walker.WalkerPanelEvidence(
            panel_id="p1",
            title="My Panel",
            fingerprint="FROMmetrics-*|LIMIT1",
            kibana_screenshot_path=str(tmp_path / "p.png"),
            t5_response_status=200,
        )
        merged = walker.merge_walker_into_verifier(verifier, [evidence], None)
        assert merged["panels"][0]["verdict"] == Verdict.PASS.value
        # And it round-trips back into a PanelRecord without exploding.
        restored = PanelRecord.from_jsonable(merged["panels"][0])
        assert restored.verdict == Verdict.PASS

    def test_merge_preserves_existing_drift_axes_and_details(self):
        verifier = _verifier_payload_with_one_panel(verdict=Verdict.DRIFT)
        verifier["panels"][0]["drift_axes"] = ["T3=T4"]
        verifier["panels"][0]["drift_details"] = {"T3=T4": "explanation"}
        evidence = walker.WalkerPanelEvidence(
            title="My Panel",
            t5_response_status=200,
            t5_response_columns=["x"],
        )
        merged = walker.merge_walker_into_verifier(verifier, [evidence], None)
        assert merged["panels"][0]["drift_axes"] == ["T3=T4"]
        assert merged["panels"][0]["drift_details"] == {"T3=T4": "explanation"}
        assert merged["panels"][0]["verdict"] == Verdict.DRIFT.value

    def test_merge_skips_unrecognised_titles(self):
        verifier = _verifier_payload_with_one_panel(title="Real Panel")
        evidence = walker.WalkerPanelEvidence(title="Ghost Panel", t5_response_status=200)
        merged = walker.merge_walker_into_verifier(verifier, [evidence], None)
        # Real Panel record unchanged.
        assert merged["panels"][0]["title"] == "Real Panel"
        assert merged["panels"][0]["live"]["response_status"] == 0

    def test_merge_does_not_mutate_input_payload(self):
        verifier = _verifier_payload_with_one_panel()
        verifier_snapshot = json.dumps(verifier, sort_keys=True)
        walker.merge_walker_into_verifier(
            verifier,
            [walker.WalkerPanelEvidence(title="My Panel", t5_response_status=200)],
            None,
        )
        assert json.dumps(verifier, sort_keys=True) == verifier_snapshot

    def test_merge_fills_har_path_from_argument_when_evidence_missing(self, tmp_path):
        verifier = _verifier_payload_with_one_panel()
        har_path = tmp_path / "run.har"
        har_path.write_text("{}", encoding="utf-8")
        evidence = walker.WalkerPanelEvidence(title="My Panel", t5_response_status=200)
        merged = walker.merge_walker_into_verifier(verifier, [evidence], har_path)
        assert merged["panels"][0]["browser"]["har_path"] == str(har_path)


# --------------------------------------------------------------------- #
# Standalone report writer
# --------------------------------------------------------------------- #


class TestWriteStandaloneReport:
    def test_writes_walker_report_json(self, tmp_path):
        config = walker.WalkerConfig(
            kibana_url="https://kib.example.com",
            dashboard_id="dash-1",
            output_dir=tmp_path,
        )
        har_path = tmp_path / "run.har"
        har_path.write_text("{}", encoding="utf-8")
        evidence = walker.WalkerPanelEvidence(
            panel_id="p1",
            title="My Panel",
            t5_response_status=200,
        )
        path = walker.write_standalone_report(tmp_path, config, [evidence], har_path, 5)
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["dashboard_id"] == "dash-1"
        assert payload["har_query_entry_count"] == 5
        assert payload["panels"][0]["title"] == "My Panel"


# --------------------------------------------------------------------- #
# fingerprint loader (verifier JSON -> {title: fingerprint})
# --------------------------------------------------------------------- #


class TestLoadFingerprintsFromVerifier:
    def test_loads_fingerprints_keyed_by_title(self):
        verifier = _verifier_payload_with_one_panel(
            title="Important", t3="FROM metrics-* | STATS x = COUNT(*)"
        )
        out = walker.load_panel_fingerprints_from_verifier(verifier)
        assert "Important" in out
        assert out["Important"] == walker.extract_fingerprint(
            "FROM metrics-* | STATS x = COUNT(*)"
        )

    def test_panels_without_ndjson_esql_are_skipped(self):
        verifier = _verifier_payload_with_one_panel(title="Empty", t3="")
        assert walker.load_panel_fingerprints_from_verifier(verifier) == {}


# --------------------------------------------------------------------- #
# Import-safety — agent-browser missing from PATH must not break imports
# --------------------------------------------------------------------- #


class TestImportSafety:
    def test_module_imports_when_agent_browser_absent(self):
        """Reimport the walker with a PATH that excludes agent-browser.

        We use ``importlib.reload`` to genuinely re-run module
        top-level code so any unguarded ``subprocess.run`` would fire.
        """
        import importlib

        original_path = os.environ.get("PATH", "")
        try:
            # Strip directories that contain agent-browser from PATH.
            scrubbed_path = ":".join(
                p
                for p in original_path.split(":")
                if p
                and p != "."
                and not Path(p, "agent-browser").exists()
            )
            os.environ["PATH"] = scrubbed_path
            module = importlib.reload(walker)
            assert hasattr(module, "Walker")
            assert hasattr(module, "extract_fingerprint")
            assert hasattr(module, "parse_har_for_query_entries")
        finally:
            os.environ["PATH"] = original_path
            importlib.reload(walker)

    def test_run_agent_browser_raises_clear_error_when_binary_missing(self, monkeypatch):
        """When agent-browser is not on PATH, _run_agent_browser raises
        :class:`AgentBrowserError` with an actionable message rather
        than a bare ``FileNotFoundError``."""

        def _raise_fnf(*args, **kwargs):
            raise FileNotFoundError("[Errno 2] No such file or directory: 'agent-browser'")

        monkeypatch.setattr(subprocess, "run", _raise_fnf)
        with pytest.raises(walker.AgentBrowserError) as excinfo:
            walker._run_agent_browser(["close", "--all"], timeout=5)
        assert "agent-browser not found" in str(excinfo.value)


# --------------------------------------------------------------------- #
# _run_agent_browser — subprocess call construction (mocked)
# --------------------------------------------------------------------- #


class TestRunAgentBrowser:
    def test_state_file_added_before_subcommand(self, monkeypatch, tmp_path):
        captured: dict[str, Any] = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        state = tmp_path / "state.json"
        walker._run_agent_browser(
            ["snapshot", "-i"],
            state_file=state,
            timeout=5,
        )
        assert captured["cmd"][0] == "agent-browser"
        assert "--state" in captured["cmd"]
        idx = captured["cmd"].index("--state")
        assert captured["cmd"][idx + 1] == str(state)
        assert captured["cmd"][-2:] == ["snapshot", "-i"]

    def test_enable_react_inserts_flag(self, monkeypatch):
        captured: dict[str, Any] = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        walker._run_agent_browser(
            ["open", "https://example.com"],
            enable_react=True,
            timeout=5,
        )
        assert "--enable" in captured["cmd"]
        idx = captured["cmd"].index("--enable")
        assert captured["cmd"][idx + 1] == "react-devtools"

    def test_check_true_raises_on_nonzero(self, monkeypatch):
        def _fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        with pytest.raises(walker.AgentBrowserError) as excinfo:
            walker._run_agent_browser(["snapshot"], check=True, timeout=5)
        assert "boom" in str(excinfo.value)


# --------------------------------------------------------------------- #
# Walker.run — mocked agent-browser end-to-end smoke test
# --------------------------------------------------------------------- #


class TestWalkerRunMocked:
    def test_run_orchestrates_open_har_eval_screenshot_and_stop(self, tmp_path, monkeypatch):
        """Mock every ``agent-browser`` call and confirm the walker
        issues the expected command sequence in the expected order."""
        seen_cmds: list[list[str]] = []

        panels_response = {
            "data": [
                {
                    "title": "Panel A",
                    "panel_index": "panel-a",
                    "selector": "[data-test-subj='dashboardPanel-0']",
                }
            ]
        }

        def _fake_run(cmd, **kwargs):
            # Strip the leading agent-browser tokens so we can match
            # on subcommand without caring about launch flags.
            seen_cmds.append(list(cmd))
            subcmd = cmd[1:] if cmd and cmd[0] == "agent-browser" else cmd
            # Step over launch-time flags --state / --enable.
            i = 0
            while i < len(subcmd):
                if subcmd[i] == "--state" or subcmd[i] == "--enable":
                    i += 2
                else:
                    break
            head = subcmd[i] if i < len(subcmd) else ""
            stdout = ""
            if head == "eval":
                stdout = json.dumps(panels_response)
            elif head == "snapshot":
                stdout = "{}"
            elif head == "screenshot":
                # Pretend the screenshot succeeded by creating the
                # target file at the path the walker passed in.
                Path(subcmd[-1]).write_bytes(b"\x89PNG\r\n")
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        config = walker.WalkerConfig(
            kibana_url="https://kib.example.com",
            dashboard_id="dash-1",
            output_dir=tmp_path,
            state_file=tmp_path / "state.json",
            wait_extra_seconds=0,
        )
        w = walker.Walker(config)
        evidences = w.run()
        assert isinstance(evidences, list)
        assert len(evidences) == 1
        assert evidences[0].title == "Panel A"

        # Confirm the expected high-level sequence appeared in order:
        # close --all  -> network har start  -> open  -> eval  -> screenshot -> network har stop.
        subcommands = []
        for cmd in seen_cmds:
            stripped = cmd[1:] if cmd and cmd[0] == "agent-browser" else cmd
            i = 0
            while i < len(stripped):
                if stripped[i] in ("--state", "--enable"):
                    i += 2
                else:
                    break
            subcommands.append(stripped[i:])
        sequence = [s[0] if s else "" for s in subcommands]
        assert sequence[0:2] == ["close", "network"]
        assert "open" in sequence
        assert "eval" in sequence
        assert "screenshot" in sequence
        assert sequence[-1] == "network"  # final HAR stop
