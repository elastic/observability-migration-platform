# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the shared Kibana space-URL helpers.

The dashboard-YAML artifact surfaces this module used to host (rendering YAML,
shelling out to ``kb-dashboard-cli compile``, the saved-objects ``_import``
upload, and the lint/layout wrappers around them) are gone: a migration writes
``native/*.native.json`` and uploads through the typed Dashboards API. What
remains here is space-URL derivation, so that is what is covered.
"""

import unittest

from observability_migration.targets.kibana import compile as shared_compile


class TestSharedCompileBehavior(unittest.TestCase):
    def test_detect_space_id_from_url_without_space_returns_empty(self):
        self.assertEqual(shared_compile.detect_space_id_from_kibana_url("http://localhost:5601"), "")

    def test_no_repo_root_helper(self):
        # Nothing here may locate the repo checkout at runtime: the installed
        # CLI has no checkout to find.
        self.assertFalse(hasattr(shared_compile, "_repo_root"))


if __name__ == "__main__":
    unittest.main()
