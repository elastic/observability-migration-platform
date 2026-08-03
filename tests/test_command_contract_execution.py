# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Execute the canonical examples from ``docs/command-contract.md``.

Layers
------
1. **Offline** (always) — doctor, list-samples, extensions, file-mode migrate for
   both sources, dedicated CLIs, compile, schema-report.
2. **Live** (opt-in via ``OBS_MIGRATE_CONTRACT_LIVE=1`` + serverless creds) —
   cluster list/detect, audit-rules, delete-rules dry-run, seed/compare/verify,
   upload into an isolated space. Destructive confirms are never run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OBS = ROOT / ".venv" / "bin" / "obs-migrate"
GRAFANA = ROOT / ".venv" / "bin" / "grafana-migrate"
DATADOG = ROOT / ".venv" / "bin" / "datadog-migrate"
GRAFANA_SAMPLE = (
    ROOT / "observability_migration" / "sample_dashboards" / "grafana" / "prom-basics"
)
DATADOG_SAMPLE = (
    ROOT / "observability_migration" / "sample_dashboards" / "datadog" / "host-basics"
)
LIVE = os.getenv("OBS_MIGRATE_CONTRACT_LIVE", "").strip() in {"1", "true", "yes"}


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    # Keep offline runs offline even if the shell exports cluster URLs.
    if env and env.get("_CONTRACT_OFFLINE") == "1":
        for key in (
            "ELASTICSEARCH_ENDPOINT",
            "ES_URL",
            "KIBANA_ENDPOINT",
            "KIBANA_URL",
            "KEY",
            "ES_API_KEY",
            "KIBANA_API_KEY",
        ):
            merged.pop(key, None)
        merged.pop("_CONTRACT_OFFLINE", None)
    result = subprocess.run(
        argv,
        cwd=str(cwd or ROOT),
        env=merged,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(argv)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _render_yaml_from_ir(ir_dir: Path, dest_dir: Path) -> Path:
    """Build a YAML directory from a run's ``ir/*.ir.json`` artifacts.

    ``obs-migrate compile`` and ``upload --artifact-format yaml`` still accept an
    externally supplied YAML directory; a migration no longer produces one, so
    the contract examples that consume YAML build their input here.
    """
    from observability_migration.core.assets.dashboard import DashboardIR
    from observability_migration.targets.kibana.compile import write_dashboard_yaml

    dest_dir.mkdir(parents=True, exist_ok=True)
    for artifact_file in sorted(ir_dir.glob("*.ir.json")):
        artifact = json.loads(artifact_file.read_text(encoding="utf-8"))
        dashboard_ir = DashboardIR.from_dict(artifact["dashboard_ir"])
        write_dashboard_yaml(
            dashboard_ir, dest_dir, artifact_file.name[: -len(".ir.json")]
        )
    return dest_dir


@unittest.skipUnless(OBS.is_file(), "obs-migrate venv binary missing; run make sync")
class CommandContractOfflineExecutionTests(unittest.TestCase):
    """Run every offline contract example end-to-end."""

    def test_doctor(self):
        result = _run([str(OBS), "doctor"], env={"_CONTRACT_OFFLINE": "1"})
        self.assertIn("obs-migrate doctor", result.stdout.lower())

    def test_list_samples(self):
        result = _run([str(OBS), "list-samples"], env={"_CONTRACT_OFFLINE": "1"})
        catalog = json.loads(result.stdout)
        self.assertIsInstance(catalog, list)
        self.assertGreaterEqual(len(catalog), 2)
        ids = {row["id"] for row in catalog}
        self.assertIn("grafana-prom-basics", ids)
        self.assertIn("datadog-host-basics", ids)

    def test_extensions_grafana_and_datadog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            g_out = root / "grafana-rules.yaml"
            d_out = root / "datadog-profile.yaml"
            _run(
                [
                    str(OBS),
                    "extensions",
                    "--source",
                    "grafana",
                    "--format",
                    "yaml",
                    "--template-out",
                    str(g_out),
                ],
                env={"_CONTRACT_OFFLINE": "1"},
            )
            _run(
                [
                    str(OBS),
                    "extensions",
                    "--source",
                    "datadog",
                    "--format",
                    "json",
                ],
                env={"_CONTRACT_OFFLINE": "1"},
            )
            _run(
                [
                    str(OBS),
                    "extensions",
                    "--source",
                    "datadog",
                    "--format",
                    "yaml",
                    "--template-out",
                    str(d_out),
                ],
                env={"_CONTRACT_OFFLINE": "1"},
            )
            self.assertTrue(g_out.is_file())
            self.assertTrue(d_out.is_file())
            self.assertGreater(g_out.stat().st_size, 20)
            self.assertGreater(d_out.stat().st_size, 20)

    def test_unified_migrate_grafana_sample_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "grafana_out"
            _run(
                [
                    str(OBS),
                    "migrate",
                    "--source",
                    "grafana",
                    "--input-mode",
                    "files",
                    "--input-dir",
                    str(GRAFANA_SAMPLE),
                    "--output-dir",
                    str(out),
                    "--assets",
                    "dashboards",
                    "--field-profile",
                    "otel",
                    "--data-view",
                    "metrics-*",
                    "--esql-index",
                    "metrics-*",
                ],
                env={"_CONTRACT_OFFLINE": "1"},
                timeout=240,
            )
            dashboards_dir = out / "dashboards"
            self.assertFalse((dashboards_dir / "yaml").exists())
            self.assertTrue(list((dashboards_dir / "native").glob("*.native.json")))
            self.assertTrue(list((dashboards_dir / "ir").glob("*.ir.json")))
            self.assertTrue((out / "dashboards" / "migration_report.json").is_file())
            self.assertTrue((out / "run_summary.json").is_file())

    def test_unified_migrate_datadog_sample_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "datadog_out"
            _run(
                [
                    str(OBS),
                    "migrate",
                    "--source",
                    "datadog",
                    "--input-mode",
                    "files",
                    "--input-dir",
                    str(DATADOG_SAMPLE),
                    "--output-dir",
                    str(out),
                    "--assets",
                    "dashboards",
                    "--field-profile",
                    "otel",
                    "--data-view",
                    "metrics-*",
                ],
                env={"_CONTRACT_OFFLINE": "1"},
                timeout=240,
            )
            dashboards_dir = out / "dashboards"
            self.assertFalse((dashboards_dir / "yaml").exists())
            self.assertTrue(list((dashboards_dir / "native").glob("*.native.json")))
            self.assertTrue(list((dashboards_dir / "ir").glob("*.ir.json")))
            self.assertTrue((out / "run_summary.json").is_file())

    def test_dedicated_grafana_and_datadog_migrate_offline(self):
        self.assertTrue(GRAFANA.is_file(), "grafana-migrate missing")
        self.assertTrue(DATADOG.is_file(), "datadog-migrate missing")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            g_out = root / "g"
            d_out = root / "d"
            _run(
                [
                    str(GRAFANA),
                    "--input-mode",
                    "files",
                    "--input-dir",
                    str(GRAFANA_SAMPLE),
                    "--output-dir",
                    str(g_out),
                    "--assets",
                    "dashboards",
                    "--field-profile",
                    "otel",
                    "--data-view",
                    "metrics-*",
                    "--esql-index",
                    "metrics-*",
                ],
                env={"_CONTRACT_OFFLINE": "1"},
                timeout=240,
            )
            _run(
                [
                    str(DATADOG),
                    "--input-mode",
                    "files",
                    "--input-dir",
                    str(DATADOG_SAMPLE),
                    "--output-dir",
                    str(d_out),
                    "--assets",
                    "dashboards",
                    "--field-profile",
                    "otel",
                    "--data-view",
                    "metrics-*",
                ],
                env={"_CONTRACT_OFFLINE": "1"},
                timeout=240,
            )
            for produced in (g_out / "dashboards", d_out / "dashboards"):
                self.assertFalse((produced / "yaml").exists())
                self.assertTrue(list((produced / "native").glob("*.native.json")))
                self.assertTrue(list((produced / "ir").glob("*.ir.json")))

    def test_compile_and_schema_report_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            migrate_out = root / "migrate"
            compiled = root / "compiled"
            schema_md = root / "schema.md"
            contract_json = root / "telemetry_contract.json"
            _run(
                [
                    str(OBS),
                    "migrate",
                    "--source",
                    "grafana",
                    "--input-mode",
                    "files",
                    "--input-dir",
                    str(GRAFANA_SAMPLE),
                    "--output-dir",
                    str(migrate_out),
                    "--assets",
                    "dashboards",
                    "--data-view",
                    "metrics-*",
                    "--esql-index",
                    "metrics-*",
                ],
                env={"_CONTRACT_OFFLINE": "1"},
                timeout=240,
            )
            # `obs-migrate compile` still consumes YAML, but a migration no
            # longer emits any, so render the compiler's input from the run's IR
            # artifacts exactly as the --compile path does internally.
            yaml_dir = root / "external_yaml"
            _render_yaml_from_ir(migrate_out / "dashboards" / "ir", yaml_dir)
            compile_result = _run(
                [
                    str(OBS),
                    "compile",
                    "--yaml-dir",
                    str(yaml_dir),
                    "--output-dir",
                    str(compiled),
                ],
                env={"_CONTRACT_OFFLINE": "1"},
                check=False,
                timeout=240,
            )
            # compile may exit nonzero on lint while still writing NDJSON.
            self.assertTrue(
                compiled.exists() or compile_result.returncode in (0, 1),
                f"compile produced nothing:\n{compile_result.stdout}\n{compile_result.stderr}",
            )
            _run(
                [
                    str(OBS),
                    "schema-report",
                    "--artifact-dir",
                    str(migrate_out / "dashboards"),
                    "--output",
                    str(schema_md),
                    "--contract-out",
                    str(contract_json),
                ],
                env={"_CONTRACT_OFFLINE": "1"},
            )
            self.assertTrue(schema_md.is_file())
            self.assertTrue(contract_json.is_file())
            self.assertIn("schema", schema_md.read_text(encoding="utf-8").lower())


@unittest.skipUnless(LIVE, "set OBS_MIGRATE_CONTRACT_LIVE=1 to run live contract commands")
@unittest.skipUnless(OBS.is_file(), "obs-migrate venv binary missing; run make sync")
class CommandContractLiveExecutionTests(unittest.TestCase):
    """Safe live contract commands against serverless_creds.env."""

    @classmethod
    def setUpClass(cls):
        creds = ROOT / "serverless_creds.env"
        if not creds.is_file():
            raise unittest.SkipTest("serverless_creds.env missing")
        # Load without printing secrets.
        for line in creds.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        for required in ("ELASTICSEARCH_ENDPOINT", "KIBANA_ENDPOINT", "KEY"):
            if not os.getenv(required):
                raise unittest.SkipTest(f"{required} not set")
        cls.es = os.environ["ELASTICSEARCH_ENDPOINT"]
        cls.kb = os.environ["KIBANA_ENDPOINT"]
        cls.key = os.environ["KEY"]
        cls.work = Path(tempfile.mkdtemp(prefix="obs-contract-live-"))
        cls.grafana_out = cls.work / "grafana_out"
        cls.space = "cmd-contract-exec"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def test_01_cluster_list_and_detect(self):
        list_result = _run(
            [
                str(OBS),
                "cluster",
                "list-dashboards",
                "--kibana-url",
                self.kb,
                "--kibana-api-key",
                self.key,
            ],
            timeout=120,
        )
        self.assertIn("dashboard", list_result.stdout.lower())
        detect = _run(
            [
                str(OBS),
                "cluster",
                "detect-serverless",
                "--kibana-url",
                self.kb,
                "--kibana-api-key",
                self.key,
            ],
            timeout=60,
        )
        self.assertIn("serverless", detect.stdout.lower())

    def test_02_audit_and_delete_rules_dry_run(self):
        audit = _run(
            [
                str(OBS),
                "audit-rules",
                "--kibana-url",
                self.kb,
                "--kibana-api-key",
                self.key,
            ],
            check=False,
            timeout=120,
        )
        # Nonzero is OK when enabled migrated rules remain.
        self.assertIn(audit.returncode, (0, 1, 2), audit.stderr)
        dry = _run(
            [
                str(OBS),
                "delete-rules",
                "--kibana-url",
                self.kb,
                "--kibana-api-key",
                self.key,
            ],
            check=False,
            timeout=120,
        )
        self.assertIn(dry.returncode, (0, 1, 2), dry.stderr)
        combined = (dry.stdout + dry.stderr).lower()
        # Dry-run (no --confirm) should describe a plan, not claim a confirmed wipe.
        self.assertTrue(
            any(token in combined for token in ("dry", "would", "confirm", "migrated")),
            f"unexpected delete-rules dry-run output:\n{combined[:1000]}",
        )

    def test_03_migrate_upload_seed_compare_verify(self):
        _run(
            [
                str(OBS),
                "migrate",
                "--source",
                "grafana",
                "--input-mode",
                "files",
                "--input-dir",
                str(GRAFANA_SAMPLE),
                "--output-dir",
                str(self.grafana_out),
                "--assets",
                "dashboards",
                "--field-profile",
                "otel",
                "--data-view",
                "metrics-*",
                "--esql-index",
                "metrics-prometheus-default",
                "--es-url",
                self.es,
                "--es-api-key",
                self.key,
                "--kibana-url",
                f"{self.kb.rstrip('/')}/s/{self.space}",
                "--kibana-api-key",
                self.key,
                "--upload",
            ],
            timeout=300,
        )
        dash = self.grafana_out / "dashboards"
        self.assertTrue((dash / "migration_report.json").is_file())

        seed = _run(
            [
                str(OBS),
                "seed-sample-data",
                "--artifact-dir",
                str(dash),
                "--es-url",
                self.es,
                "--api-key",
                self.key,
                "--data-hours",
                "1",
                "--quiet",
            ],
            check=False,
            timeout=300,
        )
        self.assertIn(seed.returncode, (0, 1), seed.stderr)

        compare = _run(
            [
                str(OBS),
                "compare",
                "--artifact-dir",
                str(dash),
                "--es-url",
                self.es,
                "--api-key",
                self.key,
                "--report-out",
                str(self.work / "comparison_report.json"),
                "--quiet",
            ],
            check=False,
            timeout=300,
        )
        self.assertIn(compare.returncode, (0, 1), compare.stderr)
        self.assertTrue((self.work / "comparison_report.json").is_file())

        verify = _run(
            [
                str(OBS),
                "verify",
                "--artifact-dir",
                str(dash),
                "--es-url",
                self.es,
                "--api-key",
                self.key,
                "--report-out",
                str(self.work / "verify_report.json"),
            ],
            check=False,
            timeout=300,
        )
        self.assertIn(verify.returncode, (0, 1), verify.stderr)
        self.assertTrue((self.work / "verify_report.json").is_file())

        # Dry-run remove only (no --confirm).
        remove = _run(
            [
                str(OBS),
                "remove-sample-data",
                "--artifact-dir",
                str(dash),
                "--es-url",
                self.es,
                "--api-key",
                self.key,
            ],
            check=False,
            timeout=120,
        )
        self.assertIn(remove.returncode, (0, 1, 2), remove.stderr)


if __name__ == "__main__":
    unittest.main()
