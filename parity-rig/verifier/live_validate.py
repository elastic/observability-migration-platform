"""Live ES|QL execution oracle (Layer 5).

The compiler (`kb-dashboard-cli`) and its lint are heuristic; Elasticsearch is
the *real* ES|QL parser and executor. This module executes each emitted panel
query against a live cluster and classifies the response so the signal that
matters - "the translator emitted ES|QL that Elasticsearch rejects" - is
separated from mere "the data isn't seeded yet".

Classification (deterministic, unit-tested):

* ``ok``        - HTTP 200; the query is valid ES|QL.
* ``real_bug``  - a parsing / type / argument / function error: the emitted
                  ES|QL is itself wrong (e.g. one-arg ``PERCENTILE(...)``,
                  issue #213). These are genuine translator bugs.
* ``data_gap``  - unknown column / unknown index / no matching indices: the
                  query is well-formed but the target telemetry is not present.
                  Not a translator bug (see ``core/verification/disposition``).
* ``other``     - anything else (5xx, transport, timeouts).

The executor is injectable so the classifier and driver are testable with no
cluster. The default executor is ``collectors.run_cluster_query``.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Reuse the package's dashboard-control-param binding when importable so the
# live oracle agrees with the uploaded-dashboard smoke path; otherwise the
# verifier stays runnable standalone and simply binds no extra params.
try:  # pragma: no cover - exercised implicitly when the package is installed
    from observability_migration.adapters.source.grafana.esql_validate import (
        _validation_params_for_query,
    )
except Exception:  # pragma: no cover
    _validation_params_for_query = None

# A runner takes (es_url, api_key, esql) and returns (status_code, body).
QueryRunner = Callable[[str, str, str], "tuple[int, dict[str, Any] | str]"]

# Well-formed query, telemetry simply absent. NOT a translation bug.
_DATA_GAP = re.compile(
    r"Unknown column|Unknown index|index_not_found|no such index|"
    r"resolved to no indices|unknown field",
    re.IGNORECASE,
)
# The emitted ES|QL is itself invalid. REAL translator bug.
_REAL_BUG = re.compile(
    r"parsing_exception|ParsingException|mismatched input|no viable alternative|"
    r"expects exactly|expects \w+ argument|expected .* argument|"
    r"error building \[|unknown function|Unknown function|"
    r"is not an aggregate function|cannot be used in|"
    r"argument of \[.*?\] must be|first argument of \[|second argument of \[|"
    r"line \d+:\d+:",
    re.IGNORECASE,
)


@dataclass
class QueryResult:
    panel_title: str
    dashboard: str
    query: str
    status: int
    classification: str  # ok | real_bug | data_gap | other
    error: str = ""
    columns: list[str] = field(default_factory=list)
    row_count: int = 0

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "panel_title": self.panel_title,
            "dashboard": self.dashboard,
            "query": self.query,
            "status": self.status,
            "classification": self.classification,
            "error": self.error,
            "columns": list(self.columns),
            "row_count": self.row_count,
        }


def classify_error(text: str) -> str:
    """Classify an Elasticsearch error body string.

    Data-gap signals win over real-bug signals when both appear: an unknown
    column is the dominant explanation and must not be mistaken for a bug.
    """
    if not text:
        return "other"
    if _DATA_GAP.search(text):
        return "data_gap"
    if _REAL_BUG.search(text):
        return "real_bug"
    return "other"


def validate_query(
    es_url: str,
    api_key: str,
    query: str,
    *,
    panel_title: str = "",
    dashboard: str = "",
    runner: QueryRunner | None = None,
) -> QueryResult:
    """Execute one query and classify the outcome."""
    if runner is None:
        from . import collectors

        # Bind dashboard control params (``?job``, ``RLIKE ?var`` -> ``.*``,
        # arithmetic params -> ``0``, etc.) the same way the uploaded-dashboard
        # smoke path does, so a valid migrated query referencing control params
        # is not mis-executed (and mis-classified as ``real_bug``) just because
        # the named parameters resolve to empty strings.
        params = _validation_params_for_query(query) if _validation_params_for_query else None

        def runner(es, key, q):
            return collectors.run_cluster_query(es, key, q, params=params or None)

    status, body = runner(es_url, api_key, query)
    if status == 200 and isinstance(body, dict):
        columns = [c.get("name", "") for c in (body.get("columns") or [])]
        return QueryResult(
            panel_title, dashboard, query, status, "ok",
            columns=columns, row_count=len(body.get("values") or []),
        )
    text = body if isinstance(body, str) else json.dumps(body)
    return QueryResult(
        panel_title, dashboard, query, status, classify_error(text), error=text[:1000],
    )


def validate_queries(
    es_url: str,
    api_key: str,
    items: Iterable[tuple[str, str, str]],
    *,
    runner: QueryRunner | None = None,
    dedup: bool = True,
) -> list[QueryResult]:
    """Validate ``(dashboard, panel_title, query)`` triples."""
    seen: set[str] = set()
    out: list[QueryResult] = []
    for dashboard, panel_title, query in items:
        q = (query or "").strip()
        if not q:
            continue
        if dedup and q in seen:
            continue
        seen.add(q)
        out.append(
            validate_query(es_url, api_key, q, panel_title=panel_title, dashboard=dashboard, runner=runner)
        )
    return out


def summarize(results: list[QueryResult]) -> dict[str, Any]:
    counts = {"ok": 0, "real_bug": 0, "data_gap": 0, "other": 0}
    for r in results:
        counts[r.classification] = counts.get(r.classification, 0) + 1
    return {"total": len(results), "by_classification": counts, "real_bugs": counts["real_bug"]}


# --------------------------------------------------------------------- #
# Extracting emitted queries from migration artifacts
# --------------------------------------------------------------------- #


def queries_from_report(report: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Yield ``(dashboard, panel_title, emitted_query)`` from migration_report.json.

    Prefer the YAML-emitted query (visual_ir.presentation.config.query, which
    includes composite-legend EVALs etc.) over the bare translator esql.
    """
    items: list[tuple[str, str, str]] = []
    for dash in report.get("dashboards", []):
        dtitle = str(dash.get("title") or "")
        for panel in dash.get("panels", []):
            if not isinstance(panel, dict):
                continue
            title = str(panel.get("title") or "")
            query = ""
            vir = panel.get("visual_ir") if isinstance(panel.get("visual_ir"), dict) else {}
            pres = vir.get("presentation") if isinstance(vir, dict) else {}
            pres = pres if isinstance(pres, dict) else {}
            cfg = pres.get("config") if isinstance(pres.get("config"), dict) else {}
            if pres.get("kind") == "esql":
                query = str(cfg.get("query") or "")
            if not query:
                query = str(panel.get("esql_query") or panel.get("esql") or "")
            if query.strip():
                items.append((dtitle, title, query))
    return items


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="verifier.live_validate",
        description="Execute emitted ES|QL against a live cluster and classify errors (Layer 5).",
    )
    p.add_argument("--migration-out", type=Path, required=True,
                   help="Migration output dir containing migration_report.json.")
    p.add_argument("--es-url", type=str, required=True)
    p.add_argument("--api-key", type=str, required=True)
    p.add_argument("--output", type=Path, help="Write the full JSON report here.")
    p.add_argument("--fail-on-bug", action="store_true",
                   help="Exit non-zero if any real_bug is found.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    report = json.loads((args.migration_out / "migration_report.json").read_text())
    items = queries_from_report(report)
    results = validate_queries(args.es_url, args.api_key, items)
    summary = summarize(results)
    payload = {"summary": summary, "results": [r.to_jsonable() for r in results]}
    if args.output:
        args.output.write_text(json.dumps(payload, indent=2))
    print(f"live ES|QL validation: {summary['total']} queries | "
          f"ok={summary['by_classification']['ok']} "
          f"data_gap={summary['by_classification']['data_gap']} "
          f"REAL_BUGS={summary['real_bugs']} "
          f"other={summary['by_classification']['other']}")
    for r in results:
        if r.classification == "real_bug":
            print(f"\n[REAL BUG] {r.dashboard} :: {r.panel_title}\n  {r.query[:240]}\n  {r.error[:300]}")
    return 1 if (args.fail_on_bug and summary["real_bugs"]) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "QueryResult",
    "QueryRunner",
    "classify_error",
    "queries_from_report",
    "summarize",
    "validate_queries",
    "validate_query",
]
