# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Shared test fixtures for the migration test suite.

Provides reusable panel builders, mock resolvers, and context factories
used across test modules.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.core.assets.dashboard import DashboardIR


def write_dashboard_ir_artifact(
    artifact_dir: Path,
    dashboard: dict[str, Any],
    *,
    stem: str = "dash",
    source_adapter: str = "test",
) -> Path:
    """Write one ``<artifact_dir>/ir/<stem>.ir.json`` from a dashboard dict.

    ``dashboard`` is a single kb-dashboard-core ``dashboards[]`` entry -- the
    shape a dashboard is naturally described in, and the shape
    ``DashboardIR.to_yaml_dict`` round-trips to. It is converted with
    ``DashboardIR.from_yaml_dict``, exactly as the Grafana translator does
    before writing its artifacts, so a fixture written here is the same
    envelope a real migration run produces.

    Use this wherever a test needs the artifact the *readers* consume (the
    telemetry contract, the verifier's T2 tier, visual-regression panel
    discovery). Tests that assert on YAML *production* should keep writing
    YAML.
    """
    ir_dir = Path(artifact_dir) / "ir"
    ir_dir.mkdir(parents=True, exist_ok=True)
    dashboard_ir = DashboardIR.from_yaml_dict(dashboard, source_adapter=source_adapter)
    path = ir_dir / f"{stem}.ir.json"
    path.write_text(
        json.dumps(
            {
                "kind": "dashboard_ir",
                "version": 1,
                "title": dashboard_ir.title,
                "source_adapter": source_adapter,
                "dashboard_ir": json.loads(
                    json.dumps(dashboard_ir.to_dict(), default=str)
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


class _IrFixtureFile:
    """Sink for one dashboard fixture, materialised as an IR artifact.

    ``write_text`` takes a serialized kb-dashboard-core
    ``{"dashboards": [...]}`` document (the shape the migration's dashboard
    YAML carried) and writes it out as ``ir/<stem>.ir.json`` instead. This
    exists so a test can keep describing a *dashboard* while the reader under
    test consumes the IR export.
    """

    def __init__(self, artifact_dir: Path, name: str) -> None:
        self._artifact_dir = artifact_dir
        self._stem = name.split(".")[0]

    def write_text(self, text: str, encoding: str = "utf-8") -> Path:
        import yaml  # local: only fixture writers need the YAML parser

        document = yaml.safe_load(text) or {}
        entries = document.get("dashboards") or []
        ir_dir = Path(self._artifact_dir) / "ir"
        ir_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for index, entry in enumerate(entries):
            stem = self._stem if index == 0 else f"{self._stem}-{index}"
            paths.append(write_dashboard_ir_artifact(self._artifact_dir, entry, stem=stem))
        # A ``dashboards: []`` fixture still has to leave ``ir/`` existing so
        # path resolution sees an empty artifact dir rather than a wrong path.
        return paths[0] if paths else ir_dir


class _IrFixtureDir:
    """``artifact_dir / "ir"`` with a dashboard-document write surface."""

    def __init__(self, artifact_dir: Path) -> None:
        self._artifact_dir = Path(artifact_dir)
        self.path = self._artifact_dir / "ir"

    def mkdir(self, parents: bool = False, exist_ok: bool = True) -> None:
        self.path.mkdir(parents=parents, exist_ok=True)

    def __truediv__(self, name: str) -> _IrFixtureFile:
        return _IrFixtureFile(self._artifact_dir, name)


def ir_fixture_dir(artifact_dir: Path) -> _IrFixtureDir:
    """Return a writer that turns dashboard fixtures into IR artifacts.

    Drop-in replacement for ``artifact_dir / "yaml"`` in fixtures that feed a
    reader which has moved to ``ir/*.ir.json``: the fixture body stays a
    kb-dashboard-core dashboard document, only the artifact it lands in
    changes.
    """
    return _IrFixtureDir(artifact_dir)


def make_grafana_panel(
    expr: str = "up",
    panel_type: str = "timeseries",
    datasource_type: str = "prometheus",
    title: str = "Test Panel",
    grid_pos: dict[str, int] | None = None,
    extra_targets: list[dict[str, Any]] | None = None,
    **extra_fields: Any,
) -> dict[str, Any]:
    """Build a minimal Grafana panel dict for testing."""
    pos = grid_pos or {"x": 0, "y": 0, "w": 12, "h": 8}
    targets = [{"expr": expr, "refId": "A"}]
    if extra_targets:
        targets.extend(extra_targets)
    panel: dict[str, Any] = {
        "type": panel_type,
        "title": title,
        "datasource": {"type": datasource_type, "uid": "prom1"},
        "targets": targets,
        "gridPos": pos,
    }
    panel.update(extra_fields)
    return panel


def make_datadog_widget(
    widget_type: str = "timeseries",
    title: str = "Test Widget",
    queries: list[dict[str, Any]] | None = None,
    layout: dict[str, Any] | None = None,
    **extra_fields: Any,
) -> dict[str, Any]:
    """Build a minimal Datadog widget dict for testing."""
    widget: dict[str, Any] = {
        "definition": {
            "type": widget_type,
            "title": title,
            "requests": queries or [],
        },
        "layout": layout or {"x": 0, "y": 0, "width": 4, "height": 2},
    }
    widget["definition"].update(extra_fields)
    return widget


def default_rule_pack() -> RulePackConfig:
    """Return a default RulePackConfig for testing."""
    return RulePackConfig()


def default_resolver(rule_pack: RulePackConfig | None = None) -> SchemaResolver:
    """Return a SchemaResolver using default rules."""
    return SchemaResolver(rule_pack or default_rule_pack())
