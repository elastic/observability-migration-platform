#!/usr/bin/env python
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Migrate a directory of Grafana dashboards under every field profile and gate
on profile-leakage + feasibility parity (and optional native byte-identity).

The harness treats ``prometheus_native`` as the feasibility baseline: every
other profile must emit at least as many runnable ES|QL queries (no silent
feasibility drop), none of the emitted queries may reference a field namespaced
for a *different* profile (profile leakage), and -- when ``--baseline-native``
is supplied -- the ``prometheus_native`` native output must stay byte-identical
to a saved baseline. Any violation exits non-zero so the harness can gate CI.
"""
from __future__ import annotations

import argparse
import glob
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# The leakage helpers live under ``parity-rig`` (a sibling of ``scripts``), which
# is not an importable package from the repo root; put it on ``sys.path`` first.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "parity-rig"))

from verifier.profile_leakage import (  # noqa: E402
    check_profile_leakage,
    extract_esql_queries,
    profiles_without_rules,
)


#: Field profiles per source. Datadog has no ``prometheus_remote_write`` and
#: names the Metricbeat layout ``prometheus``; sharing one list silently
#: skipped the Datadog profiles entirely, so the leakage gate only ever ran
#: against Grafana.
def _source_profiles() -> dict[str, list[str]]:
    """Every selectable field profile, taken from the adapters themselves.

    Hand-keeping this list let two Datadog profiles (``elastic_agent`` and
    ``default``) escape the gate entirely while both spellings of the
    Prometheus layout were listed twice -- the redundancy hid the omission, and
    the fail-closed rule check never saw that the missing profiles had no
    leakage rules. Reading the registry means a new profile joins the gate by
    existing, or trips :func:`profiles_without_rules` on its first run.

    ``auto`` is excluded: it resolves to a concrete profile at migrate time and
    is covered through whichever that turns out to be.
    """
    from observability_migration.adapters.source.datadog.field_map import (
        BUILTIN_PROFILES,
    )
    from observability_migration.adapters.source.grafana.cli import (
        _GRAFANA_FIELD_PROFILES,
    )

    return {
        "grafana": [p for p in _GRAFANA_FIELD_PROFILES if p != "auto"],
        "datadog": sorted(BUILTIN_PROFILES),
    }


PROFILES_BY_SOURCE = _source_profiles()
PROFILES = PROFILES_BY_SOURCE["grafana"]


def migrate(
    input_dir: str, out_dir: str, profile: str, index: str, source: str = "grafana"
) -> None:
    """Run the CLI migrate for one profile into ``out_dir`` (raises on failure)."""
    subprocess.run(
        [
            sys.executable,
            "-m",
            "observability_migration.app.cli",
            "migrate",
            "--source",
            source,
            "--input-mode",
            "files",
            "--input-dir",
            input_dir,
            "--output-dir",
            out_dir,
            "--assets",
            "dashboards",
            "--field-profile",
            profile,
            "--esql-index",
            index,
            "--data-view",
            index,
        ],
        check=True,
        cwd=str(_REPO_ROOT),
        env={"PYTHONPATH": "."},
    )


def _native_files(out_dir: str) -> list[str]:
    return sorted(glob.glob(f"{out_dir}/dashboards/native/*.native.json"))


def feasible_count(out_dir: str) -> int:
    """Total number of runnable ES|QL queries emitted across a migration output."""
    total = 0
    for f in _native_files(out_dir):
        with open(f, encoding="utf-8") as fh:
            total += len(extract_esql_queries(json.load(fh)))
    return total


def diff_native(out_dir: str, baseline_dir: str) -> list[str]:
    """Return human-readable diffs where the native output differs from baseline.

    Files are matched by basename. A file present on only one side, or whose
    parsed JSON differs, is reported.
    """
    failures: list[str] = []
    produced = {Path(f).name: f for f in _native_files(out_dir)}
    baseline = {
        Path(f).name: f
        for f in sorted(glob.glob(f"{baseline_dir}/*.native.json"))
    }
    for name in sorted(set(produced) | set(baseline)):
        if name not in produced:
            failures.append(f"[native-diff] baseline file missing from output: {name}")
            continue
        if name not in baseline:
            failures.append(f"[native-diff] output file absent from baseline: {name}")
            continue
        with open(produced[name], encoding="utf-8") as fh:
            new_data = json.load(fh)
        with open(baseline[name], encoding="utf-8") as fh:
            gold_data = json.load(fh)
        if new_data != gold_data:
            failures.append(f"[native-diff] native output differs from baseline: {name}")
    return failures


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Migrate every dashboard in --input-dir under each field "
            "profile and gate on profile-leakage, feasibility parity vs "
            "prometheus_native, and (optionally) native byte-identity."
        )
    )
    ap.add_argument("--input-dir", required=True, help="Directory of source dashboard JSON files.")
    ap.add_argument(
        "--source",
        choices=sorted(PROFILES_BY_SOURCE),
        default="grafana",
        help="Source vendor to migrate under (default: grafana).",
    )
    ap.add_argument(
        "--index",
        default="metrics-*",
        help="ES|QL index / data view pattern passed to migrate (default: metrics-*).",
    )
    ap.add_argument(
        "--profiles",
        nargs="*",
        default=None,
        help="Field profiles to migrate under (default: every profile of --source).",
    )
    ap.add_argument(
        "--baseline-native",
        default=None,
        help=(
            "Directory of saved prometheus_native *.native.json goldens; when "
            "set, the prometheus_native output must stay byte-identical to it."
        ),
    )
    args = ap.parse_args()
    if not args.profiles:
        args.profiles = PROFILES_BY_SOURCE[args.source]

    unchecked = profiles_without_rules(args.profiles)
    if unchecked:
        # Fail closed: a profile with no rules would report a clean pass while
        # proving nothing about it.
        print(f"no profile-leakage rules for: {', '.join(unchecked)}")
        sys.exit(2)

    baseline_count: int | None = None
    failures: list[str] = []
    for profile in args.profiles:
        out = tempfile.mkdtemp(prefix=f"xprof-{profile}-")
        migrate(args.input_dir, out, profile, args.index, source=args.source)
        count = feasible_count(out)
        if profile == "prometheus_native":
            baseline_count = count
            if args.baseline_native:
                failures.extend(diff_native(out, args.baseline_native))
        for f in _native_files(out):
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
            for q in extract_esql_queries(data):
                for v in check_profile_leakage(q, profile):
                    failures.append(f"[{profile}] {Path(f).name}: {v}")
        if baseline_count is not None and count < baseline_count:
            failures.append(
                f"[{profile}] feasible query count {count} < native {baseline_count}"
            )

    if failures:
        print("\n".join(failures))
        sys.exit(1)
    print("cross-profile gate: OK")


if __name__ == "__main__":
    main()
