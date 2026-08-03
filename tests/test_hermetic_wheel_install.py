# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Prove the wheel is standalone: clean venv, neutral cwd, offline migrate."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class HermeticWheelInstallTests(unittest.TestCase):
    """CI package-smoke twin that fails if cwd shadowing or missing package data slips in."""

    def test_clean_wheel_install_runs_from_neutral_cwd(self):
        grafana_fixture = ROOT / "infra" / "grafana" / "dashboards" / "home.json"
        datadog_fixture = ROOT / "infra" / "datadog" / "dashboards" / "sample_dashboard.json"
        self.assertTrue(grafana_fixture.is_file(), grafana_fixture)
        self.assertTrue(datadog_fixture.is_file(), datadog_fixture)

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            dist = work / "dist"
            fixtures = work / "fixtures"
            out = work / "out"
            venv_dir = work / "venv"
            neutral = work / "neutral-cwd"
            for path in (dist, fixtures / "grafana", fixtures / "datadog", out, neutral):
                path.mkdir(parents=True, exist_ok=True)

            (fixtures / "grafana" / "home.json").write_bytes(grafana_fixture.read_bytes())
            (fixtures / "datadog" / "sample_dashboard.json").write_bytes(
                datadog_fixture.read_bytes()
            )

            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    str(ROOT),
                    "--wheel",
                    "--outdir",
                    str(dist),
                ],
                check=True,
                capture_output=True,
                text=True,
                cwd=neutral,
            )
            wheels = list(dist.glob("elastic_observability_migration-*-py3-none-any.whl"))
            self.assertEqual(len(wheels), 1, wheels)
            wheel = wheels[0]

            # Prefer uv for hermetic envs (ensurepip can abort under some
            # managed CPython builds used by this repo's toolchain).
            subprocess.run(
                ["uv", "venv", "--python", sys.executable, str(venv_dir)],
                check=True,
                capture_output=True,
                text=True,
                cwd=neutral,
            )
            python = venv_dir / "bin" / "python"
            obs_migrate = venv_dir / "bin" / "obs-migrate"
            grafana_migrate = venv_dir / "bin" / "grafana-migrate"
            datadog_migrate = venv_dir / "bin" / "datadog-migrate"

            extras = "all" if sys.version_info >= (3, 12) else "grafana,datadog"
            subprocess.run(
                ["uv", "pip", "install", "--python", str(python), f"{wheel}[{extras}]"],
                check=True,
                capture_output=True,
                text=True,
                cwd=neutral,
            )

            # Must not resolve the repo checkout when cwd is outside the tree.
            loc = subprocess.run(
                [
                    str(python),
                    "-c",
                    "import observability_migration as m; print(m.__version__); print(m.__file__)",
                ],
                check=True,
                capture_output=True,
                text=True,
                cwd=neutral,
            )
            version_line, file_line = loc.stdout.strip().splitlines()
            self.assertRegex(version_line, r"^\d+\.\d+\.\d+")
            self.assertIn("site-packages", file_line)
            self.assertNotIn(str(ROOT), file_line)

            env = os.environ.copy()
            # Drop any accidental editable/src path pollution from the parent env.
            env.pop("PYTHONPATH", None)

            doctor = subprocess.run(
                [str(obs_migrate), "doctor"],
                check=True,
                capture_output=True,
                text=True,
                cwd=neutral,
                env=env,
            )
            # The dashboard-YAML compile path is gone, so doctor no longer
            # probes the external kb-dashboard-* tools; it must still report a
            # ready install from the wheel alone.
            self.assertIn("required dependencies:", doctor.stdout)
            self.assertIn("Ready.", doctor.stdout)
            self.assertNotIn("kb-dashboard-cli", doctor.stdout)

            samples = subprocess.run(
                [str(obs_migrate), "list-samples"],
                check=True,
                capture_output=True,
                text=True,
                cwd=neutral,
                env=env,
            )
            self.assertIn("grafana-prom-basics", samples.stdout)
            self.assertIn("site-packages", samples.stdout)

            gout = out / "grafana"
            subprocess.run(
                [
                    str(grafana_migrate),
                    "--source",
                    "files",
                    "--input-dir",
                    str(fixtures / "grafana"),
                    "--output-dir",
                    str(gout),
                    "--assets",
                    "dashboards",
                ],
                check=True,
                capture_output=True,
                text=True,
                cwd=neutral,
                env=env,
            )
            self.assertTrue(any(gout.rglob("*.native.json")))

            dout = out / "datadog"
            dd = subprocess.run(
                [
                    str(datadog_migrate),
                    "--input-dir",
                    str(fixtures / "datadog"),
                    "--output-dir",
                    str(dout),
                ],
                check=True,
                capture_output=True,
                text=True,
                cwd=neutral,
                env=env,
            )
            self.assertIn(f"Migration Tool v{version_line}", dd.stdout)
            self.assertTrue(any(dout.rglob("*.native.json")))

    def test_clean_sdist_install_runs_from_neutral_cwd(self):
        grafana_fixture = ROOT / "infra" / "grafana" / "dashboards" / "home.json"
        self.assertTrue(grafana_fixture.is_file(), grafana_fixture)

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            dist = work / "dist"
            fixtures = work / "fixtures" / "grafana"
            out = work / "out"
            venv_dir = work / "venv"
            neutral = work / "neutral-cwd"
            for path in (dist, fixtures, out, neutral):
                path.mkdir(parents=True, exist_ok=True)
            (fixtures / "home.json").write_bytes(grafana_fixture.read_bytes())

            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    str(ROOT),
                    "--sdist",
                    "--outdir",
                    str(dist),
                ],
                check=True,
                capture_output=True,
                text=True,
                cwd=neutral,
            )
            sdists = list(dist.glob("elastic_observability_migration-*.tar.gz"))
            self.assertEqual(len(sdists), 1, sdists)
            sdist = sdists[0]

            subprocess.run(
                ["uv", "venv", "--python", sys.executable, str(venv_dir)],
                check=True,
                capture_output=True,
                text=True,
                cwd=neutral,
            )
            python = venv_dir / "bin" / "python"
            extras = "grafana,kibana" if sys.version_info >= (3, 12) else "grafana"
            subprocess.run(
                ["uv", "pip", "install", "--python", str(python), f"{sdist}[{extras}]"],
                check=True,
                capture_output=True,
                text=True,
                cwd=neutral,
            )

            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            loc = subprocess.run(
                [
                    str(python),
                    "-c",
                    "import observability_migration as m; print(m.__file__)",
                ],
                check=True,
                capture_output=True,
                text=True,
                cwd=neutral,
                env=env,
            )
            self.assertIn("site-packages", loc.stdout)
            self.assertNotIn(str(ROOT), loc.stdout)

            gout = out / "grafana"
            subprocess.run(
                [
                    str(venv_dir / "bin" / "grafana-migrate"),
                    "--source",
                    "files",
                    "--input-dir",
                    str(fixtures),
                    "--output-dir",
                    str(gout),
                    "--assets",
                    "dashboards",
                ],
                check=True,
                capture_output=True,
                text=True,
                cwd=neutral,
                env=env,
            )
            self.assertTrue(any(gout.rglob("*.native.json")))


if __name__ == "__main__":
    unittest.main()
