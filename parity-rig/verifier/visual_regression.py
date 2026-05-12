# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""End-to-end visual regression harness for migrated dashboards.

The 5-tier verifier already proves *structural* parity between Grafana
PromQL and Kibana ES|QL; this module proves *visual* parity by driving
``agent-browser`` over both products and pixel-diffing the rendered
panels.

It is the measurement instrument the layout redesign
(``parity-rig/verifier/verifier-plans/LAYOUT-REDESIGN-2026-05-12.md``)
gates on: every layout change must hold or improve the median diff
score across all parity dashboards.

Architecture
------------

The harness has three deliberate seams so each can be tested in
isolation:

1. **Capture** - one Grafana panel + one Kibana panel become two PNGs
   on disk. Implemented as :func:`capture_grafana_panel` and
   :func:`capture_kibana_panel`. Both shell out to ``agent-browser``
   via :func:`_run_agent_browser_batch` and tolerate transient errors
   (timeouts, missing binary, auth bounces) by returning ``None`` and
   recording a note on the panel.
2. **Pairing** - we already have :func:`visual_diff.pair_panels_by_title`
   and re-use it. Title is the only stable identity across Grafana and
   Kibana.
3. **Scoring** - :func:`visual_diff.diff_screenshots` returns
   ``(0..1 score, diff_image_path)``. Aggregation lives here in
   :func:`aggregate_scores` to keep the diff module single-purpose.

CLI
---

::

    python -m verifier.visual_regression \\
        --migration-out  /tmp/mig-to-kbn-e2e/parity-out-<slug>/dashboards \\
        --grafana-url    http://localhost:23000 \\
        --grafana-uid    <grafana-dashboard-uid> \\
        --kibana-url     https://<cluster>.kb.elastic.cloud \\
        --kibana-dash-id <kibana-saved-object-id> \\
        --api-key        $KEY \\
        --output-dir     /tmp/visual-regression/<slug>/ \\
        --report         /tmp/visual-regression/<slug>/report.json \\
        [--state         $HOME/.agent-browser/state/mig-to-kbn-verifier.json] \\
        [--from now-1h --to now]                          \\
        [--threshold 0.15]                                 \\
        [--wait-extra-seconds 4]

The ``--state`` flag points at the persistent agent-browser auth file
captured by ``parity-rig/verifier/bootstrap.sh``. Without it the
Kibana side will bounce to SAML and the capture step will record
``auth_required`` notes (the Grafana side still works because the
parity-rig Grafana is anonymous).
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import requests
import yaml

LOG = logging.getLogger(__name__)


DEFAULT_VIEWPORT_WIDTH = 1280
DEFAULT_VIEWPORT_HEIGHT = 720
DEFAULT_FROM = "now-1h"
DEFAULT_TO = "now"
DEFAULT_THRESHOLD = 0.15
DEFAULT_WAIT_EXTRA_SECONDS = 4
DEFAULT_BROWSER_TIMEOUT_SECONDS = 60

# Kibana renders the expanded panel inside this selector. The DOM has
# been stable since Lens shipped solo panel view.
KIBANA_EXPANDED_PANEL_SELECTOR = "[data-test-subj='dashboardPanel']"

# When the harness can't capture a side cleanly we tag the panel so
# downstream report consumers can filter / explain.
NOTE_GRAFANA_MISSING = "grafana_capture_failed"
NOTE_KIBANA_MISSING = "kibana_capture_failed"
NOTE_KIBANA_AUTH = "kibana_auth_required"
NOTE_TINY_SCREENSHOT = "screenshot_too_small_to_be_real"
NOTE_UNPAIRED_GRAFANA = "no_matching_kibana_panel"
NOTE_UNPAIRED_KIBANA = "no_matching_grafana_panel"

# A real rendered panel is bigger than this. Below this size the PNG
# is almost certainly an auth redirect / blank loader / error toast.
# Tuned to 2 KiB after observing that markdown / "Migration Required"
# panels render in ~6 KiB while SAML redirect pages are ~3 KiB.
MIN_REAL_SCREENSHOT_BYTES = 2 * 1024


# --------------------------------------------------------------------- #
#  Data classes                                                         #
# --------------------------------------------------------------------- #


@dataclass
class PanelComparison:
    """One Grafana-vs-Kibana panel comparison result."""

    title: str
    grafana_panel_id: int | None = None
    kibana_panel_id: str = ""
    grafana_screenshot: str = ""
    kibana_screenshot: str = ""
    diff_screenshot: str = ""
    diff_score: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "grafana_panel_id": self.grafana_panel_id,
            "kibana_panel_id": self.kibana_panel_id,
            "grafana_screenshot": self.grafana_screenshot,
            "kibana_screenshot": self.kibana_screenshot,
            "diff_screenshot": self.diff_screenshot,
            "diff_score": self.diff_score,
            "notes": list(self.notes),
        }


@dataclass
class DashboardReport:
    """Aggregate visual-regression report for a single dashboard."""

    grafana_uid: str
    kibana_dashboard_id: str
    panels: list[PanelComparison] = field(default_factory=list)
    unpaired_grafana: list[str] = field(default_factory=list)
    unpaired_kibana: list[str] = field(default_factory=list)
    median_score: float = 0.0
    p95_score: float = 0.0
    max_score: float = 0.0
    captured_pairs: int = 0
    skipped_pairs: int = 0

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "grafana_uid": self.grafana_uid,
            "kibana_dashboard_id": self.kibana_dashboard_id,
            "panels": [p.to_jsonable() for p in self.panels],
            "unpaired_grafana": list(self.unpaired_grafana),
            "unpaired_kibana": list(self.unpaired_kibana),
            "median_score": self.median_score,
            "p95_score": self.p95_score,
            "max_score": self.max_score,
            "captured_pairs": self.captured_pairs,
            "skipped_pairs": self.skipped_pairs,
        }


# --------------------------------------------------------------------- #
#  Panel discovery                                                      #
# --------------------------------------------------------------------- #


def list_grafana_panels(
    grafana_url: str,
    dashboard_uid: str,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """Return the panels of a Grafana dashboard via its REST API.

    Each returned dict has ``id`` (int), ``title`` (str), and ``type``
    (str). Row panels are skipped because they're containers, not
    chart instances.

    Panels are emitted in **migration-canonical walk order** so that
    position N in this list corresponds to position N in the migrated
    Kibana dashboard's YAML/NDJSON. The order is:

    1. Top-level panels sorted by ``(gridPos.y, gridPos.x, id)``.
    2. When a row container is encountered in that sort, its
       ``panels[]`` children are emitted right after it before the
       next top-level item (mirrors the migration's
       ``_build_section_groups``).
    3. For schemaVersion 14 dashboards using legacy
       ``dashboard.rows[]``, panels are walked row-by-row in file
       order (these rarely have ``gridPos``).

    Pairing by position (U2) replaces title-based pairing because
    real dashboards routinely have empty-title panels (text/markdown
    dividers, untitled stat tiles) that can't be distinguished by
    title alone.

    Panels without a numeric ``id`` in JSON get one synthesised via
    Grafana's own runtime rule (``max(existing) + 1``) so every panel
    is capture-able via ``/d-solo?panelId=<N>``. See
    :func:`_assign_runtime_ids` for the empirical justification.

    Raises:
        requests.HTTPError: on a non-2xx response from Grafana.
    """
    sess = session or requests.Session()
    resp = sess.get(
        f"{grafana_url.rstrip('/')}/api/dashboards/uid/{dashboard_uid}",
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    dashboard = payload.get("dashboard", {}) or {}

    flat = _walk_grafana_in_migration_order(dashboard)
    return _assign_runtime_ids(flat, dashboard_uid)


def _grafana_panel_sort_key(panel: dict[str, Any]) -> tuple[int, int]:
    """The same sort key the migration's _build_section_groups uses,
    minus the ``id`` tiebreaker.

    Returns ``(gridPos.y, gridPos.x)``. The migration appends ``id``
    as a tiebreaker, but here we drop it: when two panels share
    ``(y, x)`` (or when both lack ``gridPos`` and the key reduces to
    ``(0, 0)``) we want Python's *stable* sort to preserve JSON
    document order, not reorder by id. JSON document order is the
    only reliable proxy for "what the migration emitted next" when
    coordinates collide.
    """
    grid = panel.get("gridPos") or {}
    return (
        int(grid.get("y", 0) or 0),
        int(grid.get("x", 0) or 0),
    )


def _walk_grafana_in_migration_order(
    dashboard: dict[str, Any],
) -> list[dict[str, Any]]:
    """Flatten a Grafana dashboard in the order the migration would.

    For modern dashboards:

    * Sort ``dashboard.panels`` by ``(y, x, id)``.
    * Walk that sorted list, expanding row containers in place: when
      a ``type=row`` entry is found, its ``panels[]`` children are
      appended to the output immediately and the row marker itself
      is dropped.

    For legacy (schemaVersion 14) dashboards:

    * Walk ``dashboard.rows[].panels[]`` row-by-row in file order
      (these rarely carry gridPos so we don't try to sort them).
    """
    out: list[dict[str, Any]] = []
    top_level = dashboard.get("panels", []) or []
    if top_level:
        for panel in sorted(top_level, key=_grafana_panel_sort_key):
            if panel.get("type") == "row":
                for child in panel.get("panels", []) or []:
                    if child.get("type") != "row":
                        out.append(child)
                continue
            out.append(panel)
        return out

    for row in dashboard.get("rows", []) or []:
        for child in row.get("panels", []) or []:
            if child.get("type") != "row":
                out.append(child)
    return out


def _assign_runtime_ids(
    flat_panels: list[dict[str, Any]],
    dashboard_uid: str,
) -> list[dict[str, Any]]:
    """Materialise Grafana's runtime panel-id assignment rule.

    Walks ``flat_panels`` in document order. For each panel:

    * If it has a numeric ``id`` in JSON, use it verbatim.
    * Otherwise synthesize ``max(seen_ids) + 1`` (using the running
      max of every numeric id observed so far, JSON-explicit or
      previously synthesized).

    This mirrors the assignment Grafana's frontend does at render
    time, so probing ``/d-solo?panelId=<synthesized>`` will hit the
    real panel.
    """
    kept: list[dict[str, Any]] = []
    max_seen = 0
    synthesized_count = 0
    for p in flat_panels:
        pid = p.get("id")
        if isinstance(pid, int) and pid > 0:
            assigned = pid
        else:
            assigned = max_seen + 1
            synthesized_count += 1
        if assigned > max_seen:
            max_seen = assigned
        kept.append(
            {
                "id": assigned,
                "title": (p.get("title") or "").strip(),
                "type": p.get("type"),
            }
        )

    if synthesized_count:
        LOG.info(
            "list_grafana_panels(%s): synthesized %d panel id(s) using "
            "Grafana's runtime rule (max+1 in document order); these "
            "will be probed via /d-solo at capture time",
            dashboard_uid,
            synthesized_count,
        )
    return kept


def list_kibana_panels_from_migration(
    migration_out: Path,
) -> list[dict[str, Any]]:
    """Discover Kibana panels by reading the migration's YAML.

    We deliberately use the local artifacts rather than Kibana's API
    so the harness still works in environments where the cluster
    saved-object API is locked down (the verifier already handles
    this exact case the same way).

    The migration YAML nests panels inside ``section.panels`` when
    the source had Grafana rows; we recurse so every leaf panel is
    discovered regardless of nesting depth. Section containers
    themselves are excluded (they have no ES|QL / markdown to
    render).

    The ``id`` field is **the Kibana panel UUID** taken from the
    compiled NDJSON's ``panelsJSON.panelIndex`` (cross-referenced by
    title). YAML alone doesn't carry the UUID; the compiled NDJSON
    is the canonical source.
    """
    panels: list[dict[str, Any]] = []
    yaml_files = sorted(migration_out.glob("yaml/*.yaml"))
    for yaml_file in yaml_files:
        with open(yaml_file) as f:
            doc = yaml.safe_load(f) or {}
        for dash in doc.get("dashboards") or []:
            _collect_yaml_panels(dash.get("panels") or [], panels)

    # Backfill ``id`` from the compiled NDJSON, keyed on title.
    ndjson_panel_ids = _read_compiled_panel_ids_by_title(migration_out)
    for panel in panels:
        if not panel["id"]:
            panel["id"] = ndjson_panel_ids.get(panel["title"], "")
    return panels


def _collect_yaml_panels(
    panel_nodes: list[dict[str, Any]],
    out: list[dict[str, Any]],
) -> None:
    """Recursively flatten the migration YAML's nested panel tree.

    Walks ``section.panels`` children but skips the section node
    itself. Leaf panels (markdown, esql, lens, ...) are appended to
    ``out``.
    """
    for node in panel_nodes:
        section = node.get("section")
        if isinstance(section, dict):
            _collect_yaml_panels(section.get("panels") or [], out)
            continue
        title = (node.get("title") or "").strip()
        if not title:
            continue
        out.append(
            {
                "id": node.get("id") or node.get("panel_id") or "",
                "title": title,
                "type": node.get("type"),
            }
        )


def _read_compiled_panel_ids_by_title(migration_out: Path) -> dict[str, str]:
    """Build a ``{title: panelIndex}`` map from the compiled NDJSON.

    Each migrated dashboard ends up as one ``dashboard`` saved-object
    in ``compiled/<slug>/compiled_dashboards.ndjson`` whose
    ``attributes.panelsJSON`` is a JSON string of the panels array.
    Titles live at ``embeddableConfig.savedVis.title`` for markdown
    visualizations and at ``embeddableConfig.attributes.title`` for
    Lens visualizations (in Kibana 9.5). We check both, plus a few
    fallback paths, so the lookup works across panel types.
    """
    out: dict[str, str] = {}
    compiled_dir = migration_out / "compiled"
    if not compiled_dir.is_dir():
        return out
    for ndjson in compiled_dir.glob("*/compiled_dashboards.ndjson"):
        for line in ndjson.read_text().splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "dashboard":
                continue
            panels_json = obj.get("attributes", {}).get("panelsJSON")
            if not isinstance(panels_json, str):
                continue
            try:
                panels = json.loads(panels_json)
            except json.JSONDecodeError:
                continue
            for panel in panels:
                title = _extract_compiled_panel_title(panel)
                pid = str(panel.get("panelIndex") or "")
                if title and pid:
                    out[title] = pid
    return out


def _extract_compiled_panel_title(panel: dict[str, Any]) -> str:
    """Pull the human-readable title from a compiled NDJSON panel.

    Markdown visualizations stash the title at
    ``embeddableConfig.savedVis.title``; Lens visualizations stash it
    at ``embeddableConfig.attributes.title`` (or ``panel.title`` for
    older shapes). Empty string if nothing usable is found.
    """
    cfg = panel.get("embeddableConfig") or {}
    saved_vis = cfg.get("savedVis") or {}
    title = (saved_vis.get("title") or "").strip()
    if title:
        return title
    attrs = cfg.get("attributes") or {}
    title = (attrs.get("title") or "").strip()
    if title:
        return title
    # Older shape: title directly on the panel
    return (panel.get("title") or "").strip()


# --------------------------------------------------------------------- #
#  URL builders                                                         #
# --------------------------------------------------------------------- #


def build_grafana_solo_url(
    grafana_url: str,
    dashboard_uid: str,
    dashboard_slug: str,
    panel_id: int,
    from_: str = DEFAULT_FROM,
    to: str = DEFAULT_TO,
) -> str:
    """Build a Grafana ``d-solo`` URL for a single panel.

    ``kiosk=tv`` strips top-bar chrome and is the closest Grafana has
    to a "no-frame" panel render.
    """
    base = grafana_url.rstrip("/")
    qs = urlencode(
        {"panelId": panel_id, "from": from_, "to": to, "kiosk": "tv"},
        quote_via=quote,
    )
    return f"{base}/d-solo/{dashboard_uid}/{dashboard_slug}?{qs}"


def build_kibana_expanded_panel_url(
    kibana_url: str,
    dashboard_id: str,
    panel_id: str,
    from_: str = DEFAULT_FROM,
    to: str = DEFAULT_TO,
) -> str:
    """Build a Kibana URL that lands on a single expanded panel.

    Kibana 8.16+/9.0+ supports an ``expandedPanelId`` Rison-encoded
    value in ``_a=()``. We URL-encode the panel id but leave Rison
    parentheses alone so Kibana parses it correctly.
    """
    base = kibana_url.rstrip("/")
    g_state = f"(time:(from:{from_},to:{to}))"
    a_state = f"(expandedPanelId:{panel_id})"
    return (
        f"{base}/app/dashboards#/view/{dashboard_id}"
        f"?_g={g_state}&_a={a_state}"
    )


# --------------------------------------------------------------------- #
#  agent-browser subprocess driver                                      #
# --------------------------------------------------------------------- #


# Substrings that mean the page redirected to an auth flow. Match
# Elastic Cloud's SAML/Okta/cloud-saml-kibana flow as well as the
# generic ``capture-url`` interstitial Kibana uses before showing the
# login form.
_AUTH_REDIRECT_SUBSTRINGS = (
    "capture-url",
    "cloud-saml-kibana",
    "cloud-login",
    "auth_provider_hint",
    "/login",
    "/oauth",
    "saml-auth",
)


def _looks_like_auth_redirect(url: str) -> bool:
    """Return True when ``url`` matches one of the known auth flows."""
    lower = url.lower()
    return any(sub in lower for sub in _AUTH_REDIRECT_SUBSTRINGS)


def _run_agent_browser_batch(
    commands: list[str],
    state_file: Path | None = None,
    session: str = "visual-rig",
    timeout: int = DEFAULT_BROWSER_TIMEOUT_SECONDS,
) -> tuple[bool, str, str]:
    """Run a list of ``agent-browser`` commands as a single ``batch``.

    Returns ``(ok, stdout, stderr)``. ``ok`` is True iff the binary is
    on PATH AND the subprocess exited 0 AND no step in the batch
    reported ``success: false`` in its JSON.
    """
    binary = shutil.which("agent-browser")
    if binary is None:
        LOG.warning("agent-browser not on PATH; visual regression skipped")
        return False, "", "agent-browser binary missing"

    cmd: list[str] = [binary, "--session", session]
    if state_file is not None and state_file.exists():
        cmd.extend(["--state", str(state_file)])
    cmd.extend(["batch", "--json", "--bail", *commands])

    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        LOG.warning("agent-browser batch timed out: %s", exc)
        return False, exc.stdout or "", "timeout"

    if completed.returncode != 0:
        return False, completed.stdout, completed.stderr

    try:
        steps = json.loads(completed.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return False, completed.stdout, "batch stdout was not JSON"

    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict) and step.get("success") is False:
                return False, completed.stdout, json.dumps(step)
    return True, completed.stdout, completed.stderr


def _extract_open_url(stdout: str) -> str:
    """Return the post-navigation URL recorded by the ``open`` step.

    ``agent-browser batch --json`` emits one JSON object per step;
    the ``open`` step's ``result.url`` is what the browser landed on
    after redirects. Empty when stdout is unparseable.
    """
    try:
        steps = json.loads(stdout.strip() or "[]")
    except json.JSONDecodeError:
        return ""
    if not isinstance(steps, list):
        return ""
    for step in steps:
        if not isinstance(step, dict):
            continue
        cmd = step.get("command")
        if isinstance(cmd, list) and cmd and cmd[0] == "open":
            result = step.get("result") or {}
            if isinstance(result, dict):
                return str(result.get("url", ""))
    return ""


def capture_grafana_panel(
    grafana_url: str,
    dashboard_uid: str,
    dashboard_slug: str,
    panel_id: int,
    output_path: Path,
    *,
    from_: str = DEFAULT_FROM,
    to: str = DEFAULT_TO,
    wait_extra_seconds: int = DEFAULT_WAIT_EXTRA_SECONDS,
    state_file: Path | None = None,
    session: str = "visual-rig-grafana",
) -> tuple[Path | None, list[str]]:
    """Capture a Grafana solo-panel screenshot.

    Returns ``(path, notes)``. ``path`` is ``None`` on failure;
    ``notes`` contains an explanatory string the caller can attach to
    the panel record.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    url = build_grafana_solo_url(
        grafana_url, dashboard_uid, dashboard_slug, panel_id, from_=from_, to=to
    )
    ok, stdout, stderr = _run_agent_browser_batch(
        [
            f"open {url}",
            f"wait {wait_extra_seconds * 1000}",
            f"screenshot {output_path}",
        ],
        state_file=state_file,
        session=session,
    )
    notes: list[str] = []
    if not ok:
        notes.append(NOTE_GRAFANA_MISSING)
        LOG.warning(
            "grafana capture failed for panel %s: %s",
            panel_id,
            stderr.strip() or stdout.strip(),
        )
        return None, notes
    if not output_path.exists() or output_path.stat().st_size < MIN_REAL_SCREENSHOT_BYTES:
        notes.append(NOTE_TINY_SCREENSHOT)
        return None, notes
    return output_path, notes


def capture_kibana_panel(
    kibana_url: str,
    dashboard_id: str,
    panel_id: str,
    output_path: Path,
    *,
    from_: str = DEFAULT_FROM,
    to: str = DEFAULT_TO,
    wait_extra_seconds: int = DEFAULT_WAIT_EXTRA_SECONDS,
    state_file: Path | None = None,
    session: str = "visual-rig-kibana",
    selector: str = KIBANA_EXPANDED_PANEL_SELECTOR,
) -> tuple[Path | None, list[str]]:
    """Capture a Kibana expanded-panel screenshot.

    Uses Kibana's ``expandedPanelId`` Rison state which renders a
    single panel full-screen. We scope the screenshot to the dashboard
    panel selector so the diff isn't dominated by Kibana chrome / nav
    bar.

    Failure modes we explicitly distinguish:

    * ``kibana_auth_required``  - selector capture failed AND a
      fall-back full-viewport capture is tiny (<2 KiB) -> SAML
      redirect page. Operator should run ``bootstrap.sh``.
    * ``kibana_render_failed``  - selector capture failed AND the
      full-viewport screenshot looks real-sized -> Kibana rendered
      something but the expanded panel selector wasn't found.
      Investigate the URL or panel id.
    * ``kibana_capture_failed`` - any other agent-browser subprocess
      failure (binary missing, timeout, no fall-back capture).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    url = build_kibana_expanded_panel_url(
        kibana_url, dashboard_id, panel_id, from_=from_, to=to
    )
    ok, stdout, stderr = _run_agent_browser_batch(
        [
            f"open {url}",
            f"wait {wait_extra_seconds * 1000}",
            f"screenshot {selector} {output_path}",
        ],
        state_file=state_file,
        session=session,
    )
    # The most reliable signal of an auth bounce is the post-navigation
    # URL; PNG size is unreliable because Lens panels can legitimately
    # render as small as ~1.8 KiB when gridData.h is tiny.
    landed_url = _extract_open_url(stdout)
    if _looks_like_auth_redirect(landed_url):
        return None, [NOTE_KIBANA_AUTH]

    # Trust the agent-browser response: success + file on disk means
    # we got a real panel render regardless of byte size.
    if ok and output_path.exists() and output_path.stat().st_size > 0:
        return output_path, []

    notes: list[str] = []
    LOG.warning(
        "kibana selector capture failed for panel %s (landed on %s): %s",
        panel_id,
        landed_url or "<no url>",
        stderr.strip() or stdout.strip(),
    )
    # Selector capture failed AND the URL didn't look like auth. Try
    # a fall-back viewport capture so we can distinguish render
    # failure (real-sized fall-back) from a deeper issue
    # (capture_failed).
    fallback_path = output_path.with_suffix(".fallback.png")
    fb_ok, fb_stdout, _fb_stderr = _run_agent_browser_batch(
        [
            f"open {url}",
            f"wait {wait_extra_seconds * 1000}",
            f"screenshot {fallback_path}",
        ],
        state_file=state_file,
        session=session,
    )
    fb_landed = _extract_open_url(fb_stdout)
    if _looks_like_auth_redirect(fb_landed):
        notes.append(NOTE_KIBANA_AUTH)
        return None, notes
    # Fall-back PNG IS a viewport screenshot so byte-size *is* a
    # reasonable signal of "Kibana rendered something". Use the 2 KiB
    # threshold here.
    if fb_ok and fallback_path.exists() and fallback_path.stat().st_size >= MIN_REAL_SCREENSHOT_BYTES:
        notes.append("kibana_render_failed")
        return None, notes

    notes.append(NOTE_KIBANA_MISSING)
    return None, notes


# --------------------------------------------------------------------- #
#  Panel pairing                                                        #
# --------------------------------------------------------------------- #


def pair_panels_by_position(
    grafana_panels: list[dict[str, Any]],
    kibana_panels: list[dict[str, Any]],
) -> tuple[
    list[tuple[dict[str, Any], dict[str, Any]]],
    list[str],
    list[str],
]:
    """Pair Grafana ↔ Kibana panels by walk order (U2 universal fix).

    Both inputs MUST already be in migration-canonical order:

    * Grafana side: produced by :func:`list_grafana_panels`, which
      walks ``(gridPos.y, gridPos.x, id)`` with row containers
      expanded in place.
    * Kibana side: produced by :func:`list_kibana_panels_from_migration`,
      which walks the migration YAML / NDJSON
      (``section.panels`` flattened in their emit order).

    The migration emits both sides in the same canonical order, so
    pairing reduces to: position N on the left pairs with position N
    on the right. This is the only correct approach when titles can
    be empty (text/markdown dividers), duplicated (``"Untitled"``
    appearing multiple times in ``prometheus-all``), or rewritten by
    the migration (``""`` -> ``"Untitled"``, ``""`` -> ``"2"`` for
    value-typed singlestats with empty source title).

    Returns:
        ``(paired, only_in_grafana_titles, only_in_kibana_titles)``.
        Length mismatches are tolerated: we pair the common prefix
        and report the tail of each side as unpaired. Each pair is a
        2-tuple of the underlying panel dicts (with all metadata
        including ``id``, ``title``, ``type``) so the caller can
        reach for whichever identifier it needs.
    """
    paired: list[tuple[dict[str, Any], dict[str, Any]]] = []
    common = min(len(grafana_panels), len(kibana_panels))
    for i in range(common):
        paired.append((grafana_panels[i], kibana_panels[i]))

    only_grafana_titles = [
        (p.get("title") or f"<grafana panel id={p.get('id')}>")
        for p in grafana_panels[common:]
    ]
    only_kibana_titles = [
        (p.get("title") or f"<kibana panel id={p.get('id')}>")
        for p in kibana_panels[common:]
    ]
    return paired, only_grafana_titles, only_kibana_titles


# --------------------------------------------------------------------- #
#  Aggregation                                                          #
# --------------------------------------------------------------------- #


def aggregate_scores(panels: list[PanelComparison]) -> tuple[float, float, float, int]:
    """Return ``(median, p95, max, n_scored)``.

    Only panels with at least one captured pair contribute to the
    aggregate; auth/capture failures are excluded so they don't drag
    the score artificially toward 0.
    """
    scores = [
        p.diff_score
        for p in panels
        if p.grafana_screenshot and p.kibana_screenshot
    ]
    if not scores:
        return 0.0, 0.0, 0.0, 0
    n = len(scores)
    median = statistics.median(scores)
    sorted_scores = sorted(scores)
    p95_idx = max(0, min(n - 1, round(0.95 * (n - 1))))
    p95 = sorted_scores[p95_idx]
    max_score = max(scores)
    return median, p95, max_score, n


# --------------------------------------------------------------------- #
#  End-to-end run                                                       #
# --------------------------------------------------------------------- #


def run_dashboard(
    grafana_url: str,
    grafana_uid: str,
    grafana_slug: str,
    kibana_url: str,
    kibana_dashboard_id: str,
    migration_out: Path,
    output_dir: Path,
    *,
    from_: str = DEFAULT_FROM,
    to: str = DEFAULT_TO,
    threshold: float = DEFAULT_THRESHOLD,
    wait_extra_seconds: int = DEFAULT_WAIT_EXTRA_SECONDS,
    state_file: Path | None = None,
) -> DashboardReport:
    """Run the full visual-regression loop for one dashboard.

    Steps:
        1. List Grafana panels via API (in migration-canonical order).
        2. List Kibana panels via migration YAML (also canonical).
        3. Pair by position (U2 universal fix).
        4. For each pair: capture Grafana + Kibana, diff, score.
        5. Aggregate, build report.
    """
    from . import visual_diff  # local to keep tests importing this module fast

    LOG.info(
        "visual-regression dashboard grafana=%s kibana=%s",
        grafana_uid,
        kibana_dashboard_id,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    grafana_dir = output_dir / "grafana"
    kibana_dir = output_dir / "kibana"
    diff_dir = output_dir / "diffs"
    grafana_dir.mkdir(exist_ok=True)
    kibana_dir.mkdir(exist_ok=True)
    diff_dir.mkdir(exist_ok=True)

    grafana_panels = list_grafana_panels(grafana_url, grafana_uid)
    kibana_panels = list_kibana_panels_from_migration(migration_out)
    LOG.info(
        "grafana panels: %d (non-row), kibana panels (from migration): %d",
        len(grafana_panels),
        len(kibana_panels),
    )

    paired, only_grafana, only_kibana = pair_panels_by_position(
        grafana_panels, kibana_panels
    )
    if only_grafana or only_kibana:
        LOG.info(
            "panel-count mismatch: %d only-grafana, %d only-kibana "
            "(paired the common prefix)",
            len(only_grafana),
            len(only_kibana),
        )

    report = DashboardReport(
        grafana_uid=grafana_uid,
        kibana_dashboard_id=kibana_dashboard_id,
        unpaired_grafana=only_grafana,
        unpaired_kibana=only_kibana,
    )

    for idx, (g_meta, k_meta) in enumerate(paired):
        # Use the Grafana title as the display title; fall back to a
        # position-indexed placeholder when both sides have empty
        # titles (eg. text dividers).
        display_title = (g_meta.get("title") or "").strip() or (
            (k_meta.get("title") or "").strip() or f"panel-{idx + 1:03d}"
        )
        comp = PanelComparison(
            title=display_title,
            grafana_panel_id=int(g_meta["id"]) if g_meta.get("id") else None,
            kibana_panel_id=str(k_meta.get("id") or ""),
        )
        # Slug uses the position index so empty-title and
        # duplicate-title panels still get unique filenames.
        slug = _slug_for_panel(idx, display_title)
        grafana_png = grafana_dir / f"{slug}.png"
        kibana_png = kibana_dir / f"{slug}.png"
        diff_png = diff_dir / f"{slug}.png"

        g_path, g_notes = capture_grafana_panel(
            grafana_url,
            grafana_uid,
            grafana_slug,
            int(g_meta["id"]),
            grafana_png,
            from_=from_,
            to=to,
            wait_extra_seconds=wait_extra_seconds,
            state_file=state_file,
        )
        comp.notes.extend(g_notes)
        if g_path is not None:
            comp.grafana_screenshot = str(g_path)

        # We need a kibana panel id; fall back to title-derived slug
        # (not pretty but Kibana's expandedPanelId is the panel uuid
        # from panelsJSON; we'd ideally look that up but the migration
        # YAML doesn't always include it. For the harness today we
        # screenshot the *full* dashboard if expandedPanelId isn't
        # resolvable and rely on the selector to crop to the panel).
        kibana_panel_id = str(k_meta.get("id") or "")
        if kibana_panel_id:
            k_path, k_notes = capture_kibana_panel(
                kibana_url,
                kibana_dashboard_id,
                kibana_panel_id,
                kibana_png,
                from_=from_,
                to=to,
                wait_extra_seconds=wait_extra_seconds,
                state_file=state_file,
            )
            comp.notes.extend(k_notes)
            if k_path is not None:
                comp.kibana_screenshot = str(k_path)
        else:
            comp.notes.append(NOTE_KIBANA_MISSING)

        # Diff only when we have both sides.
        if comp.grafana_screenshot and comp.kibana_screenshot:
            score, diff_path = visual_diff.diff_screenshots(
                Path(comp.grafana_screenshot),
                Path(comp.kibana_screenshot),
                diff_png,
                threshold=threshold,
            )
            comp.diff_score = score
            comp.diff_screenshot = diff_path
            report.captured_pairs += 1
        else:
            report.skipped_pairs += 1

        report.panels.append(comp)

    median, p95, mx, _ = aggregate_scores(report.panels)
    report.median_score = median
    report.p95_score = p95
    report.max_score = mx
    return report


def _slug_for_title(title: str) -> str:
    """Filesystem-safe slug derived from a panel title.

    Lowercases, collapses non-alphanumerics to ``-``, trims edges, and
    caps at 80 chars to stay well under macOS' filename limit.
    """
    out: list[str] = []
    prev_dash = False
    for ch in title.lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        else:
            if not prev_dash:
                out.append("-")
                prev_dash = True
    slug = "".join(out).strip("-")
    return slug[:80] or "untitled"


def _slug_for_panel(position_index: int, title: str) -> str:
    """Filesystem-safe slug that's unique per panel position.

    Empty-title and duplicate-title panels would collide if we used
    title alone (eg. ``prometheus-all`` has 3 empty-title panels and
    2 of them become ``"Untitled"`` in the migration). Prefixing
    with the 0-based position index, zero-padded to 3 digits, gives
    every panel a unique filename while keeping the title visible
    for human debugging.
    """
    return f"{position_index:03d}-{_slug_for_title(title)}"


# --------------------------------------------------------------------- #
#  CLI                                                                  #
# --------------------------------------------------------------------- #


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Visual regression harness for migrated dashboards.",
    )
    parser.add_argument("--migration-out", required=True, type=Path,
                        help="Per-dashboard migration output (contains yaml/, compiled/)")
    parser.add_argument("--grafana-url", default="http://localhost:23000",
                        help="Parity-rig Grafana base URL (default: http://localhost:23000)")
    parser.add_argument("--grafana-uid", required=True,
                        help="Source Grafana dashboard UID")
    parser.add_argument("--grafana-slug", required=True,
                        help="Source Grafana dashboard slug (in the URL after the UID)")
    parser.add_argument("--kibana-url", required=True,
                        help="Kibana base URL (https://...elastic.cloud)")
    parser.add_argument("--kibana-dash-id", required=True,
                        help="Kibana dashboard saved-object id")
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="Where to drop screenshots and the JSON report")
    parser.add_argument("--report", required=True, type=Path,
                        help="JSON report output path")
    parser.add_argument("--from", dest="from_", default=DEFAULT_FROM,
                        help=f"Time range start (default: {DEFAULT_FROM})")
    parser.add_argument("--to", default=DEFAULT_TO,
                        help=f"Time range end (default: {DEFAULT_TO})")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"Per-pixel diff threshold 0..1 (default: {DEFAULT_THRESHOLD})")
    parser.add_argument("--wait-extra-seconds", type=int,
                        default=DEFAULT_WAIT_EXTRA_SECONDS,
                        help="Wait time after navigation before screenshot")
    parser.add_argument("--state",
                        type=Path,
                        help="agent-browser persistent state file for Kibana auth")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    report = run_dashboard(
        grafana_url=args.grafana_url,
        grafana_uid=args.grafana_uid,
        grafana_slug=args.grafana_slug,
        kibana_url=args.kibana_url,
        kibana_dashboard_id=args.kibana_dash_id,
        migration_out=args.migration_out,
        output_dir=args.output_dir,
        from_=args.from_,
        to=args.to,
        threshold=args.threshold,
        wait_extra_seconds=args.wait_extra_seconds,
        state_file=args.state,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report.to_jsonable(), indent=2))
    print(
        f"captured={report.captured_pairs} "
        f"skipped={report.skipped_pairs} "
        f"median={report.median_score:.4f} "
        f"p95={report.p95_score:.4f} "
        f"max={report.max_score:.4f}",
        file=sys.stderr,
    )
    return 0


__all__ = [
    "DEFAULT_FROM",
    "DEFAULT_THRESHOLD",
    "DEFAULT_TO",
    "DashboardReport",
    "PanelComparison",
    "_looks_like_auth_redirect",
    "aggregate_scores",
    "build_argparser",
    "build_grafana_solo_url",
    "build_kibana_expanded_panel_url",
    "capture_grafana_panel",
    "capture_kibana_panel",
    "list_grafana_panels",
    "list_kibana_panels_from_migration",
    "main",
    "run_dashboard",
]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
