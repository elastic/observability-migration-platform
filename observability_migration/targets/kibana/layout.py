# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""In-process compiled-dashboard layout validation.

Validates compiled dashboard layout bounds and overlap invariants without
shelling out to a script, so the logic ships inside the installed wheel.
"""

from __future__ import annotations

import json
from pathlib import Path

GRID_WIDTH = 48
MAX_OVERLAPS_IN_ERROR = 10


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _panel_label(panel: dict, index: int) -> str:
    embeddable = panel.get("embeddableConfig", {})
    attributes = embeddable.get("attributes", {})
    title = attributes.get("title") or panel.get("panelIndex")
    if isinstance(title, str) and title.strip():
        return title
    return f"panel[{index}]"


def _iter_input_files(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    files = sorted(input_path.glob("**/*.json")) + sorted(input_path.glob("**/*.ndjson"))
    return [path for path in files if path.is_file()]


def _load_json_documents(path: Path) -> tuple[list[dict], list[str]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], [f"{path}: cannot read file: {exc}"]

    documents: list[dict] = []
    if path.suffix == ".ndjson":
        for line_no, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                return [], [f"{path}:{line_no}: invalid JSON: {exc}"]
            if isinstance(payload, dict):
                documents.append(payload)
        return documents, []

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], [f"{path}: invalid JSON: {exc}"]

    if isinstance(payload, dict):
        return [payload], []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)], []
    return [], [f"{path}: expected a JSON object or array of objects"]


def _load_dashboard_panels(path: Path) -> tuple[list[tuple[str, list[dict]]], list[str]]:
    documents, errors = _load_json_documents(path)
    if errors:
        return [], errors

    dashboards: list[tuple[str, list[dict]]] = []
    for index, document in enumerate(documents):
        attributes = document.get("attributes")
        if not isinstance(attributes, dict):
            continue
        panels_json = attributes.get("panelsJSON")
        if not isinstance(panels_json, str):
            continue
        try:
            panels = json.loads(panels_json)
        except json.JSONDecodeError as exc:
            errors.append(f"{path}: attributes.panelsJSON is not valid JSON: {exc}")
            continue
        if not isinstance(panels, list):
            errors.append(f"{path}: attributes.panelsJSON must decode to an array")
            continue
        title = attributes.get("title") or document.get("id") or f"{path.name}#{index}"
        dashboards.append((str(title), [panel for panel in panels if isinstance(panel, dict)]))
    return dashboards, errors


def _validate_panels(title: str, panels: list[dict]) -> list[str]:
    errors: list[str] = []

    sections: dict[str, list[tuple[int, dict]]] = {}
    for index, panel in enumerate(panels):
        grid = panel.get("gridData")
        if not isinstance(grid, dict):
            label = _panel_label(panel, index)
            errors.append(f"{title}: {label}: missing gridData object")
            continue
        section_id = str(grid.get("sectionId", "__root__") or "__root__")
        sections.setdefault(section_id, []).append((index, panel))

    for section_panels in sections.values():
        occupancy: dict[tuple[int, int], str] = {}
        overlaps: list[tuple[int, int, str, str]] = []

        for index, panel in section_panels:
            label = _panel_label(panel, index)
            grid = panel.get("gridData")
            assert isinstance(grid, dict)

            x = grid.get("x")
            y = grid.get("y")
            w = grid.get("w")
            h = grid.get("h")
            fields = {"x": x, "y": y, "w": w, "h": h}
            invalid = [name for name, value in fields.items() if not _is_int(value)]
            if invalid:
                errors.append(
                    f"{title}: {label}: gridData field(s) must be integers: {', '.join(invalid)}"
                )
                continue

            assert isinstance(x, int)
            assert isinstance(y, int)
            assert isinstance(w, int)
            assert isinstance(h, int)

            if x < 0 or y < 0:
                errors.append(f"{title}: {label}: gridData coordinates must be >= 0")
                continue
            if w <= 0 or h <= 0:
                errors.append(f"{title}: {label}: gridData width/height must be > 0")
                continue
            if x + w > GRID_WIDTH:
                errors.append(
                    f"{title}: {label}: panel exceeds {GRID_WIDTH}-column grid (x={x}, w={w}, x+w={x + w})"
                )
                continue

            for yy in range(y, y + h):
                for xx in range(x, x + w):
                    previous = occupancy.get((xx, yy))
                    if previous is not None and len(overlaps) < MAX_OVERLAPS_IN_ERROR:
                        overlaps.append((xx, yy, previous, label))
                    occupancy[(xx, yy)] = label

        for xx, yy, left, right in overlaps:
            errors.append(f"{title}: overlap at x={xx}, y={yy}: '{left}' overlaps '{right}'")
    return errors


def validate_compiled_layout(compiled_dir) -> tuple[bool, str]:
    """Validate the layout of compiled dashboards under ``compiled_dir``.

    Returns ``(ok, output)`` mirroring the previous script-backed helper.
    """
    input_path = Path(compiled_dir)
    lines: list[str] = []

    if not input_path.exists():
        return False, f"ERROR: Input path not found: {input_path}"

    files = _iter_input_files(input_path)
    if not files:
        return False, f"ERROR: No JSON or NDJSON files found in {input_path}"

    lines.append("Validating dashboard layout...")

    dashboards_checked = 0
    failed = 0
    for file_path in files:
        dashboards, load_errors = _load_dashboard_panels(file_path)
        if load_errors:
            failed += 1
            lines.append(f"--- {file_path} ---")
            for err in load_errors:
                lines.append(f"  FAIL: {err}")
            continue
        if not dashboards:
            continue
        for title, panels in dashboards:
            dashboards_checked += 1
            file_errors = _validate_panels(title, panels)
            lines.append(f"--- {file_path} :: {title} ---")
            if not file_errors:
                lines.append("  PASS")
                continue
            failed += 1
            for err in file_errors:
                lines.append(f"  FAIL: {err}")

    if dashboards_checked == 0:
        lines.append(
            f"ERROR: No dashboard saved objects with attributes.panelsJSON were found in {input_path}"
        )
        return False, "\n".join(lines)

    if failed:
        lines.append(f"\nERROR: Layout validation failed for {failed} dashboard artifact(s).")
        return False, "\n".join(lines)

    lines.append(f"\nLayout validation passed for {dashboards_checked} dashboard artifact(s).")
    return True, "\n".join(lines)


__all__ = ["GRID_WIDTH", "validate_compiled_layout"]
