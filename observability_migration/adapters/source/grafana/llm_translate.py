"""LLM-based fallback for PromQL → ES|QL translation.

When the rule engine marks a panel as ``not_feasible``, this module
can attempt a translation using a local or remote LLM (Ollama, OpenAI-
compatible endpoint).  Inspired by Elastic's NL-to-ESQL inference task
(kibana#190433), the approach feeds the LLM ES|QL reference context
alongside the PromQL expression and asks for a structured translation.
"""

from __future__ import annotations

import logging
from typing import Any

from .local_ai import request_structured_json

logger = logging.getLogger(__name__)

ESQL_REFERENCE = """\
ES|QL (Elasticsearch Query Language) uses a pipe syntax.  Key constructs:

## Source commands
- `TS <index>` — Time-series source (for TSDB metrics).
- `FROM <index>` — Standard document source.

## Processing commands
- `| WHERE <condition>` — Filter rows.
- `| STATS <agg> BY <fields>` — Aggregate. Aggregations: SUM, AVG, MIN, MAX, COUNT, COUNT_DISTINCT, PERCENTILE, MEDIAN, STD_DEV.
- `| EVAL <field> = <expr>` — Computed columns.
- `| KEEP <fields>` — Select columns.
- `| SORT <field> ASC|DESC` — Order rows.
- `| LIMIT <n>` — Cap row count.

## Time-series aggregations (TS source only)
- `RATE(<counter_field>, <window>)` — Per-second rate of a counter.
- `IRATE(<counter_field>, <window>)` — Instant rate.
- `INCREASE(<counter_field>, <window>)` — Total increase.
- `TBUCKET(<interval>)` — Time bucketing function for BY clause.

## Common patterns
- Rate query: `TS metrics-* | STATS avg_rate = AVG(RATE(http_requests_total, 5m)) BY time_bucket = TBUCKET(5 minute), instance | SORT time_bucket ASC`
- Gauge query: `TS metrics-* | STATS avg_val = AVG(node_memory_MemFree_bytes) BY time_bucket = TBUCKET(5 minute) | SORT time_bucket ASC`
- Counter sum: `TS metrics-* | STATS total = SUM(INCREASE(errors_total, 5m)) BY time_bucket = TBUCKET(5 minute), code | SORT time_bucket ASC`

## Rules
- Counter metrics (ending in _total, _count, _sum, _bucket, _created) MUST use RATE/IRATE/INCREASE inside STATS.
- Gauge metrics use raw aggregation: AVG, MIN, MAX, SUM directly.
- Always include `| SORT time_bucket ASC` for time-series charts.
- The `BY` clause must include `time_bucket = TBUCKET(<interval>)` for XY charts.
- Use `| WHERE TRANGE()` for time filtering with TS source.
"""

SYSTEM_PROMPT = f"""\
You are an expert at translating Prometheus PromQL queries into Elasticsearch ES|QL queries.

{ESQL_REFERENCE}

Given a PromQL expression and context, produce an equivalent ES|QL query.
Return a JSON object with these fields:
- "esql_query": The complete ES|QL query string.
- "metric_name": The primary metric referenced.
- "source_type": Either "TS" or "FROM".
- "confidence": A number 0.0-1.0 for how confident you are in the translation.
- "warnings": An array of strings noting any semantic gaps or approximations.

If the expression truly cannot be translated to ES|QL, set "esql_query" to ""
and explain why in "warnings".
"""


def attempt_llm_translation(
    promql_expr: str,
    index: str = "metrics-*",
    panel_type: str = "",
    endpoint: str = "",
    model: str = "",
    api_key: str = "",
    timeout: int = 30,
    extra_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Ask an LLM to translate a PromQL expression to ES|QL.

    Returns a dict with ``esql_query``, ``metric_name``, ``source_type``,
    ``confidence``, ``warnings`` on success, or *None* on failure.
    """
    if not endpoint or not model:
        return None

    payload: dict[str, Any] = {
        "promql": promql_expr,
        "target_index": index,
        "panel_type": panel_type,
    }
    if extra_context:
        payload["context"] = extra_context

    try:
        result = request_structured_json(
            payload=payload,
            endpoint=endpoint,
            model=model,
            system_prompt=SYSTEM_PROMPT,
            api_key=api_key,
            timeout=timeout,
            max_tokens=800,
        )
    except Exception as exc:
        logger.debug("LLM translation request failed: %s", exc)
        return None

    esql = str(result.get("esql_query") or "").strip()
    if not esql:
        return None

    if not _looks_like_esql(esql):
        logger.debug("LLM returned invalid ES|QL: %s", esql[:120])
        return None

    return {
        "esql_query": esql,
        "metric_name": str(result.get("metric_name") or "").strip(),
        "source_type": str(result.get("source_type") or "TS").strip(),
        "confidence": min(float(result.get("confidence") or 0.5), 0.7),
        "warnings": list(result.get("warnings") or []) + ["Translated by LLM — review recommended"],
    }


def _looks_like_esql(query: str) -> bool:
    """Basic structural check: starts with TS/FROM and has pipe commands."""
    q = query.strip().upper()
    if not (q.startswith("TS ") or q.startswith("FROM ")):
        return False
    if "|" not in query:
        return False
    return True
