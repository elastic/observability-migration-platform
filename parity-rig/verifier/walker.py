"""Browser-driven walker for the 5-tier panel verifier.

The walker drives ``agent-browser`` over a single Kibana dashboard and
collects per-panel evidence that the saved-objects API cannot give us
(specifically: the live ``_query`` body Lens dispatches, the response
shape, a panel-scoped screenshot, and optional React suspense status).

It is structured so that:

* The module imports cleanly even when ``agent-browser`` is not on
  ``PATH``; all subprocess calls are confined to :func:`Walker.run` and
  the merge / parsing helpers are pure-Python.
* HAR parsing and fingerprint matching live in module-level functions
  that are trivially unit-testable.
* The walker is **additive** with respect to the existing verifier:
  :func:`merge_walker_into_verifier` overlays browser-sourced evidence
  onto an already-rendered :class:`PanelRecord` JSON, without re-running
  :func:`parity-rig.verifier.compare.compare_panel_record`. The
  pre-existing verdict therefore survives the merge intact.

CLI usage::

    python -m verifier.walker \\
        --kibana-url https://<cluster>.kb.us-central1.gcp.staging.elastic.cloud \\
        --dashboard-id <kibana-uuid> \\
        --output-dir /tmp/walker-<slug>/ \\
        [--state $HOME/.agent-browser/state/mig-to-kbn-verifier.json] \\
        [--enable-react] \\
        [--merge /tmp/verifier-<slug>.json] \\
        [--wait-extra-seconds 8] \\
        [--browser-timeout-seconds 60] \\
        [--close-on-exit]

See ``parity-rig/verifier/bootstrap.sh`` for the one-time interactive
state-bootstrap dance.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

LOG = logging.getLogger("verifier.walker")

# Default location chosen to line up with ``bootstrap.sh``; CLI users
# can override with ``--state``.
DEFAULT_STATE_FILE = Path.home() / ".agent-browser" / "state" / "mig-to-kbn-verifier.json"

# Number of non-whitespace characters of an ES|QL query we use as a
# fingerprint when matching HAR entries to panels.  60 is short enough
# to survive trivial whitespace/parameter rewriting that Lens performs
# before dispatching, but long enough to be unique across a typical
# dashboard.
FINGERPRINT_CHAR_BUDGET = 60


# --------------------------------------------------------------------- #
# pure-python helpers (no subprocess, no agent-browser dependency)
# --------------------------------------------------------------------- #


def extract_fingerprint(esql: str, budget: int = FINGERPRINT_CHAR_BUDGET) -> str:
    """Return the first ``budget`` non-whitespace characters of ``esql``.

    Used to correlate a panel's compiled NDJSON ES|QL with a HAR entry's
    ``request.postData.text``. Empty / whitespace-only input yields an
    empty string, which callers treat as "no fingerprint, can't match".
    """
    if not esql:
        return ""
    compact = re.sub(r"\s+", "", esql)
    return compact[:budget]


def _url_ends_in_query(url: str) -> bool:
    """Return True if the URL's path ends in ``/_query``.

    Kibana hits both ``/internal/esql/...`` and ``/api/esql/...`` so we
    deliberately match on the path suffix rather than the host or full
    URL.  Trailing slashes and query-strings are stripped before the
    comparison.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    path = (parsed.path or "").rstrip("/")
    return path.endswith("/_query")


@dataclass
class HarQueryEntry:
    """One ``/_query`` request/response pair extracted from a HAR file."""

    url: str
    method: str
    request_body: str = ""
    response_body: str = ""
    response_status: int = 0
    started_at: str = ""

    def request_text_for_matching(self) -> str:
        """Concatenate request URL and body for fingerprint matching.

        We include the URL because some Kibana versions send the ES|QL
        as a query-string parameter rather than a JSON body, and we
        want to remain compatible with both layouts.
        """
        return f"{self.url}\n{self.request_body}"


def parse_har_for_query_entries(har_path: Path) -> list[HarQueryEntry]:
    """Extract every ``/_query`` request/response pair from a HAR file.

    Skips entries whose URL does not end in ``/_query``. Tolerates
    malformed HAR sections (missing keys, base64-encoded bodies marked
    via ``"encoding": "base64"``); base64 bodies are returned as-is
    because Lens never sends ES|QL bodies as base64.

    Returns an empty list if the file does not exist or is not valid
    JSON.  Never raises.
    """
    if not har_path.exists():
        return []
    try:
        doc = json.loads(har_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("could not parse HAR %s: %s", har_path, exc)
        return []
    log = doc.get("log") or {}
    out: list[HarQueryEntry] = []
    for entry in log.get("entries") or []:
        request = entry.get("request") or {}
        url = request.get("url") or ""
        if not _url_ends_in_query(url):
            continue
        post_data = request.get("postData") or {}
        response = entry.get("response") or {}
        content = response.get("content") or {}
        response_body = content.get("text") or ""
        if content.get("encoding") == "base64":
            response_body = ""  # Lens responses are JSON; ignore base64.
        out.append(
            HarQueryEntry(
                url=url,
                method=(request.get("method") or "POST").upper(),
                request_body=post_data.get("text") or "",
                response_body=response_body,
                response_status=int(response.get("status") or 0),
                started_at=entry.get("startedDateTime") or "",
            )
        )
    return out


def _parse_es_response(body: str) -> tuple[list[str], int, str]:
    """Parse an ES|QL response body into ``(columns, row_count, error)``.

    Returns empty values when the body isn't JSON. Treats ``error``
    keys as the error payload.
    """
    if not body:
        return [], 0, ""
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return [], 0, ""
    if not isinstance(parsed, dict):
        return [], 0, ""
    if "error" in parsed:
        err = parsed.get("error")
        return [], 0, json.dumps(err) if not isinstance(err, str) else err
    columns = [
        c.get("name", "")
        for c in (parsed.get("columns") or [])
        if isinstance(c, dict)
    ]
    rows = parsed.get("values") or []
    return columns, len(rows), ""


@dataclass
class WalkerPanelEvidence:
    """All browser-sourced evidence for a single panel.

    Mirrors the subset of :class:`PanelRecord` fields the walker can
    populate, plus a few diagnostic fields that have no
    :class:`PanelRecord` home (used in standalone-mode reports).
    """

    panel_id: str = ""
    title: str = ""
    fingerprint: str = ""
    har_path: str = ""
    kibana_screenshot_path: str = ""
    suspense_status: str = ""
    t4_cluster_esql: str = ""
    t5_live_query_body: str = ""
    t5_response_status: int = 0
    t5_response_columns: list[str] = field(default_factory=list)
    t5_response_row_count: int = 0
    t5_response_error: str = ""
    matched_har_url: str = ""
    matched_har_started_at: str = ""
    notes: list[str] = field(default_factory=list)

    def to_jsonable(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def match_har_to_panels(
    har_entries: list[HarQueryEntry],
    panel_fingerprints: dict[str, str],
) -> dict[str, HarQueryEntry]:
    """Return a ``{panel_id: HarQueryEntry}`` mapping.

    A panel matches an entry when its fingerprint appears in the
    entry's ``request_text_for_matching()``.  Earlier entries win on
    ties; this matches the way Lens dispatches: the first request is
    the one that populates the panel, later ones tend to be re-fetches
    for time-range scrubs.

    Panels with an empty fingerprint are dropped (no way to correlate).
    """
    out: dict[str, HarQueryEntry] = {}
    for panel_id, fingerprint in panel_fingerprints.items():
        if not fingerprint:
            continue
        for entry in har_entries:
            if fingerprint in re.sub(r"\s+", "", entry.request_text_for_matching()):
                out[panel_id] = entry
                break
    return out


def _extract_query_from_request_body(body: str) -> str:
    """Pull the ``query`` field out of a Lens ``_query`` request body.

    Falls back to returning the body verbatim if it isn't JSON or
    doesn't contain a ``query`` field; callers compare on canonical
    form so the verbatim body still works for whitespace-tolerant
    comparison.
    """
    if not body:
        return ""
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return body
    if isinstance(parsed, dict):
        return parsed.get("query") or body
    return body


def build_panel_evidence(
    panel_id: str,
    title: str,
    fingerprint: str,
    har_path: Path | None,
    har_entry: HarQueryEntry | None,
    screenshot_path: Path | None,
    suspense_status: str,
) -> WalkerPanelEvidence:
    """Assemble a :class:`WalkerPanelEvidence` from raw inputs."""
    evidence = WalkerPanelEvidence(
        panel_id=panel_id,
        title=title,
        fingerprint=fingerprint,
        har_path=str(har_path) if har_path else "",
        kibana_screenshot_path=str(screenshot_path) if screenshot_path else "",
        suspense_status=suspense_status,
    )
    if har_entry is None:
        if fingerprint:
            evidence.notes.append("no HAR entry matched fingerprint")
        else:
            evidence.notes.append("no NDJSON ES|QL fingerprint available")
        return evidence
    evidence.t4_cluster_esql = _extract_query_from_request_body(har_entry.request_body)
    evidence.t5_live_query_body = har_entry.request_body or evidence.t4_cluster_esql
    evidence.matched_har_url = har_entry.url
    evidence.matched_har_started_at = har_entry.started_at
    columns, row_count, error = _parse_es_response(har_entry.response_body)
    evidence.t5_response_status = har_entry.response_status
    evidence.t5_response_columns = columns
    evidence.t5_response_row_count = row_count
    evidence.t5_response_error = error
    return evidence


# --------------------------------------------------------------------- #
# Merge mode  (verifier JSON + walker evidence => combined verifier JSON)
# --------------------------------------------------------------------- #


def merge_walker_into_verifier(
    verifier_payload: dict[str, Any],
    panel_evidences: list[WalkerPanelEvidence],
    har_path: Path | None,
) -> dict[str, Any]:
    """Overlay walker evidence onto an existing verifier payload.

    Matching is by panel title (case-sensitive).  Only fields the
    walker actually filled in are overwritten; the existing verifier
    verdict and drift axes are preserved untouched because the walker
    is additive and does not re-run :func:`compare_panel_record`.

    Returns a *new* dict; the input ``verifier_payload`` is not
    mutated.
    """
    # We import here so the module imports cleanly even when the
    # parity-rig path hasn't been wired up yet.
    from .records import PanelRecord

    out = json.loads(json.dumps(verifier_payload))  # deep copy via json roundtrip
    by_title: dict[str, WalkerPanelEvidence] = {
        ev.title: ev for ev in panel_evidences if ev.title
    }
    for panel_blob in out.get("panels", []):
        record = PanelRecord.from_jsonable(panel_blob)
        evidence = by_title.get(record.title)
        if evidence is None:
            continue
        if evidence.har_path:
            record.har_path = evidence.har_path
        elif har_path is not None:
            record.har_path = str(har_path)
        if evidence.kibana_screenshot_path:
            record.kibana_screenshot_path = evidence.kibana_screenshot_path
        if evidence.suspense_status:
            record.suspense_status = evidence.suspense_status
        if evidence.t4_cluster_esql:
            record.t4_cluster_esql = evidence.t4_cluster_esql
        if evidence.t5_live_query_body:
            record.t5_live_query_body = evidence.t5_live_query_body
        if evidence.t5_response_status:
            record.t5_response_status = evidence.t5_response_status
        if evidence.t5_response_columns:
            record.t5_response_columns = list(evidence.t5_response_columns)
        if evidence.t5_response_row_count:
            record.t5_response_row_count = evidence.t5_response_row_count
        if evidence.t5_response_error:
            record.t5_response_error = evidence.t5_response_error
        for note in evidence.notes:
            if note not in record.notes:
                record.notes.append(note)
        # Crucially we do NOT call compare_panel_record here; the
        # existing verdict survives the merge.
        merged_blob = record.to_jsonable()
        # Verdict was already set by the verifier run; the merged blob
        # should keep the original.
        merged_blob["verdict"] = panel_blob.get("verdict", merged_blob["verdict"])
        merged_blob["drift_axes"] = list(panel_blob.get("drift_axes", merged_blob["drift_axes"]))
        merged_blob["drift_details"] = dict(panel_blob.get("drift_details", merged_blob["drift_details"]))
        panel_blob.update(merged_blob)
    return out


# --------------------------------------------------------------------- #
# agent-browser subprocess driver
# --------------------------------------------------------------------- #


class AgentBrowserError(RuntimeError):
    """Raised when an ``agent-browser`` invocation fails fatally."""


@dataclass
class _AbResult:
    """Captured stdout/stderr/exit code of one ``agent-browser`` call."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _run_agent_browser(
    args: list[str],
    *,
    state_file: Path | None = None,
    enable_react: bool = False,
    timeout: int = 60,
    stdin: str | None = None,
    check: bool = False,
) -> _AbResult:
    """Run a single ``agent-browser`` invocation, list-form (no shell).

    ``state_file`` and ``enable_react`` are launch-time flags and are
    only honoured when the daemon is starting fresh; callers are
    expected to ``close --all`` first if they need them to take effect
    (matching the agent-browser pitfalls section).
    """
    cmd: list[str] = ["agent-browser"]
    if state_file is not None:
        cmd.extend(["--state", str(state_file)])
    if enable_react:
        cmd.extend(["--enable", "react-devtools"])
    cmd.extend(args)
    LOG.debug("agent-browser: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AgentBrowserError(
            "agent-browser not found on PATH (install with `npm install -g agent-browser`)"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AgentBrowserError(f"agent-browser timed out after {timeout}s: {exc}") from exc
    result = _AbResult(
        returncode=proc.returncode,
        stdout=(proc.stdout or "").strip(),
        stderr=(proc.stderr or "").strip(),
    )
    if check and not result.ok:
        raise AgentBrowserError(
            f"agent-browser failed ({result.returncode}): {result.stderr or result.stdout}"
        )
    return result


def _ab_batch(
    commands: list[list[str]],
    *,
    state_file: Path | None = None,
    enable_react: bool = False,
    timeout: int = 120,
    bail: bool = False,
) -> _AbResult:
    """Run a batch of commands via ``agent-browser batch --json``.

    Commands are passed on stdin as a JSON array of string arrays;
    this is the form documented for batch mode and avoids any
    shell-quoting concerns.
    """
    extra: list[str] = ["batch", "--json"]
    if bail:
        extra.append("--bail")
    return _run_agent_browser(
        extra,
        state_file=state_file,
        enable_react=enable_react,
        timeout=timeout,
        stdin=json.dumps(commands),
    )


# --------------------------------------------------------------------- #
# panel discovery (JS-eval based, robust to Kibana DOM churn)
# --------------------------------------------------------------------- #


# JS used to enumerate visible panels on the dashboard. Returns
# ``[{title, panel_index, selector}]`` where ``selector`` is a stable
# CSS path the walker can hand to ``screenshot --selector``.
#
# Kibana's panels carry one of several attributes depending on version:
#   - ``data-test-subj="embeddablePanel"`` (the long-standing layer)
#   - ``data-test-subj="dashboardPanel-<index>"`` (newer)
#   - ``data-test-embeddable-id``  (a UUID, also handy for the selector)
# We probe each panel that matches and return the first stable
# attribute we find.
_PANEL_DISCOVERY_JS = r"""
(() => {
  const results = [];
  const seen = new Set();
  const containers = document.querySelectorAll(
    "[data-test-subj^='embeddablePanel'], " +
    "[data-test-subj^='dashboardPanel'], " +
    "[data-test-embeddable-id]"
  );
  containers.forEach((node, idx) => {
    let selector = "";
    const dts = node.getAttribute("data-test-subj");
    const dteid = node.getAttribute("data-test-embeddable-id");
    if (dts) {
      selector = `[data-test-subj="${dts}"]`;
    } else if (dteid) {
      selector = `[data-test-embeddable-id="${dteid}"]`;
    } else {
      return;
    }
    if (seen.has(selector)) return;
    seen.add(selector);
    // Title preference: the inline title element first, else
    // aria-label on the container, else the data-test-subj value.
    let title = "";
    const titleNode = node.querySelector(
      "[data-test-subj='dashboardPanelTitle'], " +
      "[data-test-subj='embeddablePanelHeading-'], " +
      ".embPanel__title, " +
      "h2"
    );
    if (titleNode && titleNode.textContent) {
      title = titleNode.textContent.trim();
    } else if (node.getAttribute("aria-label")) {
      title = node.getAttribute("aria-label");
    } else {
      title = dts || dteid || `panel-${idx}`;
    }
    results.push({
      title,
      panel_index: dts || dteid || `panel-${idx}`,
      selector,
    });
  });
  return results;
})()
""".strip()


def _slugify_for_filename(s: str) -> str:
    """Conservative slugifier so panel screenshots have predictable
    filenames inside ``--output-dir``."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", s).strip("-")[:80] or "panel"


# --------------------------------------------------------------------- #
# Walker orchestrator
# --------------------------------------------------------------------- #


@dataclass
class WalkerConfig:
    """Inputs to :class:`Walker`."""

    kibana_url: str
    dashboard_id: str
    output_dir: Path
    state_file: Path = DEFAULT_STATE_FILE
    enable_react: bool = False
    wait_extra_seconds: int = 8
    browser_timeout_seconds: int = 60
    close_on_exit: bool = False
    space: str = "default"
    panel_fingerprints: dict[str, str] = field(default_factory=dict)
    """``{panel_title: fingerprint}`` mapping used to correlate HAR
    entries to titled panels in merge mode.  Standalone runs can leave
    this empty; the walker will still record every ``/_query`` it sees,
    just without a panel correlation."""


class Walker:
    """Orchestrates one dashboard walkthrough.

    Side-effects (all confined to ``run``):

      1. ``agent-browser close --all`` to ensure a clean daemon.
      2. ``agent-browser network har start`` to begin recording.
      3. ``agent-browser open <kibana>/app/dashboards#/view/<id>``.
      4. ``wait --load networkidle`` then ``wait <wait_extra_seconds * 1000>``.
      5. ``agent-browser eval`` to discover panel containers + titles.
      6. ``agent-browser snapshot --json`` for diagnostic context.
      7. Per panel: ``agent-browser screenshot --selector <css> <path>``.
      8. (optional) ``agent-browser react suspense --only-dynamic --json``.
      9. ``agent-browser network har stop <output-dir>/run.har`` (idempotent).
     10. (optional) ``agent-browser close --all`` if ``--close-on-exit``.
    """

    def __init__(self, config: WalkerConfig) -> None:
        self.config = config
        self.output_dir: Path = config.output_dir
        self.har_path: Path = config.output_dir / "run.har"
        self.snapshot_path: Path = config.output_dir / "snapshot.json"
        self.panels_path: Path = config.output_dir / "panels.json"
        self.suspense_path: Path = config.output_dir / "suspense.json"

    # ----- low-level wrappers -----

    def _ab(self, args: list[str], *, timeout: int | None = None, check: bool = False) -> _AbResult:
        return _run_agent_browser(
            args,
            state_file=self.config.state_file,
            enable_react=self.config.enable_react,
            timeout=timeout or self.config.browser_timeout_seconds,
            check=check,
        )

    def _ab_silent(self, args: list[str], *, timeout: int | None = None) -> _AbResult:
        """Best-effort invocation: log on failure, never raise."""
        try:
            return self._ab(args, timeout=timeout, check=False)
        except AgentBrowserError as exc:
            LOG.warning("agent-browser command failed (%s): %s", args, exc)
            return _AbResult(returncode=1, stdout="", stderr=str(exc))

    # ----- run phases -----

    def _dashboard_url(self) -> str:
        base = self.config.kibana_url.rstrip("/")
        space = self.config.space
        # Kibana's saved-objects URL is space-scoped only when the space
        # isn't ``default``; mirroring the cli.py convention.
        space_prefix = "" if space in ("", "default") else f"/s/{space}"
        return f"{base}{space_prefix}/app/dashboards#/view/{self.config.dashboard_id}"

    def _open_and_settle(self) -> None:
        """Phase 1: clean state, start HAR, open, wait for Lens."""
        # Daemon reset so --state / --enable react-devtools actually apply.
        self._ab_silent(["close", "--all"], timeout=30)
        self._ab(["network", "har", "start"], check=True)
        self._ab(["open", self._dashboard_url()], check=True)
        # ``wait --load networkidle`` plus an extra sleep covers Lens's
        # delayed dispatch; Kibana sends the panel _query requests
        # *after* the page is otherwise idle.
        self._ab_silent(["wait", "--load", "networkidle"], timeout=self.config.browser_timeout_seconds)
        if self.config.wait_extra_seconds > 0:
            ms = self.config.wait_extra_seconds * 1000
            self._ab_silent(["wait", str(ms)], timeout=self.config.browser_timeout_seconds)

    def _discover_panels(self) -> list[dict[str, str]]:
        """Phase 2: enumerate panels via JS eval + snapshot."""
        # Diagnostic snapshot (compact, interactive). Failure is non-fatal.
        snap = self._ab_silent(["snapshot", "-i", "-c", "--json"])
        if snap.ok and snap.stdout:
            try:
                self.snapshot_path.write_text(snap.stdout, encoding="utf-8")
            except OSError as exc:
                LOG.warning("could not write snapshot.json: %s", exc)

        # Real discovery happens via eval.
        eval_result = self._ab_silent(
            ["eval", _PANEL_DISCOVERY_JS, "--json"],
            timeout=self.config.browser_timeout_seconds,
        )
        panels: list[dict[str, str]] = []
        if eval_result.ok and eval_result.stdout:
            try:
                payload = json.loads(eval_result.stdout)
            except json.JSONDecodeError as exc:
                LOG.warning("could not parse eval output: %s", exc)
                payload = {}
            # agent-browser's eval --json wraps the JS return value in
            # {success, data}; some versions return the value directly.
            data = payload.get("data") if isinstance(payload, dict) else payload
            if isinstance(data, dict) and "result" in data:
                data = data["result"]
            if isinstance(data, list):
                panels = [
                    {
                        "title": str(p.get("title") or ""),
                        "panel_index": str(p.get("panel_index") or ""),
                        "selector": str(p.get("selector") or ""),
                    }
                    for p in data
                    if isinstance(p, dict) and p.get("selector")
                ]
        try:
            self.panels_path.write_text(json.dumps(panels, indent=2), encoding="utf-8")
        except OSError as exc:
            LOG.warning("could not write panels.json: %s", exc)
        return panels

    def _capture_screenshots(self, panels: list[dict[str, str]]) -> dict[str, Path]:
        """Phase 3: take one screenshot per panel, scoped to the panel's selector."""
        out: dict[str, Path] = {}
        if not panels:
            return out
        for idx, panel in enumerate(panels):
            slug = _slugify_for_filename(panel.get("title") or panel.get("panel_index") or f"panel-{idx}")
            screenshot_path = self.output_dir / f"panel-{idx:02d}-{slug}.png"
            args = ["screenshot", panel["selector"], str(screenshot_path)]
            result = self._ab_silent(args, timeout=self.config.browser_timeout_seconds)
            if result.ok and screenshot_path.exists():
                out[panel.get("title") or panel.get("panel_index") or f"panel-{idx}"] = screenshot_path
            else:
                LOG.warning(
                    "screenshot for panel '%s' failed: %s",
                    panel.get("title"),
                    result.stderr or result.stdout,
                )
        return out

    def _capture_suspense(self) -> dict[str, str]:
        """Phase 4 (optional): record which boundaries are still pending.

        Returns ``{panel_title: "ok"|"stuck"}`` for as many panels as
        react can identify. Disabled unless ``--enable-react``.
        """
        if not self.config.enable_react:
            return {}
        res = self._ab_silent(["react", "suspense", "--only-dynamic", "--json"])
        if not (res.ok and res.stdout):
            return {}
        try:
            self.suspense_path.write_text(res.stdout, encoding="utf-8")
        except OSError as exc:
            LOG.warning("could not write suspense.json: %s", exc)
        # We don't try to correlate suspense boundaries to titled
        # panels here; react fiber names are obfuscated post-build.
        # The raw JSON is recorded for human inspection; the per-panel
        # field is set to "stuck" if *any* dynamic boundary is pending
        # (a conservative signal that something is broken).
        try:
            parsed = json.loads(res.stdout)
        except json.JSONDecodeError:
            return {}
        data = parsed.get("data") if isinstance(parsed, dict) else parsed
        if isinstance(data, dict):
            pending = data.get("pending") or data.get("stuck") or []
        elif isinstance(data, list):
            pending = data
        else:
            pending = []
        return {"__global__": "stuck" if pending else "ok"}

    def _stop_har(self) -> None:
        self._ab_silent(["network", "har", "stop", str(self.har_path)], timeout=30)

    # ----- driver -----

    def run(self) -> list[WalkerPanelEvidence]:
        """Execute the full walkthrough; return one evidence record per
        successfully-discovered panel.

        Steps that fail (e.g. a single panel screenshot) are downgraded
        to warnings; the walk continues so the HAR (which is the most
        valuable artifact) is always written.
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._open_and_settle()
            panels = self._discover_panels()
            screenshots = self._capture_screenshots(panels)
            suspense = self._capture_suspense()
        finally:
            # HAR stop is idempotent; we always run it even if
            # discovery / screenshots blew up.
            self._stop_har()
            if self.config.close_on_exit:
                self._ab_silent(["close", "--all"], timeout=30)

        har_entries = parse_har_for_query_entries(self.har_path)

        # Match panels to HAR by title -> fingerprint -> HAR entry.
        evidences: list[WalkerPanelEvidence] = []
        global_suspense = suspense.get("__global__", "")
        for idx, panel in enumerate(panels):
            title = panel.get("title") or ""
            fingerprint = self.config.panel_fingerprints.get(title, "")
            har_match = None
            if fingerprint:
                matches = match_har_to_panels(har_entries, {title: fingerprint})
                har_match = matches.get(title)
            evidence = build_panel_evidence(
                panel_id=panel.get("panel_index") or f"panel-{idx}",
                title=title,
                fingerprint=fingerprint,
                har_path=self.har_path if self.har_path.exists() else None,
                har_entry=har_match,
                screenshot_path=screenshots.get(title),
                suspense_status=global_suspense or "",
            )
            evidences.append(evidence)
        return evidences


# --------------------------------------------------------------------- #
# report writers
# --------------------------------------------------------------------- #


def write_standalone_report(
    output_dir: Path,
    config: WalkerConfig,
    panels: list[WalkerPanelEvidence],
    har_path: Path,
    har_entry_count: int,
) -> Path:
    """Write ``<output-dir>/walker-report.json``.

    Standalone-mode report shape::

        {
          "dashboard_id": "...",
          "kibana_url": "...",
          "har_path": "...",
          "har_query_entry_count": <int>,
          "panels": [WalkerPanelEvidence.to_jsonable(), ...]
        }
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "dashboard_id": config.dashboard_id,
        "kibana_url": config.kibana_url,
        "har_path": str(har_path),
        "har_query_entry_count": har_entry_count,
        "panels": [p.to_jsonable() for p in panels],
    }
    report_path = output_dir / "walker-report.json"
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return report_path


def load_panel_fingerprints_from_verifier(verifier_payload: dict[str, Any]) -> dict[str, str]:
    """Build ``{title: fingerprint}`` from an existing verifier JSON.

    Uses ``t3_ndjson_esql`` as the source-of-truth ES|QL because that
    is the closest local mirror of what Kibana stores.  Panels with no
    NDJSON ES|QL (markdown / manual / not-feasible) are skipped.
    """
    out: dict[str, str] = {}
    for panel in verifier_payload.get("panels") or []:
        tiers = panel.get("tiers") or {}
        title = panel.get("title") or ""
        esql = tiers.get("t3_ndjson_esql") or ""
        fingerprint = extract_fingerprint(esql)
        if title and fingerprint:
            out[title] = fingerprint
    return out


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="parity-rig.verifier.walker",
        description=(
            "Drive agent-browser over a Kibana dashboard and collect "
            "per-panel evidence (HAR, screenshots, live _query bodies). "
            "Operates standalone (writes walker-report.json) or in "
            "merge mode (overlays evidence onto an existing verifier JSON)."
        ),
    )
    p.add_argument(
        "--kibana-url",
        required=True,
        help="Kibana base URL (e.g. https://<cluster>.kb.us-central1.gcp.staging.elastic.cloud).",
    )
    p.add_argument(
        "--dashboard-id",
        required=True,
        help="Kibana saved-object ID of the dashboard to walk.",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory to write run.har, per-panel screenshots, and walker-report.json.",
    )
    p.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help=(
            "agent-browser persistent state file (saved by bootstrap.sh). "
            f"Default: {DEFAULT_STATE_FILE}"
        ),
    )
    p.add_argument(
        "--space",
        default="default",
        help="Kibana space (default: default).",
    )
    p.add_argument(
        "--enable-react",
        action="store_true",
        help="Launch agent-browser with the react-devtools hook and collect suspense status.",
    )
    p.add_argument(
        "--merge",
        type=Path,
        default=None,
        help=(
            "Path to an existing verifier JSON. If provided, the walker's "
            "evidence is overlaid onto each PanelRecord (matched by title) "
            "and the merged JSON is written back in-place."
        ),
    )
    p.add_argument(
        "--wait-extra-seconds",
        type=int,
        default=8,
        help="Extra wait after networkidle to let Lens dispatch panel queries.",
    )
    p.add_argument(
        "--browser-timeout-seconds",
        type=int,
        default=60,
        help="Per-command timeout for agent-browser invocations.",
    )
    p.add_argument(
        "--close-on-exit",
        action="store_true",
        help="Close all agent-browser sessions on exit (default: leave open for debugging).",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose logging.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    fingerprints: dict[str, str] = {}
    verifier_payload: dict[str, Any] | None = None
    if args.merge is not None:
        if not args.merge.exists():
            print(f"error: --merge file not found: {args.merge}", file=sys.stderr)
            return 2
        verifier_payload = json.loads(args.merge.read_text(encoding="utf-8"))
        fingerprints = load_panel_fingerprints_from_verifier(verifier_payload)
        LOG.info(
            "merge mode: loaded %d panel fingerprints from %s",
            len(fingerprints),
            args.merge,
        )

    config = WalkerConfig(
        kibana_url=args.kibana_url,
        dashboard_id=args.dashboard_id,
        output_dir=output_dir,
        state_file=args.state,
        enable_react=args.enable_react,
        wait_extra_seconds=args.wait_extra_seconds,
        browser_timeout_seconds=args.browser_timeout_seconds,
        close_on_exit=args.close_on_exit,
        space=args.space,
        panel_fingerprints=fingerprints,
    )

    walker = Walker(config)
    try:
        evidences = walker.run()
    except AgentBrowserError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

    har_entries = parse_har_for_query_entries(walker.har_path)
    LOG.info(
        "walker complete: %d panels discovered, %d /_query entries in HAR",
        len(evidences),
        len(har_entries),
    )

    standalone_report = write_standalone_report(
        output_dir,
        config,
        evidences,
        walker.har_path,
        len(har_entries),
    )
    LOG.info("wrote %s", standalone_report)

    if verifier_payload is not None and args.merge is not None:
        merged = merge_walker_into_verifier(
            verifier_payload, evidences, walker.har_path if walker.har_path.exists() else None
        )
        args.merge.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        LOG.info("merged walker evidence into %s", args.merge)

    return 0


__all__ = [
    "FINGERPRINT_CHAR_BUDGET",
    "AgentBrowserError",
    "HarQueryEntry",
    "Walker",
    "WalkerConfig",
    "WalkerPanelEvidence",
    "build_panel_evidence",
    "extract_fingerprint",
    "load_panel_fingerprints_from_verifier",
    "match_har_to_panels",
    "merge_walker_into_verifier",
    "parse_har_for_query_entries",
    "write_standalone_report",
]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
