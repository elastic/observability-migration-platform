# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the kb-dashboard tool invocation resolver."""

import unittest
from unittest import mock

from observability_migration.targets.kibana import _kbtool


class ResolverTests(unittest.TestCase):
    def test_prefers_installed_console_script(self):
        with mock.patch.object(_kbtool.shutil, "which", return_value="/usr/bin/kb-dashboard-cli"):
            argv = _kbtool.tool_argv("kb-dashboard-cli")
        self.assertEqual(argv, ["/usr/bin/kb-dashboard-cli"])

    def test_falls_back_to_pinned_uvx(self):
        def fake_which(name):
            return "/usr/bin/uvx" if name == "uvx" else None

        with mock.patch.object(_kbtool.shutil, "which", side_effect=fake_which):
            argv = _kbtool.tool_argv("kb-dashboard-lint")
        self.assertEqual(
            argv,
            ["uvx", "--from", f"kb-dashboard-lint=={_kbtool.KB_DASHBOARD_TOOL_VERSION}", "kb-dashboard-lint"],
        )

    def test_raises_clear_error_when_neither_available(self):
        with mock.patch.object(_kbtool.shutil, "which", return_value=None):
            with self.assertRaises(_kbtool.KbToolUnavailableError) as ctx:
                _kbtool.tool_argv("kb-dashboard-cli")
        msg = str(ctx.exception)
        self.assertIn("kb-dashboard-cli", msg)
        self.assertIn("obs-migrate[kibana]", msg)
        self.assertIn("uv", msg)


if __name__ == "__main__":
    unittest.main()
