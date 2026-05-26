# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Re-bless the regression baseline for the phase-B Grafana corpus integration test.

Run after intentionally changing the classifier or adding a new dashboard:

    .venv/bin/python -m observability_migration.adapters.source.grafana.cli \\
      --source files --input-dir infra/grafana/dashboards \\
      --output-dir /tmp/phase_b_baseline --assets dashboards --field-profile otel
    .venv/bin/python tests/fixtures/regression/_generate_grafana_corpus_phase_b.py

The first command produces ``/tmp/phase_b_baseline/dashboards/migration_report.json``;
this script snapshots the per-dashboard accepted / rejected / verifier-downgraded
binding names into ``grafana_corpus_phase_b.json`` next to this script. The
resulting baseline is committed to git so reviewers see the diff during PR review.
"""

from __future__ import annotations

import json
import pathlib
import sys

REPORT_PATH = pathlib.Path("/tmp/phase_b_baseline/dashboards/migration_report.json")
OUT_PATH = pathlib.Path(__file__).parent / "grafana_corpus_phase_b.json"


def main() -> int:
    if not REPORT_PATH.exists():
        print(
            f"ERROR: {REPORT_PATH} not found. Run the migration first.",
            file=sys.stderr,
        )
        return 1
    report = json.loads(REPORT_PATH.read_text())
    out = {
        d["title"]: {
            "accepted": sorted(v["name"] for v in d["variables"]["accepted"]),
            "rejected": sorted(v["name"] for v in d["variables"]["rejected"]),
            "verifier_downgraded": sorted(
                v["name"] for v in d["variables"]["verifier_downgraded"]
            ),
        }
        for d in report["dashboards"]
    }
    OUT_PATH.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
