"""Hermetic integration tests for phase B variable-controls over the real corpus.

These exercise the full Grafana migration pipeline against the canonical
corpus under ``infra/grafana/dashboards/`` and check the structured outputs
(YAML and ``migration_report.json``) for the invariants spelled out in
``docs/roadmap/2026-04-27-kibana-variable-controls-design.md`` §12 Layer 2,
including a per-dashboard regression baseline at
``tests/fixtures/regression/grafana_corpus_phase_b.json``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
GRAFANA_FIXTURES = REPO / "infra" / "grafana" / "dashboards"
REGRESSION_BASELINE = (
    REPO / "tests" / "fixtures" / "regression" / "grafana_corpus_phase_b.json"
)


def _migrate_grafana(out_dir: Path) -> dict:
    """Run the Grafana migration on the canonical corpus and return the report."""
    subprocess.run(
        [
            sys.executable,
            "-m",
            "observability_migration.adapters.source.grafana.cli",
            "--source",
            "files",
            "--input-dir",
            str(GRAFANA_FIXTURES),
            "--output-dir",
            str(out_dir),
            "--assets",
            "dashboards",
            "--field-profile",
            "otel",
        ],
        cwd=str(REPO),
        check=True,
        capture_output=True,
    )
    report_path = out_dir / "dashboards" / "migration_report.json"
    return json.loads(report_path.read_text())


class GrafanaCorpusPhaseBIntegration(unittest.TestCase):
    """End-to-end checks for phase-B variable-controls on the Grafana corpus."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="phase-b-integ-"))
        cls.report = _migrate_grafana(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_at_least_one_dashboard_has_accepted_bindings(self):
        any_accepted = any(
            d.get("variables", {}).get("accepted")
            for d in self.report.get("dashboards", [])
        )
        self.assertTrue(
            any_accepted,
            "phase B should accept at least one variable across the canonical corpus",
        )

    def test_no_leftover_source_token_for_accepted_vars(self):
        """No accepted-variable ``$varname`` should survive in compiled ES|QL.

        Markdown stubs emitted for ``not_feasible`` panels intentionally
        preserve the original PromQL (including its ``$varname``) for
        documentation; only check ES|QL query strings.
        """
        yaml_dir = self.tmp / "dashboards" / "yaml"
        esql_queries: list[str] = []
        for yaml_path in yaml_dir.glob("*.yaml"):
            doc = yaml.safe_load(yaml_path.read_text()) or {}

            def _walk(items):
                for item in items or []:
                    if "section" in item:
                        yield from _walk(item.get("section", {}).get("panels"))
                        continue
                    yield item

            for dashboard in doc.get("dashboards", []):
                for panel in _walk(dashboard.get("panels")):
                    esql_block = panel.get("esql") or {}
                    query = esql_block.get("query")
                    if isinstance(query, str):
                        esql_queries.append(query)
                    elif isinstance(query, list):
                        esql_queries.extend(part for part in query if isinstance(part, str))
        text = "\n".join(esql_queries)
        for dashboard in self.report.get("dashboards", []):
            accepted = dashboard.get("variables", {}).get("accepted", [])
            for binding in accepted:
                pattern = re.compile(rf"\${binding['name']}\b")
                self.assertFalse(
                    pattern.search(text),
                    f"accepted variable ${binding['name']} should not survive in any ES|QL query",
                )

    def test_idempotent_byte_identical_yaml(self):
        second_tmp = Path(tempfile.mkdtemp(prefix="phase-b-integ-2-"))
        try:
            _migrate_grafana(second_tmp)
            for f in (self.tmp / "dashboards" / "yaml").glob("*.yaml"):
                a = f.read_bytes()
                b = (second_tmp / "dashboards" / "yaml" / f.name).read_bytes()
                self.assertEqual(a, b, f"YAML drifted between two runs in {f.name}")
        finally:
            shutil.rmtree(second_tmp, ignore_errors=True)

    def test_minimum_kibana_version_matches_floor_function(self):
        from observability_migration.core.variable_classifier import (
            AcceptedBinding,
            RejectedBinding,
            compute_min_kibana_version,
        )

        # ``minimum_kibana_version`` is rendered into the dashboard YAML at
        # assembly time (per design §4); the JSON report carries the binding
        # map. We cross-check both representations agree with the floor
        # function applied to the report's bindings.
        yaml_dir = self.tmp / "dashboards" / "yaml"
        yamls_by_title: dict[str, dict] = {}
        for path in yaml_dir.glob("*.yaml"):
            doc = yaml.safe_load(path.read_text()) or {}
            for dash in doc.get("dashboards", []) or []:
                yamls_by_title[dash.get("name", "")] = dash

        for dashboard in self.report.get("dashboards", []):
            bindings: dict[str, AcceptedBinding | RejectedBinding] = {}
            for entry in dashboard.get("variables", {}).get("accepted", []):
                bindings[entry["name"]] = AcceptedBinding(
                    field=entry["field"],
                    multi=entry["multi"],
                    options_query="FROM x",
                )
            for entry in dashboard.get("variables", {}).get("rejected", []):
                bindings[entry["name"]] = RejectedBinding(reason=entry["reason"])
            for entry in dashboard.get("variables", {}).get("verifier_downgraded", []):
                bindings[entry["name"]] = RejectedBinding(reason=entry["reason"])
            expected_floor = compute_min_kibana_version(bindings)
            yaml_dash = yamls_by_title.get(dashboard["title"])
            self.assertIsNotNone(
                yaml_dash,
                f"YAML missing for dashboard {dashboard['title']!r}; "
                f"emitted titles: {sorted(yamls_by_title)}",
            )
            actual_floor = (yaml_dash or {}).get("minimum_kibana_version", "")
            self.assertEqual(
                actual_floor,
                expected_floor,
                f"floor mismatch on {dashboard.get('title')}: "
                f"actual={actual_floor!r}, expected={expected_floor!r}",
            )

    def test_regression_baseline(self):
        actual = {
            d["title"]: {
                "accepted": sorted(v["name"] for v in d["variables"]["accepted"]),
                "rejected": sorted(v["name"] for v in d["variables"]["rejected"]),
                "verifier_downgraded": sorted(
                    v["name"] for v in d["variables"]["verifier_downgraded"]
                ),
            }
            for d in self.report["dashboards"]
        }
        baseline_path = REGRESSION_BASELINE
        if not baseline_path.exists():
            self.fail(
                f"Regression baseline not found at {baseline_path}. Generate it via:\n"
                f"  python tests/fixtures/regression/_generate_grafana_corpus_phase_b.py"
            )
        baseline = json.loads(baseline_path.read_text())
        self.assertEqual(actual, baseline)


if __name__ == "__main__":
    unittest.main()
