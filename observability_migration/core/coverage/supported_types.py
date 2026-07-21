# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Curated registry of the source panel/widget types the migration engine
supports.

This is the single source of truth for "100% translation coverage". It is a
*curated expectation* that the cross-check tests in
``tests/core/coverage/test_supported_types.py`` keep honest against the code's
actual routing tables:

* Grafana — mirrors ``grafana.panels.PANEL_TYPE_MAP`` / ``SKIP_PANEL_TYPES``.
* Datadog — mirrors every ``widget_type`` literal referenced by the planner
  rules in ``datadog.planner`` (extracted by
  ``observability_migration.core.coverage.datadog_introspect``).

Drift in either direction is a test failure: ship a new type in the code with no
registry entry, or leave a registry entry with no code support, and CI goes red.
Each entry is expected to carry a snapshot, a matrix cell, and a semantic
assertion (enforced incrementally by the coverage gate).
"""

from __future__ import annotations

# Grafana panel ``type`` string -> Kibana visualization type.
# Verbatim mirror of grafana.panels.PANEL_TYPE_MAP.
GRAFANA_SUPPORTED_PANEL_TYPES: dict[str, str] = {
    "timeseries": "line",
    "graph": "line",
    "stat": "metric",
    "singlestat": "metric",
    "gauge": "gauge",
    # Mirrors PANEL_TYPE_MAP's default. NOTE: a grouped/multi-value bargauge is
    # dynamically routed to a bar chart by bargauge_panel_rule at translation
    # time; "gauge" is the single-value default (see grafana.panels).
    "bargauge": "gauge",
    "table": "datatable",
    "table-old": "datatable",
    "text": "markdown",
    "logs": "datatable",
    "heatmap": "heatmap",
    "piechart": "pie",
    "grafana-piechart-panel": "pie",
    "barchart": "bar",
    # Discrete-state visualizations approximated as line charts (the query is an
    # ordinary metric time series; the state-band/status-cell rendering has no
    # Kibana equivalent so the loss is disclosed as a warning). See
    # grafana.panels.APPROXIMATED_VIS_TYPE_NOTES.
    "state-timeline": "line",
    "status-history": "line",
}

# Grafana panel types deliberately skipped (no Kibana equivalent / not a chart).
# Verbatim mirror of grafana.panels.SKIP_PANEL_TYPES.
GRAFANA_SKIPPED_PANEL_TYPES: set[str] = {
    "row",
    "news",
    "dashlist",
    "alertlist",
    "nodeGraph",
    "canvas",
}

# Datadog widget ``type`` strings the planner routes. Mirror of the literals
# referenced in datadog.planner (see datadog_introspect.collect_planner_widget_types).
DATADOG_SUPPORTED_WIDGET_TYPES: set[str] = {
    "timeseries",
    "query_value",
    "toplist",
    "bar_chart",
    "table",
    "query_table",
    "heatmap",
    "distribution",
    "change",
    "pie",
    "treemap",
    "sunburst",
    "scatterplot",
    "geomap",
    "hostmap",
    "log_stream",
    "list_stream",
    "note",
    "free_text",
    "image",
    "iframe",
    "group",
    "powerpack",
    # Status / topology widgets — emitted as informative markdown placeholders
    # (Elastic uses Synthetics / Alerts / Infrastructure inventory instead).
    # No query translation, so matrix-exempt.
    "check_status",
    "manage_status",
    "hostmap",
}
