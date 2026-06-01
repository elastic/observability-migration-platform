# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Translate PromQL expressions and validate the emitted ES|QL on a live cluster.

This is a development harness for iterating on the PromQL -> ES|QL translator. It
runs each expression through the real ``translate_promql_to_esql`` path, prints the
feasibility verdict / warnings / generated ES|QL, then (unless ``--offline``) POSTs
the query to the cluster's ``_query`` API with concrete time params to confirm it
parses and executes.

Two ways to supply expressions:

* ``--expr 'sum(rate(http_requests_total[5m])) by (job)'`` (repeatable)
* ``--file queries.txt`` (one PromQL expression per line; ``#`` comments allowed)

With no expressions supplied it runs a built-in smoke corpus that covers the
feasibility-expansion constructs (clamp_max / clamp / sgn / quantile-by /
per-element ratios) plus a few baseline aggregations, so you can sanity-check the
translator end-to-end in one command.

Credentials come from the environment (``ELASTICSEARCH_ENDPOINT`` + ``KEY``) or the
matching ``--es-endpoint`` / ``--api-key`` flags. Source them from your creds file:

    set -a && . ./serverless_creds.env && set +a
    python scripts/validate_promql_esql_translations.py

Exit code is non-zero if any expression fails cluster validation (feasible queries
that error on the cluster), so this is CI/pre-commit friendly.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Allow running as a plain script from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.translate import (
    translate_promql_to_esql,
)

CTX = ssl.create_default_context()

# Built-in smoke corpus: (label, promql). Kept in sync with the feasibility
# expansion work so a bare run exercises the interesting translation paths.
DEFAULT_CORPUS: list[tuple[str, str]] = [
    # baseline aggregations
    ("avg_by", "avg(node_filesystem_avail_bytes) by (job)"),
    ("sum_rate_by", "sum(rate(http_requests_total[5m])) by (job)"),
    # Tier 1: exact 1:1 function maps
    ("clamp_max", "clamp_max(node_filesystem_avail_bytes, 100)"),
    ("clamp", "clamp(node_filesystem_avail_bytes, 0, 100)"),
    ("sgn", "sgn(node_cpu_seconds_total)"),
    ("quantile_by", "quantile(0.95, node_filesystem_avail_bytes) by (job)"),
    ("quantile_median", "quantile(0.5, node_filesystem_avail_bytes)"),
    # Tier 2: guarded per-element ratios (feasible only when label-aligned)
    ("sum_ratio_by", "sum(node_memory_used_bytes / node_memory_total_bytes) by (instance)"),
    ("max_ratio", "max(node_memory_used_bytes / node_memory_total_bytes)"),
]


def es_request(endpoint: str, key: str, method: str, path: str, body: dict | None = None) -> dict:
    """POST/GET against the cluster with light retry; returns parsed JSON or {'error': ...}."""
    url = f"{endpoint.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Authorization": f"ApiKey {key}", "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, context=CTX, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                return {"error": json.loads(raw).get("error", {"reason": raw[:300]})}
            except json.JSONDecodeError:
                return {"error": {"reason": raw[:300] or f"HTTP {e.code}"}}
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {"error": {"reason": f"Connection failed: {e}"}}
    return {"error": {"reason": "Max retries exceeded"}}


def validate_on_cluster(endpoint: str, key: str, esql: str, tstart: str, tend: str) -> tuple[bool, str]:
    """Run the ES|QL with concrete time params; return (ok, message)."""
    body = {
        "query": esql,
        "params": [{"_tstart": tstart}, {"_tend": tend}],
    }
    result = es_request(endpoint, key, "POST", "/_query?format=json", body)
    if "error" in result:
        reason = result["error"].get("reason", str(result["error"]))
        return False, f"cluster error: {reason[:300]}"
    ncols = len(result.get("columns", []))
    nrows = len(result.get("values", []))
    return True, f"OK ({ncols} cols, {nrows} rows)"


def load_expressions(args: argparse.Namespace) -> list[tuple[str, str]]:
    exprs: list[tuple[str, str]] = []
    for i, e in enumerate(args.expr or []):
        exprs.append((f"expr{i + 1}", e))
    if args.file:
        for line in Path(args.file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                exprs.append((f"line{len(exprs) + 1}", line))
    return exprs or list(DEFAULT_CORPUS)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--expr", action="append", help="A PromQL expression to translate (repeatable).")
    p.add_argument("--file", help="File with one PromQL expression per line (# comments allowed).")
    p.add_argument("--index", default="metrics-*", help="ES|QL source index pattern.")
    p.add_argument("--es-endpoint", default=os.environ.get("ELASTICSEARCH_ENDPOINT", ""))
    p.add_argument("--api-key", default=os.environ.get("KEY", ""))
    p.add_argument("--tstart", default="2026-05-01T00:00:00.000Z", help="Value for ?_tstart param.")
    p.add_argument("--tend", default="2026-06-01T00:00:00.000Z", help="Value for ?_tend param.")
    p.add_argument("--offline", action="store_true", help="Translate only; skip cluster validation.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    online = not args.offline
    if online and (not args.es_endpoint or not args.api_key):
        print(
            "ERROR: set ELASTICSEARCH_ENDPOINT and KEY (or pass --es-endpoint/--api-key), "
            "or use --offline to skip cluster validation.",
            file=sys.stderr,
        )
        return 2

    rp = RulePackConfig()
    expressions = load_expressions(args)
    failures = 0
    for name, expr in expressions:
        ctx = translate_promql_to_esql(expr, esql_index=args.index, rule_pack=rp)
        esql = ctx.esql_query or ""
        print(f"=== {name}: {expr}")
        print(f"    feasibility: {ctx.feasibility}")
        for w in ctx.warnings:
            print(f"    warning: {w}")
        if esql:
            print(f"    esql: {esql.splitlines()[0]}")
            for line in esql.splitlines()[1:]:
                print(f"          {line}")
        if online and ctx.feasibility != "not_feasible" and esql:
            ok, msg = validate_on_cluster(args.es_endpoint, args.api_key, esql, args.tstart, args.tend)
            print(f"    cluster: {'PASS' if ok else 'FAIL'} - {msg}")
            if not ok:
                failures += 1
        print()

    total = len(expressions)
    print(f"Validated {total} expression(s); {failures} cluster failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
