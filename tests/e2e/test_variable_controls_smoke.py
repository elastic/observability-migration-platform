# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Live-Kibana smoke tests for variable-controls phase B.

Runs the translate → compile → (future: upload → read-back → cleanup) round
trip against a real Kibana cluster. Requires ``KIBANA_URL`` and
``KIBANA_API_KEY`` environment variables; skipped locally without them.

These tests are marked ``live_kibana`` and skip-by-default. The CI workflow
``.github/workflows/live-kibana-smoke.yml`` exports ``KIBANA_URL`` /
``KIBANA_API_KEY`` and sets ``OBS_MIGRATION_LIVE_KIBANA_REQUIRED=1`` so the
tests fail loudly (rather than silently skip) if the secrets are missing.

Phase B's classifier is wired into the translator pipeline by the activation
task (Task 18). Until that lands the compile-and-shape assertions intentionally
fail at the "find ESQL control in NDJSON" step; once activation lands, the
same fixtures exercise the full round-trip.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

REQUIRED_ENV = "OBS_MIGRATION_LIVE_KIBANA_REQUIRED"
URL_VAR = "KIBANA_URL"
KEY_VAR = "KIBANA_API_KEY"

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "variable_controls"


def _creds_or_skip() -> None:
    """Skip locally when creds are absent; fail loudly when CI demands them."""
    if os.environ.get(REQUIRED_ENV) == "1":
        for env_var in (URL_VAR, KEY_VAR):
            if not os.environ.get(env_var):
                pytest.fail(f"live Kibana smoke required {env_var}; not set in CI")
    if not os.environ.get(URL_VAR) or not os.environ.get(KEY_VAR):
        pytest.skip("live-Kibana smoke skipped: KIBANA_URL/KIBANA_API_KEY not set")


def _python_executable() -> str:
    """Use the running interpreter so the test works from any virtualenv."""
    return sys.executable


def _stage_fixture(tmp_root: Path, fixture_name: str) -> Path:
    """Copy a single fixture into a temp input dir so the CLI sees one dashboard."""
    src = FIXTURE_DIR / fixture_name
    input_dir = tmp_root / fixture_name.replace(".json", "") / "in"
    input_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, input_dir / fixture_name)
    return input_dir


def _compile_yaml(yaml_path: Path) -> Path:
    """Compile a single dashboard YAML to NDJSON via kb-dashboard-cli."""
    out_dir = yaml_path.parent.parent / "compiled" / yaml_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "uvx",
            "kb-dashboard-cli",
            "compile",
            "--input-file",
            str(yaml_path),
            "--output-dir",
            str(out_dir),
        ],
        check=True,
    )
    return out_dir / "compiled_dashboards.ndjson"


@pytest.mark.live_kibana
class GrafanaVariableControlsSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _creds_or_skip()
        cls.tmp = Path(tempfile.mkdtemp(prefix="obs-migrate-smoke-"))

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _translate_grafana(self, fixture_name: str) -> Path:
        input_dir = _stage_fixture(self.tmp, fixture_name)
        out = input_dir.parent / "out"
        out.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                _python_executable(),
                "-m",
                "observability_migration.adapters.source.grafana.cli",
                "--source",
                "files",
                "--input-dir",
                str(input_dir),
                "--output-dir",
                str(out),
                "--assets",
                "dashboards",
                "--field-profile",
                "otel",
            ],
            check=True,
        )
        yaml_files = list((out / "dashboards" / "yaml").glob("*.yaml"))
        self.assertEqual(len(yaml_files), 1, f"expected one YAML, got {yaml_files}")
        return yaml_files[0]

    def _assert_ndjson_shape(self, ndjson: Path, *, multi: bool) -> None:
        body = ndjson.read_text(encoding="utf-8")
        self.assertIn('"type": "esql"', body)
        if multi:
            self.assertIn("MV_CONTAINS(?instance", body)
        else:
            self.assertIn("?instance", body)
        self.assertNotIn("$instance", body)

    def _round_trip(self, fixture: str, *, multi: bool) -> None:
        yaml_path = self._translate_grafana(fixture)
        ndjson = _compile_yaml(yaml_path)
        self._assert_ndjson_shape(ndjson, multi=multi)
        # Upload + read-back + cleanup are intentionally omitted in this
        # initial scaffolding so the workflow stays green on clusters that
        # don't yet support ESQL variable controls. Once the live cluster
        # supports the feature, the upload/read-back/delete stages can be
        # added here using
        # observability_migration.targets.kibana.serverless.import_saved_objects
        # and the saved-objects API.

    def test_grafana_single_value(self) -> None:
        self._round_trip("grafana_single_value.json", multi=False)

    def test_grafana_multi_value(self) -> None:
        self._round_trip("grafana_multi_value.json", multi=True)


@pytest.mark.live_kibana
class DatadogVariableControlsSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _creds_or_skip()
        cls.tmp = Path(tempfile.mkdtemp(prefix="obs-migrate-smoke-dd-"))

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _translate_datadog(self, fixture_name: str) -> Path:
        input_dir = _stage_fixture(self.tmp, fixture_name)
        out = input_dir.parent / "out"
        out.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                _python_executable(),
                "-m",
                "observability_migration.adapters.source.datadog.cli",
                "--source",
                "files",
                "--input-dir",
                str(input_dir),
                "--output-dir",
                str(out),
                "--field-profile",
                "otel",
            ],
            check=True,
        )
        yaml_files = list((out / "dashboards" / "yaml").glob("*.yaml"))
        self.assertGreaterEqual(len(yaml_files), 1, f"expected at least one YAML, got {yaml_files}")
        return yaml_files[0]

    def _assert_ndjson_shape(self, ndjson: Path, *, multi: bool) -> None:
        body = ndjson.read_text(encoding="utf-8")
        self.assertIn('"type": "esql"', body)
        if multi:
            self.assertIn("MV_CONTAINS(?host", body)
        else:
            self.assertIn("?host", body)
        self.assertNotIn("$host", body)

    def _round_trip(self, fixture: str, *, multi: bool) -> None:
        yaml_path = self._translate_datadog(fixture)
        ndjson = _compile_yaml(yaml_path)
        self._assert_ndjson_shape(ndjson, multi=multi)

    def test_datadog_single_value(self) -> None:
        self._round_trip("datadog_single_value.json", multi=False)

    def test_datadog_multi_value(self) -> None:
        self._round_trip("datadog_multi_value.json", multi=True)
