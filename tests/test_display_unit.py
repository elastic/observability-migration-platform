# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Migrate-time display-unit record (#483): did the Grafana unit survive?"""

from observability_migration.targets.kibana.emit.display import display_unit_record


def _grafana(unit):
    return {"fieldConfig": {"defaults": {"unit": unit}}}


def _xy(fmt):
    metric = {"field": "value"}
    if fmt is not None:
        metric["format"] = fmt
    return {"esql": {"type": "line", "metrics": [metric]}}


def test_seconds_kept_as_duration():
    record = display_unit_record(_grafana("s"), _xy({"type": "duration"}))
    assert record["status"] == "kept"
    assert record["source_unit"] == "s"
    assert record["expected_format"] == {"type": "duration"}


def test_scaled_bytes_are_unscaled():
    record = display_unit_record(_grafana("mbytes"), _xy({"type": "bytes"}))
    assert record["status"] == "unscaled"


def test_scaled_bits_rate_is_unscaled():
    record = display_unit_record(_grafana("Mbits"), _xy({"type": "bits", "suffix": "/s"}))
    assert record["status"] == "unscaled"


def test_unmapped_unit():
    record = display_unit_record(_grafana("furlongs"), _xy(None))
    assert record["status"] == "unmapped"
    assert record["expected_format"] is None


def test_no_unit_is_none():
    assert display_unit_record({}, _xy(None))["status"] == "none"
    assert display_unit_record(_grafana("none"), _xy({"type": "number"}))["status"] == "none"
    assert display_unit_record(_grafana("short"), _xy({"type": "number", "compact": True}))["status"] == "none"


def test_changed_family():
    # A curated override drawing a count where Grafana said bytes.
    record = display_unit_record(_grafana("bytes"), _xy({"type": "number", "decimals": 0}))
    assert record["status"] == "changed"


def test_percentunit_metric_tile_number_percent_is_kept():
    # Metric tiles deliberately emit number + "%" for percentunit (display.py).
    panel = {"esql": {"type": "metric", "primary": {"field": "v", "format": {"type": "number", "suffix": "%"}}}}
    assert display_unit_record(_grafana("percentunit"), panel)["status"] == "kept"


def test_mapped_unit_with_no_emitted_format_is_changed():
    record = display_unit_record(_grafana("bytes"), _xy(None))
    assert record["status"] == "changed"
    assert record["emitted_formats"] == []


def test_formats_collected_from_metrics_primary_and_metric():
    panel = {"esql": {
        "type": "gauge",
        "metric": {"field": "v", "format": {"type": "number", "suffix": "%"}},
        "metrics": [{"field": "a", "format": {"type": "number", "suffix": "%"}}],
        "primary": {"field": "p", "format": {"type": "number", "suffix": "%"}},
    }}
    record = display_unit_record(_grafana("percent"), panel)
    assert record["status"] == "kept"
    assert record["emitted_formats"] == [{"type": "number", "suffix": "%"}]


def _enrich(panel, yaml_panel, *, status="migrated"):
    from observability_migration.adapters.source.grafana.panels import _enrich_panel_result
    from observability_migration.core.reporting.report import PanelResult

    result = PanelResult(
        title="Memory", grafana_type="timeseries", kibana_type="line",
        status=status, confidence=1.0,
        esql_query="FROM metrics-* | STATS v = AVG(memory)",
    )
    return _enrich_panel_result(result, panel=panel, yaml_panel=yaml_panel)


def test_enrich_records_display_unit_without_changing_status():
    yaml_panel = {"esql": {"type": "line", "metrics": [{"field": "v", "format": {"type": "bytes"}}]}}
    enriched = _enrich(_grafana("mbytes"), yaml_panel)
    assert enriched.display_unit["status"] == "unscaled"
    # Report-only: a display gap must not move Migrated -> With warnings.
    assert enriched.status == "migrated"
    assert any(note.startswith("Display unit:") for note in enriched.notes)


def test_enrich_kept_unit_adds_no_note():
    yaml_panel = {"esql": {"type": "line", "metrics": [{"field": "v", "format": {"type": "duration"}}]}}
    enriched = _enrich(_grafana("s"), yaml_panel)
    assert enriched.display_unit["status"] == "kept"
    assert not any(note.startswith("Display unit:") for note in enriched.notes)


def test_enrich_without_yaml_panel_records_nothing():
    enriched = _enrich(_grafana("s"), None)
    assert enriched.display_unit == {}
