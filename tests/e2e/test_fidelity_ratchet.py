# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Layer-13 fidelity ratchet (Grafana + Datadog corpora) as offline e2e gates.

Activates the previously-dormant ``verifier.scorecard`` ratchet: scores the
Layer-9 invariant findings of the freshly-migrated committed corpora (see the
session ``grafana_corpus_dir`` / ``datadog_corpus_dir`` fixtures in conftest) and
asserts ERROR counts (overall + per category) have not increased vs the committed
baselines (``parity-rig/benchmark/fidelity_baseline_{grafana,datadog}.json``).

Improvements are always allowed. To intentionally refresh a baseline after a
deliberate change, migrate the committed corpus and re-run scorecard --update,
e.g. for Grafana::

    rm -rf /tmp/corpus_g_out && mkdir -p /tmp/corpus_g_in
    for f in $(git ls-files infra/grafana/dashboards/); do cp "$f" /tmp/corpus_g_in/; done
    .venv/bin/grafana-migrate --source files --input-dir /tmp/corpus_g_in \
        --output-dir /tmp/corpus_g_out --assets dashboards
    PYTHONPATH=parity-rig .venv/bin/python -m verifier.scorecard \
        --migration-out /tmp/corpus_g_out/dashboards \
        --baseline parity-rig/benchmark/fidelity_baseline_grafana.json --update

For Datadog, migrate with ``datadog-migrate --input-dir ... --output-dir ...``
and point ``--baseline`` at ``fidelity_baseline_datadog.json``.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "parity-rig"))

from verifier import scorecard  # noqa: E402

BENCH = REPO_ROOT / "parity-rig" / "benchmark"
GRAFANA_BASELINE = BENCH / "fidelity_baseline_grafana.json"
DATADOG_BASELINE = BENCH / "fidelity_baseline_datadog.json"


def _assert_no_regression(current, baseline_path):
    assert baseline_path.exists(), f"missing committed baseline {baseline_path}"
    baseline = scorecard.load_baseline(baseline_path)
    ok, regressions = scorecard.compare_to_baseline(current, baseline)
    assert ok, (
        "Fidelity regression vs baseline:\n  "
        + "\n  ".join(regressions)
        + f"\n(current errors={current['totals']['errors']}, "
        f"baseline errors={baseline['totals']['errors']}; "
        "refresh the baseline only if the change is intentional — see module docstring)"
    )


def test_grafana_corpus_has_no_fidelity_regression(grafana_corpus_dir):
    _assert_no_regression(
        scorecard.scorecard_for_migration(grafana_corpus_dir), GRAFANA_BASELINE
    )


def test_datadog_corpus_has_no_fidelity_regression(datadog_corpus_dir):
    _assert_no_regression(
        scorecard.scorecard_for_migration(datadog_corpus_dir), DATADOG_BASELINE
    )
