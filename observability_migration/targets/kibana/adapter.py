# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Registered Kibana target adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from observability_migration.adapters.source.grafana import smoke as grafana_smoke
from observability_migration.core.interfaces.registries import target_registry
from observability_migration.core.interfaces.target_adapter import TargetAdapter

from . import dashboards_api
from .compile import (
    compile_all,
    compile_yaml,
    detect_space_id_from_kibana_url,
    kibana_url_for_space,
    lint_dashboard_yaml,
    upload_yaml,
    validate_compiled_layout,
)
from .serverless import (
    delete_dashboards as serverless_delete_dashboards,
)
from .serverless import (
    detect_serverless,
    ensure_migration_data_views,
)
from .serverless import (
    list_dashboards as serverless_list_dashboards,
)
from .smoke import run_smoke_report


def _resolve_yaml_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix in {".yaml", ".yml"} else []
    yaml_files = sorted(path.glob("*.yaml"))
    if yaml_files:
        return yaml_files
    nested = path / "yaml"
    if nested.is_dir():
        nested_files = sorted(nested.glob("*.yaml"))
        if nested_files:
            return nested_files
    parent_nested = sorted(path.parent.glob("yaml/*.yaml"))
    return parent_nested


def _resolve_native_artifact_files(path: Path) -> list[Path]:
    """Discover ``*.native.json`` review artifacts, mirroring ``_resolve_yaml_files``.

    Accepts the same three shapes as YAML discovery: a ``native/`` directory
    directly, a dashboard artifact root that holds a ``native/``
    subdirectory (e.g. ``migration_output/dashboards``), or a sibling
    directory whose parent holds ``native/`` (e.g. pointing at
    ``migration_output/dashboards/yaml`` or ``.../compiled`` still finds
    ``migration_output/dashboards/native``).
    """
    if path.is_file():
        return [path] if path.name.endswith(".native.json") else []
    direct = sorted(path.glob("*.native.json"))
    if direct:
        return direct
    nested = path / "native"
    if nested.is_dir():
        nested_files = sorted(nested.glob("*.native.json"))
        if nested_files:
            return nested_files
    parent_nested = sorted(path.parent.glob("native/*.native.json"))
    return parent_nested


def _native_artifact_stem(path: Path) -> str:
    name = path.name
    suffix = ".native.json"
    return name[: -len(suffix)] if name.endswith(suffix) else path.stem


def _iter_leaf_panels(panels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    leaf_panels: list[dict[str, Any]] = []
    for panel in panels:
        section = panel.get("section")
        if isinstance(section, dict):
            leaf_panels.extend(_iter_leaf_panels(section.get("panels") or []))
        else:
            leaf_panels.append(panel)
    return leaf_panels


def _data_view_id_lookup(data_views: list[dict[str, Any]]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for data_view in data_views:
        title = str(data_view.get("title") or "")
        view_id = str(data_view.get("id") or "")
        if title and view_id and title != view_id:
            lookup[title] = view_id
    return lookup


def _rewrite_data_view_refs(value: Any, data_view_ids: dict[str, str]) -> Any:
    if isinstance(value, dict):
        rewritten: dict[str, Any] = {}
        for key, child in value.items():
            if key == "data_view" and isinstance(child, str):
                rewritten[key] = data_view_ids.get(child, child)
            else:
                rewritten[key] = _rewrite_data_view_refs(child, data_view_ids)
        return rewritten
    if isinstance(value, list):
        return [_rewrite_data_view_refs(item, data_view_ids) for item in value]
    return value


@target_registry.register
class KibanaTargetAdapter(TargetAdapter):
    name = "kibana"

    def _ensure_default_data_views(
        self,
        kibana_url: str,
        *,
        api_key: str = "",
        space_id: str = "",
        verify: bool | str = True,
    ) -> list[dict[str, Any]]:
        """Create the default migration data views before importing dashboards."""
        return ensure_migration_data_views(
            kibana_url,
            data_view_patterns=None,
            api_key=api_key,
            space_id=space_id,
            verify=verify,
        )

    def _prepare_upload_yaml(
        self,
        yaml_path: Path,
        output_dir: Path,
        data_views: list[dict[str, Any]],
    ) -> Path:
        data_view_ids = _data_view_id_lookup(data_views)
        if not data_view_ids:
            return yaml_path
        doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        rewritten = _rewrite_data_view_refs(doc, data_view_ids)
        if rewritten == doc:
            return yaml_path
        upload_input_dir = output_dir / "_upload_input"
        upload_input_dir.mkdir(parents=True, exist_ok=True)
        upload_path = upload_input_dir / yaml_path.name
        upload_path.write_text(
            yaml.safe_dump(rewritten, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return upload_path

    def emit_dashboard(self, dashboard_ir: Any, output_dir: Path, **kwargs: Any) -> Path:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = kwargs.get("filename") or kwargs.get("name") or "dashboard.yaml"
        output_path = output_dir / str(filename)
        if isinstance(dashboard_ir, str):
            output_path.write_text(dashboard_ir, encoding="utf-8")
        else:
            output_path.write_text(
                yaml.safe_dump(dashboard_ir, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
        return output_path

    def compile(self, yaml_dir: Path, output_dir: Path, **kwargs: Any) -> dict[str, Any]:
        yaml_dir = Path(yaml_dir)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        yaml_lint_ok, yaml_lint_output = lint_dashboard_yaml(str(yaml_dir))
        compile_results = compile_all(str(yaml_dir), str(output_dir))
        compiled_ok = sum(1 for _, ok, _ in compile_results if ok)
        layout_ok = None
        layout_output = ""
        if compiled_ok:
            layout_ok, layout_output = validate_compiled_layout(str(output_dir))
        return {
            "yaml_lint": {"ok": yaml_lint_ok, "output": yaml_lint_output},
            "compile_results": [
                {"name": name, "success": success, "output": output}
                for name, success, output in compile_results
            ],
            "summary": {
                "compiled_ok": compiled_ok,
                "total": len(compile_results),
            },
            "layout": {"ok": layout_ok, "output": layout_output},
        }

    def compile_dashboard(self, yaml_path: str | Path, output_dir: str | Path) -> tuple[bool, str]:
        return compile_yaml(str(yaml_path), str(output_dir))

    def validate_queries(self, run_dir: Path, **kwargs: Any) -> dict[str, Any]:
        run_dir = Path(run_dir)
        es_url = str(kwargs.get("es_url", "") or "")
        timeout = int(kwargs.get("timeout", 30) or 30)
        es_api_key = str(kwargs.get("es_api_key", "") or "")
        verify = kwargs.get("verify", True)
        if not es_url:
            return {
                "summary": {"queries": 0, "pass": 0, "fail": 0, "empty": 0, "skipped": 1},
                "records": [],
            }
        records: list[dict[str, Any]] = []
        pass_count = 0
        fail_count = 0
        empty_count = 0
        for yaml_file in _resolve_yaml_files(run_dir):
            payload = yaml.safe_load(yaml_file.read_text(encoding="utf-8")) or {}
            for dashboard in payload.get("dashboards") or []:
                for panel in _iter_leaf_panels(dashboard.get("panels") or []):
                    esql = panel.get("esql")
                    if not isinstance(esql, dict):
                        continue
                    query = str(esql.get("query", "") or "").strip()
                    if not query:
                        continue
                    validation = grafana_smoke.validate_esql(
                        es_url,
                        query,
                        timeout=timeout,
                        es_api_key=es_api_key,
                        verify=verify,
                    )
                    status = "empty" if validation["status"] == "pass" and validation["rows"] == 0 else validation["status"]
                    if status == "pass":
                        pass_count += 1
                    elif status == "fail":
                        fail_count += 1
                    else:
                        empty_count += 1
                    records.append(
                        {
                            "yaml_file": yaml_file.name,
                            "dashboard": dashboard.get("title", ""),
                            "panel": panel.get("title", ""),
                            "query": query,
                            "status": status,
                            "rows": validation.get("rows", 0),
                            "columns": validation.get("columns", []),
                            "error": validation.get("error", ""),
                            "materialized_query": validation.get("materialized_query", ""),
                        }
                    )
        return {
            "summary": {
                "queries": len(records),
                "pass": pass_count,
                "fail": fail_count,
                "empty": empty_count,
                "skipped": 0,
            },
            "records": records,
        }

    def _legacy_upload_file(
        self,
        yaml_file: Path,
        out_dir: Path,
        data_views: list[dict[str, Any]],
        *,
        kibana_url: str,
        space_id: str,
        kibana_api_key: str,
        verify: bool | str,
    ) -> tuple[bool, str]:
        """Compile + ``_import`` one YAML file via the legacy kb-dashboard-cli path."""
        out_dir.mkdir(parents=True, exist_ok=True)
        upload_yaml_path = self._prepare_upload_yaml(yaml_file, out_dir, data_views)
        return upload_yaml(
            str(upload_yaml_path),
            str(out_dir),
            kibana_url,
            space_id=space_id,
            kibana_api_key=kibana_api_key,
            verify=verify,
        )

    def _native_upload_file(
        self,
        yaml_file: Path,
        out_dir: Path,
        data_views: list[dict[str, Any]],
        *,
        kibana_url: str,
        space_id: str,
        kibana_api_key: str,
        verify: bool | str,
        upload_kibana_url: str,
        target_space: str,
        native_dashboard: Any = None,
        native_dashboard_stats: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Deploy one dashboard via the typed Dashboards API with legacy fallback.

        Rejected (and empty) dashboards degrade gracefully to the legacy
        compile + ``_import`` path so nothing silently vanishes.
        """
        out_dir.mkdir(parents=True, exist_ok=True)
        fallback_state: dict[str, Any] = {"used": False, "count": 0, "success": True, "output": []}

        def _fallback(_path: str, dashboard: dict[str, Any] | None = None) -> tuple[bool, str]:
            fallback_state["used"] = True
            fallback_state["count"] = int(fallback_state["count"]) + 1
            fallback_yaml = yaml_file
            if isinstance(dashboard, dict):
                fallback_input_dir = out_dir / "_fallback_input"
                fallback_input_dir.mkdir(parents=True, exist_ok=True)
                fallback_yaml = fallback_input_dir / f"dashboard_{fallback_state['count']}.yaml"
                fallback_yaml.write_text(
                    yaml.safe_dump({"dashboards": [dashboard]}, sort_keys=False, allow_unicode=True),
                    encoding="utf-8",
                )
            ok, out = self._legacy_upload_file(
                fallback_yaml,
                out_dir,
                data_views,
                kibana_url=kibana_url,
                space_id=space_id,
                kibana_api_key=kibana_api_key,
                verify=verify,
            )
            fallback_state["success"] = bool(fallback_state["success"]) and ok
            fallback_state["output"].append(out)
            return ok, out

        data_view_ids = _data_view_id_lookup(data_views)
        if native_dashboard is not None:
            results = [
                dashboards_api.upload_native_dashboard(
                    native_dashboard,
                    kibana_url,
                    api_key=kibana_api_key,
                    space_id=space_id,
                    verify=verify,
                    native_stats=native_dashboard_stats,
                    data_view_ids=data_view_ids,
                )
            ]
            # Only a genuine payload rejection degrades to the legacy compiler
            # path. A "conflict" (409) from the native PUT is NOT retried here:
            # it is reported as a terminal failure so the operator can decide
            # whether to use --legacy-import (which calls _import?overwrite=true
            # and can overwrite same-space [DELETED] placeholders) or to
            # investigate a cross-space id collision manually.
            if results[0].status == "rejected":
                # Do NOT silently fall back to the kb-dashboard-cli/YAML path.
                # That path is deprecated, and quietly succeeding through it
                # hides the fact that the typed API rejected the payload -- the
                # operator sees "uploaded" and never learns the modern path
                # failed, nor that the dashboard they got came from a different
                # compiler. Treated as terminal, matching how a 409 conflict is
                # already handled above: report it and let the operator opt into
                # --legacy-import deliberately.
                print(
                    f"    ✗ Dashboards API rejected the payload for {Path(yaml_file).name}; "
                    "not falling back to the deprecated compiler. "
                    "Re-run with --legacy-import to use it explicitly."
                )
        else:
            results = dashboards_api.upload_yaml_files(
                [str(yaml_file)],
                kibana_url,
                api_key=kibana_api_key,
                space_id=space_id,
                verify=verify,
                fallback=_fallback,
                data_view_ids=data_view_ids,
            )
        # Defensive compatibility: the current helper calls fallback per empty
        # dashboard with a dashboard payload, but older/mocked helpers may only
        # report the empty status. Route such files through legacy rather than
        # silently dropping them.
        if not fallback_state["used"] and any(r.status == "empty" for r in results):
            _fallback(str(yaml_file))
        mapped = sum(r.mapped for r in results)
        unmapped = sum(r.unmapped for r in results)
        unmapped_reasons: dict[str, int] = {}
        for r in results:
            for reason, count in (r.unmapped_reasons or {}).items():
                unmapped_reasons[reason] = unmapped_reasons.get(reason, 0) + int(count)
        if len(results) == 1:
            status = results[0].status
        elif results:
            statuses = {r.status for r in results}
            status = (
                "rejected" if "rejected" in statuses
                else "conflict" if "conflict" in statuses
                else "empty" if "empty" in statuses
                else "created" if "created" in statuses
                else "updated"
            )
        else:
            status = "empty"
        dashboard_ids = [r.dashboard_id for r in results if r.dashboard_id]

        if fallback_state["used"]:
            success = bool(fallback_state["success"])
            output = "; ".join(str(item) for item in fallback_state["output"])
        else:
            success = bool(results) and all(r.status in {"created", "updated"} for r in results)
            output = "; ".join(
                f"{r.dashboard or '(untitled)'}: {r.status}" for r in results
            ) or "no dashboards mapped"

        return {
            "yaml_file": yaml_file.name,
            "success": success,
            "output": output,
            "space_id": space_id or target_space,
            "kibana_url": upload_kibana_url,
            "status": status,
            "mapped": mapped,
            "unmapped": unmapped,
            "unmapped_reasons": unmapped_reasons,
            "fallback_used": bool(fallback_state["used"]),
            "fallback_count": int(fallback_state["count"]),
            "dashboard_ids": dashboard_ids,
        }

    def _native_artifact_upload_file(
        self,
        artifact_path: Path,
        *,
        kibana_url: str,
        space_id: str,
        kibana_api_key: str,
        verify: bool | str,
        upload_kibana_url: str,
        target_space: str,
        data_view_ids: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Deploy one persisted native review artifact file, no legacy fallback.

        A native artifact is a reviewed, already-built typed API payload (see
        ``targets/kibana/native_artifacts.py``). There is no on-disk YAML to
        re-derive here, so a rejection is reported as-is instead of silently
        degrading to a different representation -- pass
        ``--artifact-format yaml`` explicitly if that fallback is wanted.
        """
        try:
            artifact = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {
                "yaml_file": artifact_path.name,
                "success": False,
                "output": f"failed to read native artifact: {exc}",
                "space_id": space_id or target_space,
                "kibana_url": upload_kibana_url,
                "status": "rejected",
                "mapped": 0,
                "unmapped": 0,
                "unmapped_reasons": {},
                "fallback_used": False,
                "fallback_count": 0,
                "dashboard_ids": [],
            }
        result = dashboards_api.upload_native_artifact(
            artifact,
            kibana_url,
            api_key=kibana_api_key,
            space_id=space_id,
            verify=verify,
            data_view_ids=data_view_ids,
        )
        return {
            "yaml_file": artifact_path.name,
            "success": result.status in {"created", "updated"},
            "output": f"{result.dashboard or '(untitled)'}: {result.status}"
            if not result.message
            else result.message,
            "space_id": space_id or target_space,
            "kibana_url": upload_kibana_url,
            "status": result.status,
            "mapped": result.mapped,
            "unmapped": result.unmapped,
            "unmapped_reasons": dict(result.unmapped_reasons or {}),
            "fallback_used": False,
            "fallback_count": 0,
            "dashboard_ids": [result.dashboard_id] if result.dashboard_id else [],
        }

    def upload(self, compiled_dir: Path, **kwargs: Any) -> dict[str, Any]:
        compiled_dir = Path(compiled_dir)
        kibana_url = str(kwargs.get("kibana_url", "") or "")
        space_id = str(kwargs.get("space_id", "") or "")
        kibana_api_key = str(kwargs.get("kibana_api_key", "") or "")
        verify = kwargs.get("verify", True)
        use_dashboards_api = bool(kwargs.get("use_dashboards_api", True))
        target_space = detect_space_id_from_kibana_url(kibana_url) or "default"
        upload_kibana_url = kibana_url_for_space(kibana_url, space_id)

        # Legacy import can only compile+import YAML, so it forces yaml
        # regardless of the requested --artifact-format.
        requested_format = str(kwargs.get("artifact_format", "") or "auto") if use_dashboards_api else "yaml"
        native_files: list[Path] = []
        yaml_files: list[Path] = []
        if requested_format in {"native", "auto"}:
            native_files = _resolve_native_artifact_files(compiled_dir)
        if requested_format == "native" and not native_files:
            return {
                "summary": {
                    "uploaded_ok": 0,
                    "total": 0,
                    "space_id": space_id or target_space,
                    "kibana_url": upload_kibana_url,
                    "error": "no_native_artifacts_found",
                },
                "records": [],
            }

        if requested_format == "auto" and native_files and compiled_dir.name != "native":
            yaml_files = _resolve_yaml_files(compiled_dir)
            if yaml_files:
                native_stems = {_native_artifact_stem(path) for path in native_files}
                yaml_stems = {path.stem for path in yaml_files}
                if native_stems != yaml_stems:
                    return {
                        "summary": {
                            "uploaded_ok": 0,
                            "total": 0,
                            "space_id": space_id or target_space,
                            "kibana_url": upload_kibana_url,
                            "error": "mixed_native_yaml_artifacts",
                            "native_count": len(native_files),
                            "yaml_count": len(yaml_files),
                            "missing_native_artifacts": sorted(yaml_stems - native_stems),
                            "extra_native_artifacts": sorted(native_stems - yaml_stems),
                        },
                        "records": [],
                    }

        if native_files:
            data_views = self._ensure_default_data_views(
                kibana_url,
                api_key=kibana_api_key,
                space_id=space_id,
                verify=verify,
            )
            data_view_ids = _data_view_id_lookup(data_views)
            records = [
                self._native_artifact_upload_file(
                    artifact_file,
                    kibana_url=kibana_url,
                    space_id=space_id,
                    kibana_api_key=kibana_api_key,
                    verify=verify,
                    upload_kibana_url=upload_kibana_url,
                    target_space=target_space,
                    data_view_ids=data_view_ids,
                )
                for artifact_file in native_files
            ]
            summary = {
                "uploaded_ok": sum(1 for item in records if item["success"]),
                "total": len(records),
                "space_id": space_id or target_space,
                "kibana_url": upload_kibana_url,
                "artifact_format": "native",
            }
            return {"summary": summary, "records": records}

        records: list[dict[str, Any]] = []
        if not yaml_files:
            yaml_files = _resolve_yaml_files(compiled_dir)
        data_views = []
        if yaml_files:
            data_views = self._ensure_default_data_views(
                kibana_url,
                api_key=kibana_api_key,
                space_id=space_id,
                verify=verify,
            )
        for yaml_file in yaml_files:
            out_dir = compiled_dir / yaml_file.stem
            if use_dashboards_api:
                records.append(
                    self._native_upload_file(
                        yaml_file,
                        out_dir,
                        data_views,
                        kibana_url=kibana_url,
                        space_id=space_id,
                        kibana_api_key=kibana_api_key,
                        verify=verify,
                        upload_kibana_url=upload_kibana_url,
                        target_space=target_space,
                    )
                )
                continue
            success, output = self._legacy_upload_file(
                yaml_file,
                out_dir,
                data_views,
                kibana_url=kibana_url,
                space_id=space_id,
                kibana_api_key=kibana_api_key,
                verify=verify,
            )
            records.append(
                {
                    "yaml_file": yaml_file.name,
                    "success": success,
                    "output": output,
                    "space_id": space_id or target_space,
                    "kibana_url": upload_kibana_url,
                }
            )
        summary = {
            "uploaded_ok": sum(1 for item in records if item["success"]),
            "total": len(records),
            "space_id": space_id or target_space,
            "kibana_url": upload_kibana_url,
        }
        if use_dashboards_api:
            summary["fallbacks"] = sum(int(item.get("fallback_count", 0)) for item in records)
        return {
            "summary": summary,
            "records": records,
        }

    def upload_dashboard(
        self,
        yaml_path: str | Path,
        output_dir: str | Path,
        *,
        kibana_url: str,
        space_id: str = "",
        kibana_api_key: str = "",
        verify: bool | str = True,
        use_dashboards_api: bool = True,
        native_dashboard: Any = None,
        native_dashboard_stats: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data_views = self._ensure_default_data_views(
            kibana_url,
            api_key=kibana_api_key,
            space_id=space_id,
            verify=verify,
        )
        target_space = detect_space_id_from_kibana_url(kibana_url) or "default"
        upload_kibana_url = kibana_url_for_space(kibana_url, space_id)
        if use_dashboards_api:
            record = self._native_upload_file(
                Path(yaml_path),
                Path(output_dir),
                data_views,
                kibana_url=kibana_url,
                space_id=space_id,
                kibana_api_key=kibana_api_key,
                verify=verify,
                upload_kibana_url=upload_kibana_url,
                target_space=target_space,
                native_dashboard=native_dashboard,
                native_dashboard_stats=native_dashboard_stats,
            )
            return {
                "success": record["success"],
                "output": record["output"],
                "space_id": record["space_id"],
                "kibana_url": record["kibana_url"],
                "status": record["status"],
                "mapped": record["mapped"],
                "unmapped": record["unmapped"],
                "unmapped_reasons": record.get("unmapped_reasons", {}),
                "fallback_used": record["fallback_used"],
                "dashboard_ids": record["dashboard_ids"],
            }
        upload_yaml_path = self._prepare_upload_yaml(
            Path(yaml_path),
            Path(output_dir),
            data_views,
        )
        success, output = upload_yaml(
            str(upload_yaml_path),
            str(output_dir),
            kibana_url,
            space_id=space_id,
            kibana_api_key=kibana_api_key,
            verify=verify,
        )
        return {
            "success": success,
            "output": output,
            "space_id": space_id or target_space,
            "kibana_url": upload_kibana_url,
        }

    def smoke(self, **kwargs: Any) -> dict[str, Any]:
        return run_smoke_report(**kwargs)

    # ---- Serverless-aware helpers ----

    def is_serverless(
        self,
        kibana_url: str,
        *,
        api_key: str = "",
        space_id: str = "",
        verify: bool | str = True,
    ) -> bool:
        return detect_serverless(kibana_url, api_key=api_key, space_id=space_id, verify=verify)

    def list_dashboards(
        self,
        kibana_url: str,
        *,
        api_key: str = "",
        space_id: str = "",
        timeout: int = 30,
        verify: bool | str = True,
    ) -> list[dict[str, Any]]:
        """List all dashboards using the Serverless-safe _export API."""
        return serverless_list_dashboards(
            kibana_url, api_key=api_key, space_id=space_id, timeout=timeout, verify=verify,
        )

    def delete_dashboards(
        self,
        kibana_url: str,
        dashboard_ids: list[str],
        *,
        api_key: str = "",
        space_id: str = "",
        timeout: int = 30,
        verify: bool | str = True,
    ) -> dict[str, Any]:
        """Best-effort dashboard deletion (overwrite with empty content)."""
        return serverless_delete_dashboards(
            kibana_url,
            dashboard_ids,
            api_key=api_key,
            space_id=space_id,
            timeout=timeout,
            verify=verify,
        )

    def ensure_data_views(
        self,
        kibana_url: str,
        *,
        data_view_patterns: list[str] | None = None,
        api_key: str = "",
        space_id: str = "",
        timeout: int = 30,
        verify: bool | str = True,
    ) -> list[dict[str, Any]]:
        """Ensure all required data views exist in the Kibana cluster."""
        return ensure_migration_data_views(
            kibana_url,
            data_view_patterns=data_view_patterns,
            api_key=api_key,
            space_id=space_id,
            timeout=timeout,
            verify=verify,
        )


__all__ = ["KibanaTargetAdapter"]
