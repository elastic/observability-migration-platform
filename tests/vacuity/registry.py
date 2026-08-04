# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""The registry of load-bearing guards, and what must make each of them fail.

Read this file top to bottom: it is the whole inventory. Three tables, one per
flavour of vacuity (see :mod:`tests.vacuity` for why one technique is not enough):

* :data:`GUARD_CASES` — guard + healthy subject + mutations that must turn it red
  + a witness counting what it examined.
* :data:`EMPTY_INPUT_GATES` — gate + empty input it must refuse + healthy input it
  must accept.
* :data:`FIRING_GUARDS` — guard + a run in which its interesting branch must be
  observed executing at least once.

Entry criteria — a guard belongs here when **its silence lets a real defect
ship**. Not every assertion in the suite qualifies and this is deliberately not a
mutation-testing framework: a general mutant generator would report thousands of
survivors, almost all of them uninteresting, which is how mutation reports get
ignored. Each entry names the historical instance it stands for in ``catches``.
"""

from __future__ import annotations

import copy
import importlib
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT / "parity-rig") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "parity-rig"))

from observability_migration.adapters.source.grafana import panels as grafana_panels  # noqa: E402
from observability_migration.core.assets.native_dashboard import (  # noqa: E402
    NativeGrid,
    NativePanel,
)
from observability_migration.targets.kibana import dashboards_api as api  # noqa: E402
from tests import native_payload_guard as guard  # noqa: E402
from tests.vacuity import subjects  # noqa: E402

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Patch:
    """Replace ``<module>.<attr>`` for the duration of one mutation.

    ``factory`` receives the original attribute and returns the replacement, so a
    mutation can wrap the real implementation (``lambda orig: ...``) instead of
    reimplementing it. This is how a mutation reaches *inside* the code under the
    guard — which is the only way to express "the mapper itself broke", the case a
    payload-versus-payload comparison could not see (``5160d11``).
    """

    module: str
    attr: str
    factory: Callable[[Any], Any]


@dataclass(frozen=True)
class Mutation:
    """One deliberate corruption a guard must (or must not) notice."""

    name: str
    why: str
    apply: Callable[[Any], Any] = lambda subject: subject
    patches: tuple[Patch, ...] = ()
    #: ``"red"``
    #:     the guard must fail. The default, and the interesting case.
    #: ``"green"``
    #:     the guard must *not* fail. Pins the opposite error: absence of
    #:     evidence must not be turned into a defect.
    #: ``"witness_collapse"``
    #:     the guard may stay green, but its witness must fall below the floor.
    #:     For blindness mutations, where the guard examines nothing and so has
    #:     nothing to complain about — the denominator is the only tell.
    expect: str = "red"


@dataclass(frozen=True)
class GuardCase:
    """A guard, a healthy subject it passes, and the mutations that must break it."""

    guard: str
    why: str
    catches: str
    subject: Callable[[], Any]
    check: Callable[[Any], None]
    mutations: tuple[Mutation, ...]
    #: How much the guard actually examined, e.g. panels walked or queries
    #: compared. The denominator assertion: a guard that inspected nothing passed
    #: because it looked at nothing.
    witness: Callable[[Any], int] | None = None
    #: Floor for ``witness``, either a constant or derived from the subject by an
    #: *independent* route (the IR rather than a payload walker).
    min_witness: int | Callable[[Any], int] = 1
    fails_with: tuple[type[BaseException], ...] = (AssertionError,)


@dataclass(frozen=True)
class EmptyInputGate:
    """A gate that must refuse to report success when it measured nothing."""

    gate: str
    why: str
    catches: str
    #: Run the gate on empty / degenerate input.
    invoke_empty: Callable[[], Any]
    #: Run the same gate on input that should pass, so the entry cannot be
    #: satisfied by a gate that simply always fails.
    invoke_healthy: Callable[[], Any]
    #: Exceptions that count as a refusal. Empty means the gate refuses by
    #: returning a value instead.
    refuses_by_raising: tuple[type[BaseException], ...] = ()
    #: Given the return value, did the gate refuse? Default: a non-zero exit code.
    refused: Callable[[Any], bool] = lambda result: bool(result)
    #: Given the return value on healthy input, did the gate accept?
    accepted: Callable[[Any], bool] = lambda result: not result


@dataclass(frozen=True)
class FiringGuard:
    """A guard whose interesting branch must be seen executing at least once."""

    guard: str
    why: str
    catches: str
    #: ``"corpus"`` — the committed corpus must exercise it; a zero count means
    #: either the branch is dead or the corpus stopped covering it, and both are
    #: worth knowing. ``"path"`` — only a synthetic collision can trigger it, so
    #: the run proves it is *reachable through the production entry point* rather
    #: than only by hand-setting a field.
    flavour: str
    #: Returns how many times the branch executed.
    run: Callable[[], int]
    min_fires: int = 1


# --------------------------------------------------------------------------- #
# Shared subject/corpus plumbing
# --------------------------------------------------------------------------- #


@dataclass
class CorpusSubject:
    """Deep-copied corpus payloads, safe for a mutation to scribble on."""

    entries: tuple[subjects.PayloadSubject, ...]

    @property
    def ir_leaf_total(self) -> int:
        return sum(entry.ir_leaf_count for entry in self.entries)


def _corpus_subject() -> CorpusSubject:
    """Both committed corpora, translated once per session and copied per case."""
    entries = subjects.grafana_corpus() + subjects.datadog_corpus()
    return CorpusSubject(
        entries=tuple(
            subjects.PayloadSubject(
                name=entry.name,
                dashboard_ir=entry.dashboard_ir,
                payload=copy.deepcopy(entry.payload),
                ir_leaf_count=entry.ir_leaf_count,
                ir_query_count=entry.ir_query_count,
            )
            for entry in entries
        )
    )


def _first_leaf_config(payload: dict[str, Any]) -> dict[str, Any]:
    for _section, panel in api.iter_payload_leaf_panels(payload):
        config = panel.get("config")
        if isinstance(config, dict):
            return config
    raise AssertionError("subject payload has no leaf panel to mutate")


def _drop_last_leaf(payload: dict[str, Any]) -> None:
    items = payload.get("panels") or []
    for item in reversed(items):
        if isinstance(item, dict) and "type" in item:
            items.remove(item)
            return
        nested = (item or {}).get("panels")
        if isinstance(nested, list) and nested:
            nested.pop()
            return
    raise AssertionError("subject payload has no leaf panel to drop")


# --------------------------------------------------------------------------- #
# Guard 1/2/3 — the native payload we ship
# --------------------------------------------------------------------------- #


def _payload_subject() -> subjects.PayloadSubject:
    """One Grafana dashboard with sections, multi-layer xy panels and raw ES|QL.

    Rebuilt on every call: a mutation that patches the mapper needs the payload
    constructed *under* the patch, and a cached subject would silently hand it a
    clean one.
    """
    return subjects.grafana_subject(subjects.MULTI_PATTERN)


def _mapper_drops_last_panel(original: Any) -> Any:
    """Make the shared mapper lose one panel on *every* entry point.

    This is the mutation the replaced guard could not survive: it compared
    ``native_dashboard_from_ir(ir)`` against ``build_payload_from_yaml(
    ir.to_yaml_dict())``, and both funnel through ``_native_dashboard_from_parts``
    over the same ``to_yaml_panel_entry()`` list — so a mapper that drops a panel
    drops it identically on both sides and the comparison stays green.
    """

    def patched(*args: Any, **kwargs: Any) -> Any:
        entries = list(kwargs.get("panel_entries") or [])
        if entries:
            kwargs["panel_entries"] = entries[:-1]
        return original(*args, **kwargs)

    return patched


def _fixed_path_query_reader(_original: Any) -> Any:
    """Read queries from ``config.data_source.query`` only.

    The blind-reader failure mode: ``xy`` panels carry one query per layer under
    ``config.layers[*]``, so this finds nothing on them (4 of 8 on the subject
    dashboard) and a guard built on it passes vacuously.
    """

    def patched(payload: dict[str, Any]) -> dict[tuple[str, str], list[str]]:
        index: dict[tuple[str, str], list[str]] = {}
        for section, panel in api.iter_payload_leaf_panels(payload):
            config = panel.get("config") if isinstance(panel.get("config"), dict) else {}
            source = config.get("data_source") if isinstance(config.get("data_source"), dict) else {}
            query = str(source.get("query") or "")
            index[(section, str(config.get("title") or ""))] = [query] if query else []
        return index

    return patched


def _rewrite_a_query(subject: subjects.PayloadSubject) -> subjects.PayloadSubject:
    for _section, panel in api.iter_payload_leaf_panels(subject.payload):
        queries: list[str] = []
        api._collect_esql_queries(panel.get("config") or {}, queries)
        if not queries:
            continue
        config = panel["config"]
        _replace_first_query(config, queries[0], queries[0] + " | LIMIT 1")
        return subject
    raise AssertionError("subject payload has no ES|QL query to rewrite")


def _replace_first_query(node: Any, old: str, new: str) -> bool:
    if isinstance(node, dict):
        source = node.get("data_source")
        if isinstance(source, dict) and source.get("query") == old:
            source["query"] = new
            return True
        for key, child in node.items():
            if key == "data_source":
                continue
            if _replace_first_query(child, old, new):
                return True
    elif isinstance(node, list):
        for child in node:
            if _replace_first_query(child, old, new):
                return True
    return False


def _check_payload_matches_ir(subject: subjects.PayloadSubject) -> None:
    guard.assert_payload_matches_ir(subject.payload, subject.dashboard_ir, label=subject.name)


def _check_dict_shape_bridge(subject: subjects.PayloadSubject) -> None:
    guard.assert_payload_matches_dict_shape_bridge(
        subject.payload,
        subject.dashboard_ir,
        allow_divergent_keys=_BRIDGE_DIVERGENT_KEYS,
        label=subject.name,
    )


# ``tags`` is read straight off the IR by the native path; the dict shape
# declares ``additionalProperties: false`` and cannot carry it. Pinned as a known
# gap rather than ignored (see ``native_dashboard_from_ir``).
_BRIDGE_DIVERGENT_KEYS = frozenset({"tags"})


def _check_no_kibana_rejections(subject: CorpusSubject) -> None:
    total = 0
    for entry in subject.entries:
        examined = guard.assert_payload_has_no_kibana_rejections(
            entry.payload, label=entry.name, min_examined_panels=0
        )
        total += examined["panels"]
    assert total >= 1, "the rejection oracle walked no panels across the whole corpus"


def _kibana_rejection_witness(subject: CorpusSubject) -> int:
    colors = 0
    for entry in subject.entries:
        _findings, examined = guard.payload_kibana_rejections(entry.payload)
        colors += examined["colors"]
    return colors


def _inject_palette(subject: CorpusSubject, color: dict[str, Any]) -> CorpusSubject:
    """Hang *color* where Kibana actually keeps one: on a metric, not on the panel.

    Every one of the 31 colour objects the two corpora emit is nested (Datadog
    metric colours live at ``config.metrics[*].color``); none sits at
    ``config.color``. Injecting at the nested position is what makes the
    blindness mutation below meaningful.
    """
    config = _first_leaf_config(subject.entries[0].payload)
    config["metrics"] = [{"field": "__probe__", "color": color}]
    return subject


def _inject_single_step_palette(subject: CorpusSubject) -> CorpusSubject:
    return _inject_palette(
        subject,
        {"type": "dynamic", "range": "absolute", "steps": [{"gte": 0, "color": "#e7664c"}]},
    )


def _inject_boundaryless_step(subject: CorpusSubject) -> CorpusSubject:
    return _inject_palette(
        subject,
        {
            "type": "dynamic",
            "range": "absolute",
            "steps": [{"gte": 0, "color": "#54b399"}, {"color": "#e7664c"}],
        },
    )


def _blind_colour_walker(_original: Any) -> Any:
    """Look for colours only at ``config.color``.

    Datadog metric colours hang off ``config.metrics[*].color``, so a reader that
    knows one shape examines nothing on them. Exactly the shape of the guard
    ``458f4e2`` added: it walked for ``color`` dicts and asserted none had a
    single step — with no assertion that it had found any.
    """

    def patched(node: Any) -> list[dict[str, Any]]:
        color = node.get("color") if isinstance(node, dict) else None
        return [color] if isinstance(color, dict) else []

    return patched


# --------------------------------------------------------------------------- #
# Guard 4 — the ``lossy`` upload comparison
# --------------------------------------------------------------------------- #


@dataclass
class UploadEchoSubject:
    """Corpus payloads paired with the response body Kibana would echo."""

    pairs: list[tuple[str, dict[str, Any], dict[str, Any]]]
    ir_leaf_total: int = 0


def _upload_echo_subject() -> UploadEchoSubject:
    corpus = _corpus_subject()
    pairs = [
        (entry.name, entry.payload, {"id": "x", "data": {"panels": copy.deepcopy(entry.payload["panels"])}})
        for entry in corpus.entries
    ]
    return UploadEchoSubject(pairs=pairs, ir_leaf_total=corpus.ir_leaf_total)


def _check_no_panel_loss(subject: UploadEchoSubject) -> None:
    for name, payload, body in subject.pairs:
        res = api.UploadResult(dashboard=name)
        api._record_panel_loss(res, payload, body)
        assert res.status != "lossy", f"{name}: {res.message}"


def _loss_witness(subject: UploadEchoSubject) -> int:
    """Leaves the loss comparison actually counted, summed over the corpus."""
    counted = 0
    for _name, payload, _body in subject.pairs:
        counted += len(api._leaf_panel_descriptors(payload.get("panels")))
    return counted


def _echo_drops_one_panel(subject: UploadEchoSubject) -> UploadEchoSubject:
    for _name, _payload, body in subject.pairs:
        panels = body["data"]["panels"]
        if panels:
            _drop_last_leaf(body["data"])
            return subject
    raise AssertionError("no echoed panel to drop")


def _echo_drops_every_panel(subject: UploadEchoSubject) -> UploadEchoSubject:
    for _name, _payload, body in subject.pairs:
        body["data"]["panels"] = []
    return subject


def _echo_omits_panels_entirely(subject: UploadEchoSubject) -> UploadEchoSubject:
    for _name, _payload, body in subject.pairs:
        body["data"].pop("panels", None)
    return subject


# --------------------------------------------------------------------------- #
# Guard 5 — the verifier's T1 tier
# --------------------------------------------------------------------------- #


@dataclass
class ReportCorpus:
    reports: tuple[subjects.ReportSubject, ...]

    @property
    def panel_total(self) -> int:
        return sum(item.panel_count for item in self.reports)


def _report_corpus() -> ReportCorpus:
    with tempfile.TemporaryDirectory() as tmp:
        return ReportCorpus(
            reports=tuple(
                subjects.migration_report(source, Path(tmp))
                for source in subjects.REPORT_SOURCES
            )
        )


def _check_t1_is_populated(subject: ReportCorpus) -> None:
    from verifier.collectors import panels_from_migration_report
    from verifier.compare import compare_panel_record
    from verifier.records import Verdict

    for item in subject.reports:
        records = list(panels_from_migration_report(item.report))
        assert len(records) == item.panel_count, (
            f"{item.source}: collector yielded {len(records)} record(s) for a report "
            f"describing {item.panel_count} panel(s)"
        )
        populated = [record for record in records if record.t1_translator_esql]
        assert len(populated) >= item.panels_with_query, (
            f"{item.source}: T1 was populated for {len(populated)} of "
            f"{item.panels_with_query} panel(s) that carry translator output. An "
            f"empty T1 short-circuits every comparison to SKIP, so the whole "
            f"five-tier gate reports zero drift on every axis no matter what the "
            f"translator emitted."
        )
        for record in populated:
            assert compare_panel_record(record) is not Verdict.SKIP, (
                f"{item.source}: a panel with translator output still verdicts SKIP"
            )


def _t1_witness(subject: ReportCorpus) -> int:
    from verifier.collectors import panels_from_migration_report

    return sum(len(list(panels_from_migration_report(item.report))) for item in subject.reports)


def _collector_reads_only_grafanas_key(original: Any) -> Any:
    """Re-introduce ``07e5829``: read ``esql`` and never ``esql_query``.

    Implemented by hiding the Datadog key from the real collector, which is
    behaviourally identical to the historical ``panel.get("esql")`` and cannot
    drift from it.
    """

    def patched(idx: int, panel: dict[str, Any], dash_uid: str, dash_title: str) -> Any:
        stripped = {key: value for key, value in panel.items() if key != "esql_query"}
        return original(idx, stripped, dash_uid, dash_title)

    return patched


# --------------------------------------------------------------------------- #
# Guard 6 — the bucket-sort idempotence guard
# --------------------------------------------------------------------------- #

_SORT_STAGE = "| SORT "


def _corpus_queries() -> tuple[str, ...]:
    """Every ES|QL query the Grafana corpus emits, plus one raw single-line one.

    The single-line case is the whole point: author-supplied ES|QL arrives as one
    physical line, which is what made a line-based idempotence check dead code.
    """
    queries: list[str] = []
    for entry in subjects.grafana_corpus():
        for value in api.payload_panel_queries(entry.payload).values():
            queries.extend(value)
    queries.append(
        "FROM logs-* | STATS count = COUNT(*) BY time_bucket = "
        "BUCKET(@timestamp, 50, ?_tstart, ?_tend) | SORT time_bucket ASC"
    )
    assert queries, "no ES|QL queries in the Grafana corpus"
    return tuple(queries)


def _adjacent_duplicate_sorts(esql: str) -> int:
    stages = [stage.strip() for stage in esql.split("|") if stage.strip()]
    return sum(
        1
        for before, after in pairwise(stages)
        if before.upper().startswith("SORT ") and before == after
    )


def _check_bucket_sort_is_idempotent(subject: tuple[str, ...]) -> None:
    for query in subject:
        once = grafana_panels._ensure_bucket_sort(query)
        twice = grafana_panels._ensure_bucket_sort(once)
        assert once == twice, (
            "_ensure_bucket_sort is not idempotent; a second pass appended another "
            f"sort:\n  in:  {query!r}\n  once: {once!r}\n  twice: {twice!r}"
        )
        assert not _adjacent_duplicate_sorts(once), (
            f"emitted query carries a duplicated adjacent SORT stage:\n  {once!r}"
        )


def _line_based_sort_guard(_original: Any) -> Any:
    """Re-introduce ``da25a51``: compare the last physical *line*.

    Author-supplied ES|QL is a single line, so the comparison is against the whole
    query and never matches — the guard silently never fires and the sort is
    appended a second time.
    """

    def patched(esql: str, time_field: str) -> bool:
        asc_sort = f"| SORT {time_field} ASC"
        stripped_lines = [line.strip() for line in esql.splitlines() if line.strip()]
        return bool(stripped_lines) and stripped_lines[-1] == asc_sort

    return patched


# --------------------------------------------------------------------------- #
# GUARD_CASES
# --------------------------------------------------------------------------- #

GUARD_CASES: tuple[GuardCase, ...] = (
    GuardCase(
        guard="tests.native_payload_guard.assert_payload_matches_ir",
        why=(
            "native/*.native.json is deployed byte-for-byte by obs-migrate upload, "
            "and this is the only offline check that a panel or a query did not "
            "vanish between the DashboardIR and the wire. Its predecessor compared "
            "the payload against a second construction of itself."
        ),
        catches="5160d11 — tautological comparison",
        subject=_payload_subject,
        check=_check_payload_matches_ir,
        witness=lambda s: sum(len(v) for v in api.payload_panel_queries(s.payload).values()),
        # From the IR, not from a payload walker: a blind walker must not be able
        # to certify its own denominator.
        min_witness=lambda s: s.ir_query_count,
        mutations=(
            Mutation(
                name="rewrite_a_shipped_query",
                why="a query edited on the way out must not pass as the IR's own",
                apply=_rewrite_a_query,
            ),
            Mutation(
                name="drop_a_shipped_panel",
                why="a panel the IR declares but the payload omits is silent data loss",
                apply=lambda s: (_drop_last_leaf(s.payload), s)[1],
            ),
            Mutation(
                name="rename_a_shipped_panel",
                why="a retitled panel breaks every downstream join on title",
                apply=lambda s: (
                    _first_leaf_config(s.payload).__setitem__("title", "__renamed__"),
                    s,
                )[1],
            ),
            Mutation(
                name="the_mapper_itself_drops_a_panel",
                why=(
                    "the mutation the replaced guard survived: both of its sides ran "
                    "the same mapper, so a mapper bug was invisible"
                ),
                patches=(
                    Patch(
                        module="observability_migration.targets.kibana.dashboards_api",
                        attr="_native_dashboard_from_parts",
                        factory=_mapper_drops_last_panel,
                    ),
                ),
            ),
            Mutation(
                name="the_payload_reader_goes_blind",
                why=(
                    "a fixed-path query reader finds nothing on xy panels; the guard "
                    "must notice its own reader has stopped seeing queries"
                ),
                patches=(
                    Patch(
                        module="observability_migration.targets.kibana.dashboards_api",
                        attr="payload_panel_queries",
                        factory=_fixed_path_query_reader,
                    ),
                ),
            ),
        ),
    ),
    GuardCase(
        guard="tests.native_payload_guard.assert_payload_matches_dict_shape_bridge",
        why=(
            "the one place two different call paths through the mapper must agree; it "
            "pins the dashboard-level derivations (stable id, filters, title, "
            "description) that a per-panel check cannot see. Known blind spot, by "
            "construction: it cannot catch a mapper-level panel drop — that is the "
            "IR-anchored guard's job, and the pairing of the two is the point."
        ),
        catches="5160d11 — the surviving half, kept honest",
        subject=_payload_subject,
        check=_check_dict_shape_bridge,
        witness=lambda s: len(s.payload.get("panels") or []),
        mutations=(
            Mutation(
                name="retitle_the_dashboard",
                why="the title is a dashboard-level derivation only this check sees",
                apply=lambda s: (s.payload.__setitem__("title", "__retitled__"), s)[1],
            ),
            Mutation(
                name="drop_the_pinned_controls",
                why=(
                    "controls are a dashboard-level derivation; the per-panel check "
                    "never looks at pinned_panels, so only this comparison sees them go"
                ),
                apply=lambda s: (s.payload.pop("pinned_panels", None), s)[1],
            ),
            Mutation(
                name="describe_the_dashboard_differently",
                why="description is dashboard-level and reaches Kibana verbatim",
                apply=lambda s: (s.payload.__setitem__("description", "__drifted__"), s)[1],
            ),
            Mutation(
                name="add_a_key_only_one_path_can_produce",
                why=(
                    "the two paths must agree on the whole document, not on a "
                    "hand-listed subset of it"
                ),
                apply=lambda s: (s.payload.__setitem__("__unexpected__", 1), s)[1],
            ),
        ),
    ),
    GuardCase(
        guard="tests.native_payload_guard.assert_payload_has_no_kibana_rejections",
        why=(
            "Kibana answers 2xx and silently DROPS a panel whose config it cannot "
            "transform, and the upload path reads only `id` off the response, so this "
            "offline oracle is the only pre-upload notice. A single-step palette cost "
            "6 panels across 4 of 13 Datadog dashboards while every test was green."
        ),
        catches="458f4e2 — a wrong expectation, and the corpus guard that replaced it",
        subject=_corpus_subject,
        check=_check_no_kibana_rejections,
        witness=_kibana_rejection_witness,
        # A floor well under the ~31 colour objects the corpora carry today: corpus
        # churn will not trip it, wholesale blindness will.
        min_witness=10,
        mutations=(
            Mutation(
                name="emit_a_single_step_dynamic_palette",
                why="the exact payload Kibana rejects, and the shape a test used to pin",
                apply=_inject_single_step_palette,
            ),
            Mutation(
                name="emit_a_step_with_no_boundary",
                why="the root form of the same refusal",
                apply=_inject_boundaryless_step,
            ),
            Mutation(
                name="the_colour_walker_goes_blind",
                why=(
                    "reading only config.color examines none of the corpus's 31 colour "
                    "objects, so the guard has nothing to complain about and passes. "
                    "The denominator is the only tell, which is why every guard here "
                    "carries a witness."
                ),
                apply=_inject_single_step_palette,
                expect="witness_collapse",
                patches=(
                    Patch(
                        module="tests.native_payload_guard",
                        attr="_iter_color_objects",
                        factory=_blind_colour_walker,
                    ),
                ),
            ),
        ),
    ),
    GuardCase(
        guard="observability_migration.targets.kibana.dashboards_api._record_panel_loss",
        why=(
            "the only thing between a partial write and '[OK] updated, exit 0'. Kibana "
            "accepts a dashboard with HTTP 200 and keeps only some of its panels; "
            "before this comparison existed a lossy write was indistinguishable from a "
            "clean one."
        ),
        catches="07e5829 — silent success on a lossy upload",
        subject=_upload_echo_subject,
        check=_check_no_panel_loss,
        witness=_loss_witness,
        # The IR's own leaf count: if the descriptor walk goes blind, sent and
        # accepted both collapse to 0, 0 >= 0 holds, and every upload reads clean.
        min_witness=lambda s: s.ir_leaf_total,
        mutations=(
            Mutation(
                name="response_drops_one_panel",
                why="the measured live failure: 35 of 36 panels accepted, exit 0",
                apply=_echo_drops_one_panel,
            ),
            Mutation(
                name="response_drops_every_panel",
                why="total loss must not read as a clean write either",
                apply=_echo_drops_every_panel,
            ),
            Mutation(
                name="response_omits_panels_entirely",
                why=(
                    "absent evidence must never fail an upload: a response echoing no "
                    "panels is unverifiable, not lossy"
                ),
                apply=_echo_omits_panels_entirely,
                expect="green",
            ),
        ),
    ),
    GuardCase(
        guard="verifier.collectors.panels_from_migration_report",
        why=(
            "T1 is the translator's own output, and an empty T1 short-circuits "
            "compare_panel_record to SKIP. A collector that cannot read a source's "
            "report key turns the entire five-tier verifier into 'all SKIP, zero drift "
            "on all five axes' for that source, whatever the translator emitted."
        ),
        catches="07e5829 — empty denominator (Datadog verification was vacuous)",
        subject=_report_corpus,
        check=_check_t1_is_populated,
        witness=_t1_witness,
        min_witness=lambda s: s.panel_total,
        mutations=(
            Mutation(
                name="collector_reads_only_grafanas_key",
                why="the historical defect: Grafana writes esql, Datadog writes esql_query",
                patches=(
                    Patch(
                        module="verifier.collectors",
                        attr="_record_from_report_panel",
                        factory=_collector_reads_only_grafanas_key,
                    ),
                ),
            ),
        ),
    ),
    GuardCase(
        guard="observability_migration.adapters.source.grafana.panels._ensure_bucket_sort",
        why=(
            "21 call sites append a trailing bucket sort through this one choke point, "
            "so its idempotence guard is what stops a doubled '| SORT ... ASC' on "
            "author-supplied raw ES|QL. Cosmetic in ES, but it was the last permanent "
            "known-noise finding on the drift gate, and a gate with permanent noise is "
            "one people learn to ignore."
        ),
        catches="da25a51 — a guard that never executed its interesting branch",
        subject=_corpus_queries,
        check=_check_bucket_sort_is_idempotent,
        witness=len,
        min_witness=100,
        mutations=(
            Mutation(
                name="idempotence_guard_compares_the_last_physical_line",
                why="the historical implementation; single-line ES|QL never matched it",
                patches=(
                    Patch(
                        module="observability_migration.adapters.source.grafana.panels",
                        attr="_already_ends_with_ascending_sort",
                        factory=_line_based_sort_guard,
                    ),
                ),
            ),
        ),
    ),
)


# --------------------------------------------------------------------------- #
# EMPTY_INPUT_GATES
# --------------------------------------------------------------------------- #


def _validate_panel_queries_module() -> Any:
    return _load_script("validate_panel_queries")


def _validate_panels_from_artifacts_module() -> Any:
    return _load_script("validate_panels_from_artifacts")


def _load_script(name: str) -> Any:
    """Load ``scripts/<name>.py`` as a *fresh* module object.

    Fresh rather than via ``sys.modules``, because a gate invocation stubs module
    globals (``collect_panels``, ``es_esql``) and a shared instance would leak
    those into whatever else imports the script. Some of these scripts read
    credentials at import time, so placeholders are set for the duration.
    """
    import importlib.util
    import os

    previous = {key: os.environ.get(key) for key in ("ELASTICSEARCH_ENDPOINT", "KEY")}
    os.environ.setdefault("ELASTICSEARCH_ENDPOINT", "https://vacuity.invalid")
    os.environ.setdefault("KEY", "vacuity-probe-key")
    try:
        spec = importlib.util.spec_from_file_location(
            f"_vacuity_{name}", REPO_ROOT / "scripts" / f"{name}.py"
        )
        assert spec and spec.loader, f"cannot load scripts/{name}.py"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _broken_percentage_empty() -> Any:
    return _validate_panel_queries_module().broken_percentage(0, 0, 0)


def _broken_percentage_healthy() -> Any:
    module = _validate_panel_queries_module()
    return module.broken_percentage(10, 0, 0)


def _panels_from_artifacts_empty() -> Any:
    module = _validate_panels_from_artifacts_module()
    module.collect_panels = lambda: []
    return module.main()


def _panels_from_artifacts_healthy() -> Any:
    """One structurally clean panel, with the ES round-trip stubbed out.

    The gate entry is about the verdict on empty versus non-empty input, so the
    cluster call and the static linter are stubbed: what must be pinned is that a
    gate refusing an empty corpus still accepts a non-empty one.
    """
    module = _validate_panels_from_artifacts_module()
    module.collect_panels = lambda: [
        {
            "kind": "esql",
            "slug": "probe",
            "dashboard": "Vacuity Probe",
            "panel": "CPU",
            "query": "FROM metrics-* | STATS c = COUNT(*)",
            "expected_cols": ["c"],
            "identifier_params": {},
        }
    ]
    module.es_esql = lambda *args, **kwargs: {"ok": True, "columns": ["c"], "row_count": 1}
    module.static_structural_issues = lambda panel: []
    module._E2E_ROOT = tempfile.mkdtemp()
    return module.main()


def _pair_panels_empty() -> Any:
    from verifier import visual_regression as vr

    return vr.pair_panels_by_position([], [])


def _pair_panels_healthy() -> Any:
    from verifier import visual_regression as vr

    left = [{"id": 1, "title": "CPU", "type": "timeseries"}]
    right = [{"id": "u1", "title": "CPU", "type": "lens"}]
    paired, only_left, only_right = vr.pair_panels_by_position(left, right)
    return 0 if (len(paired) == 1 and not only_left and not only_right) else 1


def _artifact_dir_declaring_controls(count: int = 10) -> Path:
    """A native artifact dir whose ``mapping.controls`` declares *count* controls."""
    import json

    root = Path(tempfile.mkdtemp())
    native = root / "native"
    native.mkdir()
    (native / "probe.native.json").write_text(
        json.dumps({"kind": "native_dashboard", "mapping": {"controls": count}}),
        encoding="utf-8",
    )
    return root


def _control_fields_empty() -> Any:
    from observability_migration.core import sample_data

    return sample_data._require_control_fields(
        [_artifact_dir_declaring_controls()], {"metrics-probe": {"control_fields": {}}}
    )


def _control_fields_healthy() -> Any:
    from observability_migration.core import sample_data

    sample_data._require_control_fields(
        [_artifact_dir_declaring_controls()],
        {"metrics-probe": {"control_fields": {"labels.pod": ["a"]}}},
    )
    return 0


def _verifier_vacuous_tiers_empty() -> Any:
    from verifier.cli import vacuous_tier_reason

    return vacuous_tier_reason({"panels": 415, "tiers": {"t1_translator_esql": 0}})


def _verifier_vacuous_tiers_healthy() -> Any:
    from verifier.cli import vacuous_tier_reason

    return vacuous_tier_reason({"panels": 415, "tiers": {"t1_translator_esql": 251}})


EMPTY_INPUT_GATES: tuple[EmptyInputGate, ...] = (
    EmptyInputGate(
        gate="scripts/validate_panel_queries.py::broken_percentage",
        why=(
            "zero validated panels divided out to broken_pct == 0, which is under every "
            "MAX_BROKEN_PCT threshold, so an empty corpus printed VALIDATION PASSED."
        ),
        catches="0c4f3a2 — gate success on a zero denominator",
        invoke_empty=_broken_percentage_empty,
        invoke_healthy=_broken_percentage_healthy,
        refuses_by_raising=(RuntimeError,),
        accepted=lambda result: result == 0.0,
    ),
    EmptyInputGate(
        gate="scripts/validate_panels_from_artifacts.py::main",
        why="returned 0 on '0/0 panels passed', i.e. an all-green summary of nothing.",
        catches="0c4f3a2 — gate success on a zero denominator",
        invoke_empty=_panels_from_artifacts_empty,
        invoke_healthy=_panels_from_artifacts_healthy,
    ),
    EmptyInputGate(
        gate="verifier.visual_regression::pair_panels_by_position",
        why=(
            "the return shape cannot distinguish 'paired perfectly' from 'nothing to "
            "pair': both are ([], [], []). It exited 0 printing captured=0 "
            "median=0.0000 after discovering 0 of 265 panels."
        ),
        catches="0c4f3a2 — gate success on a zero denominator",
        invoke_empty=_pair_panels_empty,
        invoke_healthy=_pair_panels_healthy,
        refuses_by_raising=(RuntimeError,),
    ),
    EmptyInputGate(
        gate="observability_migration.core.sample_data::_require_control_fields",
        why=(
            "streams stay non-empty when only the control-carrying artifact is "
            "unreadable, so the 'no telemetry requirements' guard does not fire and "
            "control_fields silently collapses from 10 to 0. The seeded documents then "
            "match no control selection, which reads as a product bug."
        ),
        catches="0c4f3a2 — a guard that did not fire on its own failure",
        invoke_empty=_control_fields_empty,
        invoke_healthy=_control_fields_healthy,
        refuses_by_raising=(RuntimeError,),
    ),
    EmptyInputGate(
        gate="verifier.cli::vacuous_tier_reason",
        why=(
            "found while building this harness: the five-tier verifier reported "
            "tier_population but still exited 0 when T1 was populated for zero panels "
            "— the precise state 07e5829 fixed the cause of, with nothing left to stop "
            "the next cause."
        ),
        catches="07e5829 — the residual hole its fix left open",
        invoke_empty=_verifier_vacuous_tiers_empty,
        invoke_healthy=_verifier_vacuous_tiers_healthy,
        refused=lambda result: bool(result),
        accepted=lambda result: not result,
    ),
)


# --------------------------------------------------------------------------- #
# FIRING_GUARDS
# --------------------------------------------------------------------------- #


@contextmanager
def _count_matching_results(
    module: Any, attr: str, matched: Callable[[Any, tuple[Any, ...]], bool]
) -> Iterator[list[int]]:
    """Count calls to ``module.attr`` whose ``(result, args)`` match *matched*.

    Counting the real function's own results is what makes this a coverage probe
    rather than a second implementation of the branch condition.
    """
    original = getattr(module, attr)
    fires = [0]

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if matched(result, args):
            fires[0] += 1
        return result

    setattr(module, attr, wrapper)
    try:
        yield fires
    finally:
        setattr(module, attr, original)


def _translate_corpus_under(module: Any, attr: str, matched: Any) -> int:
    """Re-translate the Grafana corpus with ``module.attr`` instrumented."""
    subjects.grafana_corpus.cache_clear()
    try:
        with _count_matching_results(module, attr, matched) as fires:
            subjects.grafana_corpus()
        return fires[0]
    finally:
        # The corpus was built under a patched module; drop it so no other case
        # inherits an instrumented build.
        subjects.grafana_corpus.cache_clear()


def _fires_already_sorted_on_corpus() -> int:
    return _translate_corpus_under(
        grafana_panels,
        "_already_ends_with_ascending_sort",
        lambda result, _args: result is True,
    )


def _fires_sort_append_on_corpus() -> int:
    return _translate_corpus_under(
        grafana_panels,
        "_ensure_bucket_sort",
        lambda result, args: bool(args) and result != args[0],
    )


def _fires_id_disambiguator() -> int:
    """Both adapters' stem allocators must hand back a disambiguation token."""
    from observability_migration.adapters.source.datadog import cli as dd_cli
    from observability_migration.adapters.source.grafana import cli as gf_cli
    from observability_migration.core.assets.dashboard import DashboardIR

    fires = 0
    allocators = (
        lambda used: gf_cli._allocate_dashboard_output_stem(
            title="Shared Title", dashboard_uid="", used_stems=used
        ),
        lambda used: dd_cli._allocate_artifact_stem(
            title="Shared Title", dashboard_id="", used_stems=used
        ),
    )
    for allocate in allocators:
        used: set[str] = set()
        _first_stem, first_token = allocate(used)
        _second_stem, second_token = allocate(used)
        if first_token or not second_token:
            continue
        first_id = api._stable_dashboard_id_from_ir(
            DashboardIR(title="Shared Title", id_disambiguator=first_token)
        )
        second_id = api._stable_dashboard_id_from_ir(
            DashboardIR(title="Shared Title", id_disambiguator=second_token)
        )
        note = api.dashboard_id_disambiguation_note(
            DashboardIR(title="Shared Title", id_disambiguator=second_token)
        )
        if first_id != second_id and note:
            fires += 1
    return fires


def _fires_duplicate_id_ledger() -> int:
    """The batch ledger must reject the second payload landing on one id."""
    from unittest import mock

    from observability_migration.core.assets.native_dashboard import NativeDashboard

    def _dashboard(title: str) -> NativeDashboard:
        native = NativeDashboard(title=title, dashboard_id="obs-migrate-shared-title")
        native.items = [NativePanel(grid=NativeGrid(), type="vis", config={"type": "metric"})]
        return native

    response = mock.Mock(status_code=200)
    response.json.return_value = {"id": "obs-migrate-shared-title"}
    session = mock.Mock()
    session.put.return_value = response
    session.headers = {}

    fires = 0
    ledger: set[str] = set()
    with mock.patch.object(api, "_session", return_value=session):
        for _ in range(2):
            result = api.upload_native_dashboard(
                _dashboard("Shared Title"),
                "http://kibana.invalid",
                dashboard_id="obs-migrate-shared-title",
                seen_dashboard_ids=ledger,
            )
            # The adapter derives success from the status
            # (``status in {"created", "updated"}``), so ``duplicate_id`` already
            # fails the run; what must be true here is that it is named and
            # explained rather than silently upserted.
            if result.status == "duplicate_id" and result.message:
                fires += 1
    return fires


def _fires_vacuous_t1_exit() -> int:
    """``verifier.cli.main`` must actually *act* on a vacuous T1, not just report it.

    A guard computed and printed but never wired to the exit code is vacuous in the
    way that matters: CI reads the exit code. Driven through ``main`` for that
    reason, with no cluster flags so only the offline tiers run.
    """
    import json

    from verifier import cli as verifier_cli

    root = Path(tempfile.mkdtemp())
    (root / "migration_report.json").write_text(
        json.dumps(
            {
                "dashboards": [
                    {
                        "uid": "vacuity-probe",
                        "title": "Vacuity Probe",
                        "panels": [
                            {"title": "CPU", "status": "not_feasible", "grafana_type": "timeseries"}
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    strict = verifier_cli.main(
        ["--migration-out", str(root), "--output", str(root / "report.json")]
    )
    permitted = verifier_cli.main(
        [
            "--migration-out",
            str(root),
            "--output",
            str(root / "report.json"),
            "--allow-empty-t1",
        ]
    )
    return 1 if (strict != 0 and permitted == 0) else 0


def _fires_lossy_status() -> int:
    """A corpus payload whose echo loses one panel must come back ``lossy``."""
    subject = _upload_echo_subject()
    _name, payload, body = subject.pairs[0]
    _drop_last_leaf(body["data"])
    res = api.UploadResult(dashboard="probe")
    api._record_panel_loss(res, payload, body)
    return 1 if res.status == "lossy" and res.dropped_panels else 0


FIRING_GUARDS: tuple[FiringGuard, ...] = (
    FiringGuard(
        guard="grafana.panels._already_ends_with_ascending_sort → True",
        why=(
            "this is the branch that suppresses a duplicate trailing sort. It compared "
            "the last physical line while author-supplied ES|QL is one long line, so it "
            "returned False for every input and the guard was dead code for as long as "
            "it existed. A zero count means it is dead again, or the corpus stopped "
            "carrying a raw-ES|QL panel."
        ),
        catches="da25a51 — dead branch",
        flavour="corpus",
        run=_fires_already_sorted_on_corpus,
    ),
    FiringGuard(
        guard="grafana.panels._ensure_bucket_sort → appended a sort",
        why=(
            "the other side of the same choke point. Without it, an idempotence guard "
            "that always answered 'already sorted' would look perfect: nothing is ever "
            "appended twice because nothing is ever appended."
        ),
        catches="da25a51 — the mutation the idempotence check alone cannot catch",
        flavour="corpus",
        run=_fires_sort_append_on_corpus,
    ),
    FiringGuard(
        guard="dashboard id disambiguation (both source adapters)",
        why=(
            "the Kibana dashboard id is the upsert key, and two dashboards sharing a "
            "title silently upserted onto one id — reported as a routine 'updated', "
            "leaving one dashboard. The corpora have no title collision, so only a "
            "synthetic one proves the token is reachable through the production "
            "allocator instead of only by hand-setting id_disambiguator."
        ),
        catches="da25a51 — a dedup that existed only in a removed code path",
        flavour="path",
        run=_fires_id_disambiguator,
        min_fires=2,
    ),
    FiringGuard(
        guard="dashboards_api batch id ledger → duplicate_id",
        why=(
            "the last line of defence behind unique id derivation. It only fires if the "
            "caller passes the ledger, which is exactly what regressed before: the "
            "seen_ids dedup lived solely in the upload path that was deleted."
        ),
        catches="da25a51 — silent overwrite reported as success",
        flavour="path",
        run=_fires_duplicate_id_ledger,
    ),
    FiringGuard(
        guard="verifier.cli.main → non-zero on a vacuous T1",
        why=(
            "the verifier already *reported* tier_population and still exited 0 when "
            "T1 was populated for zero panels, which is the state where every verdict "
            "is SKIP and every drift axis a vacuous 0. A guard that computes a reason "
            "but never reaches the exit code is vacuous in the way that matters, since "
            "CI reads the exit code. Also pins that --allow-empty-t1 really opts out."
        ),
        catches="07e5829 — the residual hole its fix left open",
        flavour="path",
        run=_fires_vacuous_t1_exit,
    ),
    FiringGuard(
        guard="dashboards_api._record_panel_loss → lossy",
        why=(
            "no corpus upload is lossy (353/353 panels round-trip), so nothing proves "
            "the terminal status is reachable except a poisoned response. It names the "
            "dropped panel by section+title+grid, and that identification runs only "
            "inside this branch."
        ),
        catches="07e5829 — silent success on a lossy upload",
        flavour="path",
        run=_fires_lossy_status,
    ),
)


def resolve_module(dotted: str) -> Any:
    """Import ``dotted`` for a :class:`Patch`."""
    return importlib.import_module(dotted)


__all__ = [
    "EMPTY_INPUT_GATES",
    "FIRING_GUARDS",
    "GUARD_CASES",
    "EmptyInputGate",
    "FiringGuard",
    "GuardCase",
    "Mutation",
    "Patch",
    "resolve_module",
]
