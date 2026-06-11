# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Grafana dashboard extraction and text-panel normalization."""

from __future__ import annotations

import json
import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from observability_migration.core.http import apply_tls
from observability_migration.core.selection import (
    AssetSelectionMetadata,
    parse_selection_datetime,
)


def _safe_parse_dt(value: Any) -> Any:
    """Best-effort datetime parse; return None on anything unusable."""
    if value is None or value == "":
        return None
    try:
        return parse_selection_datetime(str(value))
    except (ValueError, TypeError, OverflowError):
        return None


def _datasource_token(datasource: Any) -> str:
    """Return a datasource selection token (type for dicts, the string otherwise)."""
    if isinstance(datasource, dict):
        return str(datasource.get("type", "") or "")
    if isinstance(datasource, str):
        return datasource
    return ""


def selection_metadata_from_grafana_dashboard(dashboard: dict[str, Any]) -> AssetSelectionMetadata:
    """Map a raw Grafana dashboard dict into the source-agnostic selection view.

    ``folder``/``updated_at``/``starred`` come from the ``_grafana_meta`` block
    (absent in bare file exports -> ``None`` -> degrade gracefully). ``team`` is
    always ``None`` (Grafana dashboards have no first-class team).
    """
    meta = dashboard.get("_grafana_meta")
    has_meta = isinstance(meta, dict)
    meta = meta if has_meta else {}

    datasources: list[str] = []
    seen: set[str] = set()

    def _add(token: str) -> None:
        if token and token not in seen:
            seen.add(token)
            datasources.append(token)

    panels: list[dict[str, Any]] = []
    for panel in dashboard.get("panels", []) or []:
        if isinstance(panel, dict):
            panels.append(panel)
            panels.extend(p for p in (panel.get("panels", []) or []) if isinstance(p, dict))
    for row in dashboard.get("rows", []) or []:
        if isinstance(row, dict):
            panels.extend(p for p in (row.get("panels", []) or []) if isinstance(p, dict))
    for panel in panels:
        _add(_datasource_token(panel.get("datasource")))
        for target in panel.get("targets", []) or []:
            if isinstance(target, dict):
                _add(_datasource_token(target.get("datasource")))

    folder = meta.get("folderTitle") if has_meta else None
    starred = meta.get("isStarred") if has_meta else None
    return AssetSelectionMetadata(
        folder=folder,
        tags=[str(t) for t in (dashboard.get("tags") or [])],
        datasources=datasources,
        team=None,
        updated_at=_safe_parse_dt(meta.get("updated")) if has_meta else None,
        starred=bool(starred) if starred is not None else None,
    )


def selection_metadata_from_grafana_alert_rule(
    rule: dict[str, Any],
    datasource_map: dict[str, dict[str, Any]] | None = None,
) -> AssetSelectionMetadata:
    """Map a Grafana Unified Alerting rule dict into the selection view.

    ``folder`` is ``None``: unified rules expose only a ``folderUID`` (not the
    folder name a user selects on), so folder selection degrades gracefully.
    Labels are rendered as ``key:value`` tags; ``team`` is read from a ``team``
    label.
    """
    datasource_map = datasource_map or {}
    labels = rule.get("labels") if isinstance(rule.get("labels"), dict) else {}
    tags = [f"{k}:{v}" for k, v in labels.items()]
    team = None
    for key, value in labels.items():
        if str(key).casefold() == "team":
            team = str(value)
            break

    datasources: list[str] = []
    seen: set[str] = set()
    for entry in rule.get("data", []) or []:
        if not isinstance(entry, dict):
            continue
        uid = str(entry.get("datasourceUid", "") or "")
        if not uid:
            continue
        token = str((datasource_map.get(uid) or {}).get("type", "") or "") or uid
        if token and token not in seen:
            seen.add(token)
            datasources.append(token)

    return AssetSelectionMetadata(
        folder=None,
        tags=tags,
        datasources=datasources,
        team=team,
        updated_at=_safe_parse_dt(rule.get("updated")),
        starred=None,
    )


def extract_dashboards_from_grafana(
    grafana_url: str,
    user: str,
    password: str,
    *,
    token: str = "",
    verify: bool | str = True,
) -> list[dict[str, Any]]:
    """Extract all dashboards from Grafana via HTTP API."""
    session = _grafana_session(grafana_url, user=user, password=password, token=token, verify=verify)
    search_resp = session.get(f"{grafana_url}/api/search?type=dash-db&limit=500")
    search_resp.raise_for_status()
    dashboard_list = search_resp.json()
    dashboards = []
    for entry in dashboard_list:
        uid = entry.get("uid", "")
        resp = session.get(f"{grafana_url}/api/dashboards/uid/{uid}")
        if resp.status_code == 200:
            data = resp.json()
            dashboard = data.get("dashboard", data)
            dashboard["_grafana_meta"] = data.get("meta", {})
            dashboards.append(dashboard)
    return dashboards


def extract_dashboards_from_files(directory: str) -> list[dict[str, Any]]:
    """Load dashboards from local JSON files."""
    dashboards = []
    for f in sorted(Path(directory).glob("*.json")):
        with open(f) as fh:
            try:
                d = json.load(fh)
                if isinstance(d, dict) and isinstance(d.get("dashboard"), dict):
                    dashboard = d["dashboard"]
                    dashboard["_grafana_meta"] = d.get("meta", {})
                    d = dashboard
                if isinstance(d, dict) and ("panels" in d or "rows" in d):
                    d["_source_file"] = f.name
                    dashboards.append(d)
            except json.JSONDecodeError:
                print(f"  WARN: Could not parse {f.name}")
    return dashboards


_HTML_BLOCK_TAGS = {"br", "p", "div", "li", "ul", "ol", "table", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6"}


def _normalize_inline_text(value):
    text = unescape(str(value or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _escape_markdown_label(value):
    return str(value or "").replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _fallback_link_label(href, title=""):
    cleaned_title = _normalize_inline_text(title)
    if cleaned_title:
        return cleaned_title.split(" - ", 1)[0].strip()
    parsed = urlparse(str(href or ""))
    path_parts = [part for part in str(parsed.path or "").split("/") if part]
    if path_parts:
        tail = re.sub(r"\.[a-z0-9]+$", "", path_parts[-1], flags=re.IGNORECASE)
        tail = re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", tail)
        tail = tail.replace("-", " ").replace("_", " ").strip()
        tail = re.sub(r"\s+", " ", tail)
        if tail and tail.lower() not in {"index", "home"}:
            return " ".join(word.capitalize() for word in tail.split())
    host = (parsed.netloc or parsed.path or "").strip().lower()
    host = re.sub(r"^www\.", "", host)
    if host:
        stem = host.split(".", 1)[0].replace("-", " ").replace("_", " ").strip()
        if stem:
            return " ".join(part.capitalize() for part in stem.split())
        return host
    return "Link"


class _HtmlToMarkdownTextPanelParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self._parts = []
        self._links = []

    def handle_starttag(self, tag, attrs):
        attr_map = {str(key): str(value or "") for key, value in attrs}
        lowered = str(tag or "").lower()
        if lowered == "a":
            self._links.append(
                {
                    "href": attr_map.get("href", "").strip(),
                    "title": attr_map.get("title", "").strip(),
                    "parts": [],
                }
            )
            return
        if lowered == "img":
            alt_text = _normalize_inline_text(attr_map.get("alt") or attr_map.get("title") or "")
            if self._links:
                if alt_text:
                    self._emit(alt_text)
                return
            src = attr_map.get("src", "").strip()
            if src:
                label = alt_text or _fallback_link_label(src, "")
                self._emit(f"![{_escape_markdown_label(label)}]({src})")
            elif alt_text:
                self._emit(alt_text)
            return
        if lowered == "iframe":
            src = attr_map.get("src", "").strip()
            if src:
                label = _fallback_link_label(src, attr_map.get("title", "")) or "Embedded content"
                self._emit(f"[{_escape_markdown_label(label)}]({src})")
            return
        if lowered in _HTML_BLOCK_TAGS:
            self._emit("\n")

    def handle_endtag(self, tag):
        lowered = str(tag or "").lower()
        if lowered == "a" and self._links:
            link = self._links.pop()
            label = _normalize_inline_text("".join(link["parts"]))
            if not label:
                label = _fallback_link_label(link.get("href", ""), link.get("title", ""))
            rendered = f"[{_escape_markdown_label(label)}]({link.get('href', '')})" if link.get("href") else label
            self._emit(rendered)
            return
        if lowered in _HTML_BLOCK_TAGS:
            self._emit("\n")

    def handle_data(self, data):
        self._emit(unescape(data))

    def handle_entityref(self, name):
        self._emit(unescape(f"&{name};"))

    def handle_charref(self, name):
        self._emit(unescape(f"&#{name};"))

    def _emit(self, value):
        if not value:
            return
        if self._links:
            self._links[-1]["parts"].append(value)
        else:
            self._parts.append(value)

    def render(self):
        return "".join(self._parts)


def _cleanup_markdown_text_panel_content(content):
    text = str(content or "").replace("\r", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]*\|[ \t]*\n[ \t]*", " | ", text)
    text = re.sub(r"[ \t]*\|[ \t]*", " | ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [line.strip() for line in text.splitlines()]
    merged = []
    for line in lines:
        if not line:
            if merged and merged[-1] != "":
                merged.append("")
            continue
        if merged and (merged[-1].endswith("|") or line.startswith("|")):
            merged[-1] = merged[-1].rstrip(" |") + " | " + line.lstrip(" |")
        else:
            merged.append(line)
    return "\n".join(merged).strip()


def _normalize_text_panel_content(content, mode=""):
    raw = str(content or "")
    if not raw.strip():
        return ""
    lowered_mode = str(mode or "").strip().lower()
    if lowered_mode == "html" or re.search(r"<[a-zA-Z][^>]*>", raw):
        parser = _HtmlToMarkdownTextPanelParser()
        parser.feed(raw)
        parser.close()
        return _cleanup_markdown_text_panel_content(parser.render())
    return raw


def _grafana_session(grafana_url, user="", password="", token="", verify: bool | str = True):
    """Build a requests session with Bearer token or HTTP basic auth."""
    session = requests.Session()
    apply_tls(session, verify)
    tok = str(token or "").strip()
    if tok:
        session.headers["Authorization"] = f"Bearer {tok}"
    elif user:
        session.auth = (user, str(password or ""))
    return session


def _fetch_unified_provisioning_json(session, base_url, path, empty_on_error, resource_label):
    """GET JSON from Grafana unified provisioning; warn and return empty on failure."""
    url = f"{base_url}{path}"
    try:
        resp = session.get(url, timeout=60)
        if resp.status_code == 404:
            print(
                f"  WARN: {resource_label} not found (404); unified alerting may be disabled."
            )
            return empty_on_error
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        print(f"  WARN: Failed to fetch {resource_label}: {exc}")
        return empty_on_error
    except ValueError as exc:
        print(f"  WARN: Invalid JSON for {resource_label}: {exc}")
        return empty_on_error


def extract_unified_alert_rules(grafana_url, user="", password="", token="", verify: bool | str = True):
    """Fetch all Grafana Unified Alerting rules (GET /api/v1/provisioning/alert-rules)."""
    base = str(grafana_url or "").rstrip("/")
    session = _grafana_session(grafana_url, user=user, password=password, token=token, verify=verify)
    data = _fetch_unified_provisioning_json(
        session,
        base,
        "/api/v1/provisioning/alert-rules",
        [],
        "unified alert rules",
    )
    return data if isinstance(data, list) else []


def extract_unified_contact_points(grafana_url, user="", password="", token="", verify: bool | str = True):
    """Fetch contact points (GET /api/v1/provisioning/contact-points)."""
    base = str(grafana_url or "").rstrip("/")
    session = _grafana_session(grafana_url, user=user, password=password, token=token, verify=verify)
    data = _fetch_unified_provisioning_json(
        session,
        base,
        "/api/v1/provisioning/contact-points",
        [],
        "unified contact points",
    )
    return data if isinstance(data, list) else []


def extract_unified_notification_policies(grafana_url, user="", password="", token="", verify: bool | str = True):
    """Fetch notification policy tree (GET /api/v1/provisioning/policies)."""
    base = str(grafana_url or "").rstrip("/")
    session = _grafana_session(grafana_url, user=user, password=password, token=token, verify=verify)
    data = _fetch_unified_provisioning_json(
        session,
        base,
        "/api/v1/provisioning/policies",
        {},
        "unified notification policies",
    )
    return data if isinstance(data, dict) else {}


def extract_unified_mute_timings(grafana_url, user="", password="", token="", verify: bool | str = True):
    """Fetch mute timings (GET /api/v1/provisioning/mute-timings)."""
    base = str(grafana_url or "").rstrip("/")
    session = _grafana_session(grafana_url, user=user, password=password, token=token, verify=verify)
    data = _fetch_unified_provisioning_json(
        session,
        base,
        "/api/v1/provisioning/mute-timings",
        [],
        "unified mute timings",
    )
    return data if isinstance(data, list) else []


def extract_unified_templates(grafana_url, user="", password="", token="", verify: bool | str = True):
    """Fetch notification templates (GET /api/v1/provisioning/templates)."""
    base = str(grafana_url or "").rstrip("/")
    session = _grafana_session(grafana_url, user=user, password=password, token=token, verify=verify)
    data = _fetch_unified_provisioning_json(
        session,
        base,
        "/api/v1/provisioning/templates",
        [],
        "unified notification templates",
    )
    return data if isinstance(data, list) else []


def extract_datasources(grafana_url, user="", password="", token="", verify: bool | str = True):
    """Fetch Grafana datasources and return a UID-keyed metadata map."""
    base = str(grafana_url or "").rstrip("/")
    session = _grafana_session(grafana_url, user=user, password=password, token=token, verify=verify)
    data = _fetch_unified_provisioning_json(
        session,
        base,
        "/api/datasources",
        [],
        "datasources",
    )
    datasources: dict[str, dict[str, str]] = {}
    if not isinstance(data, list):
        return datasources
    for item in data:
        if not isinstance(item, dict):
            continue
        uid = str(item.get("uid", "") or "")
        if not uid:
            continue
        datasources[uid] = {
            "uid": uid,
            "type": str(item.get("type", "") or ""),
            "name": str(item.get("name", "") or ""),
        }
    return datasources


def filter_unified_alert_rules(
    rules: list[dict[str, Any]],
    *,
    uids: list[str] | None = None,
    folder_uids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return only the rules that match all supplied filters (AND semantics).

    ``uids`` — keep rules whose ``uid`` is in the set.
    ``folder_uids`` — keep rules whose ``folderUID`` is in the set.
    Either filter is skipped when the corresponding argument is falsy.
    """
    uid_set = set(uids) if uids else None
    folder_set = set(folder_uids) if folder_uids else None
    if uid_set is None and folder_set is None:
        return rules
    return [
        r for r in rules
        if isinstance(r, dict)
        and (uid_set is None or str(r.get("uid", "") or "") in uid_set)
        and (folder_set is None or str(r.get("folderUID", "") or "") in folder_set)
    ]


def extract_all_alerting_resources(grafana_url, user="", password="", token="", verify: bool | str = True):
    """Fetch all unified alerting provisioning resources; each part degrades gracefully."""
    return {
        "alert_rules": extract_unified_alert_rules(
            grafana_url, user=user, password=password, token=token, verify=verify
        ),
        "contact_points": extract_unified_contact_points(
            grafana_url, user=user, password=password, token=token, verify=verify
        ),
        "notification_policies": extract_unified_notification_policies(
            grafana_url, user=user, password=password, token=token, verify=verify
        ),
        "mute_timings": extract_unified_mute_timings(
            grafana_url, user=user, password=password, token=token, verify=verify
        ),
        "templates": extract_unified_templates(
            grafana_url, user=user, password=password, token=token, verify=verify
        ),
        "datasources": extract_datasources(
            grafana_url, user=user, password=password, token=token, verify=verify
        ),
    }


def _load_json_file_if_present(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"  WARN: Could not parse {path}: {exc}")
        return None


def _normalize_file_datasources(raw: Any) -> dict[str, dict[str, str]]:
    datasources: dict[str, dict[str, str]] = {}
    items: list[dict[str, Any]] = []
    if isinstance(raw, list):
        items = [item for item in raw if isinstance(item, dict)]
    elif isinstance(raw, dict):
        if isinstance(raw.get("datasources"), list):
            items = [item for item in raw["datasources"] if isinstance(item, dict)]
        else:
            for uid, meta in raw.items():
                if isinstance(meta, dict):
                    items.append({"uid": uid, **meta})
    for item in items:
        uid = str(item.get("uid", "") or "")
        if not uid:
            continue
        datasources[uid] = {
            "uid": uid,
            "type": str(item.get("type", "") or ""),
            "name": str(item.get("name", "") or ""),
        }
    return datasources


def _normalize_file_alert_rules(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict) and isinstance(raw.get("alert_rules"), list):
        return [item for item in raw["alert_rules"] if isinstance(item, dict)]
    return []


def _normalize_file_list(raw: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict) and isinstance(raw.get(key), list):
        return [item for item in raw[key] if isinstance(item, dict)]
    return []


def _normalize_file_dict(raw: Any, key: str) -> dict[str, Any]:
    if isinstance(raw, dict):
        if isinstance(raw.get(key), dict):
            return dict(raw.get(key) or {})
        return dict(raw)
    return {}


def extract_all_alerting_resources_from_files(directory: str) -> dict[str, Any]:
    """Load unified alerting provisioning resources from local JSON files."""
    root = Path(directory)
    alerts_dir = root / "alerts"
    search_dirs = [alerts_dir, root]

    def _first_present(names: list[str]) -> Any:
        for base in search_dirs:
            for name in names:
                raw = _load_json_file_if_present(base / name)
                if raw is not None:
                    return raw
        return None

    alert_rules_raw = _first_present([
        "grafana_alert_rules.json",
        "alert_rules.json",
        "unified_alert_rules.json",
    ])
    contact_points_raw = _first_present([
        "grafana_contact_points.json",
        "contact_points.json",
    ])
    notification_policies_raw = _first_present([
        "grafana_notification_policies.json",
        "notification_policies.json",
        "policies.json",
    ])
    mute_timings_raw = _first_present([
        "grafana_mute_timings.json",
        "mute_timings.json",
    ])
    templates_raw = _first_present([
        "grafana_templates.json",
        "templates.json",
    ])
    datasources_raw = _first_present([
        "grafana_datasources.json",
        "datasources.json",
    ])

    return {
        "alert_rules": _normalize_file_alert_rules(alert_rules_raw),
        "contact_points": _normalize_file_list(contact_points_raw, "contact_points"),
        "notification_policies": _normalize_file_dict(notification_policies_raw, "notification_policies"),
        "mute_timings": _normalize_file_list(mute_timings_raw, "mute_timings"),
        "templates": _normalize_file_list(templates_raw, "templates"),
        "datasources": _normalize_file_datasources(datasources_raw),
    }


__all__ = [
    "_cleanup_markdown_text_panel_content",
    "_normalize_text_panel_content",
    "extract_all_alerting_resources",
    "extract_all_alerting_resources_from_files",
    "extract_dashboards_from_files",
    "extract_dashboards_from_grafana",
    "extract_datasources",
    "extract_unified_alert_rules",
    "extract_unified_contact_points",
    "extract_unified_mute_timings",
    "extract_unified_notification_policies",
    "extract_unified_templates",
    "filter_unified_alert_rules",
]
