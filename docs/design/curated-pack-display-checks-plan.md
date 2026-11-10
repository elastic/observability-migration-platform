# Curated Pack Display Checks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close [#483](https://github.com/elastic/observability-migration-platform/issues/483): record at migrate time whether each Grafana panel's unit survived into the emitted Kibana format, let the render audit observe drawn axis ticks and legend names, and make both part of the curated-pack done-when.

**Architecture:** Three independent pieces. (1) A pure `display_unit_record()` beside the unit table in `emit/display.py`, wired into `PanelResult` as report-only metadata (never a status change). (2) A pure chart-state classifier in `render_audit.py` fed by an opt-in Playwright capture in `render_audit_driver.py` (`--chart-state`, `--fail-on-display`) that reads Elastic Charts' `data-ech-debug-state`. (3) Docs/gate updates plus a live acceptance run on the Grafana 763 rig.

**Tech Stack:** Python 3.11+, pytest (`make test`), ruff (`make lint`), mypy (`make typecheck`), Playwright via the existing `browser` extra (already used by `interaction_driver.py`).

**Spec:** `docs/design/curated-pack-display-checks.md` (approved 2026-11-10).

## Global Constraints

- **No commits.** Repo rule (`AGENTS.md`): commit only when the user explicitly asks. Each task ends with a test checkpoint, not a commit. Leave the tree on branch `feat/curated-display-checks-483` for review.
- **Report-only units.** `display_unit` never changes panel Green/Yellow/Red status or `PanelResult.status`. The note text must avoid the semantic-loss tokens in `verification.py` (`manual`, `verify`, `not preserved`, `redesign`, `transformation`, `link`, `approximat`, `drop`) or `_enrich_panel_result` will silently promote panels to `migrated_with_warnings`. Append the note *after* the status-promotion block (see Task 2).
- **Degrade gracefully.** Old migration reports (no `display_unit`), panels with hidden titles, and panels with no Elastic Charts element are skipped with the reason recorded — never guessed, never silently passed.
- **Browser is opt-in.** Playwright is imported lazily inside the capture function (the `browser` extra is not in the end-user `all` extra). Without `--chart-state`, driver output and exit codes are byte-identical to today.
- **Source headers.** Every new `.py` file starts with the two-line Elastic license header used by all existing files; `make lint` enforces it.
- **Line length 120**, ruff rules as configured in `pyproject.toml`.
- Run tests from the repo root with `.venv/bin/python -m pytest <file> -q` during development; run `make test`, `make lint`, `make typecheck` before claiming done.

---

### Task 1: `display_unit_record()` — pure unit-vs-format comparison

**Files:**
- Modify: `observability_migration/targets/kibana/emit/display.py` (after `grafana_unit_to_yaml_format`, currently ending at line 141)
- Test: `tests/test_display_unit.py` (create)

**Interfaces:**
- Consumes: `extract_grafana_unit(panel)`, `grafana_unit_to_yaml_format(unit)`, `GRAFANA_UNIT_TO_YAML` — all already in `display.py`.
- Produces: `display_unit_record(grafana_panel: dict, yaml_panel: dict) -> dict` returning `{"source_unit": str, "expected_format": dict | None, "emitted_formats": list[dict], "status": "none" | "kept" | "unmapped" | "unscaled" | "changed"}`. Task 2 and Task 3 consume this exact shape.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_display_unit.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_display_unit.py -q`
Expected: FAIL — `ImportError: cannot import name 'display_unit_record'`.

- [ ] **Step 3: Implement `display_unit_record` in `display.py`**

Insert after `grafana_unit_to_yaml_format` (after line 141):

```python
# Grafana units that name a *scaled* quantity. Kibana's bytes/bits formatters
# rescale for display, but the value is not rescaled at emit time, so a Grafana
# ``mbytes`` value of 512 is drawn as "512 B". Reported as ``unscaled`` by
# display_unit_record; rescaling the value is a separate translator change.
_SCALED_UNITS = frozenset({
    "kbytes", "mbytes", "gbytes", "tbytes",
    "KBs", "MBs", "GBs", "Kbits", "Mbits", "Gbits",
})


def _format_family(fmt: dict | None) -> str:
    """The unit family a YAML format draws: percent, duration, bytes, bits,
    ``number:<suffix>``, or the bare type. ``{"type": "percent"}`` and
    ``{"type": "number", "suffix": "%"}`` are one family — metric tiles emit
    the latter for Grafana ``percentunit`` (see enrich_yaml_panel_display)."""
    if not isinstance(fmt, dict):
        return ""
    fmt_type = str(fmt.get("type") or "")
    suffix = str(fmt.get("suffix") or "").strip()
    if fmt_type == "percent" or suffix == "%":
        return "percent"
    if fmt_type in ("duration", "bytes", "bits"):
        return fmt_type
    if fmt_type == "number":
        return f"number:{suffix}" if suffix else "number"
    return fmt_type


def _emitted_measure_formats(yaml_panel: dict) -> list[dict]:
    """Distinct ``format`` dicts on the emitted panel's measures, in order."""
    esql = yaml_panel.get("esql") if isinstance(yaml_panel, dict) else None
    if not isinstance(esql, dict):
        return []
    entries: list[dict] = []
    metrics = esql.get("metrics")
    if isinstance(metrics, list):
        entries.extend(entry for entry in metrics if isinstance(entry, dict))
    for key in ("primary", "metric"):
        entry = esql.get(key)
        if isinstance(entry, dict):
            entries.append(entry)
    formats: list[dict] = []
    for entry in entries:
        fmt = entry.get("format")
        if isinstance(fmt, dict) and fmt not in formats:
            formats.append(dict(fmt))
    return formats


def display_unit_record(grafana_panel: dict, yaml_panel: dict) -> dict:
    """Did *grafana_panel*'s unit survive into the emitted *yaml_panel*?

    Returns ``{"source_unit", "expected_format", "emitted_formats", "status"}``
    with status ``none`` (no unit to preserve), ``kept``, ``unmapped`` (the unit
    has no Kibana format mapping), ``unscaled`` (a scaled byte/bit unit drawn
    as unscaled bytes/bits), or ``changed`` (a different family was emitted,
    e.g. by a curated override). Report-only: callers must not feed this into
    panel status.
    """
    source_unit = extract_grafana_unit(grafana_panel or {})
    expected = grafana_unit_to_yaml_format(source_unit) if source_unit else None
    emitted = _emitted_measure_formats(yaml_panel or {})
    if source_unit in ("", "none", "short"):
        status = "none"
    elif expected is None:
        status = "unmapped"
    elif source_unit in _SCALED_UNITS and any(
        _format_family(fmt) in ("bytes", "bits") for fmt in emitted
    ):
        status = "unscaled"
    elif not emitted:
        status = "changed"
    elif all(_format_family(fmt) == _format_family(expected) for fmt in emitted):
        status = "kept"
    else:
        status = "changed"
    return {
        "source_unit": source_unit,
        "expected_format": dict(expected) if expected else None,
        "emitted_formats": emitted,
        "status": status,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_display_unit.py -q`
Expected: 9 passed.

Also run the existing display tests so the insert broke nothing:
`.venv/bin/python -m pytest tests/test_migrate.py -q -k "unit or enrich or humanize"` — Expected: PASS.

---

### Task 2: Wire `display_unit` into `PanelResult`, the JSON report, and the summary

**Files:**
- Modify: `observability_migration/core/reporting/report.py` — `PanelResult` (line 115), `print_report` (line 574), `save_detailed_report` panel dict (line ~769)
- Modify: `observability_migration/adapters/source/grafana/panels.py` — import (line 29), `_enrich_panel_result` (line 4137)
- Test: `tests/test_display_unit.py` (append), `tests/core/reporting/test_print_report.py` (append one test)

**Interfaces:**
- Consumes: `display_unit_record(grafana_panel, yaml_panel)` from Task 1.
- Produces: `PanelResult.display_unit: dict`; `migration_report.json` panels carry `display_unit`; `print_display_unit_summary(results)` in `report.py` (Task 3's driver reads `display_unit.source_unit` from the JSON, not from this function).

- [ ] **Step 1: Write the failing wiring tests**

Append to `tests/test_display_unit.py`:

```python
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
```

Append to `tests/core/reporting/test_print_report.py`:

```python
class DisplayUnitSummaryTests(unittest.TestCase):
    def test_summary_line_counts_statuses_and_skips_rows(self):
        result = MigrationResult(dashboard_title="Redis", dashboard_uid="redis")
        kept = _panel("Uptime", "migrated")
        kept.display_unit = {"source_unit": "s", "status": "kept"}
        unscaled = _panel("Memory", "migrated")
        unscaled.display_unit = {"source_unit": "mbytes", "status": "unscaled"}
        row = _row("Section")
        row.display_unit = {"source_unit": "bytes", "status": "changed"}
        result.panel_results = [kept, unscaled, row]
        result.total_panels = 3
        result.migrated = 2
        result.skipped = 1

        output = _capture_report(result)
        self.assertIn("Display units:       1 kept / 0 unmapped / 1 unscaled / 0 changed", output)

    def test_no_display_unit_line_when_no_panel_has_a_unit(self):
        result = _argocd_fixture()
        self.assertNotIn("Display units:", _capture_report(result))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_display_unit.py tests/core/reporting/test_print_report.py -q`
Expected: FAIL — `PanelResult` has no `display_unit`; no `Display units:` line.

- [ ] **Step 3a: Add the field and serialization in `report.py`**

In `PanelResult`, after `target_binding` (line 152):

```python
    # Whether the Grafana unit survived into the emitted format:
    # {"source_unit", "expected_format", "emitted_formats", "status"} with
    # status none|kept|unmapped|unscaled|changed. Report-only (#483): it never
    # feeds panel status, unlike target_binding's smoke rollups.
    display_unit: dict = field(default_factory=dict)
```

In `save_detailed_report`'s panel dict, after `"target_binding": pr.target_binding,`:

```python
                    "display_unit": pr.display_unit,
```

- [ ] **Step 3b: Add `print_display_unit_summary` and call it**

In `report.py`, after `print_target_binding_summary` (ends line 571), add:

```python
def print_display_unit_summary(results):
    """Print whether Grafana units survived into the emitted formats (#483).

    Report-only: a unit drawn differently (a scaled ``mbytes`` value shown as
    plain bytes, a unit with no Kibana mapping) is a display gap worth seeing,
    not a translation failure, so nothing here touches panel status.
    """
    records = [
        pr.display_unit
        for r in results
        for pr in r.panel_results
        if pr.grafana_type != "row" and pr.display_unit
    ]
    counts = Counter(str(record.get("status") or "none") for record in records)
    if not any(counts[status] for status in ("kept", "unmapped", "unscaled", "changed")):
        return
    print(
        f"Display units:       {counts['kept']} kept / {counts['unmapped']} unmapped / "
        f"{counts['unscaled']} unscaled / {counts['changed']} changed"
    )
```

Check the top of `report.py`: `Counter` must be imported (`from collections import Counter`); add it if absent. Then in `print_report`, after `print_target_binding_summary(results)` (line 630):

```python
    print_display_unit_summary(results)
```

- [ ] **Step 3c: Record the unit in `_enrich_panel_result`**

In `panels.py`, extend the `emit.display` import (line 29) with `display_unit_record`. In `_enrich_panel_result`, immediately before `return panel_result` (after `_sync_visual_ir(panel_result, yaml_panel)`, line 4221) — i.e. *after* the `panel_notes_imply_warning` status-promotion block, so the note cannot promote the panel:

```python
    # Display-unit record (#483): computed here, the one place that sees both
    # the source panel and the final emitted panel (after curated overrides),
    # and appended after the status-promotion block above so the note stays
    # report-only. The wording avoids the semantic-loss tokens in
    # verification.py for the same reason.
    if yaml_panel and isinstance(yaml_panel.get("esql"), dict):
        record = display_unit_record(panel, yaml_panel)
        panel_result.display_unit = record
        status = record["status"]
        if status == "unmapped":
            _append_unique(
                panel_result.notes,
                f'Display unit: Grafana unit "{record["source_unit"]}" has no Kibana '
                "format mapping, so the panel shows plain numbers.",
            )
        elif status == "unscaled":
            _append_unique(
                panel_result.notes,
                f'Display unit: Grafana unit "{record["source_unit"]}" is drawn as '
                "unscaled bytes/bits, with the value left in the Grafana unit.",
            )
        elif status == "changed":
            _append_unique(
                panel_result.notes,
                f'Display unit: Grafana unit "{record["source_unit"]}" is drawn with '
                "a different format family than the unit mapping gives.",
            )
    return panel_result
```

Note: if `_enrich_panel_result` already ends with `return panel_result` after `_sync_visual_ir`, merge into that ending — do not add a second return. Also confirm the local variable holding the source panel is named `panel` (it is: `panel = panel or {}` at line 4149).

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_display_unit.py tests/core/reporting/test_print_report.py tests/test_target_binding.py -q`
Expected: PASS (including Task 1's tests; `test_target_binding.py` guards that the neighbouring summary line did not shift).

Then a translation smoke check: `.venv/bin/python -m pytest tests/test_migrate.py -q -k "redis or gauge or percent"` — Expected: PASS.

---

### Task 3: Chart-state classifier in `render_audit.py`

**Files:**
- Modify: `observability_migration/targets/kibana/render_audit.py` (append a new section at the end of the file)
- Test: `tests/test_render_audit_display.py` (create)

**Interfaces:**
- Consumes: a captured panel list `[{"title": str, "charts": [debug_state, ...], "skipped": str (optional)}]` where each debug state is the parsed Elastic Charts JSON (`axes.y[*].labels`, `axes.y[*].title`, `legend.items[*].name`); `grafana_unit_to_yaml_format` from `observability_migration.targets.kibana.emit.display` for the expected format of a source unit.
- Produces:
  - `DisplayFinding` dataclass (`kind`, `detail`) with `to_dict()`.
  - `PanelDisplayResult` dataclass (`title`, `findings`, `skipped`) with `to_dict()` → `{"title", "findings": [...], "skipped"}`.
  - `DisplayVerdict` dataclass (`status`, `panels`, `reasons`, `notes`) with `to_dict()` → `{"status", "panels", "reasons", "notes"}`.
  - `check_chart_state(title: str, charts: list[dict], *, source_unit: str = "") -> list[DisplayFinding]`
  - `audit_chart_state(captured, *, source_unit_by_title=None, fail_on_display=False, unit_source_available=True) -> DisplayVerdict`
  - `display_unit_by_panel(report: dict) -> dict[str, str]`
  Task 4 consumes all of these.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_render_audit_display.py`:

```python
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Render-audit display findings (#483): what Kibana actually drew.

Fixtures are Elastic Charts debug states (``data-ech-debug-state``) modelled
on Grafana 763: the "0 ms" axes of #480 and the "(null)" legends of #481.
"""

from observability_migration.targets.kibana.render_audit import (
    audit_chart_state,
    check_chart_state,
    display_unit_by_panel,
)


def _chart(*, y_labels=None, legend=(), y_title=""):
    chart = {"legend": {"items": [{"name": name} for name in legend]}}
    if y_labels is not None:
        chart["axes"] = {"y": [{"labels": list(y_labels), "title": y_title}]}
    return chart


def _kinds(findings):
    return [finding.kind for finding in findings]


def test_all_identical_ticks_are_unreadable():
    charts = [_chart(y_labels=["0 ms"] * 5, legend=["get"])]
    findings = check_chart_state("Average Time Spent by Command / sec", charts, source_unit="s")
    assert _kinds(findings) == ["unreadable_axis"]
    assert "0 ms" in findings[0].detail


def test_fitted_second_ticks_are_clean():
    charts = [_chart(y_labels=["0.00030 s", "0.00025 s", "0.00020 s"], legend=["get"])]
    assert check_chart_state("Average Time Spent by Command / sec", charts, source_unit="s") == []


def test_single_tick_is_not_unreadable():
    assert check_chart_state("P", [_chart(y_labels=["0 ms"])]) == []


def test_null_legend_entries_are_flagged():
    charts = [_chart(y_labels=["0 B", "500 B"], legend=["(null) - used", "(null) - max"])]
    findings = check_chart_state("Total Memory Usage", charts, source_unit="bytes")
    assert _kinds(findings) == ["null_legend", "null_legend"]


def test_empty_legend_name_is_flagged():
    findings = check_chart_state("P", [_chart(y_labels=["0 B"], legend=["  "])])
    assert _kinds(findings) == ["null_legend"]


def test_real_legend_names_are_clean():
    charts = [_chart(y_labels=["0 B", "500 B"], legend=["redis-a:6379 - used", "redis-a:6379 - max"])]
    assert check_chart_state("Total Memory Usage", charts, source_unit="bytes") == []


def test_unit_mismatch_when_ticks_carry_no_unit():
    charts = [_chart(y_labels=["0", "500", "1000"], legend=["used"])]
    findings = check_chart_state("Total Memory Usage", charts, source_unit="bytes")
    assert _kinds(findings) == ["unit_mismatch"]


def test_unit_mismatch_percent():
    charts = [_chart(y_labels=["0", "50", "100"], legend=["used"])]
    assert _kinds(check_chart_state("P", charts, source_unit="percentunit")) == ["unit_mismatch"]
    charts = [_chart(y_labels=["0 %", "50 %", "100 %"], legend=["used"])]
    assert check_chart_state("P", charts, source_unit="percentunit") == []


def test_dual_axis_passes_when_any_axis_carries_the_unit():
    chart = {
        "axes": {"y": [
            {"labels": ["0", "10", "20"], "title": "count"},
            {"labels": ["0 B", "1 KB", "2 KB"], "title": "size"},
        ]},
        "legend": {"items": []},
    }
    assert check_chart_state("P", [chart], source_unit="bytes") == []


def test_gauge_without_y_axis_only_checks_the_legend():
    charts = [_chart(legend=["Memory Usage"])]  # no axes key at all
    assert check_chart_state("Memory Usage", charts, source_unit="percent") == []
    charts = [_chart(legend=["(null)"])]
    assert _kinds(check_chart_state("Memory Usage", charts, source_unit="percent")) == ["null_legend"]


def test_unmapped_unit_is_not_checked():
    charts = [_chart(y_labels=["0", "5", "10"], legend=["x"])]
    assert check_chart_state("P", charts, source_unit="furlongs") == []
    assert check_chart_state("P", charts, source_unit="") == []


def test_audit_warns_by_default_and_fails_on_request():
    captured = [{"title": "P", "charts": [_chart(y_labels=["0 ms"] * 4, legend=["x"])]}]
    warn = audit_chart_state(captured, source_unit_by_title={"P": "s"})
    assert warn.status == "warn"
    assert warn.panels[0].findings[0].kind == "unreadable_axis"
    fail = audit_chart_state(captured, source_unit_by_title={"P": "s"}, fail_on_display=True)
    assert fail.status == "fail"


def test_audit_records_skips():
    captured = [
        {"title": "Table", "charts": []},
        {"title": "Hidden", "charts": [], "skipped": "panel title did not render"},
    ]
    verdict = audit_chart_state(captured)
    assert verdict.status == "pass"
    assert verdict.panels[0].skipped
    assert verdict.panels[1].skipped == "panel title did not render"


def test_audit_notes_when_report_predates_display_unit():
    captured = [{"title": "P", "charts": [_chart(y_labels=["0", "1"], legend=["x"])]}]
    verdict = audit_chart_state(captured, unit_source_available=False)
    assert verdict.status == "pass"
    assert any("display_unit" in note for note in verdict.notes)


def test_display_unit_by_panel_reads_source_units():
    report = {"dashboards": [{"panels": [
        {"title": "A", "display_unit": {"source_unit": "s", "status": "kept"}},
        {"title": "B", "display_unit": {}},
        {"title": "C"},
    ]}]}
    assert display_unit_by_panel(report) == {"A": "s"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_render_audit_display.py -q`
Expected: FAIL — `ImportError: cannot import name 'audit_chart_state'`.

- [ ] **Step 3: Implement the classifier**

Append to `render_audit.py`. First add the import near the top (after the `BROWSER_ERROR_PATTERNS` import, line 27):

```python
from observability_migration.targets.kibana.emit.display import grafana_unit_to_yaml_format
```

(Check for a cycle before running: `emit/display.py` imports only `re` and `typing`, so this is safe.)

Then append at the end of the file:

```python
# --------------------------------------------------------------------------- #
# Display findings: what the chart actually drew (issue #483)
# --------------------------------------------------------------------------- #
#
# The DOM/a11y audit above can see legend text but not axis ticks — Elastic
# Charts draws ticks on canvas. The debug state Kibana exposes when
# ``window._echDebugStateFlag`` is set (``data-ech-debug-state`` on
# ``.echChartStatus``) carries the rendered tick labels and legend item names,
# which is what these checks read. Three findings, all observed on Grafana 763:
# a ``(null)`` legend (#481), an axis whose every tick reads the same ("0 ms",
# #480), and ticks that carry none of the Grafana unit.

_DURATION_TICK_RE = re.compile(
    r"(?:\bns\b|µs|μs|\bus\b|\bms\b|\bsec(?:ond)?s?\b|\bmin(?:ute)?s?\b"
    r"|\bhours?\b|\bhrs?\b|\bdays?\b|(?<=\s)[shd]\b)",
    re.IGNORECASE,
)
_BYTES_TICK_RE = re.compile(r"(?:KiB|MiB|GiB|TiB|PiB|KB|MB|GB|TB|PB|kB|\bB\b|\bbytes?\b)", re.IGNORECASE)
_BITS_TICK_RE = re.compile(r"(?:kbits?|mbits?|gbits?|Kbit|Mbit|Gbit|\bbits?\b|\bb\b)", re.IGNORECASE)


@dataclass
class DisplayFinding:
    """One thing the drawn chart got wrong: ``kind`` + a human ``detail``."""

    kind: str  # null_legend | unreadable_axis | unit_mismatch
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "detail": self.detail}


@dataclass
class PanelDisplayResult:
    title: str
    findings: list[DisplayFinding] = field(default_factory=list)
    skipped: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "findings": [finding.to_dict() for finding in self.findings],
            "skipped": self.skipped,
        }


@dataclass
class DisplayVerdict:
    status: str = "pass"  # "pass" | "warn" | "fail"
    panels: list[PanelDisplayResult] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "panels": [panel.to_dict() for panel in self.panels],
            "reasons": self.reasons,
            "notes": self.notes,
        }


def _chart_y_axes(chart: dict) -> list[dict]:
    axes = chart.get("axes") if isinstance(chart, dict) else None
    y_axes = axes.get("y") if isinstance(axes, dict) else None
    return [axis for axis in y_axes if isinstance(axis, dict)] if isinstance(y_axes, list) else []


def _chart_legend_names(chart: dict) -> list[str]:
    legend = chart.get("legend") if isinstance(chart, dict) else None
    items = legend.get("items") if isinstance(legend, dict) else None
    names: list[str] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            names.append(str(item.get("name") or ""))
        elif item is not None:
            names.append(str(item))
    return names


def _tick_carries_unit(fmt: dict, labels: list[str]) -> bool | None:
    """True/False when *fmt*'s family can be recognised in tick *labels*.

    ``None`` means the family has no tick signature to check (plain numbers,
    compact, booleans), so the unit check does not apply.
    """
    fmt_type = str(fmt.get("type") or "")
    suffix = str(fmt.get("suffix") or "").strip()
    if fmt_type == "percent" or suffix == "%":
        return any("%" in label for label in labels)
    if fmt_type == "duration":
        return any(_DURATION_TICK_RE.search(label) for label in labels)
    if fmt_type == "bytes":
        return any(_BYTES_TICK_RE.search(label) for label in labels)
    if fmt_type == "bits":
        return any(_BITS_TICK_RE.search(label) for label in labels)
    if suffix:
        return any(suffix in label for label in labels)
    return None


def check_chart_state(title: str, charts: list[dict], *, source_unit: str = "") -> list[DisplayFinding]:
    """Display findings for one panel's drawn charts (empty == drawn correctly).

    ``charts`` are parsed Elastic Charts debug states. Findings:
    ``null_legend`` (a legend name is ``(null)`` or empty), ``unreadable_axis``
    (a y-axis with 2+ tick labels that are all identical), ``unit_mismatch``
    (the Grafana ``source_unit`` has a recognisable family and no y-axis tick
    carries it — any one axis carrying it passes, so dual-axis panels are not
    flagged for their other axis). A chart with no y-axis (gauge, metric,
    partition) only gets the legend check.
    """
    findings: list[DisplayFinding] = []
    for chart in charts or []:
        for name in _chart_legend_names(chart):
            if not name.strip() or "(null)" in name:
                findings.append(DisplayFinding(
                    "null_legend",
                    f"legend entry {name.strip()!r} is empty or (null)",
                ))
        for axis in _chart_y_axes(chart):
            labels = [str(label) for label in axis.get("labels") or []]
            if len(labels) >= 2 and len(set(labels)) == 1:
                findings.append(DisplayFinding(
                    "unreadable_axis",
                    f"{len(labels)} y-axis ticks all read {labels[0]!r}",
                ))
    expected = grafana_unit_to_yaml_format(source_unit) if source_unit else None
    if expected:
        all_labels = [
            str(label)
            for chart in charts or []
            for axis in _chart_y_axes(chart)
            for label in axis.get("labels") or []
        ]
        if all_labels:
            carries = _tick_carries_unit(expected, all_labels)
            if carries is False:
                findings.append(DisplayFinding(
                    "unit_mismatch",
                    f"no y-axis tick carries the Grafana unit {source_unit!r} "
                    f"(ticks start {all_labels[0]!r})",
                ))
    return findings


def display_unit_by_panel(report: dict) -> dict[str, str]:
    """``{panel title: Grafana source unit}`` from a migration report (#483).

    Reads ``panels[].display_unit.source_unit`` (written since the migrate-time
    unit record). Panels from older reports have no record and are absent, so
    their unit check is skipped rather than guessed.
    """
    out: dict[str, str] = {}
    for dashboard in (report or {}).get("dashboards", []):
        for panel in dashboard.get("panels", []):
            if not isinstance(panel, dict):
                continue
            record = panel.get("display_unit")
            if isinstance(record, dict) and record.get("source_unit"):
                out[str(panel.get("title") or "")] = str(record["source_unit"])
    return out


def audit_chart_state(
    captured,
    *,
    source_unit_by_title: dict[str, str] | None = None,
    fail_on_display: bool = False,
    unit_source_available: bool = True,
) -> DisplayVerdict:
    """Aggregate per-panel display findings into one verdict.

    ``captured`` is ``[{"title", "charts", "skipped"?}, ...]``. A panel with
    no charts (table, markdown, hidden or unrendered title) is recorded as
    skipped, never as passed. Findings are ``warn`` by default — on an
    operator's target a ``(null)`` legend can be a label-binding gap, not a
    translator bug — and ``fail`` only under ``fail_on_display`` (a seeded
    curated-pack rig). ``unit_source_available=False`` records in ``notes``
    that unit checks were skipped because the report predates ``display_unit``.
    """
    source_unit_by_title = source_unit_by_title or {}
    verdict = DisplayVerdict()
    if not unit_source_available:
        verdict.notes.append(
            "unit checks skipped: the migration report has no display_unit "
            "records (it predates #483); re-migrate to enable them"
        )
    for entry in captured or []:
        title = str(entry.get("title") or "")
        charts = entry.get("charts") or []
        skipped = str(entry.get("skipped") or "")
        if skipped or not charts:
            verdict.panels.append(PanelDisplayResult(
                title=title,
                skipped=skipped or "no Elastic Charts chart state captured "
                                   "(table, markdown, or hidden title)",
            ))
            continue
        findings = check_chart_state(
            title, charts, source_unit=source_unit_by_title.get(title, "")
        )
        verdict.panels.append(PanelDisplayResult(title=title, findings=findings))
        verdict.reasons.extend(
            f"{title}: {finding.kind} — {finding.detail}" for finding in findings
        )
    if verdict.reasons:
        verdict.status = "fail" if fail_on_display else "warn"
    return verdict
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_render_audit_display.py tests/test_render_audit.py tests/test_render_audit_driver.py -q`
Expected: PASS. (The existing render-audit suites guard that the new import and section did not disturb the current classifier.)

---

### Task 4: `--chart-state` capture and CLI wiring in `render_audit_driver.py`

**Files:**
- Modify: `observability_migration/targets/kibana/render_audit_driver.py` — imports (line 45), `run_audit_cli` (line 503), `_build_argparser` (line 740)
- Test: `tests/test_render_audit_driver.py` (append)

**Interfaces:**
- Consumes: `audit_chart_state`, `display_unit_by_panel`, `DisplayVerdict` from Task 3; the already-scoped `report` and `verdict` inside `run_audit_cli`; `build_dashboard_url` (already imported).
- Produces: CLI flags `--chart-state` and `--fail-on-display`; an injectable `chart_state_fetcher: Callable[[str], list[dict]]` parameter on `run_audit_cli` (mirroring `dom_fetcher`); `capture_chart_state(url, user_data_dir, *, no_sandbox=False, timeout_ms=60000) -> list[dict]`; a `"display"` key in the JSON output when `--chart-state` is set.

- [ ] **Step 1: Write the failing driver tests**

Append to `tests/test_render_audit_driver.py`. Note the local `_cli_args` helper builds a `Namespace` without the new keys, so these tests pass a full namespace — `run_audit_cli` must read the new flags with `getattr(..., False)` so the older tests keep working.

```python
def _display_args(tmp_path, *, fail_on_display=False, fail_on_error=True):
    import argparse
    import json as _json

    report = {"dashboards": [{"title": "Redis", "panels": [
        {"title": "Average Time Spent by Command / sec", "kibana_type": "line",
         "display_unit": {"source_unit": "s", "status": "kept"},
         "yaml_panel": {"esql": {"type": "line", "query": "FROM metrics-* | STATS v = AVG(x)"}}},
    ]}]}
    (tmp_path / "migration_report.json").write_text(_json.dumps(report))
    return argparse.Namespace(
        kibana_url="https://kb", dashboard_id="Redis", space="", user_data_dir="",
        time_from="now-1h", time_to="now", fail_on_error=fail_on_error, elements=False,
        migration_out=str(tmp_path), es_url="", es_api_key="", es_index="metrics-*",
        insecure=False, agent_browser=False, chrome_no_sandbox=False,
        chart_state=True, fail_on_display=fail_on_display,
    )


_ZERO_MS = [{"title": "Average Time Spent by Command / sec", "charts": [
    {"axes": {"y": [{"labels": ["0 ms"] * 5, "title": "Seconds"}]},
     "legend": {"items": [{"name": "get"}]}},
]}]
_FITTED = [{"title": "Average Time Spent by Command / sec", "charts": [
    {"axes": {"y": [{"labels": ["0.00030 s", "0.00020 s", "0.00010 s"], "title": "Seconds"}]},
     "legend": {"items": [{"name": "get"}]}},
]}]


def _run_display_cli(args, captured, capsys):
    import json as _json

    from observability_migration.targets.kibana.render_audit_driver import run_audit_cli

    rc = run_audit_cli(
        args,
        # The DOM must name the panel, or the render audit (correctly) reports
        # an unmatched title and the render verdict is warn, not pass.
        dom_fetcher=lambda _u: "Average Time Spent by Command / sec line chart rendered",
        chart_state_fetcher=lambda _u: captured,
    )
    return rc, _json.loads(capsys.readouterr().out)


def test_cli_chart_state_warns_without_fail_flags(tmp_path, capsys):
    rc, output = _run_display_cli(_display_args(tmp_path), _ZERO_MS, capsys)
    assert rc == 0
    assert output["display"]["status"] == "warn"
    assert output["display"]["panels"][0]["findings"][0]["kind"] == "unreadable_axis"
    assert output["render"]["status"] == "pass"  # render verdict untouched


def test_cli_chart_state_fail_on_display_exits_1(tmp_path, capsys):
    rc, output = _run_display_cli(
        _display_args(tmp_path, fail_on_display=True), _ZERO_MS, capsys
    )
    assert rc == 1
    assert output["display"]["status"] == "fail"


def test_cli_chart_state_clean_capture_passes(tmp_path, capsys):
    rc, output = _run_display_cli(_display_args(tmp_path), _FITTED, capsys)
    assert rc == 0
    assert output["display"]["status"] == "pass"


def test_cli_without_chart_state_has_no_display_key(tmp_path, capsys):
    args = _display_args(tmp_path)
    args.chart_state = False
    rc, output = _run_display_cli(args, _ZERO_MS, capsys)
    assert rc == 0
    assert "display" not in output
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_render_audit_driver.py -q -k display`
Expected: FAIL — `run_audit_cli() got an unexpected keyword argument 'chart_state_fetcher'`.

- [ ] **Step 3a: Add the capture function and fetcher type**

In `render_audit_driver.py`, extend the `render_audit` import (line 45) with `audit_chart_state` and `display_unit_by_panel`. After the `FieldFetcher` definition (line 70), add:

```python
# A chart-state fetcher takes a dashboard URL and returns the drawn charts:
# [{"title": str, "charts": [Elastic Charts debug state, ...]}, ...].
ChartStateFetcher = Callable[[str], list]
```

After `dump_dom` (ends line 447), add the capture:

```python
_CHART_STATE_JS = """
() => [...document.querySelectorAll('[data-test-subj="dashboardPanel"]')].map((panel) => {
  const titleEl = panel.querySelector('[data-test-subj="embeddablePanelTitle"]');
  const charts = [...panel.querySelectorAll('.echChartStatus[data-ech-debug-state]')]
    .map((el) => { try { return JSON.parse(el.getAttribute('data-ech-debug-state')); }
                   catch (e) { return null; } })
    .filter(Boolean);
  return { title: titleEl ? titleEl.textContent.trim() : '', charts };
}).filter((entry) => entry.title || entry.charts.length)
"""

_RENDER_COMPLETE_JS = """
() => {
  const statuses = [...document.querySelectorAll('.echChartStatus')];
  return statuses.length > 0
    && statuses.every((el) => el.getAttribute('data-ech-render-complete') === 'true');
}
"""


def capture_chart_state(
    url: str,
    user_data_dir: str,
    *,
    no_sandbox: bool = False,
    timeout_ms: int = 60_000,
) -> list:
    """Drawn axis ticks and legend names per panel, via Elastic Charts debug state.

    Sets ``window._echDebugStateFlag`` before navigation (Lens XY/gauge/
    partition/heatmap pass it as ``debugState``), waits for every chart to
    report ``data-ech-render-complete``, then reads each ``dashboardPanel``'s
    title and parsed ``data-ech-debug-state``. Requires the ``browser`` extra
    (Playwright); imported lazily so the DOM-only audit never needs it.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "render audit --chart-state needs the browser extra: "
            "pip install 'elastic-observability-migration[browser]' "
            "and 'playwright install chromium'"
        ) from exc
    launch_args = ["--no-sandbox"] if no_sandbox else []
    with sync_playwright() as playwright:
        if user_data_dir:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir, headless=True, args=launch_args,
            )
            browser = None
        else:
            browser = playwright.chromium.launch(headless=True, args=launch_args)
            context = browser.new_context()
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.add_init_script("window._echDebugStateFlag = true;")
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_function(_RENDER_COMPLETE_JS, timeout=timeout_ms)
            except Exception:
                # A chart that never finishes leaves partial state; what did
                # draw is still checked, and the render verdict above remains
                # the authority on panels that failed to load.
                pass
            return page.evaluate(_CHART_STATE_JS)
        finally:
            context.close()
            if browser is not None:
                browser.close()
```

(The selectors are Kibana's own test contract: `dashboardPanel` in `dashboard_grid_item.tsx`, `embeddablePanelTitle` in `presentation_panel_title.tsx`, `.echChartStatus[data-ech-render-complete=true]` in Kibana's `elastic_chart.ts` test service.)

- [ ] **Step 3b: Wire `run_audit_cli`**

Change the signature to accept the fetcher:

```python
def run_audit_cli(
    args: argparse.Namespace,
    *,
    dom_fetcher: DomFetcher | None = None,
    field_fetcher: FieldFetcher | None = None,
    tab_driver: TabDriver | None = None,
    chart_state_fetcher: ChartStateFetcher | None = None,
) -> int:
```

After `output: dict[str, object] = {"render": verdict.to_dict()}` (line 716) and the `--elements` block, before `print(json.dumps(output, indent=2))`, insert:

```python
    display = None
    if getattr(args, "chart_state", False):
        fetch_state = chart_state_fetcher or (
            lambda u: capture_chart_state(
                u,
                getattr(args, "user_data_dir", "") or "",
                no_sandbox=bool(getattr(args, "chrome_no_sandbox", False)),
            )
        )
        captured = fetch_state(url)
        # Panels the capture did not see (hidden title, never rendered) are
        # recorded as skipped by title, so a missing panel cannot pass silently.
        if report is not None:
            seen = {str(entry.get("title") or "") for entry in captured}
            for title in panel_titles_in_order(report):
                if title not in seen:
                    captured.append({
                        "title": title, "charts": [],
                        "skipped": "panel title did not render in the chart-state capture",
                    })
        unit_by_title = display_unit_by_panel(report) if report is not None else {}
        unit_available = bool(unit_by_title) or (
            report is not None
            and any(
                isinstance(panel.get("display_unit"), dict)
                for dashboard in report.get("dashboards", [])
                for panel in dashboard.get("panels", [])
            )
        )
        display = audit_chart_state(
            captured,
            source_unit_by_title=unit_by_title,
            fail_on_display=bool(getattr(args, "fail_on_display", False)),
            unit_source_available=unit_available,
        )
        output["display"] = display.to_dict()
```

Replace the exit block at the end:

```python
    print(json.dumps(output, indent=2))
    if verdict.status == "fail" and args.fail_on_error:
        return 1
    return 0
```

with:

```python
    print(json.dumps(output, indent=2))
    if args.fail_on_error and (
        verdict.status == "fail" or (display is not None and display.status == "fail")
    ):
        return 1
    return 0
```

- [ ] **Step 3c: Add the CLI flags**

In `_build_argparser`, after `--elements` (line 758):

```python
    parser.add_argument(
        "--chart-state", action="store_true",
        help="Also read each chart's drawn axis ticks and legend names (Elastic "
             "Charts debug state) and report display findings: (null) legends, "
             "axes whose ticks all read the same, ticks missing the Grafana unit. "
             "Needs the browser extra (Playwright). Findings warn by default.",
    )
    parser.add_argument(
        "--fail-on-display", action="store_true",
        help="With --chart-state, make any display finding a fail (exit non-zero "
             "with --fail-on-error). For seeded curated-pack rigs, where a "
             "(null) legend or unreadable axis is a pack failure, not data readiness.",
    )
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_render_audit_driver.py tests/test_render_audit_display.py tests/test_render_audit.py -q`
Expected: PASS, including the four new driver tests and every pre-existing driver test (proving `getattr` kept old `Namespace`s working).

---

### Task 5: Docs and the curated-pack gate

**Files:**
- Modify: `docs/design/curated-dashboard-packs.md` — Step 5 (line 539) and the gate table (line ~572)
- Modify: `docs/architecture/asset-model.md` — after the Target Binding section (ends line 43)
- Modify: `docs/command-contract.md` — new short section after Target Binding (line 324)
- Modify: `docs/sources/grafana.md` — near the target-binding paragraph (line ~928)
- Modify: `docs/testing.md` — Render audit section (line 365)
- Modify: `docs/contributing/dev-commands.md` — `render_audit_driver` row (line 60) and the render-audit examples (lines ~190-258)

**Interfaces:**
- Consumes: the exact CLI flags, summary line, JSON keys, and note wording from Tasks 1-4. Write docs last so they describe shipped behaviour.

- [ ] **Step 1: `docs/design/curated-dashboard-packs.md`**

In Step 5's gate list, after item 4 (render audit), add the display and smoke gates and renumber the translation-modes item:

```markdown
# 5. Smoke against the real data (catches (null) series and unreadable axes)
grafana-validate-uploaded ...   # zero null_series, zero unreadable_axis panels

# 6. Display audit (what Kibana actually drew: ticks + legend names)
python -m observability_migration.targets.kibana.render_audit_driver \
  --chart-state --fail-on-display --fail-on-error ...

# 7. Both translation modes
obs-migrate --translation-mode native ...
obs-migrate --translation-mode esql ...
```

Extend the "All PERFECT panels…" paragraph with:

```markdown
Display findings are part of the same bar: on PERFECT and APPROXIMATE panels the `--chart-state` audit must report zero `null_legend`, `unreadable_axis`, and `unit_mismatch`, and smoke must report zero `null_series` / `unreadable_axis`. The migrate summary's `Display units:` line must show no `unmapped` / `unscaled` / `changed` for the pack's panels unless the panel's `fidelity_manifest.yaml` notes explain the difference.
```

Add gate-table rows:

```markdown
| `render_audit --chart-state --fail-on-display` | both | Zero `null_legend` / `unreadable_axis` / `unit_mismatch` on PERFECT and APPROXIMATE panels |
| `grafana-validate-uploaded` (smoke) | both | Zero `null_series` and `unreadable_axis` panels |
| migrate summary | both | No `unmapped` / `unscaled` / `changed` display unit unexplained by the fidelity manifest |
```

- [ ] **Step 2: `docs/architecture/asset-model.md`**

After the Target Binding section, add:

```markdown
### Display Unit (orthogonal to status)

`PanelResult.display_unit` records whether the panel's Grafana unit survived
into the emitted format: `{"source_unit", "expected_format", "emitted_formats",
"status"}` with status `none` | `kept` | `unmapped` | `unscaled` | `changed`.
Computed in `targets/kibana/emit/display.py::display_unit_record` from the
source panel and the final emitted panel. Report-only: it never changes panel
status (unlike target binding, whose smoke rollups move the gate). It is
serialized as `panels[].display_unit` in `migration_report.json`, summarized by
the migrate summary's `Display units:` line, and read back by the render
audit's `--chart-state` unit check.
```

- [ ] **Step 3: `docs/command-contract.md` and `docs/sources/grafana.md`**

In `command-contract.md`, after the Target Binding section, add a short
`### Display units` section:

```markdown
### Display units

Migration also records whether each panel's Grafana unit survived into the
emitted Kibana format. The summary prints, under `Verification gate`:

```text
Display units:       11 kept / 1 unmapped / 0 unscaled / 1 changed
```

- `kept` — the emitted format draws the Grafana unit (percent and number+`%`
  count as one family).
- `unmapped` — the Grafana unit has no Kibana format mapping; the panel shows
  plain numbers.
- `unscaled` — a scaled Grafana unit (`mbytes`, `Mbits`, …) drawn as unscaled
  bytes/bits with the value left in the Grafana unit, so 512 MB reads "512 B".
- `changed` — a different format family was emitted (for example a curated
  override that draws a count where Grafana said bytes).

`migration_report.json` records this per panel as `panels[].display_unit`, and
any panel not `kept` carries a `Display unit:` note. This is report-only: it
does not change a panel's Green/Yellow/Red status. What Kibana actually drew —
axis ticks and legend names — is checked after upload by the render audit's
`--chart-state` display findings.
```

In `docs/sources/grafana.md`, beside the target-binding paragraph (~line 928), add one sentence pointing at it:

```markdown
Whether each panel's Grafana unit survived into the emitted format is reported
separately (the summary's `Display units:` line and `panels[].display_unit`);
see [Display units](../command-contract.md#display-units).
```

- [ ] **Step 4: `docs/testing.md` and `docs/contributing/dev-commands.md`**

In `docs/testing.md`'s Render audit section, add a bullet after the `--elements` bullet:

```markdown
- **`--chart-state` reads what was drawn, not what was queried.** With
  `window._echDebugStateFlag` set, Elastic Charts publishes its rendered tick
  labels and legend item names (`data-ech-debug-state`); the audit flags
  `null_legend` (a legend entry that is `(null)` or empty), `unreadable_axis`
  (2+ y-axis ticks that all read the same, e.g. "0 ms" on Kibana 9.6+), and
  `unit_mismatch` (no y-axis tick carries the panel's Grafana unit, from the
  report's `display_unit`). These are the drawn counterparts of smoke's
  data-inferred `null_series` / `unreadable_axis`. Findings warn by default;
  `--fail-on-display` (with `--fail-on-error`) makes them fail, which is the
  curated-pack gate on a seeded rig. Needs the `browser` extra.
```

In `docs/contributing/dev-commands.md`, extend the `render_audit_driver` gate row (line 60) with the display clause:

```markdown
With `--chart-state` (browser extra) it also reports display findings — `(null)` legends, axes whose ticks all read the same, ticks missing the Grafana unit — as warns, or fails under `--fail-on-display`.
```

And add the example next to the existing render-audit examples (~line 227):

```bash
# Display audit: drawn ticks + legend names vs the Grafana units in the report.
.venv/bin/python -m observability_migration.targets.kibana.render_audit_driver \
  --kibana-url "$KIBANA_URL" --dashboard-id "<id>" --migration-out <dir>/dashboards \
  --chart-state --fail-on-display --fail-on-error
```

- [ ] **Step 5: Check the docs build rules**

Run: `make lint` (includes the source-header check) and grep the new docs for stale flag names:
`rg -n "fail-on-display|chart-state|Display units" docs/` — Expected: matches in exactly the six files above.

---

### Task 6: Full gates and live acceptance on the 763 rig

**Files:**
- No code changes expected. If the live run finds a defect, fix it under Tasks 1-4 with a failing test first (TDD), do not patch around it here.

- [ ] **Step 1: Offline gates**

```bash
.venv/bin/python -m pytest tests/test_display_unit.py tests/test_render_audit_display.py tests/test_render_audit_driver.py tests/test_render_audit.py tests/core/reporting/test_print_report.py -q
make test
make lint
make typecheck
```

Expected: all PASS. Watch the denominators (`make test` total must not drop).

- [ ] **Step 2: Bring up a 9.6 stack**

```bash
STACK_VERSION=9.6.0-SNAPSHOT docker compose -f parity-rig/docker-compose.render-audit.yml up -d --wait
```

(If that tag is not pullable, use the newest local 9.6 tag and record the exact version in the issue comment.) Confirm: `curl -s localhost:5601/api/status | head -c 200` shows 9.6.

- [ ] **Step 3: Offline-unit acceptance (the #480 case)**

Migrate Grafana 763 (`parity-rig/curated/grafana_763_redis_exporter/grafana_provisioning/dashboards/redis_763.json`) and upload **without** `--es-url`, then run the display audit:

```bash
.venv/bin/python -m observability_migration.targets.kibana.render_audit_driver \
  --kibana-url http://localhost:5601 --dashboard-id <uploaded-id> \
  --migration-out <out>/dashboards --es-url http://localhost:9200 \
  --chart-state --fail-on-display --fail-on-error
```

Expected: the two "Time Spent by Command" panels report `unreadable_axis` ("0 ms" ticks); exit 1. (On 9.6+, Kibana stores `duration` with `decimals: 0` — elastic/kibana#301576.)

- [ ] **Step 4: Fitted-upload acceptance**

Re-upload with `--es-url` so live fitting switches the sub-millisecond series to seconds, re-run the same audit.
Expected: no display findings; exit 0. Also confirm the migrate output prints a `Display units:` line and `migration_report.json` panels carry `display_unit`.

- [ ] **Step 5: `(null)`-legend acceptance (the #481 case)**

Migrate 763 without `--es-url` against the rig's remote-write-shaped target (or reuse the Step 3 upload while its `instance` binding is guessed), run the audit.
Expected: "Total Memory Usage" reports `null_legend` for `(null) - used` / `(null) - max`. After migrating with `--es-url`, the same panel is clean.

- [ ] **Step 6: Close the loop on the issue**

Post a comment on #483 summarizing: the three checks, where each runs (migrate summary + `display_unit`, smoke — already shipped, `--chart-state` render audit), the curated-pack done-when change, and the live 9.6 evidence (uploaded-without-`--es-url` flagged, fitted upload clean). Note the non-goal: scaled-unit rescaling (`mbytes` drawn as bytes) is only *reported* (`unscaled`) and needs its own issue. Then close #483 if every done-when item is met. Do not commit unless the user asks.

---

## Self-Review

**Spec coverage:**
- Migrate-time unit record + statuses + precedence + percent equivalence → Task 1 (`display_unit_record`) and Task 2 (field, note, summary, serialization). ✔
- Render-audit capture (`--chart-state`, debug flag, render-complete wait, per-panel titles, injectable) → Task 4 Step 3a/3b. ✔
- Classifier (`null_legend`, `unreadable_axis`, `unit_mismatch`, families, dual-axis pass, no-axis legend-only, skips recorded) → Task 3. ✔
- Output `display` section + warn-by-default + `--fail-on-display` → Task 3 (`audit_chart_state`) and Task 4 (output/exit). ✔
- Curated-pack gate (Step 5 + gate table) → Task 5 Step 1. ✔
- Docs list (curated packs, asset-model, grafana source, testing, dev-commands) → Task 5 Steps 1-4, plus `command-contract.md` (required by `CLAUDE.md` for operator-visible summary changes). ✔
- Testing list (record cases, report serialization/note/summary, 763 fixtures, dual axis, gauge, driver with/without `--chart-state`) → Tasks 1-4 test steps. ✔
- Live acceptance on 763 / 9.6 (without `--es-url` flagged, with `--es-url` clean, `(null)` legend) → Task 6 Steps 2-5. ✔
- Non-goals (no scaled-unit fix, no nightly CI enablement, no axis-title check) → Global Constraints and Task 6 Step 6 (file separately). ✔

**Placeholder scan:** no TBD/TODO; every code step carries the code; live-stack tag has an explicit fallback (record the version used).

**Type consistency:** `display_unit_record` returns the four keys Task 2 serializes and Task 3 reads (`source_unit`); `audit_chart_state`/`DisplayVerdict`/`display_unit_by_panel` names match between Tasks 3 and 4; driver flags `--chart-state`/`--fail-on-display` and the `chart_state_fetcher` parameter are spelled identically in tests, implementation, and docs.

**Known deliberate deviation:** the spec says "Set where panel display metadata is applied" for `display_unit`; the plan sets it in `_enrich_panel_result`, the one function that sees both the source panel and the final emitted panel on every return path (including curated overrides, which return after `enrich_yaml_panel_display`). This is stricter compliance with the spec's own "after curated overrides" requirement.
