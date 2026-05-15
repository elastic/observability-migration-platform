"""mig-to-kbn panel-verification framework.

A 5-tier comparison pipeline that records, for every panel of a migrated
dashboard, the exact representation of its query at each stage of the
pipeline:

    T0  source PromQL  (the Grafana panel as authored)
    T1  translator out (what mig-to-kbn emitted, from migration_report.json)
    T2  YAML on disk   (kb-dashboard-cli input)
    T3  compiled NDJSON (kb-dashboard-cli output, ready for upload)
    T4  cluster Lens   (what Kibana stores as the saved object)
    T5  live _query    (what Lens actually dispatches when the panel renders)

Each tier's value is captured in a :class:`PanelRecord`, and pairs of
adjacent tiers are checked for drift.  Visual parity (Grafana vs Kibana
panel screenshots) and per-panel snapshots / HARs / React introspection
are layered on top via ``parity-rig/verifier/walker.py``.

This package exposes:

* ``records``  — the :class:`PanelRecord` dataclass and verdict vocabulary.
* ``collectors`` — functions that fill in each tier from local artifacts
  and the live cluster.
* ``compare``  — pairwise drift detection between tiers.
* ``cli``      — the ``obs-migrate verify-panels`` entry point.

It intentionally has zero dependency on ``observability_migration`` so it
can run against an artifact set without re-installing the translator.
"""

from __future__ import annotations

from .records import (
    DRIFT_AXES,
    PanelRecord,
    Verdict,
)

__all__ = ["DRIFT_AXES", "PanelRecord", "Verdict"]
