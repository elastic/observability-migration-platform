# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from __future__ import annotations

import importlib
import json


def _load(monkeypatch):
    monkeypatch.setenv("ELASTICSEARCH_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("KEY", "test-key")
    return importlib.import_module("scripts.validate_panels_from_artifacts")


def test_build_params_does_not_treat_identifier_as_value_param(monkeypatch):
    monkeypatch.setenv("ELASTICSEARCH_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("KEY", "test-key")
    module = importlib.import_module("scripts.validate_panels_from_artifacts")

    assert module._build_params(
        "TS metrics-* | STATS value = SUM(metric) BY grouping = ??grouping"
    ) is None


def test_build_params_binds_identifier_from_field_control_default(monkeypatch):
    monkeypatch.setenv("ELASTICSEARCH_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("KEY", "test-key")
    module = importlib.import_module("scripts.validate_panels_from_artifacts")
    query = "TS metrics-* | STATS value = SUM(metric) BY grouping = ??grouping"

    assert module._build_params(
        query,
        identifier_params={"grouping": "exporter"},
    ) == [{"grouping": "exporter"}]


def test_field_control_defaults_use_default_then_first_choice(monkeypatch):
    monkeypatch.setenv("ELASTICSEARCH_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("KEY", "test-key")
    module = importlib.import_module("scripts.validate_panels_from_artifacts")

    assert module._field_control_defaults([
        {
            "type": "esql",
            "variable_name": "grouping",
            "variable_type": "fields",
            "choices": ["exporter", "transport"],
            "default": "transport",
        },
        {
            "type": "esql",
            "variable_name": "secondary",
            "variable_type": "fields",
            "choices": ["receiver"],
        },
    ]) == {"grouping": "transport", "secondary": "receiver"}


# ---------------------------------------------------------------------------
# Empty corpus is fatal
#
# ``main`` used to print "RESULTS: 0/0 panels passed" and return 0 (the
# failure count) whenever the artifact globs matched nothing, so an un-run
# migration was indistinguishable from a clean validation.
# ---------------------------------------------------------------------------


def _write_ir_artifact(path, dashboard: dict) -> None:
    """Write one ``ir/<stem>.ir.json`` envelope for a kb-dashboard-core dict."""
    from observability_migration.core.assets.dashboard import DashboardIR

    path.parent.mkdir(parents=True, exist_ok=True)
    ir = DashboardIR.from_yaml_dict(dashboard, source_adapter="grafana")
    path.write_text(
        json.dumps(
            {
                "kind": "dashboard_ir",
                "version": 1,
                "title": ir.title,
                "source_adapter": "grafana",
                "dashboard_ir": json.loads(json.dumps(ir.to_dict(), default=str)),
            }
        ),
        encoding="utf-8",
    )


def test_main_fails_when_no_ir_artifact_matches(monkeypatch, capsys):
    module = _load(monkeypatch)
    monkeypatch.setattr(module, "IR_FILES", [])
    monkeypatch.setattr(module, "IR_GLOBS", ["/nowhere/*/dashboards/ir/*.ir.json"])

    rc = module.main()
    out = capsys.readouterr().out

    assert rc == 1
    assert "0 panels to validate" in out
    # Names the globs it searched so the operator can fix the path.
    assert "/nowhere/*/dashboards/ir/*.ir.json" in out
    assert "0/0 panels passed" not in out


def test_main_fails_when_ir_has_no_validatable_panels(monkeypatch, capsys, tmp_path):
    module = _load(monkeypatch)
    artifact = tmp_path / "grafana" / "slug" / "dashboards" / "ir" / "dash.ir.json"
    _write_ir_artifact(
        artifact,
        {
            "name": "Markdown only",
            "panels": [
                {"title": "Notes", "markdown": {"content": "nothing to validate"}}
            ],
        },
    )
    monkeypatch.setattr(module, "IR_FILES", [str(artifact)])

    rc = module.main()
    out = capsys.readouterr().out

    assert rc == 1
    assert "0 panels to validate" in out
    assert "none carried a validatable ES|QL query" in out
    assert "0/0 panels passed" not in out


def test_collect_panels_reads_queries_from_ir_artifacts(monkeypatch, tmp_path):
    """The validator's corpus now comes from ``ir/*.ir.json``.

    Panel titles, ES|QL text and the ``??param`` field-control default must
    survive the IR round-trip -- these are exactly what the YAML read supplied
    before, so a difference here would be a silent loss of coverage.
    """
    module = _load(monkeypatch)
    artifact = tmp_path / "grafana" / "my-slug" / "dashboards" / "ir" / "dash.ir.json"
    _write_ir_artifact(
        artifact,
        {
            "name": "Sectioned dashboard",
            "controls": [
                {
                    "type": "esql",
                    "variable_name": "grouping",
                    "variable_type": "fields",
                    "available_options": ["exporter", "transport"],
                }
            ],
            "panels": [
                {
                    "title": "Row",
                    "section": {
                        "panels": [
                            {
                                "title": "Nested metric",
                                "esql": {
                                    "type": "metric",
                                    "query": (
                                        "TS metrics-* | STATS value = SUM(m) "
                                        "BY grouping = ??grouping"
                                    ),
                                },
                            }
                        ]
                    },
                },
                {"title": "Notes", "markdown": {"content": "skipped"}},
            ],
        },
    )
    monkeypatch.setattr(module, "IR_FILES", [str(artifact)])

    panels = module.collect_panels()

    assert [p["panel"] for p in panels] == ["Nested metric"]
    entry = panels[0]
    assert entry["slug"] == "my-slug"
    assert entry["dashboard"] == "Sectioned dashboard"
    assert entry["query"] == (
        "TS metrics-* | STATS value = SUM(m) BY grouping = ??grouping"
    )
    assert entry["identifier_params"] == {"grouping": "exporter"}
