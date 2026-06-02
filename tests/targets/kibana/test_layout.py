# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for in-process compiled-dashboard layout validation."""

import json
import tempfile
import unittest
from pathlib import Path

from observability_migration.targets.kibana import layout


def _ndjson(panels: list[dict]) -> str:
    doc = {"attributes": {"title": "Dash", "panelsJSON": json.dumps(panels)}}
    return json.dumps(doc) + "\n"


class LayoutValidationTests(unittest.TestCase):
    def test_clean_layout_passes(self):
        panels = [{"gridData": {"x": 0, "y": 0, "w": 24, "h": 10}}]
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "d.ndjson"
            f.write_text(_ndjson(panels), encoding="utf-8")
            ok, output = layout.validate_compiled_layout(tmp)
        self.assertTrue(ok, msg=output)

    def test_overlap_fails(self):
        panels = [
            {"gridData": {"x": 0, "y": 0, "w": 24, "h": 10}},
            {"gridData": {"x": 0, "y": 0, "w": 24, "h": 10}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "d.ndjson"
            f.write_text(_ndjson(panels), encoding="utf-8")
            ok, output = layout.validate_compiled_layout(tmp)
        self.assertFalse(ok)
        self.assertIn("overlap", output)

    def test_exceeds_grid_width_fails(self):
        panels = [{"gridData": {"x": 40, "y": 0, "w": 24, "h": 10}}]
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "d.ndjson"
            f.write_text(_ndjson(panels), encoding="utf-8")
            ok, output = layout.validate_compiled_layout(tmp)
        self.assertFalse(ok)
        self.assertIn("48-column grid", output)


if __name__ == "__main__":
    unittest.main()
