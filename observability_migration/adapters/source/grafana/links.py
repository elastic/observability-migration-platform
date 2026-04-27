# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Translate Grafana dashboard and panel links to Kibana equivalents.

Grafana supports two types of links:

- **Dashboard links** (``dashboard.links[]``): top-level navigation to other
  dashboards, optionally forwarding template variables and time range.
- **Panel links** (``panel.links[]``): per-panel drilldowns to dashboards or
  external URLs with variable substitution.

Kibana does not have a direct equivalent of Grafana's link system.  This module
translates them into the closest available representations:

- Dashboard links → metadata preserved in the YAML and manifest for manual
  wiring (Kibana uses navigation links in the dashboard description or markdown).
- Panel links → URL drilldowns where possible, or preserved as panel notes.
"""

from __future__ import annotations

import re
from typing import Any

_GRAFANA_VAR_RE = re.compile(
    r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?::[^}]+)?\}"
    r"|\$(?P<plain>[A-Za-z_][A-Za-z0-9_]*)"
    r"|\[\[(?P<bracket>[A-Za-z_][A-Za-z0-9_]*)(?::[^\]]+)?\]\]"
)


def _rewrite_grafana_variables_to_kibana(url: str) -> str:
    """Best-effort rewrite of ``$variable`` / ``${variable}`` in URLs.

    Kibana drilldowns use ``{{kibanaContext.savedObjectId}}`` and similar,
    but there is no 1:1 mapping.  We preserve the variable reference as a
    placeholder comment so the reviewer knows what to wire.
    """
    def _replace(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("plain") or match.group("bracket") or ""
        return f"{{{{context.{name}}}}}" if name else match.group(0)

    return _GRAFANA_VAR_RE.sub(_replace, url)


def translate_dashboard_links(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract and translate dashboard-level links."""
    raw_links = dashboard.get("links") or []
    translated: list[dict[str, Any]] = []
    for link in raw_links:
        link_type = str(link.get("type", "") or "").lower()
        title = str(link.get("title", "") or "")
        url = str(link.get("url", "") or "")
        target_blank = bool(link.get("targetBlank"))
        include_vars = bool(link.get("includeVars"))
        keep_time = bool(link.get("keepTime"))
        tags = list(link.get("tags", []) or [])

        entry: dict[str, Any] = {
            "type": link_type,
            "title": title,
            "original_url": url,
        }

        if link_type == "dashboards":
            entry["description"] = (
                f"Grafana dashboard list link"
                f"{' (tags: ' + ', '.join(tags) + ')' if tags else ''}"
                f"{' — forwards variables' if include_vars else ''}"
                f"{' — preserves time range' if keep_time else ''}"
            )
            entry["kibana_action"] = "manual_navigation"
        elif link_type == "link":
            rewritten = _rewrite_grafana_variables_to_kibana(url) if url else ""
            entry["translated_url"] = rewritten
            entry["target_blank"] = target_blank
            entry["kibana_action"] = "url_drilldown"
        else:
            entry["kibana_action"] = "unsupported"

        translated.append(entry)

    return translated


def translate_panel_links(panel: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract and translate panel-level links and drilldowns."""
    raw_links = panel.get("links") or []
    translated: list[dict[str, Any]] = []
    for link in raw_links:
        link_type = str(link.get("type", "") or "").lower()
        title = str(link.get("title", "") or "")
        url = str(link.get("url", "") or "")
        target_blank = bool(link.get("targetBlank"))
        dashboard_uid = str(link.get("dashUri", "") or link.get("dashboard", "") or "")
        include_vars = bool(link.get("includeVars"))
        keep_time = bool(link.get("keepTime"))

        entry: dict[str, Any] = {
            "type": link_type,
            "title": title,
            "original_url": url,
        }

        if link_type == "dashboard" and dashboard_uid:
            entry["target_dashboard"] = dashboard_uid
            entry["include_vars"] = include_vars
            entry["keep_time"] = keep_time
            entry["kibana_action"] = "dashboard_drilldown"
            entry["description"] = (
                f"Panel drilldown to dashboard '{dashboard_uid}'"
                f"{' — forwards variables' if include_vars else ''}"
                f"{' — preserves time range' if keep_time else ''}"
            )
        elif link_type == "absolute" or url:
            rewritten = _rewrite_grafana_variables_to_kibana(url) if url else ""
            entry["translated_url"] = rewritten
            entry["target_blank"] = target_blank
            entry["kibana_action"] = "url_drilldown"
        else:
            entry["kibana_action"] = "unsupported"

        translated.append(entry)

    return translated


def build_links_summary(
    dashboard_links: list[dict[str, Any]],
    panel_links_map: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Build a summary of all links for the manifest."""
    total_dashboard = len(dashboard_links)
    total_panel = sum(len(v) for v in panel_links_map.values())
    url_drilldowns = sum(
        1 for link in dashboard_links if link.get("kibana_action") == "url_drilldown"
    ) + sum(
        1 for links in panel_links_map.values()
        for link in links if link.get("kibana_action") == "url_drilldown"
    )
    dashboard_drilldowns = sum(
        1 for links in panel_links_map.values()
        for link in links if link.get("kibana_action") == "dashboard_drilldown"
    )
    manual = total_dashboard + total_panel - url_drilldowns - dashboard_drilldowns

    return {
        "dashboard_links": total_dashboard,
        "panel_links": total_panel,
        "url_drilldowns": url_drilldowns,
        "dashboard_drilldowns": dashboard_drilldowns,
        "manual_wiring_needed": manual,
    }


__all__ = [
    "build_links_summary",
    "translate_dashboard_links",
    "translate_panel_links",
]
