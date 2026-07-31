#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Apply the counter typing a migration says the target needs.

PromQL does not enforce metric types; Elasticsearch does. ``rate()`` and
``irate()`` are counter-only in PromQL, so a dashboard using them *asserts* the
metric is a counter — but many exporters publish true counters as
``# TYPE ... untyped`` (mysqld_exporter and node_exporter both do), and
Elasticsearch then infers ``gauge`` from the missing ``_total`` suffix. Every
``RATE()`` over such a field fails at render time with::

    first argument of [RATE(x, 5m)] must be [counter_long, counter_integer or
    counter_double]

The migration already records which fields must be counters, in
``telemetry_contract.json``. This turns that record into the index template that
makes it true, so panels render a *real* rate rather than an error or an
approximation.

Usage::

    python scripts/apply_counter_mappings.py \\
        --contract <out>/dashboards/telemetry_contract.json \\
        --es-url http://localhost:9201 --dry-run

Drop ``--dry-run`` to apply. Existing data streams keep their current mapping —
Elasticsearch cannot retype a field in place — so roll over (or recreate) the
stream afterwards for the change to take effect; the script says so per index.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

# Mirrors the built-in ``metrics-prometheus@template``. ``index.mode`` must be set
# here because none of the composed components set it, and the routing path comes
# from the ``labels`` passthrough in ``metrics-prometheus@mappings`` — so that
# component must be composed and ``labels`` must not be redefined locally.
_COMPOSED_OF = [
    "metrics-prometheus@mappings",
    "metrics-prometheus@settings",
    "metrics@mappings",
    "data-streams@mappings",
    "metrics@settings",
]


def counter_fields(contract: dict) -> dict[str, list[str]]:
    """Index pattern -> field names the migration marked as counters."""
    out: dict[str, list[str]] = {}
    for stream, body in (contract.get("streams") or {}).items():
        names = sorted(
            name
            for name, spec in (body.get("fields") or {}).items()
            # ``counter_locked`` is the authoritative signal: rate()/irate() in the
            # source, which cannot be applied to a gauge in PromQL.
            if str(spec.get("metric_kind", "")).startswith("counter")
        )
        if names:
            out[stream] = names
    return out


def build_template(index_pattern: str, fields: list[str]) -> dict:
    props = {
        name[len("metrics.") :] if name.startswith("metrics.") else name: {
            "type": "double",
            "time_series_metric": "counter",
        }
        for name in fields
    }
    # Index templates match the DATA STREAM name, not the backing index name.
    # Appending "-*" produced "metrics-mysql.prometheus-default-*", which never
    # matches the stream "metrics-mysql.prometheus-default", so the template was
    # created and silently ignored.
    return {
        "index_patterns": [index_pattern],
        "data_stream": {},
        "priority": 600,
        "composed_of": _COMPOSED_OF,
        "template": {
            "settings": {"index.mode": "time_series"},
            "mappings": {"properties": {"metrics": {"properties": props}}},
        },
    }


def put(es_url: str, name: str, body: dict, api_key: str = "") -> tuple[bool, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
    req = urllib.request.Request(
        f"{es_url.rstrip('/')}/_index_template/{name}",
        json.dumps(body).encode(), headers, method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return True, str(resp.status)
    except urllib.error.HTTPError as exc:
        return False, exc.read().decode()[:300]
    except OSError as exc:
        return False, str(exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--es-url", required=True)
    parser.add_argument("--es-api-key", default="")
    parser.add_argument(
        "--index",
        action="append",
        default=[],
        help="Apply to this index/data-stream instead of the contract's stream name. "
             "Repeatable. Needed when one dashboard queries several exporters: a field "
             "declared counter in one stream and left gauge in another makes a wildcard "
             "query fail with 'ambiguities being mapped as [2] incompatible types'.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    with open(args.contract, encoding="utf-8") as handle:
        contract = json.load(handle)

    by_stream = counter_fields(contract)
    if not by_stream:
        print("No counter fields recorded in the contract; nothing to apply.")
        return 0

    if args.index:
        merged: list[str] = sorted({f for fields in by_stream.values() for f in fields})
        by_stream = dict.fromkeys(args.index, merged)

    failures = 0
    for stream, fields in by_stream.items():
        if "*" in stream.rstrip("*"):
            print(f"skip {stream}: not a concrete index pattern")
            continue
        name = "obs-migrate-counters-" + stream.replace("*", "").replace(".", "-").strip("-")
        body = build_template(stream, fields)
        print(f"\n{stream}: {len(fields)} counter field(s) -> template {name}")
        if args.dry_run:
            print(json.dumps(body, indent=2)[:900])
            continue
        ok, detail = put(args.es_url, name, body, args.es_api_key)
        if ok:
            print(f"  applied. Roll over or recreate {stream} for it to take effect "
                  "(Elasticsearch cannot retype an existing field in place).")
        else:
            failures += 1
            print(f"  FAILED: {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
