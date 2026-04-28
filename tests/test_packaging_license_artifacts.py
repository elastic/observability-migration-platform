# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_ARTIFACT_SUFFIXES = (
    "LICENSE",
    "NOTICE.txt",
    "THIRD_PARTY_NOTICES.md",
    "docs/licenses/dependencies.md",
    "docs/licenses/sbom.cdx.json",
    "licenses/Apache-2.0.txt",
    "licenses/BSD-3-Clause-DataDog-integrations-core.txt",
    "licenses/MIT-FUSAKLA-Prometheus2-grafana-dashboard.txt",
    "licenses/MIT-strawgate-kb-yaml-to-lens.txt",
)

MPL_DEPENDENCY_SOURCE_LINKS = (
    "certifi: https://github.com/certifi/python-certifi",
    "fqdn: https://github.com/ypcrts/fqdn",
    "hypothesis: https://github.com/HypothesisWorks/hypothesis",
    "pathspec: https://github.com/cpburnz/python-pathspec",
)


class PackagingLicenseArtifactsTests(unittest.TestCase):
    def test_notice_lists_mpl_dependency_source_links(self):
        notice = (ROOT / "NOTICE.txt").read_text(encoding="utf-8")

        for source_link in MPL_DEPENDENCY_SOURCE_LINKS:
            with self.subTest(source_link=source_link):
                self.assertIn(source_link, notice)

    def test_wheel_and_sdist_include_legal_and_dependency_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    str(ROOT),
                    "--wheel",
                    "--sdist",
                    "--no-isolation",
                    "--outdir",
                    str(output_dir),
                ],
                cwd=output_dir,
                check=True,
                capture_output=True,
                text=True,
            )

            wheel = next(output_dir.glob("*.whl"))
            sdist = next(output_dir.glob("*.tar.gz"))

            with zipfile.ZipFile(wheel) as archive:
                wheel_members = archive.namelist()
            with tarfile.open(sdist, "r:gz") as archive:
                sdist_members = archive.getnames()

        for suffix in REQUIRED_ARTIFACT_SUFFIXES:
            with self.subTest(artifact=suffix, distribution="wheel"):
                self.assertTrue(
                    any(member.endswith(suffix) for member in wheel_members),
                    f"{suffix} missing from wheel",
                )
            with self.subTest(artifact=suffix, distribution="sdist"):
                self.assertTrue(
                    any(member.endswith(suffix) for member in sdist_members),
                    f"{suffix} missing from sdist",
                )


if __name__ == "__main__":
    unittest.main()
