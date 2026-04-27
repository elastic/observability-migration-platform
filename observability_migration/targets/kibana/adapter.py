"""Registered Kibana target adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from observability_migration.adapters.source.grafana import smoke as grafana_smoke
from observability_migration.core.interfaces.registries import target_registry
from observability_migration.core.interfaces.target_adapter import TargetAdapter

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


def _iter_leaf_panels(panels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    leaf_panels: list[dict[str, Any]] = []
    for panel in panels:
        section = panel.get("section")
        if isinstance(section, dict):
            leaf_panels.extend(_iter_leaf_panels(section.get("panels") or []))
        else:
            leaf_panels.append(panel)
    return leaf_panels


@target_registry.register
class KibanaTargetAdapter(TargetAdapter):
    name = "kibana"

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

    def upload(self, compiled_dir: Path, **kwargs: Any) -> dict[str, Any]:
        compiled_dir = Path(compiled_dir)
        kibana_url = str(kwargs.get("kibana_url", "") or "")
        space_id = str(kwargs.get("space_id", "") or "")
        kibana_api_key = str(kwargs.get("kibana_api_key", "") or "")
        records: list[dict[str, Any]] = []
        target_space = detect_space_id_from_kibana_url(kibana_url) or "default"
        upload_kibana_url = kibana_url_for_space(kibana_url, space_id)
        for yaml_file in _resolve_yaml_files(compiled_dir):
            out_dir = compiled_dir / yaml_file.stem
            out_dir.mkdir(parents=True, exist_ok=True)
            success, output = upload_yaml(
                str(yaml_file),
                str(out_dir),
                kibana_url,
                space_id=space_id,
                kibana_api_key=kibana_api_key,
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
        return {
            "summary": {
                "uploaded_ok": sum(1 for item in records if item["success"]),
                "total": len(records),
                "space_id": space_id or target_space,
                "kibana_url": upload_kibana_url,
            },
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
    ) -> dict[str, Any]:
        success, output = upload_yaml(
            str(yaml_path),
            str(output_dir),
            kibana_url,
            space_id=space_id,
            kibana_api_key=kibana_api_key,
        )
        return {
            "success": success,
            "output": output,
            "space_id": space_id or detect_space_id_from_kibana_url(kibana_url) or "default",
            "kibana_url": kibana_url_for_space(kibana_url, space_id),
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
    ) -> bool:
        return detect_serverless(kibana_url, api_key=api_key, space_id=space_id)

    def list_dashboards(
        self,
        kibana_url: str,
        *,
        api_key: str = "",
        space_id: str = "",
        timeout: int = 30,
    ) -> list[dict[str, Any]]:
        """List all dashboards using the Serverless-safe _export API."""
        return serverless_list_dashboards(
            kibana_url, api_key=api_key, space_id=space_id, timeout=timeout,
        )

    def delete_dashboards(
        self,
        kibana_url: str,
        dashboard_ids: list[str],
        *,
        api_key: str = "",
        space_id: str = "",
        timeout: int = 30,
    ) -> dict[str, Any]:
        """Best-effort dashboard deletion (overwrite with empty content)."""
        return serverless_delete_dashboards(
            kibana_url,
            dashboard_ids,
            api_key=api_key,
            space_id=space_id,
            timeout=timeout,
        )

    def ensure_data_views(
        self,
        kibana_url: str,
        *,
        data_view_patterns: list[str] | None = None,
        api_key: str = "",
        space_id: str = "",
        timeout: int = 30,
    ) -> list[dict[str, Any]]:
        """Ensure all required data views exist in the Kibana cluster."""
        return ensure_migration_data_views(
            kibana_url,
            data_view_patterns=data_view_patterns,
            api_key=api_key,
            space_id=space_id,
            timeout=timeout,
        )


__all__ = ["KibanaTargetAdapter"]
