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
    list_data_views,
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


def _data_view_inventory(data_views: list[dict[str, Any]]) -> frozenset[str]:
    """Every title *and* id in ``data_views``.

    The complement of :func:`_data_view_id_lookup`, which deliberately omits a
    data view whose title is its own id because rewriting it would be a no-op.
    A control ``data_view_id`` outside this set names nothing Kibana holds, so
    leaving it in place is the silent failure worth warning about -- while a
    value already naming a real data view (by id, or by a title that *is* its
    id) is a correct fallback and must stay quiet. See
    ``dashboards_api._resolve_pinned_panel_data_view_ids``.
    """
    refs: set[str] = set()
    for data_view in data_views:
        for key in ("title", "id"):
            value = str(data_view.get(key) or "")
            if value:
                refs.add(value)
    return frozenset(refs)


def _report_unresolved_data_views(
    result: Any,
    label: str,
    kibana_url: str = "",
    *,
    api_key: str = "",
    space_id: str = "",
    verify: bool | str = True,
) -> None:
    """Warn, per control, about a ``data_view_id`` that stayed a raw pattern.

    ``ensure_migration_data_views`` is supposed to make the lookup complete by
    construction, so a miss means ensuring failed for that pattern and Kibana
    will render the control as "An error occurred". The dashboard itself
    uploaded, so this is a warning -- but a named one, because the symptom is
    otherwise indistinguishable from a product bug.

    The *ensured* data views are the wrong yardstick for "does this data view
    exist": a control can legitimately point at one the operator (or an earlier
    ingest) already created, which this upload had no reason to ensure. So a
    reported fallback is re-checked against every data view in the space before
    it is warned about -- and that listing happens only when there is something
    to re-check, never on the clean path. Listing is best effort: if it fails,
    the resolver's own verdict stands rather than the warning being dropped.
    """
    unresolved = list(getattr(result, "unresolved_data_views", None) or [])
    if not unresolved:
        return
    live: frozenset[str] = frozenset()
    try:
        live = _data_view_inventory(
            list_data_views(kibana_url, api_key=api_key, space_id=space_id, verify=verify)
        )
    except Exception:
        pass
    unresolved = [item for item in unresolved if item.data_view not in live]
    # Pruned on the result too, so the JSON upload record cannot carry a
    # false positive the console decided not to print.
    result.unresolved_data_views = unresolved
    for item in unresolved:
        control = item.control or "(untitled)"
        print(
            f"    warning: {label}: control '{control}' points at data view "
            f"'{item.data_view}', which matches no Kibana data view id or title; "
            "the control will render an error until that data view exists.",
            file=sys.stderr,
        )


# The data views every migration output is assumed to need, whatever the
# payload happens to reference. Kept as the floor rather than replaced by the
# referenced set so an upload of a dashboard with no controls still leaves the
# space usable, exactly as before.
_DEFAULT_DATA_VIEW_PATTERNS = ("metrics-prometheus-*", "metrics-*", "logs-*")


def _extra_data_view_patterns(extra_patterns: list[str] | None) -> list[str]:
    """The referenced patterns the defaults do not already cover, deduped."""
    return [
        pattern
        for pattern in dict.fromkeys(extra_patterns or ())
        if pattern and pattern not in _DEFAULT_DATA_VIEW_PATTERNS
    ]


def _merged_data_view_patterns(extras: list[str]) -> list[str]:
    """The defaults first, then anything extra the payload asked for."""
    return list(dict.fromkeys([*_DEFAULT_DATA_VIEW_PATTERNS, *extras]))


def _payload_data_view_patterns(payload: Any) -> list[str]:
    """Every ``data_view``/``data_view_id`` value in a typed API payload.

    In first-seen order, deduped. Shared by the two upload entry points so the
    in-memory pipeline payload and the persisted review artifact are read the
    same way.
    """
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
    return _payload_data_view_patterns(payload)


def _artifact_data_view_patterns(artifact_file: Path) -> list[str]:
    """The data view patterns one persisted ``*.native.json`` references.

    The batch upload path (:meth:`KibanaTargetAdapter.upload`) sends reviewed
    artifacts from disk rather than an in-memory ``NativeDashboard``, so it
    needs the artifact-envelope counterpart of
    :func:`_referenced_data_view_patterns`. A file that cannot be read or parsed
    contributes nothing: ``_native_artifact_upload_file`` reads it again and
    reports the same corruption as a per-record rejection, which is where that
    failure belongs -- collecting patterns must not be the thing that decides an
    artifact is broken.
    """
    try:
        artifact = json.loads(Path(artifact_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(artifact, dict):
        return []
    return _payload_data_view_patterns(artifact.get("payload"))


def _batch_data_view_patterns(
    artifact_files: list[Path],
) -> tuple[dict[Path, list[str]], list[str]]:
    """Per-artifact and batch-wide data view patterns for one upload.

    Returned together because both are needed and each artifact should be read
    once: the union drives a *single* ensure round-trip for the whole batch (13
    artifacts naming ``metrics-*`` must not ensure it 13 times), while the
    per-artifact lists are what attributes an ensure failure back to the
    dashboards it actually breaks.
    """
    per_artifact = {
        artifact_file: _artifact_data_view_patterns(artifact_file)
        for artifact_file in artifact_files
    }
    batch: list[str] = []
    for patterns in per_artifact.values():
        for pattern in patterns:
            if pattern not in batch:
                batch.append(pattern)
    return per_artifact, batch


def _fail_record_on_unavailable_data_view(
    record: dict[str, Any],
    referenced_patterns: list[str],
    unavailable: dict[str, str],
) -> None:
    """Downgrade an otherwise-successful upload whose data view could not exist.

    Ensuring is attempted for every pattern the payload references, so a
    refusal (bad pattern, missing privilege, target error) means the data view
    is *not* there and the control bound to it will render "An error occurred".
    That is a 2xx upload that is knowably incomplete, which is exactly the
    ``lossy`` case: only ``created``/``updated`` count as success, so the record
    stops counting toward ``uploaded_ok`` and the run exits non-zero. No new
    mechanism -- the same status/``success`` path ``lossy`` and ``duplicate_id``
    already use, with the target's own reason carried in ``output`` so it
    reaches both the console and the JSON upload record.

    Only a record that currently reports success is downgraded: a rejection or a
    panel loss is the more specific failure and keeps its status.

    The console line names the artifact and the pattern but not the target's
    reason: ``_ensure_data_views_for_upload`` already printed that once, and a
    batch where 13 artifacts share one refused pattern would otherwise repeat a
    multi-line HTTP error 13 times. The reason still travels per record in
    ``output``, which is what the CLI prints on the ``[FAIL]`` line and what the
    JSON upload record keeps.
    """
    if not unavailable or not record.get("success"):
        return
    blocking = [pattern for pattern in referenced_patterns if pattern in unavailable]
    if not blocking:
        return
    detail = "; ".join(f"'{pattern}': {unavailable[pattern][:300]}" for pattern in blocking)
    record["status"] = "data_view_unavailable"
    record["success"] = False
    record["output"] = (
        f"{record.get('output') or '(untitled)'} — data view {detail}; "
        "the control(s) bound to it will render an error in Kibana"
    )
    named = ", ".join(f"'{pattern}'" for pattern in blocking)
    print(
        f"    ✗ {record.get('artifact') or '(native payload)'} uploaded, but the "
        f"data view its control needs ({named}) could not be created, so that "
        "control will render an error in Kibana.",
        file=sys.stderr,
    )


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
        payload has something to resolve to. A referenced pattern the defaults
        already cover adds nothing, so the request stays byte-identical to the
        default-only call rather than re-stating the same three patterns --
        which is what keeps a payload that only names ``metrics-*`` off this
        code path entirely.
        """
        extras = _extra_data_view_patterns(extra_patterns)
        patterns = _merged_data_view_patterns(extras) if extras else None
        return ensure_migration_data_views(
            kibana_url,
            data_view_patterns=patterns,
            api_key=api_key,
            space_id=space_id,
            verify=verify,
        )

    def _ensure_data_views_for_upload(
        self,
        kibana_url: str,
        *,
        api_key: str = "",
        space_id: str = "",
        verify: bool | str = True,
        extra_patterns: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Ensure the upload's data views, attributing any refusal to a pattern.

        Returns ``(data_views, unavailable)`` where ``unavailable`` maps a
        pattern that could not be ensured to the target's reason. One refused
        pattern must not cost the operator every *other* dashboard in the batch,
        and an exception escaping here would do exactly that -- before any
        artifact was sent, with a traceback instead of a reason.

        The happy path is one call with the whole list, unchanged: a batch that
        needs nothing beyond the defaults issues precisely the request it always
        did. Only after a failure is the list retried pattern by pattern, to
        find out *which* pattern the target refused. Re-ensuring the patterns
        that already succeeded is free -- ``ensure_data_view`` returns an
        existing data view rather than recreating it -- and those extra requests
        only ever happen on a run that is already failing.
        """
        try:
            return (
                self._ensure_default_data_views(
                    kibana_url,
                    api_key=api_key,
                    space_id=space_id,
                    verify=verify,
                    extra_patterns=extra_patterns,
                ),
                {},
            )
        except Exception:
            pass
        data_views: list[dict[str, Any]] = []
        unavailable: dict[str, str] = {}
        for pattern in _merged_data_view_patterns(_extra_data_view_patterns(extra_patterns)):
            try:
                data_views.extend(
                    ensure_migration_data_views(
                        kibana_url,
                        data_view_patterns=[pattern],
                        api_key=api_key,
                        space_id=space_id,
                        verify=verify,
                    )
                )
            except Exception as exc:
                unavailable[pattern] = str(exc) or exc.__class__.__name__
        for pattern, reason in unavailable.items():
            print(
                f"    warning: could not ensure data view '{pattern}': {reason[:300]}",
                file=sys.stderr,
            )
        return data_views, unavailable

    def _native_upload_file(
        self,
        data_views: list[dict[str, Any]],
        *,
        kibana_url: str,
        space_id: str,
        kibana_api_key: str,
        es_url: str,
        es_api_key: str,
        verify: bool | str,
        upload_kibana_url: str,
        target_space: str,
        native_dashboard: Any,
        native_dashboard_stats: dict[str, Any] | None = None,
        artifact_label: str = "",
        seen_dashboard_ids: set[str] | None = None,
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
                es_url=es_url,
                es_api_key=es_api_key,
                space_id=space_id,
                verify=verify,
                native_stats=native_dashboard_stats,
                data_view_ids=data_view_ids,
                data_view_inventory=_data_view_inventory(data_views),
                seen_dashboard_ids=seen_dashboard_ids,
            )
        ]
        _report_unresolved_data_views(
            results[0],
            label,
            kibana_url,
            api_key=kibana_api_key,
            space_id=space_id,
            verify=verify,
        )
        # A "conflict" (409) is reported as a terminal failure so the operator
        # can investigate a cross-space id collision rather than having it
        # silently overwritten.
        if results[0].status == "rejected":
            print(f"    ✗ Dashboards API rejected the payload for {label}.")
        elif results[0].status == "duplicate_id":
            # Same discipline as "lossy": the upload that would have looked like
            # a success is the one that destroys data.
            print(
                f"    ✗ {label} resolves to dashboard id "
                f"'{results[0].dashboard_id}', which another dashboard in this "
                "upload already wrote; nothing was sent for this one.",
                file=sys.stderr,
            )
        elif results[0].status == "lossy":
            # HTTP 200 with something missing. Say so loudly: this is the
            # failure mode nobody investigates, because everything else
            # about the run looks green. A "lossy" upload is not always a
            # dropped panel -- it can also be a dashboard-level time_range/
            # refresh_interval, a pinned control, or a critical per-panel
            # property (see ``_audit_accepted_panels``) -- so the panel-count
            # line only prints when panels actually accounts for the loss.
            lossy = results[0]
            if lossy.dropped_panel_count:
                print(
                    f"    ✗ Kibana accepted {label} but silently dropped "
                    f"{lossy.dropped_panel_count} of {lossy.panels_sent} "
                    "panel(s); the uploaded dashboard is incomplete.",
                    file=sys.stderr,
                )
                for dropped in lossy.dropped_panels:
                    # Kibana's validation errors run to thousands of characters;
                    # the console gets a readable head, the JSON report the rest.
                    detail = f": {dropped.reason[:300]}" if dropped.reason else ""
                    where = f" [section {dropped.section}]" if dropped.section else ""
                    print(
                        f"        - {dropped.title or '(untitled)'}{where}{detail}",
                        file=sys.stderr,
                    )
            if lossy.dropped_controls or lossy.dropped_properties:
                print(
                    f"    ✗ Kibana accepted {label} but silently dropped state: "
                    f"{lossy.message}",
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
                # A "lossy"/"duplicate_id" status alone reads like a shrug; the
                # message names what the operator lost, so it travels with it
                # into ``upload_error`` and the migration report.
                + (
                    f" — {result.message}"
                    if result.status in {"lossy", "duplicate_id"} and result.message
                    else ""
                )
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
            "unresolved_data_views": [
                unresolved.to_dict() for unresolved in (result.unresolved_data_views or [])
            ],
        }

    def _native_artifact_upload_file(
        self,
        artifact_path: Path,
        *,
        kibana_url: str,
        space_id: str,
        kibana_api_key: str,
        es_url: str,
        es_api_key: str,
        verify: bool | str,
        upload_kibana_url: str,
        target_space: str,
        data_view_ids: dict[str, str] | None = None,
        data_view_inventory: frozenset[str] = frozenset(),
        seen_dashboard_ids: set[str] | None = None,
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
                "unresolved_data_views": [],
            }
        result = dashboards_api.upload_native_artifact(
            artifact,
            kibana_url,
            api_key=kibana_api_key,
            es_url=es_url,
            es_api_key=es_api_key,
            space_id=space_id,
            verify=verify,
            data_view_ids=data_view_ids,
            data_view_inventory=data_view_inventory,
            seen_dashboard_ids=seen_dashboard_ids,
        )
        _report_unresolved_data_views(
            result,
            artifact_path.name,
            kibana_url,
            api_key=kibana_api_key,
            space_id=space_id,
            verify=verify,
        )
        if result.status == "duplicate_id":
            # Two reviewed artifacts resolving to one id would leave Kibana
            # holding whichever was uploaded last, with both reported OK.
            print(
                f"    ✗ {artifact_path.name} resolves to dashboard id "
                f"'{result.dashboard_id}', which another artifact in this upload "
                "already wrote; nothing was sent for this one.",
                file=sys.stderr,
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
            "unresolved_data_views": [
                unresolved.to_dict() for unresolved in (result.unresolved_data_views or [])
            ],
        }

    def upload(self, artifact_dir: Path, **kwargs: Any) -> dict[str, Any]:
        """Deploy the ``native/*.native.json`` artifacts under ``artifact_dir``.

        Native Dashboard-as-Code artifacts are the only upload input: they are
        what ``obs-migrate migrate`` writes and what a reviewer inspects, and
        they are sent to the typed Kibana Dashboards API byte-for-byte.

        The data views the batch's controls reference are ensured before the
        first artifact is sent, from the union of what those artifacts actually
        ask for -- not a fixed default list. A reviewed artifact naming
        ``metrics-*.prometheus-*`` (the Datadog ``prometheus_native`` profile)
        otherwise had nothing to resolve against, so its control shipped
        carrying the raw pattern and Kibana rendered it as "An error occurred".
        """
        artifact_dir = Path(artifact_dir)
        kibana_url = str(kwargs.get("kibana_url", "") or "")
        space_id = str(kwargs.get("space_id", "") or "")
        kibana_api_key = str(kwargs.get("kibana_api_key", "") or "")
        es_url = str(kwargs.get("es_url", "") or "")
        es_api_key = str(kwargs.get("es_api_key", "") or "")
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

        patterns_by_artifact, batch_patterns = _batch_data_view_patterns(native_files)
        data_views, unavailable = self._ensure_data_views_for_upload(
            kibana_url,
            api_key=kibana_api_key,
            space_id=space_id,
            verify=verify,
            extra_patterns=batch_patterns,
        )
        data_view_ids = _data_view_id_lookup(data_views)
        data_view_inventory = _data_view_inventory(data_views)
        # One ledger for the whole batch: artifact *stems* are unique, dashboard
        # ids are the upsert key, and two artifacts reaching one id would leave
        # Kibana holding only the last while both records said OK.
        seen_dashboard_ids: set[str] = set()
        records = []
        for artifact_file in native_files:
            record = self._native_artifact_upload_file(
                artifact_file,
                kibana_url=kibana_url,
                space_id=space_id,
                kibana_api_key=kibana_api_key,
                es_url=es_url,
                es_api_key=es_api_key,
                verify=verify,
                upload_kibana_url=upload_kibana_url,
                target_space=target_space,
                data_view_ids=data_view_ids,
                data_view_inventory=data_view_inventory,
                seen_dashboard_ids=seen_dashboard_ids,
            )
            _fail_record_on_unavailable_data_view(
                record, patterns_by_artifact.get(artifact_file, []), unavailable,
            )
            records.append(record)
        summary = {
            "uploaded_ok": sum(1 for item in records if item["success"]),
            "total": len(records),
            "space_id": space_id or target_space,
            "kibana_url": upload_kibana_url,
            "artifact_format": "native",
            "panels_dropped": _records_panels_dropped(records),
        }
        if unavailable:
            # Only present when something was refused, so the clean path's
            # summary is byte-identical to what it has always been.
            summary["data_views_unavailable"] = dict(unavailable)
        return {"summary": summary, "records": records}

    def upload_dashboard(
        self,
        *,
        kibana_url: str,
        space_id: str = "",
        kibana_api_key: str = "",
        es_url: str = "",
        es_api_key: str = "",
        verify: bool | str = True,
        native_dashboard: Any,
        native_dashboard_stats: dict[str, Any] | None = None,
        artifact_label: str = "",
        seen_dashboard_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Deploy one dashboard from the in-memory ``NativeDashboard`` payload.

        The migration pipeline holds the typed payload it just built and uploads
        that, passing ``artifact_label`` (the artifact stem) for reporting.
        ``seen_dashboard_ids`` is the caller's per-run id ledger: a pipeline
        uploading many dashboards passes one set across the loop so a repeated
        dashboard id fails instead of overwriting an earlier dashboard.
        """
        if native_dashboard is None:
            raise ValueError("upload_dashboard requires a native_dashboard payload")
        referenced_patterns = _referenced_data_view_patterns(native_dashboard)
        data_views, unavailable = self._ensure_data_views_for_upload(
            kibana_url,
            api_key=kibana_api_key,
            space_id=space_id,
            verify=verify,
            extra_patterns=referenced_patterns,
        )
        target_space = detect_space_id_from_kibana_url(kibana_url) or "default"
        upload_kibana_url = kibana_url_for_space(kibana_url, space_id)
        record = self._native_upload_file(
            data_views,
            kibana_url=kibana_url,
            space_id=space_id,
            kibana_api_key=kibana_api_key,
            es_url=es_url,
            es_api_key=es_api_key,
            verify=verify,
            upload_kibana_url=upload_kibana_url,
            target_space=target_space,
            native_dashboard=native_dashboard,
            native_dashboard_stats=native_dashboard_stats,
            artifact_label=artifact_label,
            seen_dashboard_ids=seen_dashboard_ids,
        )
        _fail_record_on_unavailable_data_view(record, referenced_patterns, unavailable)
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
            "unresolved_data_views": record.get("unresolved_data_views", []),
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
