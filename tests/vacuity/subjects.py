# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Subjects the vacuity harness feeds its guards.

Everything here is built from committed corpora (``infra/grafana/dashboards`` and
``infra/datadog/dashboards/integrations``) or from the product's own report
writers — never from a hand-written fixture of the shape under test. A hand-written
fixture is how a guard goes vacuous: it can only ever contain the shape whoever
wrote it already had in mind (see ``458f4e2``, where a shared ``_DYNAMIC`` fixture
pinned a single-step palette no live Kibana accepts, and nine assertions inherited
it).

Two rules matter for correctness of the harness itself:

* Corpus loaders are cached; **per-dashboard builders are not**. A mutation that
  patches the mapper has to be able to rebuild its subject *under the patch*, and
  a cache would hand it a pre-patch object and turn the mutation green.
* Leaf-panel counts used as denominators come from the ``DashboardIR``, not from a
  payload walker, so a blind walker cannot certify itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from observability_migration.core.assets.dashboard import DashboardIR
from observability_migration.targets.kibana import dashboards_api
from tests import native_payload_guard

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GRAFANA_DASHBOARD_DIR = REPO_ROOT / "infra" / "grafana" / "dashboards"
DATADOG_DASHBOARD_DIR = REPO_ROOT / "infra" / "datadog" / "dashboards" / "integrations"

# A Grafana dashboard that exercises the awkward shapes on purpose: sections,
# multi-layer xy panels, and author-supplied raw ES|QL on one physical line (the
# input that made ``_ensure_bucket_sort``'s idempotence guard dead code).
MULTI_PATTERN = "multi-pattern-coverage.json"
# A Datadog dashboard whose widgets carry conditional formats, i.e. colour
# palettes — the payload family ``458f4e2`` was about.
DATADOG_PALETTE_DASHBOARD = "redis.json"


@dataclass
class PayloadSubject:
    """One dashboard as the IR *and* the native payload derived from it."""

    name: str
    dashboard_ir: DashboardIR
    payload: dict[str, Any]
    #: Leaf panels the IR declares. Counted off the IR so it is independent of
    #: every payload walker the guards use, and can serve as their denominator.
    ir_leaf_count: int = 0
    #: Leaf panels the IR declares *with* an ES|QL query.
    ir_query_count: int = 0


def _subject(name: str, dashboard_ir: DashboardIR) -> PayloadSubject:
    native, _stats = dashboards_api.native_dashboard_from_ir(dashboard_ir)
    entries = native_payload_guard.ir_leaf_entries(dashboard_ir)
    return PayloadSubject(
        name=name,
        dashboard_ir=dashboard_ir,
        payload=native.to_api_payload(),
        ir_leaf_count=len(entries),
        ir_query_count=sum(
            1 for entry in entries if native_payload_guard.entry_queries(entry)
        ),
    )


# --------------------------------------------------------------------------- #
# Grafana
# --------------------------------------------------------------------------- #


def grafana_dashboard_ir(name: str) -> DashboardIR:
    """Translate one committed Grafana dashboard. Uncached on purpose."""
    from observability_migration.adapters.source.grafana.panels import translate_dashboard
    from observability_migration.adapters.source.grafana.rules import RulePackConfig
    from observability_migration.adapters.source.grafana.schema import SchemaResolver

    rule_pack = RulePackConfig()
    raw = json.loads((GRAFANA_DASHBOARD_DIR / name).read_text(encoding="utf-8"))
    result = translate_dashboard(
        raw,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rule_pack,
        resolver=SchemaResolver(rule_pack),
    )
    return result.dashboard_ir


def grafana_subject(name: str = MULTI_PATTERN) -> PayloadSubject:
    """One Grafana dashboard as ``(IR, payload)``. Uncached on purpose."""
    return _subject(name, grafana_dashboard_ir(name))


@lru_cache(maxsize=1)
def grafana_corpus() -> tuple[PayloadSubject, ...]:
    """Every committed Grafana dashboard, translated once per session."""
    names = sorted(path.name for path in GRAFANA_DASHBOARD_DIR.glob("*.json"))
    assert names, f"no Grafana dashboards under {GRAFANA_DASHBOARD_DIR}"
    return tuple(grafana_subject(name) for name in names)


# --------------------------------------------------------------------------- #
# Datadog
# --------------------------------------------------------------------------- #


def _datadog_widgets(widgets: Any) -> list[Any]:
    ordered: list[Any] = []
    for widget in widgets or []:
        ordered.append(widget)
        ordered.extend(_datadog_widgets(getattr(widget, "children", []) or []))
    return ordered


def datadog_subject(name: str = DATADOG_PALETTE_DASHBOARD) -> PayloadSubject:
    """One committed Datadog integration dashboard. Uncached on purpose."""
    from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
    from observability_migration.adapters.source.datadog.generate import (
        generate_dashboard_artifacts,
    )
    from observability_migration.adapters.source.datadog.normalize import normalize_dashboard
    from observability_migration.adapters.source.datadog.planner import plan_widget
    from observability_migration.adapters.source.datadog.translate import translate_widget

    raw = json.loads((DATADOG_DASHBOARD_DIR / name).read_text(encoding="utf-8"))
    normalized = normalize_dashboard(raw)
    results = [
        translate_widget(widget, plan_widget(widget), OTEL_PROFILE)
        for widget in _datadog_widgets(normalized.widgets)
    ]
    _native, _stats, dashboard_ir = generate_dashboard_artifacts(
        normalized, results, field_map=OTEL_PROFILE
    )
    return _subject(name, dashboard_ir)


@lru_cache(maxsize=1)
def datadog_corpus() -> tuple[PayloadSubject, ...]:
    """Every committed Datadog integration dashboard, translated once per session."""
    names = sorted(
        path.name
        for path in DATADOG_DASHBOARD_DIR.glob("*.json")
        if "widgets" in json.loads(path.read_text(encoding="utf-8"))
    )
    assert names, f"no Datadog dashboards under {DATADOG_DASHBOARD_DIR}"
    return tuple(datadog_subject(name) for name in names)


# --------------------------------------------------------------------------- #
# migration_report.json, written by each adapter's own report writer
# --------------------------------------------------------------------------- #

_SAMPLE_ESQL = (
    "TS metrics-* | STATS value = AVG(RATE(http_requests_total)) "
    "BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend) | SORT time_bucket ASC"
)


@dataclass
class ReportSubject:
    """A ``migration_report.json`` document plus how many panels it describes."""

    source: str
    report: dict[str, Any]
    panel_count: int = 0
    #: Panels whose entry carries translator output under *whatever* key this
    #: adapter spells it with. The verifier's T1 collector must find them all.
    panels_with_query: int = 0


def _grafana_report(tmp_path: Path) -> ReportSubject:
    from observability_migration.core.reporting import report as reporting

    panel = reporting.PanelResult(
        title="CPU Usage",
        grafana_type="timeseries",
        kibana_type="line",
        status="migrated",
        confidence=1.0,
        promql_expr="rate(http_requests_total[5m])",
        esql_query=_SAMPLE_ESQL,
        source_panel_id="7",
    )
    dashboard = reporting.MigrationResult(
        dashboard_title="Vacuity Probe",
        dashboard_uid="vacuity-probe",
        total_panels=1,
        migrated=1,
        panel_results=[panel],
    )
    out = tmp_path / "grafana_migration_report.json"
    reporting.save_detailed_report([dashboard], str(out))
    return ReportSubject(
        source="grafana",
        report=json.loads(out.read_text(encoding="utf-8")),
        panel_count=1,
        panels_with_query=1,
    )


def _datadog_report(tmp_path: Path) -> ReportSubject:
    from observability_migration.adapters.source.datadog import report as dd_report
    from observability_migration.adapters.source.datadog.models import (
        DashboardResult,
        TranslationResult,
    )

    widget = TranslationResult(
        widget_id="w-1",
        title="CPU Usage",
        dd_widget_type="timeseries",
        kibana_type="line",
        status="ok",
        esql_query=_SAMPLE_ESQL,
        source_queries=["avg:system.cpu.user{*}"],
    )
    dashboard = DashboardResult(
        dashboard_id="vacuity-probe",
        dashboard_title="Vacuity Probe",
        total_widgets=1,
        migrated=1,
        panel_results=[widget],
    )
    out = tmp_path / "datadog_migration_report.json"
    dd_report.save_detailed_report([dashboard], str(out))
    return ReportSubject(
        source="datadog",
        report=json.loads(out.read_text(encoding="utf-8")),
        panel_count=1,
        panels_with_query=1,
    )


def migration_report(source: str, tmp_path: Path) -> ReportSubject:
    """A real ``migration_report.json`` for *source*, via that adapter's writer.

    Deliberately not a literal: Grafana spells the translator output ``esql`` and
    Datadog spells it ``esql_query``, and reading the key out of a fixture would
    have kept the same blind spot the collector had (``07e5829``). Going through
    the writer means a future rename on either side breaks the harness.
    """
    builders = {"grafana": _grafana_report, "datadog": _datadog_report}
    assert source in builders, f"unknown source {source!r}"
    return builders[source](tmp_path)


REPORT_SOURCES = ("grafana", "datadog")
