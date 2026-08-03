# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Registered Kibana target adapter."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from observability_migration.core.interfaces.registries import target_registry
from observability_migration.core.interfaces.target_adapter import TargetAdapter

from . import dashboards_api
from .compile import (
    detect_space_id_from_kibana_url,
    kibana_url_for_space,
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


def _resolve_native_artifact_files(path: Path) -> list[Path]:
    """Discover the ``*.native.json`` review artifacts to upload.

    Accepts three shapes: a ``native/`` directory directly, a dashboard
    artifact root that holds a ``native/`` subdirectory (e.g.
    ``migration_output/dashboards``), or a sibling directory whose parent holds
    ``native/`` (so pointing at any child of the artifact root still resolves).
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


def _records_panels_dropped(records: list[dict[str, Any]]) -> int:
    """Total leaf panels Kibana silently dropped across a batch of uploads.

    A non-zero value means at least one dashboard was written incomplete behind
    an HTTP 200, so it belongs in the upload summary next to ``uploaded_ok``.
    """
    return sum(len(item.get("dropped_panels") or []) for item in records)


def _data_view_id_lookup(data_views: list[dict[str, Any]]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for data_view in data_views:
        title = str(data_view.get("title") or "")
        view_id = str(data_view.get("id") or "")
        if title and view_id and title != view_id:
            lookup[title] = view_id
    return lookup


def _referenced_data_view_patterns(native_dashboard: Any) -> list[str]:
    """Index patterns the payload references as a data view, in first-seen order.

    ``_ensure_default_data_views`` only ensures a fixed default list
    (metrics-prometheus-*, metrics-*, logs-*). A control pointing at anything
    else -- e.g. the Datadog prometheus_native profile's
    ``metrics-*.prometheus-*`` -- therefore had no data view to resolve against,
    so ``dashboards_api._resolve_pinned_panel_data_view_ids`` left the raw
    pattern in ``data_view_id`` and Kibana rendered the control as "An error
    occurred". Ensuring exactly what the payload asks for makes the lookup
    complete by construction.
    """
    if native_dashboard is None:
        return []
    try:
        payload = native_dashboard.to_api_payload()
    except Exception:
        return []
    found: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if key in {"data_view", "data_view_id"} and isinstance(child, str):
                    text = child.strip()
                    # A real saved-object id is already resolved; only patterns
                    # (which look like index expressions) need ensuring.
                    if text and text not in found:
                        found.append(text)
                else:
                    _walk(child)
        elif isinstance(node, list):
            for child in node:
                _walk(child)

    _walk(payload)
    return found


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
        extra_patterns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Create the migration data views before importing dashboards.

        ``extra_patterns`` carries the patterns the payload actually references,
        on top of the defaults, so every ``data_view``/``data_view_id`` in the
        payload has something to resolve to.
        """
        patterns = None
        if extra_patterns:
            patterns = list(
                dict.fromkeys(
                    ["metrics-prometheus-*", "metrics-*", "logs-*", *extra_patterns]
                )
            )
        return ensure_migration_data_views(
            kibana_url,
            data_view_patterns=patterns,
            api_key=api_key,
            space_id=space_id,
            verify=verify,
        )

    def _native_upload_file(
        self,
        data_views: list[dict[str, Any]],
        *,
        kibana_url: str,
        space_id: str,
        kibana_api_key: str,
        verify: bool | str,
        upload_kibana_url: str,
        target_space: str,
        native_dashboard: Any,
        native_dashboard_stats: dict[str, Any] | None = None,
        artifact_label: str = "",
    ) -> dict[str, Any]:
        """Deploy one in-memory ``NativeDashboard`` via the typed Dashboards API.

        Nothing is written to disk: the payload is sent as held in memory. A
        rejection is reported as-is so the operator sees that the typed API
        refused the payload instead of a green "uploaded" produced by a
        different renderer.
        """
        label = artifact_label or "(native payload)"
        if native_dashboard is None:
            raise ValueError("_native_upload_file needs a native_dashboard payload")

        data_view_ids = _data_view_id_lookup(data_views)
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
        # A "conflict" (409) is reported as a terminal failure so the operator
        # can investigate a cross-space id collision rather than having it
        # silently overwritten.
        if results[0].status == "rejected":
            print(f"    ✗ Dashboards API rejected the payload for {label}.")
        elif results[0].status == "lossy":
            # HTTP 200 with panels missing. Say so loudly: this is the
            # failure mode nobody investigates, because everything else
            # about the run looks green.
            print(
                f"    ✗ Kibana accepted {label} but silently dropped "
                f"{results[0].dropped_panel_count} of {results[0].panels_sent} "
                "panel(s); the uploaded dashboard is incomplete.",
                file=sys.stderr,
            )
            for dropped in results[0].dropped_panels:
                # Kibana's validation errors run to thousands of characters;
                # the console gets a readable head, the JSON report the rest.
                detail = f": {dropped.reason[:300]}" if dropped.reason else ""
                where = f" [section {dropped.section}]" if dropped.section else ""
                print(
                    f"        - {dropped.title or '(untitled)'}{where}{detail}",
                    file=sys.stderr,
                )
        # One payload in, one result out. The multi-result status ranking the
        # YAML batch path needed is gone with it.
        result = results[0]
        return {
            "artifact": label,
            "success": result.status in {"created", "updated"},
            "output": (
                f"{result.dashboard or '(untitled)'}: {result.status}"
                # A "lossy" status alone reads like a shrug; the message names
                # the panels the operator lost, so it travels with it into
                # ``upload_error`` and the migration report.
                + (f" — {result.message}" if result.status == "lossy" and result.message else "")
            ),
            "space_id": space_id or target_space,
            "kibana_url": upload_kibana_url,
            "status": result.status,
            "mapped": result.mapped,
            "unmapped": result.unmapped,
            "unmapped_reasons": dict(result.unmapped_reasons or {}),
            "dashboard_ids": [result.dashboard_id] if result.dashboard_id else [],
            "panels_sent": result.panels_sent,
            "panels_accepted": result.panels_accepted,
            "dropped_panels": [dropped.to_dict() for dropped in (result.dropped_panels or [])],
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
        """Deploy one persisted native review artifact file.

        A native artifact is a reviewed, already-built typed API payload (see
        ``targets/kibana/native_artifacts.py``). It is uploaded exactly as
        reviewed; a rejection is reported as-is, with no re-derivation through
        any other representation.
        """
        try:
            artifact = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {
                "artifact": artifact_path.name,
                "success": False,
                "output": f"failed to read native artifact: {exc}",
                "space_id": space_id or target_space,
                "kibana_url": upload_kibana_url,
                "status": "rejected",
                "mapped": 0,
                "unmapped": 0,
                "unmapped_reasons": {},
                "dashboard_ids": [],
                "panels_sent": 0,
                "panels_accepted": 0,
                "dropped_panels": [],
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
            "artifact": artifact_path.name,
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
            "dashboard_ids": [result.dashboard_id] if result.dashboard_id else [],
            "panels_sent": result.panels_sent,
            "panels_accepted": result.panels_accepted,
            "dropped_panels": [dropped.to_dict() for dropped in (result.dropped_panels or [])],
        }

    def upload(self, artifact_dir: Path, **kwargs: Any) -> dict[str, Any]:
        """Deploy the ``native/*.native.json`` artifacts under ``artifact_dir``.

        Native Dashboard-as-Code artifacts are the only upload input: they are
        what ``obs-migrate migrate`` writes and what a reviewer inspects, and
        they are sent to the typed Kibana Dashboards API byte-for-byte.
        """
        artifact_dir = Path(artifact_dir)
        kibana_url = str(kwargs.get("kibana_url", "") or "")
        space_id = str(kwargs.get("space_id", "") or "")
        kibana_api_key = str(kwargs.get("kibana_api_key", "") or "")
        verify = kwargs.get("verify", True)
        target_space = detect_space_id_from_kibana_url(kibana_url) or "default"
        upload_kibana_url = kibana_url_for_space(kibana_url, space_id)

        native_files = _resolve_native_artifact_files(artifact_dir)
        if not native_files:
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
            "panels_dropped": _records_panels_dropped(records),
        }
        return {"summary": summary, "records": records}

    def upload_dashboard(
        self,
        *,
        kibana_url: str,
        space_id: str = "",
        kibana_api_key: str = "",
        verify: bool | str = True,
        native_dashboard: Any,
        native_dashboard_stats: dict[str, Any] | None = None,
        artifact_label: str = "",
    ) -> dict[str, Any]:
        """Deploy one dashboard from the in-memory ``NativeDashboard`` payload.

        The migration pipeline holds the typed payload it just built and uploads
        that, passing ``artifact_label`` (the artifact stem) for reporting.
        """
        if native_dashboard is None:
            raise ValueError("upload_dashboard requires a native_dashboard payload")
        data_views = self._ensure_default_data_views(
            kibana_url,
            api_key=kibana_api_key,
            space_id=space_id,
            verify=verify,
            extra_patterns=_referenced_data_view_patterns(native_dashboard),
        )
        target_space = detect_space_id_from_kibana_url(kibana_url) or "default"
        upload_kibana_url = kibana_url_for_space(kibana_url, space_id)
        record = self._native_upload_file(
            data_views,
            kibana_url=kibana_url,
            space_id=space_id,
            kibana_api_key=kibana_api_key,
            verify=verify,
            upload_kibana_url=upload_kibana_url,
            target_space=target_space,
            native_dashboard=native_dashboard,
            native_dashboard_stats=native_dashboard_stats,
            artifact_label=artifact_label,
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
            "dashboard_ids": record["dashboard_ids"],
            "panels_sent": record.get("panels_sent", 0),
            "panels_accepted": record.get("panels_accepted", 0),
            "dropped_panels": record.get("dropped_panels", []),
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
