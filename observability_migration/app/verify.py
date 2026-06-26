# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Package-native ``obs-migrate verify`` orchestrator.

A thin aggregator that runs the *package-native* correctness gates over an
already-migrated dashboard artifact directory and prints ONE consolidated
scorecard:

1. **Emitted-query acceptance gate** -- read each panel's emitted ES|QL from
   ``verification_packets.json`` (``packets[].translated_query``) and/or
   ``migration_report.json`` (``dashboards[].panels[].esql_query``), dedupe
   identical queries, run each against Elasticsearch through
   :func:`observability_migration.adapters.source.grafana.esql_validate.validate_esql`,
   and classify each result as ``ok`` / ``real_bug`` / ``data_gap`` / ``other``.
   This mirrors ``parity-rig/verifier/live_validate.py`` but uses ONLY
   package-native code (no ``parity-rig`` import).

2. **Numeric parity gate (optional)** -- invoke the existing ``obs-migrate
   compare`` implementation *in-process* over the same artifact dir and surface
   its STRICT/FUZZY/SHAPE/FAIL/ERROR counts. Compare is never re-implemented
   here; the verify CLI calls the real ``_run_compare`` via an injected runner.

3. **Coverage honesty** -- explicitly lists the deeper gates this command does
   NOT run (Kibana typed-contract ``dashboards_api`` and the browser render
   audit, which live in ``parity-rig/`` and are not importable from the
   installed package) together with the exact commands to run them.

The core logic is unit-testable without a live cluster: the ES|QL validator and
the compare runner are both injectable seams (mirroring
``live_validate.validate_query(..., runner=...)``).

Exit codes (see :func:`exit_code_for`):

* ``2`` -- cluster unreachable / bad inputs (missing artifact dir, missing
  creds, no emitted queries).
* ``1`` -- any ``real_bug`` in the acceptance gate, or a compare ``FAIL`` /
  ``ERROR``.
* ``0`` -- otherwise (``data_gap`` / ``other`` are warnings, not failures).

The command is read-only on the cluster: it only POSTs ``_query`` (validation)
and, when compare is enabled, the read-only native-PROMQL/ES|QL parity probes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from observability_migration.adapters.source.grafana.esql_validate import validate_esql

# A validator runs one ES|QL query and returns ``(ok, error)`` exactly like
# ``esql_validate.validate_esql``: ``ok`` is ``True`` (accepted), ``False``
# (HTTP error response), or ``None`` (transport failure / unreachable).
Validator = Callable[..., "tuple[bool | None, str]"]

# A compare runner returns the structured compare result dict. The real CLI
# wires this to ``cli._run_compare``-derived logic; tests inject a stub.
CompareRunner = Callable[..., "dict[str, Any]"]


# --------------------------------------------------------------------- #
# Error classification (package-native; mirrors live_validate semantics)
# --------------------------------------------------------------------- #

# Well-formed query, telemetry simply absent. NOT a translation bug. Data-gap
# signals win over real-bug signals when both appear (an unknown column is the
# dominant explanation and must not be mistaken for a bug).
_DATA_GAP = re.compile(
    r"Unknown column|Unknown index|index_not_found|no such index|"
    r"resolved to no indices|unknown field|no indices",
    re.IGNORECASE,
)
# The emitted ES|QL is itself invalid: a genuine parse / type / argument /
# function error. REAL translator bug.
_REAL_BUG = re.compile(
    r"parsing_exception|ParsingException|mismatched input|no viable alternative|"
    r"expects exactly|expects \w+ argument|expected .* argument|"
    r"error building \[|unknown function|Unknown function|"
    r"is not an aggregate function|cannot be used in|"
    r"argument of \[.*?\] must be|first argument of \[|second argument of \[|"
    r"verification_exception|line \d+:\d+:",
    re.IGNORECASE,
)

# Acceptance-gate classification buckets, in stable display order.
_BUCKETS = ("ok", "real_bug", "data_gap", "other", "unreachable")


def classify_validation(ok: bool | None, error: str) -> str:
    """Classify one ``validate_esql`` ``(ok, error)`` result.

    * ``ok is True``  -> ``"ok"`` (the query is valid ES|QL).
    * ``ok is None``  -> ``"unreachable"`` (transport error / no cluster).
    * Data-gap error  -> ``"data_gap"`` (well-formed query, telemetry absent).
    * Real-bug error  -> ``"real_bug"`` (the emitted ES|QL is itself wrong).
    * anything else   -> ``"other"`` (5xx, timeouts, unrecognized).
    """
    if ok is True:
        return "ok"
    if ok is None:
        return "unreachable"
    text = error or ""
    if not text:
        return "other"
    if _DATA_GAP.search(text):
        return "data_gap"
    if _REAL_BUG.search(text):
        return "real_bug"
    return "other"


# --------------------------------------------------------------------- #
# Extracting emitted queries from migration artifacts
# --------------------------------------------------------------------- #


def _queries_from_packets(path: Path) -> list[tuple[str, str, str]]:
    """Yield ``(dashboard, panel, translated_query)`` from verification_packets.json."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    out: list[tuple[str, str, str]] = []
    for pkt in data.get("packets") or []:
        if not isinstance(pkt, dict):
            continue
        query = str(pkt.get("translated_query") or "")
        if query.strip():
            out.append((str(pkt.get("dashboard") or ""), str(pkt.get("panel") or ""), query))
    return out


def _queries_from_report(path: Path) -> list[tuple[str, str, str]]:
    """Yield ``(dashboard, panel, esql)`` from migration_report.json."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    out: list[tuple[str, str, str]] = []
    for dash in data.get("dashboards") or []:
        if not isinstance(dash, dict):
            continue
        dtitle = str(dash.get("title") or "")
        for panel in dash.get("panels") or []:
            if not isinstance(panel, dict):
                continue
            query = str(panel.get("esql_query") or panel.get("esql") or "")
            if query.strip():
                out.append((dtitle, str(panel.get("title") or ""), query))
    return out


def collect_emitted_queries(artifact_dir: Path) -> list[tuple[str, str, str]]:
    """Collect emitted ES|QL from an artifact dir's packets and/or report.

    Reads ``verification_packets.json`` (preferred -- carries the exact
    ``translated_query`` used for parity) and ``migration_report.json``
    (``esql_query`` fallback). The acceptance gate dedups identical query text
    at run time, so a query present in both files is probed only once.
    """
    artifact_dir = Path(artifact_dir)
    return [
        *_queries_from_packets(artifact_dir / "verification_packets.json"),
        *_queries_from_report(artifact_dir / "migration_report.json"),
    ]


# --------------------------------------------------------------------- #
# Reachability preflight
# --------------------------------------------------------------------- #


def cluster_reachable(
    es_url: str,
    api_key: str,
    *,
    index: str = "metrics-*",
    validator: Validator = validate_esql,
) -> bool:
    """Probe the cluster with a trivial, data-free query (read-only).

    ``ROW`` needs no index or data, so a transport error (``ok is None``) means
    the cluster is unreachable. An HTTP error response (``ok is False``) still
    proves we reached Elasticsearch, so that counts as reachable.
    """
    ok, _ = validator("ROW _probe = 1", es_url, index_pattern=index, es_api_key=api_key)
    return ok is not None


# --------------------------------------------------------------------- #
# Gate 1: emitted-query acceptance
# --------------------------------------------------------------------- #


def run_acceptance_gate(
    items: list[tuple[str, str, str]],
    *,
    es_url: str,
    api_key: str,
    index: str = "metrics-*",
    validator: Validator = validate_esql,
) -> dict[str, Any]:
    """Run each unique emitted query through ``validate_esql`` and classify it."""
    counts = {bucket: 0 for bucket in _BUCKETS}
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for dashboard, panel, query in items:
        q = (query or "").strip()
        if not q or q in seen:
            continue
        seen.add(q)
        ok, error = validator(q, es_url, index_pattern=index, es_api_key=api_key)
        classification = classify_validation(ok, error)
        counts[classification] += 1
        results.append(
            {
                "dashboard": dashboard,
                "panel": panel,
                "classification": classification,
                "error": (error or "")[:500],
                "query": q,
            }
        )
    return {
        "total": len(results),
        "counts": counts,
        "results": results,
        "unreachable": counts["unreachable"] > 0,
    }


# --------------------------------------------------------------------- #
# Coverage honesty
# --------------------------------------------------------------------- #


def uncovered_gates() -> list[dict[str, str]]:
    """List the deeper correctness gates this command does NOT run.

    These live in ``parity-rig/`` and are repo-only (not importable from the
    installed package), so ``verify`` is intentionally not exhaustive. Each
    entry carries the exact command to run that gate.
    """
    return [
        {
            "gate": "verifier.dashboards_api (Kibana typed UI-contract validation)",
            "why_not_run": (
                "Validates the compiled saved object against Kibana's typed "
                "dashboard/Lens contract (accessor wiring, column refs). Lives in "
                "parity-rig/ and is not importable from the installed package."
            ),
            "command": (
                "python -m parity-rig/verifier/dashboards_api "
                "--migration-out <output-dir> --kibana-url <...> --api-key <...>"
            ),
        },
        {
            "gate": "render audit (browser per-panel render gate)",
            "why_not_run": (
                "The ONLY gate that catches Lens accessor / 'invalid column' / "
                "empty-state render failures that ES|QL execution and the schema "
                "gate miss. Requires a browser session against an uploaded "
                "dashboard; lives in parity-rig/render_audit_driver."
            ),
            "command": (
                "python parity-rig/render_audit_driver.py "
                "--kibana-url <...> --api-key <...> --dashboard-id <...>"
            ),
        },
        {
            "gate": "verifier.live_validate / verify-panels (5-tier panel verifier)",
            "why_not_run": (
                "The full source-PromQL -> translator -> YAML -> NDJSON -> "
                "cluster -> live _query tier ladder. obs-migrate verify covers "
                "the package-native cluster acceptance tier only; the deeper "
                "tiers and invariant linter live in parity-rig/."
            ),
            "command": (
                "obs-migrate verify-panels --migration-out <output-dir> "
                "--es-url <...> --api-key <...> --output <report.json>   "
                "# (delegates to parity-rig/verifier)"
            ),
        },
    ]


# --------------------------------------------------------------------- #
# Report assembly + verdict + exit code
# --------------------------------------------------------------------- #


def _compare_fail_count(compare: dict[str, Any] | None) -> int:
    if not compare or not compare.get("ran"):
        return 0
    summary = compare.get("summary") or {}
    return int(summary.get("FAIL", 0)) + int(summary.get("ERROR", 0)) + int(
        summary.get("SOURCE_FAIL", 0)
    )


def build_report(
    *,
    acceptance: dict[str, Any],
    compare: dict[str, Any] | None,
    artifact_dir: str = "",
) -> dict[str, Any]:
    """Assemble the unified report dict with a verdict and coverage section."""
    counts = acceptance.get("counts") or {}
    real_bugs = int(counts.get("real_bug", 0))
    data_gaps = int(counts.get("data_gap", 0))
    others = int(counts.get("other", 0))
    unreachable = bool(acceptance.get("unreachable"))
    compare_fails = _compare_fail_count(compare)

    if unreachable:
        verdict = "UNREACHABLE"
    elif real_bugs or compare_fails:
        # Genuine failure signal, but we still surface the full scorecard;
        # call it ATTENTION (exit code carries the hard fail).
        verdict = "ATTENTION"
    elif data_gaps or others:
        verdict = "ATTENTION"
    else:
        verdict = "PASS"

    return {
        "artifact_dir": artifact_dir,
        "verdict": verdict,
        "acceptance": acceptance,
        "compare": compare,
        "not_run_gates": uncovered_gates(),
        "summary": {
            "emitted_queries": acceptance.get("total", 0),
            "real_bugs": real_bugs,
            "data_gaps": data_gaps,
            "other_errors": others,
            "compare_failures": compare_fails,
            "unreachable": unreachable,
        },
    }


def exit_code_for(report: dict[str, Any]) -> int:
    """Map a report to a process exit code (2 unreachable, 1 fail, 0 clean)."""
    summary = report.get("summary") or {}
    if summary.get("unreachable") or report.get("verdict") == "UNREACHABLE":
        return 2
    if int(summary.get("real_bugs", 0)) or int(summary.get("compare_failures", 0)):
        return 1
    return 0


# --------------------------------------------------------------------- #
# Human-readable scorecard
# --------------------------------------------------------------------- #


def render_scorecard(report: dict[str, Any]) -> str:
    """Render ONE consolidated, human-readable scorecard."""
    acc = report.get("acceptance") or {}
    counts = acc.get("counts") or {}
    compare = report.get("compare")
    summary = report.get("summary") or {}
    lines: list[str] = []
    lines.append("=" * 66)
    lines.append("  obs-migrate verify -- package-native correctness scorecard")
    lines.append("=" * 66)
    if report.get("artifact_dir"):
        lines.append(f"  Artifact dir: {report['artifact_dir']}")
    lines.append("")

    # Gate 1
    lines.append("  [Gate 1] Emitted-query acceptance (live ES|QL execution)")
    lines.append(f"    queries probed (deduped): {acc.get('total', 0)}")
    lines.append(f"      ok        : {counts.get('ok', 0)}")
    lines.append(f"      real_bug  : {counts.get('real_bug', 0)}   (translator bug -> FAIL)")
    lines.append(f"      data_gap  : {counts.get('data_gap', 0)}   (telemetry absent -> warn)")
    lines.append(f"      other     : {counts.get('other', 0)}   (5xx/timeout/unclassified -> warn)")
    if counts.get("unreachable"):
        lines.append(f"      unreachable: {counts.get('unreachable', 0)}   (transport error)")
    bugs = [r for r in (acc.get("results") or []) if r.get("classification") == "real_bug"]
    if bugs:
        lines.append("    real_bug detail:")
        for b in bugs[:10]:
            lines.append(f"      - {b.get('dashboard','')} :: {b.get('panel','')}")
            lines.append(f"          {(b.get('error') or '')[:160]}")
    lines.append("")

    # Gate 2
    lines.append("  [Gate 2] Numeric parity (obs-migrate compare, native PromQL oracle)")
    if compare is None:
        lines.append("    not requested (pass --compare or seed data + --compare).")
    elif not compare.get("ran"):
        lines.append(f"    did not run: {compare.get('reason', 'unavailable')}")
    else:
        csum = compare.get("summary") or {}
        ordered = ["STRICT_PASS", "FUZZY_PASS", "SHAPE_PASS", "SKIP",
                   "STRUCTURAL", "FAIL", "ERROR", "SOURCE_FAIL"]
        lines.append(f"    panels compared: {csum.get('panels', 0)}")
        for verdict in ordered:
            if verdict in csum:
                lines.append(f"      {verdict:<12}: {csum[verdict]}")
        # Surface any verdicts not in the canonical order list.
        for verdict, n in csum.items():
            if verdict not in (*ordered, "panels"):
                lines.append(f"      {verdict:<12}: {n}")
    lines.append("")

    # Coverage honesty
    lines.append("  [Coverage] Gates NOT run by this command (run them separately):")
    for g in report.get("not_run_gates") or []:
        lines.append(f"    - {g['gate']}")
        lines.append(f"        run: {g['command']}")
    lines.append("")

    # Verdict
    lines.append("-" * 66)
    verdict = report.get("verdict", "")
    fail_bits = []
    if summary.get("real_bugs"):
        fail_bits.append(f"{summary['real_bugs']} real_bug")
    if summary.get("compare_failures"):
        fail_bits.append(f"{summary['compare_failures']} compare FAIL/ERROR")
    tail = f"  ({', '.join(fail_bits)})" if fail_bits else ""
    lines.append(f"  VERDICT: {verdict}{tail}")
    lines.append("-" * 66)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------- #


def run_verify(
    *,
    artifact_dir: str,
    es_url: str,
    api_key: str,
    index: str = "metrics-*",
    report_out: str | None = "verify_report.json",
    run_compare: bool = False,
    validator: Validator = validate_esql,
    compare_runner: CompareRunner | None = None,
) -> int:
    """Run the verify orchestrator end-to-end and return a process exit code.

    ``validator`` and ``compare_runner`` are injectable seams so the whole flow
    is testable without a cluster. The default ``validator`` is the real
    ``esql_validate.validate_esql``; ``compare_runner`` is wired by the CLI to
    the in-process compare implementation.
    """
    art = Path(artifact_dir)
    if not art.is_dir():
        print(json.dumps({"error": "missing_artifact_dir", "path": str(art)}, indent=2))
        return 2
    if not es_url or not api_key:
        print(
            json.dumps(
                {"error": "es_url and api_key are required (or set ELASTICSEARCH_ENDPOINT/KEY)"},
                indent=2,
            )
        )
        return 2

    items = collect_emitted_queries(art)
    if not items:
        print(
            json.dumps(
                {
                    "error": "no_emitted_queries",
                    "detail": (
                        "no translated_query in verification_packets.json nor "
                        "esql_query in migration_report.json under this artifact dir"
                    ),
                    "path": str(art),
                },
                indent=2,
            )
        )
        return 2

    # Reachability preflight before spending the full query sweep.
    if not cluster_reachable(es_url, api_key, index=index, validator=validator):
        print(json.dumps({"error": "es_unreachable", "es_url": es_url}, indent=2))
        return 2

    acceptance = run_acceptance_gate(
        items, es_url=es_url, api_key=api_key, index=index, validator=validator
    )
    if acceptance.get("unreachable"):
        # The cluster dropped mid-sweep; treat as unreachable (exit 2).
        report = build_report(acceptance=acceptance, compare=None, artifact_dir=str(art))
        print(render_scorecard(report))
        _maybe_write(report, report_out)
        return 2

    compare: dict[str, Any] | None = None
    if run_compare and compare_runner is not None:
        compare = compare_runner(
            artifact_dir=str(art), es_url=es_url, api_key=api_key, index=index
        )

    report = build_report(acceptance=acceptance, compare=compare, artifact_dir=str(art))
    print(render_scorecard(report))
    _maybe_write(report, report_out)
    return exit_code_for(report)


def _maybe_write(report: dict[str, Any], report_out: str | None) -> None:
    if not report_out:
        return
    out = Path(report_out)
    if out.parent != Path("") and not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"verify report written: {out}")


__all__ = [
    "build_report",
    "classify_validation",
    "cluster_reachable",
    "collect_emitted_queries",
    "exit_code_for",
    "render_scorecard",
    "run_acceptance_gate",
    "run_verify",
    "uncovered_gates",
]
