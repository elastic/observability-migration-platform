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


def test_committed_openapi_schema_is_full() -> None:
    """CI authority: the pinned OpenAPI file must stay a full write schema."""
    path = fetcher.COMMITTED_SCHEMA
    assert path.is_file(), f"missing committed OpenAPI pin: {path}"
    schema = fetcher.parse_schema(path.read_text(encoding="utf-8"))
    summary = fetcher.validate_dashboard_schema(schema, require_full_schema=True)
    assert summary["dashboard_paths"] >= 1
    assert summary["has_full_dashboard_write_schema"] is True


def test_main_check_only_accepts_committed_schema_path(capsys) -> None:
    exit_code = fetcher.main(
        [
            "--url",
            str(fetcher.COMMITTED_SCHEMA),
            "--check-only",
            "--require-full-schema",
        ]
    )
    assert exit_code == 0
    assert "full_write_schema=True" in capsys.readouterr().out


KIBANA_BUNDLE = """
openapi: 3.0.3
info:
  title: Kibana Serverless API
  version: ''
servers:
  - url: https://{kibana_url}
security:
  - apiKeyAuth: []
tags:
  - name: Dashboards
    description: Dashboards API.
  - name: Status
x-topics:
  - title: Getting started
paths:
  /api/status:
    get:
      tags: [Status]
      responses:
        '200':
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/StatusResponse'
  /api/dashboards:
    get:
      tags: [Dashboards]
      parameters:
        - $ref: '#/components/parameters/page'
      responses:
        '200':
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/DashboardSearchResponse'
    post:
      tags: [Dashboards]
      parameters:
        - $ref: '#/components/parameters/kbn-xsrf'
      requestBody:
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/DashboardData'
            examples:
              simple:
                $ref: '#/components/examples/createDashboard'
components:
  examples:
    createDashboard:
      value: {title: x}
    unusedExample:
      value: {}
  parameters:
    page:
      name: page
      in: query
    kbn-xsrf:
      name: kbn-xsrf
      in: header
    unusedParameter:
      name: unused
      in: query
  schemas:
    DashboardData:
      type: object
      properties:
        panels:
          type: array
          items:
            $ref: '#/components/schemas/Panel'
    Panel:
      type: object
    DashboardSearchResponse:
      type: object
    StatusResponse:
      type: object
    UnusedSchema:
      type: object
  securitySchemes:
    apiKeyAuth:
      type: apiKey
    basicAuth:
      type: http
"""


def test_filter_keeps_dashboard_paths_and_what_they_reference() -> None:
    schema = fetcher.parse_schema(KIBANA_BUNDLE)

    filtered = fetcher.filter_dashboard_schema(schema)

    assert list(filtered["paths"]) == ["/api/dashboards"]
    components = filtered["components"]
    assert list(components["schemas"]) == ["DashboardData", "Panel", "DashboardSearchResponse"]
    assert list(components["parameters"]) == ["page", "kbn-xsrf"]
    assert list(components["examples"]) == ["createDashboard"]
    assert list(components["securitySchemes"]) == ["apiKeyAuth"]
    assert [tag["name"] for tag in filtered["tags"]] == ["Dashboards"]
    assert filtered["security"] == [{"apiKeyAuth": []}]
    assert filtered["servers"] == [{"url": "https://{kibana_url}"}]
    assert "x-topics" not in filtered
    assert filtered["info"]["title"] == "Kibana Dashboards API"


def test_filter_is_idempotent() -> None:
    schema = fetcher.parse_schema(KIBANA_BUNDLE)

    once = fetcher.filter_dashboard_schema(schema)

    assert fetcher.filter_dashboard_schema(once) == once


def test_filter_does_not_modify_its_input() -> None:
    schema = fetcher.parse_schema(KIBANA_BUNDLE)
    before = fetcher.parse_schema(KIBANA_BUNDLE)

    fetcher.filter_dashboard_schema(schema)

    assert schema == before


def test_filter_requires_dashboard_paths() -> None:
    schema = fetcher.parse_schema(REDIRECT_ONLY_SCHEMA)
    schema["paths"].pop("/api/dashboards")

    with pytest.raises(ValueError, match="no /api/dashboards paths"):
        fetcher.filter_dashboard_schema(schema)


def test_filter_rejects_unresolved_and_external_refs() -> None:
    missing = fetcher.parse_schema(KIBANA_BUNDLE)
    del missing["components"]["schemas"]["Panel"]
    with pytest.raises(ValueError, match="Panel"):
        fetcher.filter_dashboard_schema(missing)

    external = fetcher.parse_schema(KIBANA_BUNDLE)
    external["components"]["schemas"]["Panel"] = {"$ref": "other.yaml#/Panel"}
    with pytest.raises(ValueError, match=r"other\.yaml"):
        fetcher.filter_dashboard_schema(external)


def test_main_writes_the_filtered_schema_with_a_source_header(tmp_path, monkeypatch, capsys) -> None:
    output = tmp_path / "pin.openapi.yaml"
    monkeypatch.setattr(fetcher, "fetch_schema_text", lambda _url, timeout=30: KIBANA_BUNDLE)

    exit_code = fetcher.main(["--url", "https://example.test/kibana.yaml", "--output", str(output)])

    assert exit_code == 0
    text = output.read_text(encoding="utf-8")
    assert text.startswith("# Filtered to /api/dashboards* from https://example.test/kibana.yaml")
    written = fetcher.parse_schema(text)
    assert list(written["paths"]) == ["/api/dashboards"]
    assert "UnusedSchema" not in text
    assert "full_write_schema=True" in capsys.readouterr().out


def test_default_schema_url_is_kibana_bundle() -> None:
    assert fetcher.DEFAULT_SCHEMA_URL == (
        "https://raw.githubusercontent.com/elastic/kibana/main/oas_docs/output/kibana.serverless.yaml"
    )
