# Curated pack display checks: axis units, unreadable ticks, `(null)` legends

**Date:** 2026-11-10
**Status:** Approved design
**Issue:** [#483](https://github.com/elastic/observability-migration-platform/issues/483)

---

## Problem

The Grafana 763 curated pack passed its done-when checks (no `UNKNOWN` in
`fidelity_manifest.yaml`, no `render_error` in the render audit) while the
uploaded dashboard showed "0 ms" on every y-axis tick (#480) and legends that
read "(null) - used" / "(null) - max" (#481).

v1.4.0 added two smoke checks that infer these from data: `null_series` (a
breakdown column null in every row) and `unreadable_axis` (a duration column
whose live peak is below what its stored decimals can show). Neither is part of
the curated-pack gates, and neither observes what Kibana actually drew. Nothing
checks that a panel's axis unit matches its Grafana unit.

## Goals

1. Record, at migrate time and for every Grafana dashboard, whether each
   panel's Grafana unit survived into the emitted format.
2. Let the render audit observe each chart's drawn y-axis ticks and legend
   names, and flag `(null)` legends, all-identical ticks, and ticks that do not
   carry the Grafana unit.
3. Make those checks part of the curated-pack done-when.

## Non-goals

- Fixing scaled units. `kbytes`, `mbytes`, `gbytes`, `tbytes`, `KBs`, `MBs`,
  `GBs`, `Kbits`, `Mbits` and `Gbits` are emitted as plain `bytes` / `bits`
  without rescaling the value (a Grafana `mbytes` value of 512 renders as
  "512 B"). This design reports them; the fix is a separate issue.
- Enabling `--chart-state` in the nightly `render-audit-local` workflow.
- Checking axis titles against tick units (e.g. a "Seconds" title over ms
  ticks).

## Design

### 1. Migrate-time unit record

A pure helper beside `GRAFANA_UNIT_TO_YAML` in
`observability_migration/targets/kibana/emit/display.py`:

```python
def display_unit_record(grafana_panel: dict, yaml_panel: dict) -> dict:
    """{"source_unit", "expected_format", "emitted_formats", "status"}"""
```

- `source_unit`: `extract_grafana_unit(grafana_panel)`.
- `expected_format`: `grafana_unit_to_yaml_format(source_unit)`.
- `emitted_formats`: the distinct `format` dicts on the emitted panel's
  measures (`esql.metrics[*]`, `esql.primary`, `esql.metric`).
- `status`:

| Status | Meaning |
|---|---|
| `none` | No unit, or `none` / `short` (nothing to preserve). |
| `kept` | Every emitted measure format has the expected format's family (`type`, plus `suffix` for `number`). |
| `unmapped` | The unit is set but not in `GRAFANA_UNIT_TO_YAML`, so no unit format was emitted. |
| `unscaled` | The unit is a scaled byte/bit unit (`kbytes` ... `Gbits`) emitted as unscaled `bytes` / `bits`. |
| `changed` | An emitted measure format has a different family than the expected one (e.g. a curated override that draws a count instead of bytes). |

Precedence when several apply: `unmapped`, `unscaled`, `changed`, `kept`.

`{"type": "percent"}` and `{"type": "number", "suffix": "%"}` are the same
family, because metric tiles deliberately emit the latter for `percentunit`.

Wiring:

- New `PanelResult.display_unit: dict` (default `{}`), serialized into
  `migration_report.json`. Set where panel display metadata is applied, after
  curated overrides, so it describes the final emitted panel.
- Any status other than `none` / `kept` appends one note to
  `PanelResult.notes`, e.g. `Display unit: Grafana unit "mbytes" is drawn as
  unscaled bytes (512 MB reads "512 B").`
- The migrate summary prints one line when any panel has a unit:
  `Display units:  11 kept / 1 unmapped / 0 unscaled / 1 changed`.
- Panel status (Green/Yellow/Red) is not changed. Fidelity baselines do not
  move.

### 2. Render audit: observed chart state

#### Capture

`render_audit_driver --chart-state` (requires the `browser` extra, i.e.
Playwright, already used by the interaction audit):

1. Launch Chromium (reusing `--user-data-dir` when given), add the init script
   `window._echDebugStateFlag = true`, open the dashboard URL.
2. Wait until no panel shows a loading indicator and every `.echChartStatus`
   has `data-ech-render-complete="true"`, bounded by the existing virtual-time
   budget.
3. For each dashboard panel element, read its title
   (`[data-test-subj="embeddablePanelTitle"]`) and every
   `.echChartStatus[data-ech-debug-state]` inside it, parsed as JSON.

Returns `[{"title": str, "charts": [debug_state, ...]}]`. The capture is
injectable (like `dom_fetcher`), so tests and CI need no browser. Without
`--chart-state` the driver behaves exactly as today.

Elastic Charts' debug state (enabled by `window._echDebugStateFlag`, which
Lens XY, gauge, partition and heatmap pass as `debugState`) carries
`axes.y[*].labels` (rendered tick text), `axes.y[*].title`, and
`legend.items[*].name`.

#### Classification

A pure function in `observability_migration/targets/kibana/render_audit.py`:

```python
def check_chart_state(title: str, charts: list[dict], *, source_unit: str = "") -> list[DisplayFinding]
```

| Finding | Rule |
|---|---|
| `null_legend` | A legend item name is `(null)`, empty after trimming, or has a blank segment when split on `" - "` (e.g. `" - used"` — a real Kibana 9.6 null-breakdown legend splices the empty value into the static `" - <metric>"` suffix, not always as the literal substring `(null)`). |
| `unreadable_axis` | A y-axis has two or more tick labels and they are all identical. |
| `unit_mismatch` | `source_unit` maps to a recognizable family and no y-axis has a tick label carrying it. |

Unit families for `unit_mismatch`, matched against tick labels:

| Grafana unit family | A tick label carries it when it matches |
|---|---|
| duration (`s`, `dtdurations`, `dthms`, `ms`, `µs`, `ns`, `m`, `h`, `d`) | a time unit token: `ns`, `µs`, `us`, `ms`, `s`, `sec`, `second(s)`, `min`, `minute(s)`, `h`, `hr`, `hour(s)`, `d`, `day(s)` |
| bytes (`bytes`, `decbytes`, `kbytes` ...) | a byte unit token: `B`, `KB`, `MB`, `GB`, `TB`, `PB`, `kB`, `KiB` ... |
| bits (`bits`, `decbits`, `kbits` ...) | a bit unit token: `b`, `bit(s)`, `Kb`, `Mb`, `Gb`, `kbit` ... |
| percent (`percent`, `percentunit`) | `%` |
| literal suffix (`reqps` -> ` req/s`, `celsius` -> ` °C`, ...) | the suffix text, trimmed |

Units with no family (`short`, `none`, `locale`, `bool*`, unmapped) are not
checked. A panel passes `unit_mismatch` when any y-axis carries the unit, so a
dual-axis panel whose right axis has another unit is not flagged.

`source_unit` comes from the report's `display_unit.source_unit` for the panel
(section 1), matched by title. A report from before this change has no
`display_unit`; unit checks are then skipped and the skip is recorded.

Skipped, with the reason recorded in the finding detail:

- the panel title is hidden or did not match a report panel;
- the panel has no Elastic Charts element (tables, markdown);
- the chart has no y-axis (metric tiles, gauges, partitions) - only the legend
  check applies.

#### Output and severity

The driver output gains `"display"`:

```json
{
  "status": "pass|warn|fail",
  "panels": [
    {"title": "...", "findings": [{"kind": "unreadable_axis", "detail": "5 ticks all read \"0 ms\""}],
     "skipped": ""}
  ],
  "reasons": ["..."]
}
```

Display findings are `warn` by default: on an operator's target a `(null)`
legend can be a label-binding gap rather than a translator bug.
`--fail-on-display` makes any display finding a `fail` (exit non-zero with
`--fail-on-error`), which is what a seeded curated rig wants. The `render`
verdict is unchanged.

### 3. Curated-pack gate

`docs/design/curated-dashboard-packs.md` Step 5 and the gate table add:

| Gate | Pass criterion |
|---|---|
| `render_audit --chart-state --fail-on-display` | Zero `null_legend` / `unreadable_axis` / `unit_mismatch` on PERFECT and APPROXIMATE panels |
| `grafana-validate-uploaded` (smoke) | Zero `null_series` and `unreadable_axis` panels |
| migrate summary | No `unmapped` / `unscaled` / `changed` display unit unless the panel's fidelity-manifest notes explain it |

## Docs to update

- `docs/design/curated-dashboard-packs.md`: Step 5 and gate table.
- `docs/architecture/asset-model.md`: `PanelResult.display_unit`.
- `docs/sources/grafana.md`: the migrate summary "Display units" line and
  per-panel note.
- `docs/testing.md`: render-audit display findings.
- `docs/contributing/dev-commands.md`: `--chart-state` and
  `--fail-on-display`.

## Testing

Offline (`make test`):

- `display_unit_record`: `s` kept; `mbytes` unscaled; an unmapped unit; a
  curated override that changes bytes to a count (`changed`); no unit (`none`);
  `percentunit` emitted as number + `%` for metric tiles (`kept`).
- Report: `display_unit` is serialized; the note and summary line appear only
  for non-`kept` statuses.
- `check_chart_state` with debug-state fixtures modeled on 763:
  - "Average Time Spent by Command / sec": five ticks all "0 ms" ->
    `unreadable_axis`; ticks `0.00030 s ... 0.00005 s` -> clean.
  - "Total Memory Usage": legend "(null) - used" / "(null) - max" ->
    `null_legend`; real instance names -> clean.
  - `bytes` panel with ticks "0", "500", "1000" -> `unit_mismatch`; with
    "0 B", "500 B" -> clean.
  - Dual axis, only one carrying the unit -> clean.
  - Gauge (no y-axis) -> only the legend check runs.
- Driver: injected chart-state fetcher plus a report -> `display` section;
  `--fail-on-display` turns a finding into exit 1; without `--chart-state` the
  output has no `display` key.

Live acceptance (763 rig, Kibana 9.6):

- Upload without `--es-url`: the two Time Spent panels report
  `unreadable_axis`.
- Upload with `--es-url`: no display findings.
- Migrate without `--es-url` against the remote-write target:
  "Total Memory Usage" reports `null_legend`.
