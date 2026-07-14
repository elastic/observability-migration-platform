# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Helpers for the local dashboard interaction-audit orchestration script."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from observability_migration.core.coverage.interaction_canary import (
    INTERACTION_CANARY_TITLE,
    build_interaction_canary,
    write_interaction_canary_artifact,
)
from observability_migration.targets.kibana.dashboards_api import native_dashboard_from_ir
from observability_migration.targets.kibana.interaction_audit import (
    _IDENTIFIER_PARAM_TOKEN,
    _VALUE_PARAM_TOKEN,
)
from observability_migration.targets.kibana.interaction_runner import PanelContract
from observability_migration.targets.kibana.interaction_scenarios import (
    DashboardScenario,
    load_scenario,
)
from observability_migration.targets.kibana.lint import lint_dashboard_yaml
from observability_migration.targets.kibana.native_artifacts import (
    write_ir_artifact,
    write_native_artifact,
    write_native_artifact_index,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_MANIFEST_DIR = _REPO_ROOT / "parity-rig" / "interaction-scenarios"
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
_MIN_STACK_VERSION = (9, 5, 0)


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    manifest_path: Path
    source_kind: str
    source_path: Path = Path()

    def resolve(self, project_root: Path | None = None) -> ScenarioSpec:
        root = project_root or _REPO_ROOT
        return ScenarioSpec(
            scenario_id=self.scenario_id,
            manifest_path=(root / self.manifest_path).resolve(),
            source_kind=self.source_kind,
            source_path=(root / self.source_path).resolve() if self.source_path else Path(),
        )


SCENARIO_REGISTRY: dict[str, ScenarioSpec] = {
    "synthetic-controls": ScenarioSpec(
        scenario_id="synthetic-controls",
        manifest_path=Path("parity-rig/interaction-scenarios/synthetic-controls.yaml"),
        source_kind="synthetic",
    ),
    "redis-11835": ScenarioSpec(
        scenario_id="redis-11835",
        manifest_path=Path("parity-rig/interaction-scenarios/redis-11835.yaml"),
        source_kind="grafana",
        source_path=Path("infra/grafana/dashboards/redis-11835.json"),
    ),
    "k8s-views-global": ScenarioSpec(
        scenario_id="k8s-views-global",
        manifest_path=Path("parity-rig/interaction-scenarios/k8s-views-global.yaml"),
        source_kind="grafana",
        source_path=Path("infra/grafana/dashboards/k8s-views-global.json"),
    ),
}

DEFAULT_SCENARIO_ORDER: tuple[str, ...] = (
    "synthetic-controls",
    "redis-11835",
    "k8s-views-global",
)


def parse_stack_version(version: str) -> tuple[int, int, int] | None:
    match = _SEMVER_RE.match(str(version or "").strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def require_stack_version(version: str, minimum: tuple[int, int, int] = _MIN_STACK_VERSION) -> None:
    parsed = parse_stack_version(version)
    if parsed is None:
        return
    if parsed < minimum:
        raise ValueError(
            f"STACK_VERSION {version!r} is below required {'.'.join(str(part) for part in minimum)}"
        )


def parse_scenario_selection(raw: str) -> tuple[str, ...]:
    requested = [item.strip() for item in str(raw or "").split(",") if item.strip()]
    if not requested:
        raise ValueError("SCENARIOS must not be empty")
    unknown = [item for item in requested if item not in SCENARIO_REGISTRY]
    if unknown:
        joined = ", ".join(unknown)
        raise ValueError(f"unknown scenario(s): {joined}")
    return tuple(requested)


def resolve_scenario(spec: ScenarioSpec, project_root: Path | None = None) -> ScenarioSpec:
    resolved = spec.resolve(project_root)
    if not resolved.manifest_path.is_file():
        raise FileNotFoundError(f"missing scenario manifest: {resolved.manifest_path}")
    if resolved.source_kind == "grafana" and not resolved.source_path.is_file():
        raise FileNotFoundError(f"missing scenario source dashboard: {resolved.source_path}")
    return resolved


def _request_json(url: str, *, method: str = "GET", headers: Mapping[str, str] | None = None) -> Any:
    request = urllib.request.Request(url, method=method, headers=dict(headers or {}))
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def check_stack_available(es_url: str, kibana_url: str) -> None:
    for label, url in (("Elasticsearch", es_url), ("Kibana", kibana_url)):
        try:
            payload = _request_json(url.rstrip("/") + ("/api/status" if label == "Kibana" else ""))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{label} unavailable at {url}: {exc}") from exc
        if label == "Kibana":
            level = str(((payload.get("status") or {}).get("overall") or {}).get("level") or "")
            if level and level not in {"available", "green", "yellow"}:
                raise RuntimeError(f"Kibana status at {url} is {level!r}")


def find_dashboard_ids_by_title(
    kibana_url: str,
    title: str,
    *,
    api_key: str = "",
) -> list[str]:
    from urllib.parse import quote

    query = quote(f'title:"{title}"')
    url = f"{kibana_url.rstrip('/')}/api/saved_objects/_find?type=dashboard&search={query}&per_page=200"
    headers = {"kbn-xsrf": "true"}
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
    payload = _request_json(url, headers=headers)
    matches = [
        str(item.get("id") or "")
        for item in payload.get("saved_objects", [])
        if str((item.get("attributes") or {}).get("title") or "") == title
    ]
    return [item for item in matches if item]


def require_unique_dashboard_id(kibana_url: str, title: str, *, api_key: str = "") -> str:
    matches = find_dashboard_ids_by_title(kibana_url, title, api_key=api_key)
    if not matches:
        raise LookupError(f"no uploaded dashboard matched exact title {title!r}")
    if len(matches) > 1:
        joined = ", ".join(matches)
        raise LookupError(f"duplicate dashboards matched exact title {title!r}: {joined}")
    return matches[0]


def _dashboard_api_headers(api_key: str = "") -> dict[str, str]:
    headers = {"kbn-xsrf": "true"}
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
    return headers


def fetch_dashboard_payload(kibana_url: str, dashboard_id: str, *, api_key: str = "") -> dict[str, Any]:
    payload = _request_json(
        f"{kibana_url.rstrip('/')}/api/dashboards/{dashboard_id}",
        headers=_dashboard_api_headers(api_key),
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError(f"Kibana dashboard {dashboard_id!r} returned an unexpected response shape")
    return payload


def _runtime_panels(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return []
    panels = data.get("panels")
    if not isinstance(panels, list):
        return []
    flattened: list[Mapping[str, Any]] = []

    def visit(items: Sequence[Any]) -> None:
        for panel in items:
            if not isinstance(panel, Mapping):
                continue
            nested = panel.get("panels")
            if isinstance(nested, list):
                visit(nested)
            else:
                flattened.append(panel)

    visit(panels)
    return flattened


def runtime_panels_by_title_from_payload(payload: Mapping[str, Any]) -> dict[str, str]:
    runtime_by_title: dict[str, str] = {}
    for panel in _runtime_panels(payload):
        runtime_id = str(panel.get("id") or "").strip()
        config = panel.get("config") if isinstance(panel.get("config"), dict) else {}
        title = str(config.get("title") or "").strip()
        if runtime_id and title:
            if title in runtime_by_title:
                raise ValueError(f"duplicate runtime panel title {title!r}")
            runtime_by_title[title] = runtime_id
    return runtime_by_title


def fetch_runtime_panels_by_title(kibana_url: str, dashboard_id: str, *, api_key: str = "") -> dict[str, str]:
    return runtime_panels_by_title_from_payload(
        fetch_dashboard_payload(kibana_url, dashboard_id, api_key=api_key)
    )


def _queries_from_config(value: Any) -> tuple[str, ...]:
    queries: list[str] = []
    if isinstance(value, Mapping):
        data_source = value.get("data_source")
        if isinstance(data_source, Mapping):
            query = data_source.get("query")
            if isinstance(query, str) and query.strip():
                queries.append(query)
        for key, child in value.items():
            if key != "data_source":
                queries.extend(_queries_from_config(child))
    elif isinstance(value, list | tuple):
        for child in value:
            queries.extend(_queries_from_config(child))
    return tuple(dict.fromkeys(queries))


def runtime_query_panels_from_payload(payload: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    parsed: list[tuple[str, str, str]] = []
    for panel in _runtime_panels(payload):
        runtime_id = str(panel.get("id") or "").strip()
        config = panel.get("config") if isinstance(panel.get("config"), dict) else {}
        title = str(config.get("title") or runtime_id).strip()
        for query in _queries_from_config(config):
            parsed.append((runtime_id, title, query))
    return parsed


def fetch_runtime_query_panels(kibana_url: str, dashboard_id: str, *, api_key: str = "") -> list[tuple[str, str, str]]:
    return runtime_query_panels_from_payload(
        fetch_dashboard_payload(kibana_url, dashboard_id, api_key=api_key)
    )


def query_control_dependencies(query: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    value_names = tuple(dict.fromkeys(_VALUE_PARAM_TOKEN.findall(query)))
    identifier_names = tuple(dict.fromkeys(_IDENTIFIER_PARAM_TOKEN.findall(query)))
    return value_names, identifier_names


def derive_panel_contract(
    runtime_panels: Sequence[tuple[str, str, str]],
    *,
    control_keys: Sequence[str] = (),
    global_control_keys: Sequence[str] = (),
) -> PanelContract:
    """Build a runtime panel contract from live dashboard panels and query tokens."""
    all_query_panels: list[str] = []
    by_control: dict[str, set[str]] = {key: set() for key in control_keys}

    for runtime_id, _title, query in runtime_panels:
        all_query_panels.append(runtime_id)
        value_names, identifier_names = query_control_dependencies(query)
        for name in value_names:
            if name in by_control:
                by_control[name].add(runtime_id)
        for name in identifier_names:
            if name in by_control:
                by_control[name].add(runtime_id)

    all_query_panels = list(dict.fromkeys(all_query_panels))
    for key in global_control_keys:
        if key in by_control:
            by_control[key].update(all_query_panels)

    return PanelContract(
        all_query_panels=tuple(all_query_panels),
        by_control={key: tuple(sorted(ids)) for key, ids in by_control.items() if ids},
    )


def load_control_keys_from_migration_out(migration_out: Path) -> tuple[str, ...]:
    report_path = migration_out / "migration_report.json"
    if not report_path.is_file():
        return ()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    dashboards = report.get("dashboards") or []
    if not dashboards:
        return ()
    dashboard = dashboards[0]
    title = str(dashboard.get("title") or "")
    native_dir = migration_out / "native"
    for path in sorted(native_dir.glob("*.native.json")):
        artifact = json.loads(path.read_text(encoding="utf-8"))
        if title and str(artifact.get("title") or "") not in {title, ""}:
            continue
        payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else {}
        keys: list[str] = []
        for control in payload.get("pinned_panels") or []:
            if not isinstance(control, dict):
                continue
            config = control.get("config") if isinstance(control.get("config"), dict) else {}
            for candidate in (config.get("variable_name"), config.get("field_name")):
                cleaned = str(candidate or "").strip()
                if cleaned:
                    keys.append(cleaned)
        if keys:
            return tuple(dict.fromkeys(keys))
    return ()


def load_control_keys_from_ir(migration_out: Path) -> tuple[str, ...]:
    ir_dir = migration_out / "ir"
    for path in sorted(ir_dir.glob("*.ir.json")):
        artifact = json.loads(path.read_text(encoding="utf-8"))
        dashboard_ir = artifact.get("dashboard_ir") if isinstance(artifact.get("dashboard_ir"), dict) else {}
        keys = [
            str(control.get("control_id") or control.get("variable_name") or control.get("field_name") or "").strip()
            for control in dashboard_ir.get("controls") or []
            if isinstance(control, dict)
        ]
        cleaned = tuple(dict.fromkeys(key for key in keys if key))
        if cleaned:
            return cleaned
    return load_control_keys_from_migration_out(migration_out)


def write_panel_contract(path: Path, contract: PanelContract) -> None:
    payload = {
        "all_query_panels": list(contract.all_query_panels),
        "by_control": {key: list(panels) for key, panels in contract.by_control.items()},
        "panel_aliases": dict(contract.panel_aliases),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def build_dashboard_url(
    kibana_url: str,
    dashboard_id: str,
    *,
    time_from: str = "now-3h",
    time_to: str = "now",
) -> str:
    base = kibana_url.rstrip("/")
    return f"{base}/app/dashboards#/view/{dashboard_id}?_g=(time:(from:{time_from},to:{time_to}))"


def assert_native_mapping(
    migration_out: Path,
    *,
    expected_panels: int | None = None,
    expected_controls: int | None = None,
    dashboard_title: str = "",
) -> dict[str, Any]:
    report_path = migration_out / "migration_report.json"
    dashboard: dict[str, Any] = {}
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        dashboards = report.get("dashboards") or []
        if not dashboards:
            raise ValueError("migration report contains no dashboards")
        dashboard = next(
            (
                item
                for item in dashboards
                if not dashboard_title or str(item.get("title") or "") == dashboard_title
            ),
            dashboards[0],
        )
    title = str(dashboard.get("title") or dashboard_title)
    native_dir = migration_out / "native"
    artifact_path = None
    artifact: dict[str, Any] = {}
    for path in sorted(native_dir.glob("*.native.json")):
        candidate = json.loads(path.read_text(encoding="utf-8"))
        if title and str(candidate.get("title") or "") not in {title, ""}:
            continue
        artifact_path = path
        artifact = candidate
        break
    if artifact_path is None:
        raise FileNotFoundError(f"no native artifact found under {native_dir} for dashboard {title!r}")

    mapping = artifact.get("mapping") if isinstance(artifact.get("mapping"), dict) else {}
    mapped = int(mapping.get("mapped", 0) or 0)
    unmapped = int(mapping.get("unmapped", 0) or 0)
    controls = int(mapping.get("controls", 0) or 0)
    payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else {}
    payload_panels = _native_payload_panel_count(payload)
    payload_controls = len(payload.get("pinned_panels") or [])
    if unmapped:
        raise ValueError(f"native mapping for {title!r} left {unmapped} panel(s) unmapped")
    if mapped != payload_panels:
        raise ValueError(
            f"native mapping count for {title!r} is {mapped}, but payload contains {payload_panels} panels"
        )
    if controls != payload_controls:
        raise ValueError(
            f"native control count for {title!r} is {controls}, but payload contains {payload_controls} controls"
        )
    if expected_panels is not None and mapped != expected_panels:
        raise ValueError(
            f"expected {expected_panels} mapped panels for {title!r}, got mapped={mapped} unmapped={unmapped}"
        )
    if expected_controls is not None and controls != expected_controls:
        raise ValueError(f"expected {expected_controls} mapped controls for {title!r}, got {controls}")

    if dashboard:
        report_panels = [
            panel
            for panel in dashboard.get("panels") or []
            if isinstance(panel, dict)
            and str(panel.get("status") or "") not in {"skipped", "not_feasible"}
        ]
        if expected_panels is not None and len(report_panels) < expected_panels:
            raise ValueError(
                f"migration report lists {len(report_panels)} panel(s) for {title!r}, "
                f"expected at least {expected_panels}"
            )
    return {
        "title": title,
        "mapped": mapped,
        "unmapped": unmapped,
        "controls": controls,
        "native_artifact": str(artifact_path),
    }


def _native_payload_panel_count(payload: Mapping[str, Any]) -> int:
    panels = payload.get("panels")
    total = 0
    if isinstance(panels, list):
        for panel in panels:
            if not isinstance(panel, Mapping):
                continue
            nested = panel.get("panels")
            total += len(nested) if isinstance(nested, list) else 1
    return total


def lint_migration_yaml(migration_out: Path) -> None:
    yaml_dir = migration_out / "yaml"
    yaml_files = sorted(yaml_dir.glob("*.yaml"))
    if not yaml_files:
        raise FileNotFoundError(f"no dashboard YAML found under {yaml_dir}")
    failures: list[str] = []
    for yaml_file in yaml_files:
        ok, output = lint_dashboard_yaml(str(yaml_file))
        if not ok:
            failures.append(f"{yaml_file.name}:\n{output.strip()}")
    if failures:
        raise RuntimeError("dashboard YAML lint failed:\n" + "\n".join(failures))


def run_live_validate(migration_out: Path, es_url: str, *, api_key: str = "", project_root: Path | None = None) -> None:
    root = project_root or _REPO_ROOT
    env = {**os.environ, "PYTHONPATH": str(root / "parity-rig")}
    command = [
        sys.executable,
        "-m",
        "verifier.live_validate",
        "--migration-out",
        str(migration_out),
        "--es-url",
        es_url,
        "--api-key",
        api_key,
        "--fail-on-bug",
    ]
    completed = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        message = (completed.stdout + "\n" + completed.stderr).strip()
        raise RuntimeError(message or "live ES|QL validation failed")


def validate_final_artifact(migration_out: Path, es_url: str, *, api_key: str = "", project_root: Path | None = None) -> None:
    lint_migration_yaml(migration_out)
    run_live_validate(migration_out, es_url, api_key=api_key, project_root=project_root)


def scenario_from_manifest(path: Path) -> DashboardScenario:
    return load_scenario(path)


def map_stable_panel_ids(
    stable_panels: Sequence[tuple[str, str]],
    runtime_by_title: Mapping[str, str],
) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for stable_id, title in stable_panels:
        runtime_id = runtime_by_title.get(title)
        if runtime_id:
            mapped[stable_id] = runtime_id
    return mapped


def load_stable_panels_from_ir(
    migration_out: Path,
    *,
    dashboard_title: str = "",
) -> tuple[tuple[str, str], ...]:
    ir_dir = migration_out / "ir"
    for path in sorted(ir_dir.glob("*.ir.json")):
        artifact = json.loads(path.read_text(encoding="utf-8"))
        if dashboard_title and str(artifact.get("title") or "") != dashboard_title:
            continue
        dashboard_ir = artifact.get("dashboard_ir")
        if not isinstance(dashboard_ir, Mapping):
            continue
        stable_panels = [
            (
                str(panel.get("panel_id") or "").strip(),
                str(panel.get("title") or "").strip(),
            )
            for panel in dashboard_ir.get("panels") or []
            if isinstance(panel, Mapping)
        ]
        cleaned = tuple(
            (stable_id, title)
            for stable_id, title in stable_panels
            if stable_id and title
        )
        if cleaned:
            return cleaned
    raise FileNotFoundError(
        f"no IR panel identities found under {ir_dir} for dashboard {dashboard_title!r}"
    )


def prepare_synthetic_artifacts(artifact_dir: Path) -> dict[str, Any]:
    dashboard_ir = build_interaction_canary()
    write_interaction_canary_artifact(artifact_dir)
    native_dashboard, counts = native_dashboard_from_ir(dashboard_ir)
    native_path = write_native_artifact(
        dashboard_ir=dashboard_ir,
        native_dashboard=native_dashboard,
        native_stats={
            "mapped": counts.mapped,
            "unmapped": counts.unmapped,
            "sections": counts.sections,
            "controls": counts.controls,
            "reasons": counts.reasons,
        },
        native_dir=artifact_dir / "native",
        stem="interaction-canary",
    )
    ir_path = write_ir_artifact(
        dashboard_ir=dashboard_ir,
        ir_dir=artifact_dir / "ir",
        stem="interaction-canary",
    )
    write_native_artifact_index(
        artifact_dir / "native",
        [
            {
                "stem": "interaction-canary",
                "title": dashboard_ir.title,
                "dashboard_id": native_dashboard.dashboard_id,
                "native_path": str(native_path.relative_to(artifact_dir)),
                "ir_path": str(ir_path.relative_to(artifact_dir)),
            }
        ],
    )
    return assert_native_mapping(
        artifact_dir,
        expected_panels=8,
        expected_controls=7,
        dashboard_title=INTERACTION_CANARY_TITLE,
    )


def prepare_runtime_artifacts(
    *,
    manifest_path: Path,
    migration_out: Path,
    kibana_url: str,
    output_dir: Path,
    api_key: str = "",
) -> dict[str, Any]:
    scenario = scenario_from_manifest(manifest_path)
    dashboard_id = require_unique_dashboard_id(
        kibana_url,
        scenario.dashboard_title,
        api_key=api_key,
    )
    payload = fetch_dashboard_payload(kibana_url, dashboard_id, api_key=api_key)
    runtime_by_title = runtime_panels_by_title_from_payload(payload)
    stable_panels = load_stable_panels_from_ir(
        migration_out,
        dashboard_title=scenario.dashboard_title,
    )
    stable_to_runtime = map_stable_panel_ids(stable_panels, runtime_by_title)
    missing_stable = [
        stable_id
        for stable_id, _title in stable_panels
        if stable_id not in stable_to_runtime
    ]
    if missing_stable:
        raise ValueError(
            "live dashboard is missing mapped panel(s): " + ", ".join(missing_stable)
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    control_keys = tuple(control.key for control in scenario.controls)
    global_control_keys = tuple(
        control.key for control in scenario.controls if control.adapter == "query_bar"
    )
    runtime_query_panels = runtime_query_panels_from_payload(payload)
    derived_contract = derive_panel_contract(
        runtime_query_panels,
        control_keys=control_keys,
        global_control_keys=global_control_keys,
    )
    panel_contract = PanelContract(
        all_query_panels=derived_contract.all_query_panels,
        by_control=derived_contract.by_control,
        panel_aliases=stable_to_runtime,
    )
    panel_contract_path = output_dir / "panel-contract.json"
    write_panel_contract(panel_contract_path, panel_contract)

    native_counts = assert_native_mapping(
        migration_out,
        dashboard_title=scenario.dashboard_title,
    )
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    pinned_panels = data.get("pinned_panels") if isinstance(data, Mapping) else []
    runtime_panel_count = len(_runtime_panels(payload))
    runtime_control_count = len(pinned_panels) if isinstance(pinned_panels, list) else 0
    if runtime_panel_count != native_counts["mapped"]:
        raise ValueError(
            f"live dashboard contains {runtime_panel_count} panels, "
            f"native artifact mapped {native_counts['mapped']}"
        )
    if runtime_control_count != native_counts["controls"]:
        raise ValueError(
            f"live dashboard contains {runtime_control_count} controls, "
            f"native artifact mapped {native_counts['controls']}"
        )

    metadata = {
        "scenario_id": scenario.id,
        "dashboard_id": dashboard_id,
        "dashboard_title": scenario.dashboard_title,
        "dashboard_url": build_dashboard_url(
            kibana_url,
            dashboard_id,
            time_from=scenario.time_from,
            time_to=scenario.time_to,
        ),
        "manifest": str(manifest_path),
        "panel_contract": str(panel_contract_path),
        "panels": runtime_panel_count,
        "query_panels": len(panel_contract.all_query_panels),
        "controls": runtime_control_count,
        "native": native_counts,
    }
    metadata_path = output_dir / "runtime.json"
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check-environment")
    check.add_argument("--stack-version", required=True)
    check.add_argument("--scenarios", required=True)
    check.add_argument("--es-url", required=True)
    check.add_argument("--kibana-url", required=True)

    synthetic = subparsers.add_parser("prepare-synthetic")
    synthetic.add_argument("--artifact-dir", type=Path, required=True)

    validate = subparsers.add_parser("validate-final")
    validate.add_argument("--migration-out", type=Path, required=True)
    validate.add_argument("--es-url", required=True)
    validate.add_argument("--api-key", default="")

    runtime = subparsers.add_parser("prepare-runtime")
    runtime.add_argument("--manifest", type=Path, required=True)
    runtime.add_argument("--migration-out", type=Path, required=True)
    runtime.add_argument("--kibana-url", required=True)
    runtime.add_argument("--output-dir", type=Path, required=True)
    runtime.add_argument("--api-key", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "check-environment":
        require_stack_version(args.stack_version)
        selected = parse_scenario_selection(args.scenarios)
        for scenario_id in selected:
            resolve_scenario(SCENARIO_REGISTRY[scenario_id])
        check_stack_available(args.es_url, args.kibana_url)
        print("\n".join(selected))
        return 0
    if args.command == "prepare-synthetic":
        print(json.dumps(prepare_synthetic_artifacts(args.artifact_dir), indent=2))
        return 0
    if args.command == "validate-final":
        validate_final_artifact(
            args.migration_out,
            args.es_url,
            api_key=args.api_key,
        )
        print(json.dumps(assert_native_mapping(args.migration_out), indent=2))
        return 0
    if args.command == "prepare-runtime":
        metadata = prepare_runtime_artifacts(
            manifest_path=args.manifest,
            migration_out=args.migration_out,
            kibana_url=args.kibana_url,
            output_dir=args.output_dir,
            api_key=args.api_key,
        )
        print(json.dumps(metadata, indent=2))
        return 0
    return 2


__all__ = [
    "DEFAULT_SCENARIO_ORDER",
    "SCENARIO_REGISTRY",
    "ScenarioSpec",
    "assert_native_mapping",
    "build_dashboard_url",
    "check_stack_available",
    "derive_panel_contract",
    "fetch_dashboard_payload",
    "fetch_runtime_panels_by_title",
    "fetch_runtime_query_panels",
    "find_dashboard_ids_by_title",
    "lint_migration_yaml",
    "load_control_keys_from_ir",
    "load_control_keys_from_migration_out",
    "load_stable_panels_from_ir",
    "map_stable_panel_ids",
    "parse_scenario_selection",
    "parse_stack_version",
    "query_control_dependencies",
    "require_stack_version",
    "require_unique_dashboard_id",
    "resolve_scenario",
    "run_live_validate",
    "runtime_panels_by_title_from_payload",
    "runtime_query_panels_from_payload",
    "scenario_from_manifest",
    "validate_final_artifact",
    "write_panel_contract",
]


if __name__ == "__main__":
    raise SystemExit(main())
