# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "check_source_headers.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("check_source_headers_script", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CheckSourceHeadersScriptTests(unittest.TestCase):
    def test_accepts_header_after_shebang(self):
        module = _load_script_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = pathlib.Path(tmpdir) / "tool.py"
            path.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.",
                        "# SPDX-License-Identifier: Elastic-2.0",
                        "",
                        "print('ok')",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertTrue(module.has_valid_header(path))

    def test_reports_missing_header(self):
        module = _load_script_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = pathlib.Path(tmpdir) / "tool.py"
            path.write_text("print('missing header')\n", encoding="utf-8")

            missing = module.find_missing_headers([path])

        self.assertEqual(missing, [path])

    def test_candidate_scan_excludes_generated_and_third_party_paths(self):
        module = _load_script_module()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            included = root / "observability_migration" / "app.py"
            excluded_generated = root / "docs" / "licenses" / "dependencies.md"
            excluded_third_party = root / "licenses" / "Apache-2.0.txt"
            for path in [included, excluded_generated, excluded_third_party]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("content\n", encoding="utf-8")

            candidates = set(module.iter_candidate_paths(root))

        self.assertIn(included, candidates)
        self.assertNotIn(excluded_generated, candidates)
        self.assertNotIn(excluded_third_party, candidates)


if __name__ == "__main__":
    unittest.main()
