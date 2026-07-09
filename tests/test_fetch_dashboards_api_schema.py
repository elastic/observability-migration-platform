# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "fetch_dashboards_api_schema.py"
SPEC = importlib.util.spec_from_file_location("fetch_dashboards_api_schema", SCRIPT_PATH)
fetcher = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(fetcher)


FULL_SCHEMA = """
openapi: 3.0.0
info:
  title: Kibana API
  version: latest
paths:
  /api/dashboards:
    post:
      requestBody:
        content:
          application/json:
            schema:
              type: object
    get:
      responses:
        '200':
          description: ok
  /api/dashboards/{id}:
    put:
      requestBody:
        content:
          application/json:
            schema:
              type: object
    delete:
      responses:
        '204':
          description: deleted
"""

REDIRECT_ONLY_SCHEMA = """
openapi: 3.0.0
info:
  title: Kibana API
  version: latest
paths:
  /api/dashboards:
    post:
      description: Redirect to external technical-preview docs.
  /api/status:
    get:
      responses:
        '200':
          description: ok
"""


def test_summary_detects_full_dashboard_write_schema() -> None:
    schema = fetcher.parse_schema(FULL_SCHEMA)

    summary = fetcher.validate_dashboard_schema(schema, require_full_schema=True)

    assert summary["dashboard_paths"] == 2
    assert summary["dashboard_operations"] == 4
    assert summary["has_full_dashboard_write_schema"] is True


def test_require_full_schema_rejects_redirect_only_bundle() -> None:
    schema = fetcher.parse_schema(REDIRECT_ONLY_SCHEMA)

    with pytest.raises(ValueError, match="no POST/PUT request-body schema"):
        fetcher.validate_dashboard_schema(schema, require_full_schema=True)


def test_main_writes_fetched_schema(tmp_path, monkeypatch, capsys) -> None:
    output = tmp_path / "kibana.openapi.yaml"
    monkeypatch.setattr(fetcher, "fetch_schema_text", lambda _url, timeout=30: FULL_SCHEMA)

    exit_code = fetcher.main(["--url", "https://example.test/kibana.yaml", "--output", str(output)])

    assert exit_code == 0
    assert "/api/dashboards" in output.read_text(encoding="utf-8")
    assert "full_write_schema=True" in capsys.readouterr().out
