# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Shared e2e fixtures.

The Grafana corpus migration is expensive (~12s, ~270 panels) and several
offline gates (fidelity ratchet, Kibana-schema validation) need its output. This
migrates the *committed* corpus once per session with the current code and shares
the resulting ``dashboards/`` directory.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def committed_grafana_dashboards() -> list[Path]:
    """Committed Grafana dashboard JSONs (excludes gitignored third-party ones)."""
    out = subprocess.run(
        ["git", "ls-files", "infra/grafana/dashboards/"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    return [REPO_ROOT / line for line in out.stdout.split() if line.endswith(".json")]


def committed_datadog_dashboards() -> list[Path]:
    """Committed Datadog dashboard JSONs (top-level + integrations/)."""
    out = subprocess.run(
        ["git", "ls-files", "infra/datadog/dashboards/"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    return [REPO_ROOT / line for line in out.stdout.split() if line.endswith(".json")]


@pytest.fixture(scope="session")
def grafana_corpus_dir() -> Path:
    """Migrate the committed Grafana corpus once; return its ``dashboards/`` dir."""
    dashboards = committed_grafana_dashboards()
    assert dashboards, "no committed Grafana dashboards found"

    tmp = REPO_ROOT / ".tmp" / "e2e_grafana_corpus"
    in_dir = tmp / "in"
    out_dir = tmp / "out"
    if tmp.exists():
        shutil.rmtree(tmp)
    in_dir.mkdir(parents=True)
    for src in dashboards:
        shutil.copy(src, in_dir / src.name)

    proc = subprocess.run(
        [
            sys.executable, "-m",
            "observability_migration.adapters.source.grafana.cli",
            "--source", "files",
            "--input-dir", str(in_dir),
            "--output-dir", str(out_dir),
            "--assets", "dashboards",
        ],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    dashboards_dir = out_dir / "dashboards"
    report = dashboards_dir / "migration_report.json"
    assert report.exists(), (
        f"corpus migration did not produce {report}\n"
        f"stdout tail:\n{proc.stdout[-1500:]}\nstderr tail:\n{proc.stderr[-1500:]}"
    )
    return dashboards_dir


@pytest.fixture(scope="session")
def datadog_corpus_dir() -> Path:
    """Migrate the committed Datadog corpus once; return its ``dashboards/`` dir."""
    dashboards = committed_datadog_dashboards()
    assert dashboards, "no committed Datadog dashboards found"

    tmp = REPO_ROOT / ".tmp" / "e2e_datadog_corpus"
    in_dir = tmp / "in"
    out_dir = tmp / "out"
    if tmp.exists():
        shutil.rmtree(tmp)
    in_dir.mkdir(parents=True)
    for src in dashboards:
        shutil.copy(src, in_dir / src.name)

    proc = subprocess.run(
        [
            sys.executable, "-m",
            "observability_migration.adapters.source.datadog.cli",
            "--input-dir", str(in_dir),
            "--output-dir", str(out_dir),
        ],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    dashboards_dir = out_dir / "dashboards"
    report = dashboards_dir / "migration_report.json"
    assert report.exists(), (
        f"datadog corpus migration did not produce {report}\n"
        f"stdout tail:\n{proc.stdout[-1500:]}\nstderr tail:\n{proc.stderr[-1500:]}"
    )
    return dashboards_dir
