# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Live filter-semantics gate: do emitted WHERE clauses select the right rows?

The existing layers each miss a class of filter bug:

* ``verifier.live_validate`` executes the query and classifies the response, so
  it catches a *rejected* predicate (``http.response.status_code == "500"`` on a
  numeric field -> ``verification_exception``). It cannot catch a predicate that
  is valid but selects the wrong rows.
* ``verifier.dashboards_api`` and the schema gate check shape, not semantics.
* The render audit sees "no rows" as ``unexpected_empty`` / ``data_gap`` -- a
  warn, and indistinguishable from telemetry simply not being seeded.

A silently-wrong filter therefore passes every gate. Two real examples, both
found by this gate's approach and fixed:

1. ``{host:web-*}`` emitted ``LIKE "web-%"``. ES|QL has no ``%`` wildcard --
   it is a literal character -- so the panel matched **nothing** and rendered
   empty with no error.
2. ``{!host:canary*}`` emitted ``NOT LIKE "canary%"``, which matches
   **everything**: the exclusion silently did nothing.

This gate seeds a throwaway index per (field profile, field type), emits every
tag-filter shape through the real translator, executes it, and compares the
**selected rows** against the expected ones. Neither validity nor row count is
a pass on its own -- a predicate that returns the right number of the wrong
documents is precisely the bug class here. It also asserts the numeric and
string mappings of the same logical filter select the same rows, which is the
property a field profile has to preserve.

Every index it creates is named with a per-invocation token and deleted in a
``finally``, so concurrent runs cannot collide and nothing outside this run is
ever deleted -- the gate takes an arbitrary ``--es-url`` and must not be able
to destroy an unrelated index that happens to share a name.

Needs a cluster. Local stack:

    STACK_VERSION=9.6.0-SNAPSHOT docker compose \
      -f parity-rig/docker-compose.render-audit.yml up -d --wait
    python -m verifier.filter_semantics_gate --es-url http://localhost:9200
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from copy import deepcopy
from dataclasses import dataclass, field

from observability_migration.adapters.source.datadog.field_map import BUILTIN_PROFILES
from observability_migration.adapters.source.datadog.models import TagFilter
from observability_migration.adapters.source.datadog.translate import _tag_filter_to_esql
from observability_migration.core.verification.field_capabilities import FieldCapability
from observability_migration.targets.kibana.emit.esql_utils import esql_identifier

# The tag under test and the documents seeded for it. OTel semantic conventions
# map http.response.status_code to `long`, which is what made this the field
# that exposed the original bug.
TAG = "http.response.status_code"
SEEDED = ("500", "503", "200")

#: ``(shape_id, TagFilter kwargs, expected selected values)``.
SHAPES: tuple[tuple[str, dict, tuple[str, ...]], ...] = (
    ("eq", {"value": "500"}, ("500",)),
    ("neq", {"value": "500", "negated": True}, ("503", "200")),
    ("or-list", {"value": "500|503"}, ("500", "503")),
    ("neg-or-list", {"value": "500|503", "negated": True}, ("200",)),
    ("in-list", {"value": "500|503", "is_in_list": True}, ("500", "503")),
    ("not-in-list", {"value": "500|503", "is_in_list": True, "negated": True}, ("200",)),
    ("glob", {"value": "5*"}, ("500", "503")),
    ("neg-glob", {"value": "5*", "negated": True}, ("200",)),
    # `?` matches exactly one character, so `20?` selects 200 alone while
    # `50?` would also take 503 -- picked to keep the expectation unambiguous.
    ("glob-single-char", {"value": "20?"}, ("200",)),
    # Mixed literal + glob inside one `a|b` value.
    ("glob-or-list", {"value": "500|20*"}, ("500", "200")),
)

#: One numeric and one string mapping of the same tag. A field profile must
#: select the same rows either way.
FIELD_TYPES = ("long", "keyword")


@dataclass
class Finding:
    profile: str
    field_type: str
    shape: str
    predicate: str
    #: The rows themselves, not how many: a predicate that selects the wrong
    #: document of the right cardinality is exactly the bug class this gate
    #: exists for, and a count cannot see it.
    expected: object
    actual: object
    detail: str = ""

    def render(self) -> str:
        return (
            f"{self.profile}/{self.field_type}/{self.shape}: "
            f"expected {self.expected}, got {self.actual}"
            f"{' — ' + self.detail if self.detail else ''}\n"
            f"    {self.predicate}"
        )


@dataclass
class GateResult:
    executed: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


def _request(es_url, method, path, body=None, ndjson=False):
    if ndjson:
        data = body.encode()
        content_type = "application/x-ndjson"
    else:
        data = json.dumps(body).encode() if body is not None else None
        content_type = "application/json"
    req = urllib.request.Request(
        f"{es_url.rstrip('/')}{path}",
        data=data,
        method=method,
        headers={"Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            return 200, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _error_reason(body: str) -> str:
    try:
        err = json.loads(body)["error"]
        root = (err.get("root_cause") or [{}])[0].get("reason") or ""
        return (root or err.get("reason", "")).replace("\n", " ")[:200]
    except Exception:
        return str(body)[:200]


def _seed(
    es_url: str, index: str, mapped_field: str, field_type: str, request=_request
) -> str:
    """Create and fill this run's index. Returns "" on success, else the reason.

    Nothing is deleted here. The index name carries a per-run token, so there
    is nothing of ours to clear first and a pre-emptive ``DELETE`` could only
    ever destroy someone else's index that happened to share the name.

    Every response is checked. Discarding them let the gate run its assertions
    against an index that was never written, and report a pass.
    """
    code, body = request(
        es_url,
        "PUT",
        f"/{index}",
        {
            "mappings": {
                "properties": {
                    "@timestamp": {"type": "date"},
                    mapped_field: {"type": field_type},
                }
            }
        },
    )
    if not 200 <= code < 300:
        return f"could not create {index}: {_error_reason(body)}"
    lines = []
    for value in SEEDED:
        lines.append(json.dumps({"index": {}}))
        lines.append(
            json.dumps(
                {
                    "@timestamp": "2026-01-01T00:00:00Z",
                    mapped_field: value if field_type == "keyword" else int(value),
                }
            )
        )
    code, body = request(
        es_url, "POST", f"/{index}/_bulk?refresh=true", "\n".join(lines) + "\n", ndjson=True
    )
    if not 200 <= code < 300:
        return f"could not seed {index}: {_error_reason(body)}"
    # A bulk can answer 200 while rejecting every document.
    if isinstance(body, dict) and body.get("errors"):
        reasons = [
            str(((item.get("index") or {}).get("error") or {}).get("reason", ""))
            for item in (body.get("items") or [])
        ]
        first = next((r for r in reasons if r), "unreported")
        return f"could not seed {index}: {first}"
    return ""


def _selected_values(body: object) -> list[str]:
    """The rows a query returned, normalized for comparison.

    A ``long`` mapping answers with ints and the expectation table is written
    in strings, and ES|QL row order is not part of the contract -- so compare
    sorted strings.
    """
    rows = body.get("values") or [] if isinstance(body, dict) else []
    return sorted(str(row[0]) for row in rows if row)


def run_gate(es_url: str, profiles: list[str] | None = None, request=_request) -> GateResult:
    """Execute every shape and return the findings.

    ``request`` is injectable so the driver is testable with no cluster, the
    way ``live_validate`` makes its query runner injectable.
    """
    result = GateResult()
    # One token per invocation, so two runs -- concurrent or not -- can never
    # name the same index, and so cleanup can key on "did this run create it?"
    run_token = uuid.uuid4().hex[:10]
    created: list[str] = []
    try:
        for profile_name in profiles or sorted(BUILTIN_PROFILES):
            for field_type in FIELD_TYPES:
                profile = deepcopy(BUILTIN_PROFILES[profile_name])
                mapped = profile.map_tag(TAG, context="metric")
                profile.metric_field_caps = {
                    mapped: FieldCapability(name=mapped, type=field_type)
                }
                index = (
                    f"filter-semantics-{run_token}-"
                    f"{profile_name.replace('_', '-')}-{field_type}"
                )
                created.append(index)
                seed_error = _seed(es_url, index, mapped, field_type, request=request)
                if seed_error:
                    # Every shape for this index is unanswerable; say so once
                    # rather than reporting ten row-set mismatches against an
                    # index that holds nothing.
                    result.findings.append(
                        Finding(profile_name, field_type, "(seed)", "(none)",
                                sorted(SEEDED), "not seeded", seed_error)
                    )
                    continue
                keep = esql_identifier(mapped)
                for shape, kwargs, expected in SHAPES:
                    predicate = _tag_filter_to_esql(
                        TagFilter(key=TAG, **kwargs), profile, context="metric"
                    )
                    result.executed += 1
                    if not predicate:
                        result.findings.append(
                            Finding(profile_name, field_type, shape, "(empty)",
                                    sorted(expected), "no predicate emitted")
                        )
                        continue
                    code, body = request(
                        es_url,
                        "POST",
                        "/_query",
                        {
                            "query": (
                                f"FROM {index} | WHERE {predicate} "
                                f"| KEEP {keep} | SORT {keep} ASC"
                            )
                        },
                    )
                    if code != 200:
                        result.findings.append(
                            Finding(profile_name, field_type, shape, predicate,
                                    sorted(expected), "query rejected",
                                    _error_reason(body))
                        )
                        continue
                    actual = _selected_values(body)
                    if actual != sorted(expected):
                        result.findings.append(
                            Finding(profile_name, field_type, shape, predicate,
                                    sorted(expected), actual,
                                    "selected the wrong rows"
                                    if len(actual) == len(expected)
                                    else "")
                        )
    finally:
        # Only what this invocation made, and even if a shape blew up.
        for index in created:
            request(es_url, "DELETE", f"/{index}")
    return result


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--es-url", default="http://localhost:9200")
    parser.add_argument(
        "--profile",
        action="append",
        dest="profiles",
        help="Limit to these Datadog field profiles (repeatable).",
    )
    parser.add_argument("--json", action="store_true", help="Emit a JSON summary.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    result = run_gate(args.es_url, args.profiles)
    if args.json:
        print(json.dumps({
            "executed": result.executed,
            "failures": len(result.findings),
            "findings": [
                {
                    "profile": f.profile, "field_type": f.field_type, "shape": f.shape,
                    "predicate": f.predicate, "expected": f.expected,
                    "actual": f.actual, "detail": f.detail,
                }
                for f in result.findings
            ],
        }, indent=2))
    else:
        print(f"filter semantics: executed {result.executed} shapes against {args.es_url}")
        for finding in result.findings:
            print(f"  FAIL {finding.render()}")
        print("  all shapes selected the expected rows" if result.ok
              else f"  {len(result.findings)} failing shape(s)")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
