"""Elastic Serverless Kibana API helpers.

Serverless Kibana restricts the saved-objects surface to two endpoints:
  POST /api/saved_objects/_export
  POST /api/saved_objects/_import

All other saved-objects operations (GET, DELETE, _find, _bulk_delete)
return 400/404 and are intentionally blocked.

Data-view management has full CRUD:
  GET    /api/data_views                          — list
  POST   /api/data_views/data_view                — create
  GET    /api/data_views/data_view/{id}            — get
  POST   /api/data_views/data_view/{id}            — update
  DELETE /api/data_views/data_view/{id}            — delete
  POST   /api/data_views/data_view/{id}/runtime_field — create runtime field
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import requests

from observability_migration.targets.kibana.compile import (
    kibana_url_for_space,
)

logger = logging.getLogger(__name__)


def _api_base(kibana_url: str, space_id: str = "") -> str:
    base = kibana_url_for_space(kibana_url, space_id).rstrip("/")
    if space_id and not base.endswith(f"/s/{space_id}"):
        base = f"{base}/s/{space_id}"
    return base


def _session(api_key: str = "") -> requests.Session:
    session = requests.Session()
    session.headers.update({"kbn-xsrf": "true"})
    if api_key:
        session.headers["Authorization"] = f"ApiKey {api_key}"
    return session


# ---------------------------------------------------------------------------
# Dashboard operations (saved-objects level)
# ---------------------------------------------------------------------------

def list_dashboards(
    kibana_url: str,
    *,
    api_key: str = "",
    space_id: str = "",
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """Return all dashboards via _export (Serverless-safe)."""
    session = _session(api_key)
    base = _api_base(kibana_url, space_id)
    response = session.post(
        f"{base}/api/saved_objects/_export",
        json={"type": ["dashboard"], "excludeExportDetails": True},
        timeout=timeout,
    )
    response.raise_for_status()
    dashboards: list[dict[str, Any]] = []
    for raw_line in response.text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("type") == "dashboard":
            dashboards.append(item)
    return sorted(
        dashboards,
        key=lambda d: (d.get("attributes", {}).get("title") or ""),
    )


def import_saved_objects(
    kibana_url: str,
    ndjson_path_or_bytes: str | bytes,
    *,
    api_key: str = "",
    space_id: str = "",
    overwrite: bool = True,
    timeout: int = 60,
) -> dict[str, Any]:
    """Import an NDJSON file via POST /api/saved_objects/_import."""
    session = _session(api_key)
    base = _api_base(kibana_url, space_id)
    if isinstance(ndjson_path_or_bytes, (str,)):
        with open(ndjson_path_or_bytes, "rb") as fh:
            file_bytes = fh.read()
    else:
        file_bytes = ndjson_path_or_bytes

    response = session.post(
        f"{base}/api/saved_objects/_import",
        params={"overwrite": "true"} if overwrite else {},
        files={"file": ("import.ndjson", io.BytesIO(file_bytes), "application/ndjson")},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def delete_dashboards(
    kibana_url: str,
    dashboard_ids: list[str],
    *,
    api_key: str = "",
    space_id: str = "",
    timeout: int = 30,
) -> dict[str, Any]:
    """Best-effort dashboard deletion for Serverless.

    Serverless Kibana blocks DELETE /api/saved_objects/dashboard/{id}.
    Workaround: re-import each dashboard with an empty panelsJSON and
    a title prefixed with "[DELETED]", effectively clearing the content.
    The dashboard object remains but is harmless.

    Returns a summary with counts of cleared / failed IDs.
    """
    cleared: list[str] = []
    failed: list[dict[str, str]] = []
    for dashboard_id in dashboard_ids:
        stub = json.dumps({
            "type": "dashboard",
            "id": dashboard_id,
            "attributes": {
                "title": f"[DELETED] {dashboard_id}",
                "panelsJSON": "[]",
                "optionsJSON": "{}",
                "kibanaSavedObjectMeta": {"searchSourceJSON": "{}"},
            },
            "references": [],
        })
        try:
            result = import_saved_objects(
                kibana_url,
                stub.encode("utf-8"),
                api_key=api_key,
                space_id=space_id,
                overwrite=True,
                timeout=timeout,
            )
            if result.get("success"):
                cleared.append(dashboard_id)
            else:
                errors = result.get("errors", [])
                failed.append({
                    "id": dashboard_id,
                    "error": json.dumps(errors[:1]) if errors else "unknown",
                })
        except Exception as exc:
            failed.append({"id": dashboard_id, "error": str(exc)})
    return {
        "cleared": cleared,
        "failed": failed,
        "note": (
            "Serverless Kibana does not support DELETE for saved objects. "
            "Cleared dashboards have been overwritten with empty content. "
            "To fully remove them, use the Kibana UI."
        ),
    }


# ---------------------------------------------------------------------------
# Data-view operations (full CRUD available in Serverless)
# ---------------------------------------------------------------------------

def list_data_views(
    kibana_url: str,
    *,
    api_key: str = "",
    space_id: str = "",
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """GET /api/data_views — returns all data views."""
    session = _session(api_key)
    base = _api_base(kibana_url, space_id)
    response = session.get(f"{base}/api/data_views", timeout=timeout)
    response.raise_for_status()
    return response.json().get("data_view", [])


def get_data_view(
    kibana_url: str,
    view_id: str,
    *,
    api_key: str = "",
    space_id: str = "",
    timeout: int = 30,
) -> dict[str, Any]:
    """GET /api/data_views/data_view/{viewId}."""
    session = _session(api_key)
    base = _api_base(kibana_url, space_id)
    response = session.get(
        f"{base}/api/data_views/data_view/{view_id}",
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json().get("data_view", {})


def create_data_view(
    kibana_url: str,
    *,
    title: str,
    name: str = "",
    view_id: str = "",
    time_field: str = "@timestamp",
    api_key: str = "",
    space_id: str = "",
    override: bool = True,
    timeout: int = 30,
) -> dict[str, Any]:
    """POST /api/data_views/data_view — create (or override) a data view.

    When ``view_id`` is supplied the data view is created with that exact
    Kibana saved-object ID.  This is important because ``kb-dashboard-cli``
    compiles NDJSON that references the data view by ID, and by default that
    ID equals the index pattern title.
    """
    session = _session(api_key)
    base = _api_base(kibana_url, space_id)
    dv: dict[str, Any] = {
        "title": title,
        "timeFieldName": time_field,
    }
    if view_id:
        dv["id"] = view_id
    if name:
        dv["name"] = name
    body: dict[str, Any] = {"data_view": dv, "override": override}
    response = session.post(
        f"{base}/api/data_views/data_view",
        json=body,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json().get("data_view", {})


def delete_data_view(
    kibana_url: str,
    view_id: str,
    *,
    api_key: str = "",
    space_id: str = "",
    timeout: int = 30,
) -> bool:
    """DELETE /api/data_views/data_view/{viewId}."""
    session = _session(api_key)
    base = _api_base(kibana_url, space_id)
    response = session.delete(
        f"{base}/api/data_views/data_view/{view_id}",
        timeout=timeout,
    )
    return response.status_code == 204


def ensure_data_view(
    kibana_url: str,
    *,
    title: str,
    name: str = "",
    time_field: str = "@timestamp",
    api_key: str = "",
    space_id: str = "",
    timeout: int = 30,
) -> dict[str, Any]:
    """Create a data view if one with the same title doesn't exist.

    Returns the existing or newly created data view.
    """
    existing = list_data_views(
        kibana_url, api_key=api_key, space_id=space_id, timeout=timeout,
    )
    for dv in existing:
        if dv.get("title") == title:
            logger.info("Data view '%s' already exists (id=%s)", title, dv.get("id"))
            return dv
    logger.info("Creating data view '%s'", title)
    return create_data_view(
        kibana_url,
        title=title,
        name=name or title,
        view_id=title,
        time_field=time_field,
        api_key=api_key,
        space_id=space_id,
        timeout=timeout,
    )


def set_default_data_view(
    kibana_url: str,
    view_id: str,
    *,
    api_key: str = "",
    space_id: str = "",
    timeout: int = 30,
) -> bool:
    """POST /api/data_views/default — set the default data view."""
    session = _session(api_key)
    base = _api_base(kibana_url, space_id)
    response = session.post(
        f"{base}/api/data_views/default",
        json={"data_view_id": view_id, "force": True},
        timeout=timeout,
    )
    return response.status_code == 200


# ---------------------------------------------------------------------------
# Convenience: ensure data views needed by migration output
# ---------------------------------------------------------------------------

def ensure_migration_data_views(
    kibana_url: str,
    *,
    data_view_patterns: list[str] | None = None,
    api_key: str = "",
    space_id: str = "",
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """Ensure all data views needed by migration output exist.

    Common patterns: metrics-prometheus-*, metrics-*, logs-*, datadog-migrate-*.
    """
    patterns = data_view_patterns or [
        "metrics-prometheus-*",
        "metrics-*",
        "logs-*",
    ]
    created: list[dict[str, Any]] = []
    for pattern in patterns:
        dv = ensure_data_view(
            kibana_url,
            title=pattern,
            api_key=api_key,
            space_id=space_id,
            timeout=timeout,
        )
        created.append(dv)
    return created


# ---------------------------------------------------------------------------
# API capability detection
# ---------------------------------------------------------------------------

def detect_serverless(
    kibana_url: str,
    *,
    api_key: str = "",
    space_id: str = "",
    timeout: int = 10,
) -> bool:
    """Detect whether Kibana is running in Serverless mode.

    Tries GET /api/saved_objects/_find?type=dashboard&per_page=1 — if it
    returns 400/404 with the "not available" message, it's Serverless.
    """
    session = _session(api_key)
    base = _api_base(kibana_url, space_id)
    try:
        response = session.get(
            f"{base}/api/saved_objects/_find",
            params={"type": "dashboard", "per_page": 1},
            timeout=timeout,
        )
        if response.status_code in {400, 404}:
            try:
                msg = response.json().get("message", "")
            except ValueError:
                msg = response.text
            if "not available" in str(msg).lower():
                return True
        return False
    except Exception:
        return False


__all__ = [
    "create_data_view",
    "delete_dashboards",
    "delete_data_view",
    "detect_serverless",
    "ensure_data_view",
    "ensure_migration_data_views",
    "get_data_view",
    "import_saved_objects",
    "list_dashboards",
    "list_data_views",
    "set_default_data_view",
]
