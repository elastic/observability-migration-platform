# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""In-code registry + resolver for the bundled sample dashboards."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path


@dataclass(frozen=True)
class SampleDashboard:
    id: str
    source: str  # "grafana" | "datadog"
    title: str
    description: str
    relative_dir: str  # e.g. "grafana/prom-basics"
    expected_unsupported: tuple[str, ...]


_CATALOG: tuple[SampleDashboard, ...] = (
    SampleDashboard(
        id="grafana-prom-basics",
        source="grafana",
        title="Sample: Prometheus Basics",
        description=(
            "Grafana dashboard with timeseries, stat, and table panels on a "
            "Prometheus datasource, plus one unsupported plugin panel."
        ),
        relative_dir="grafana/prom-basics",
        expected_unsupported=("World Map (unsupported)",),
    ),
    SampleDashboard(
        id="datadog-host-basics",
        source="datadog",
        title="Sample: Host Basics",
        description=(
            "Datadog dashboard with timeseries, query_value, and toplist widgets, "
            "plus one unsupported hostmap widget."
        ),
        relative_dir="datadog/host-basics",
        expected_unsupported=("Host Map (unsupported)",),
    ),
)


def list_samples() -> list[SampleDashboard]:
    """Return the bundled sample dashboards."""
    return list(_CATALOG)


def _by_id(sample_id: str) -> SampleDashboard:
    for sample in _CATALOG:
        if sample.id == sample_id:
            return sample
    raise KeyError(sample_id)


def resolve_input_dir(sample_id: str) -> Path:
    """Absolute on-disk directory for a sample, suitable for ``--input-dir``."""
    sample = _by_id(sample_id)
    root = resources.files("observability_migration.sample_dashboards")
    return Path(str(root.joinpath(sample.relative_dir)))
