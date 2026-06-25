# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Coverage-completeness gate: keep the supported-type registry honest against
the code's actual routing tables, and assert every chart-bearing type has matrix
coverage. See docs/superpowers/specs/2026-06-25-migration-confidence-pyramid-design.md
(specs are gitignored)."""

from observability_migration.core.coverage import supported_types as st


def test_registry_exposes_grafana_and_datadog_sets():
    assert st.GRAFANA_SUPPORTED_PANEL_TYPES["timeseries"] == "line"
    assert "row" in st.GRAFANA_SKIPPED_PANEL_TYPES
    assert "timeseries" in st.DATADOG_SUPPORTED_WIDGET_TYPES
    assert "query_value" in st.DATADOG_SUPPORTED_WIDGET_TYPES


def test_grafana_registry_matches_code_routing():
    from observability_migration.adapters.source.grafana import panels

    # Drift either way fails: a routed type missing from the registry, or a
    # registry entry the code no longer routes.
    assert dict(panels.PANEL_TYPE_MAP) == dict(st.GRAFANA_SUPPORTED_PANEL_TYPES), (
        "PANEL_TYPE_MAP and GRAFANA_SUPPORTED_PANEL_TYPES diverged — update the "
        "registry (and add a snapshot + matrix cell for any new type)."
    )
    assert set(panels.SKIP_PANEL_TYPES) == set(st.GRAFANA_SKIPPED_PANEL_TYPES)


def test_datadog_registry_covers_all_planner_widget_types():
    from observability_migration.core.coverage import datadog_introspect as di

    referenced = di.collect_planner_widget_types()
    missing = referenced - st.DATADOG_SUPPORTED_WIDGET_TYPES
    assert not missing, (
        f"planner.py references widget types not in the registry: {sorted(missing)}. "
        "Add them to DATADOG_SUPPORTED_WIDGET_TYPES with a snapshot + matrix cell."
    )


# Types that carry no query (text/markdown/group/stream) — matrix-exempt by design.
_GRAFANA_MATRIX_EXEMPT = {"text"}  # -> markdown, no query
_DATADOG_MATRIX_EXEMPT = {
    "note", "free_text", "image", "iframe",  # text widgets
    "group", "powerpack",                       # containers
    "log_stream", "list_stream",                # log/event streams (dedicated tests)
}
# Chart-bearing Datadog types not yet in the combinatorial matrix. Tracked
# explicitly so coverage loss is visible, not silent (spec: "no silent caps").
# Now empty: every chart-bearing widget type is covered by the Datadog matrix.
_DATADOG_MATRIX_DEFERRED: set[str] = set()


def test_every_chart_bearing_grafana_type_has_matrix_coverage():
    from tests.test_panel_matrix import _PANEL_TYPES

    covered_kibana = {st.GRAFANA_SUPPORTED_PANEL_TYPES[t] for t in _PANEL_TYPES}
    need = set(st.GRAFANA_SUPPORTED_PANEL_TYPES) - _GRAFANA_MATRIX_EXEMPT
    missing = {t for t in need if st.GRAFANA_SUPPORTED_PANEL_TYPES[t] not in covered_kibana}
    assert not missing, (
        f"grafana panel types with no matrix-covered Kibana target: {sorted(missing)}. "
        "Add the type to tests/test_panel_matrix.py::_PANEL_TYPES or exempt it."
    )


def test_every_chart_bearing_datadog_type_is_covered_or_deferred():
    from tests.test_datadog_panel_matrix import _WIDGETS

    matrix = set(_WIDGETS)
    # A type cannot be both in the matrix and on the deferred list.
    assert not (matrix & _DATADOG_MATRIX_DEFERRED), (
        f"datadog types both matrixed and deferred: {sorted(matrix & _DATADOG_MATRIX_DEFERRED)}"
    )
    # Deferred/exempt entries must still be real supported types (no stale entries).
    stale = (_DATADOG_MATRIX_DEFERRED | _DATADOG_MATRIX_EXEMPT) - st.DATADOG_SUPPORTED_WIDGET_TYPES
    assert not stale, f"stale deferred/exempt datadog entries: {sorted(stale)}"

    need = st.DATADOG_SUPPORTED_WIDGET_TYPES - _DATADOG_MATRIX_EXEMPT
    uncovered = need - matrix - _DATADOG_MATRIX_DEFERRED
    assert not uncovered, (
        f"datadog widget types neither matrixed nor explicitly deferred: {sorted(uncovered)}. "
        "Add a matrix cell in tests/test_datadog_panel_matrix.py::_WIDGETS or list as deferred."
    )
