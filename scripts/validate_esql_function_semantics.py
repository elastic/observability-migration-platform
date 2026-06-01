# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Verify ES|QL function semantics with synthetic ROW data on a live cluster.

Where ``validate_promql_esql_translations.py`` checks that a *full translated query*
parses and runs, this harness pins down the *numeric semantics* of the individual
ES|QL idioms the translator emits (LEAST / GREATEST / SIGNUM / PERCENTILE, and the
STATS -> EVAL -> STATS composition used for per-element ratios). Each case feeds
concrete literals via ``ROW`` so the result is independent of which metric fields
exist on the cluster, and asserts the computed value matches the PromQL semantics.

Use it when adding a new function mapping to prove the ES|QL equivalent is exact
before wiring it into the translator.

    set -a && . ./serverless_creds.env && set +a
    python scripts/validate_esql_function_semantics.py

Exit code is non-zero if any case mismatches or errors.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

CTX = ssl.create_default_context()

# (label, esql, column_index, expected_value). column_index selects the cell to
# check from the first result row.
CASES: list[tuple[str, str, int, float]] = [
    # clamp_max -> LEAST(value, hi): 150 clamped to 100
    ("clamp_max LEAST", "ROW value = 150.0 | EVAL value = LEAST(value, 100)", -1, 100.0),
    # clamp -> GREATEST(LEAST(v, hi), lo): low and high edges
    ("clamp low edge", "ROW value = -5.0 | EVAL value = GREATEST(value, 0) | EVAL value = LEAST(value, 100)", -1, 0.0),
    ("clamp high edge", "ROW value = 150.0 | EVAL value = GREATEST(value, 0) | EVAL value = LEAST(value, 100)", -1, 100.0),
    # sgn -> SIGNUM(value)
    ("sgn negative", "ROW value = -3.0 | EVAL value = SIGNUM(value)", -1, -1.0),
    ("sgn positive", "ROW value = 7.0 | EVAL value = SIGNUM(value)", -1, 1.0),
    # quantile -> PERCENTILE over a STATS BY group; median of [10, 20] == 15
    (
        "quantile PERCENTILE BY",
        'ROW g="a", v=10.0 | EVAL v = MV_APPEND(v, 20.0) | MV_EXPAND v | STATS p = PERCENTILE(v, 50) BY g',
        0,
        15.0,
    ),
    # The per-element ratio composition is built by ratio_sum_case() because it
    # needs paired multivalue arrays rather than a single-row literal.
]


def es_query(endpoint: str, key: str, esql: str) -> tuple[bool, object]:
    body = json.dumps({"query": esql}).encode()
    req = urllib.request.Request(
        f"{endpoint.rstrip('/')}/_query?format=json",
        data=body,
        headers={"Authorization": f"ApiKey {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30) as resp:
            return True, json.loads(resp.read()).get("values", [])
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            reason = json.loads(raw).get("error", {}).get("reason", raw[:300])
        except json.JSONDecodeError:
            reason = raw[:300]
        return False, f"HTTP {e.code}: {reason}"
    except Exception as e:
        return False, f"ERR: {e}"


def ratio_sum_case() -> tuple[str, str, int, float]:
    """Build a clean two-series per-element ratio sum that doesn't rely on row pairing.

    Two independent ROWs unioned with the FORK/expand idioms are awkward; instead we
    encode two (a, b) pairs as aligned multivalue arrays, MV_EXPAND to a 2-row table
    keyed by an index, compute r = a / b per row, and SUM. With pairs (10/40)=0.25
    and (30/60)=0.5 the expected sum is 0.75.
    """
    esql = (
        "ROW i = [0, 1], a = [10.0, 30.0], b = [40.0, 60.0] "
        "| MV_EXPAND i "
        "| EVAL a = MV_SLICE(a, i, i), b = MV_SLICE(b, i, i) "
        "| EVAL r = a / b "
        "| STATS total = SUM(r)"
    )
    return ("per-element ratio sum", esql, -1, 0.75)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--es-endpoint", default=os.environ.get("ELASTICSEARCH_ENDPOINT", ""))
    p.add_argument("--api-key", default=os.environ.get("KEY", ""))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.es_endpoint or not args.api_key:
        print("ERROR: set ELASTICSEARCH_ENDPOINT and KEY (or pass --es-endpoint/--api-key).", file=sys.stderr)
        return 2

    cases = list(CASES) + [ratio_sum_case()]
    failures = 0
    for name, esql, col, expected in cases:
        ok, result = es_query(args.es_endpoint, args.api_key, esql)
        if not ok:
            failures += 1
            print(f"[FAIL] {name}\n        {esql}\n        -> {result}\n")
            continue
        got = None
        if result and result[0]:
            got = result[0][col]
        match = got is not None and abs(float(got) - expected) < 1e-6
        failures += 0 if match else 1
        print(f"[{'PASS' if match else 'FAIL'}] {name}: got={got} expected={expected}")
        if not match:
            print(f"        {esql}")
        print()

    print("ALL PASS" if failures == 0 else f"{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
