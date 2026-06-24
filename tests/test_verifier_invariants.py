# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the Layer-9 deterministic invariant linter (parity-rig verifier).

Two concerns are covered:

* **Layer 9** - the linter detects broken Lens accessors, merged-series
  (multi-dimension grouping collapsed onto a single XY breakdown without a
  composite legend or a disclosing warning), legend/breakdown mismatches, and
  silent markdown placeholders, all from a synthetic ``migration_report.json``
  payload with no cluster.

* **Layer 12** - a *self-test*: start from a panel the linter passes, corrupt a
  single field, and assert the linter emits exactly the expected category. This
  proves the checks actually bite (guards against silent false-PASS).
"""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
VERIFIER_PARENT = ROOT / "parity-rig"
sys.path.insert(0, str(VERIFIER_PARENT))

from verifier import invariants  # noqa: E402
from verifier.invariants import (  # noqa: E402
    InvariantCategory,
    Severity,
    lint_report,
    lint_report_panel,
    lint_translation,
    static_query_columns,
)

# --------------------------------------------------------------------- #
# fixtures / builders
# --------------------------------------------------------------------- #

_CLEAN_XY_QUERY = (
    "TS metrics-* "
    "| STATS value = AVG(rate) "
    "BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), verb "
    "| SORT time_bucket"
)

_COMPOSITE_QUERY = (
    "TS metrics-* "
    "| STATS value = AVG(rate) "
    "BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), verb, resource_kind "
    "| EVAL legend = CONCAT(verb, \" \", resource_kind) "
    "| KEEP time_bucket, value, legend "
    "| SORT time_bucket"
)


def _esql_panel(
    *,
    title: str = "Requests by verb",
    chart_type: str = "line",
    query: str = _CLEAN_XY_QUERY,
    breakdown_field: str | None = "verb",
    output_group_fields: list[str] | None = None,
    status: str = "migrated",
    reasons: list[str] | None = None,
    warnings: list[str] | None = None,
    semantic_losses: list[str] | None = None,
    metrics_field: str = "value",
    dimension_field: str = "time_bucket",
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "type": chart_type,
        "query": query,
        "dimension": {"field": dimension_field},
        "metrics": [{"field": metrics_field}],
    }
    if breakdown_field is not None:
        config["breakdown"] = {"field": breakdown_field}
    return {
        "title": title,
        "status": status,
        "reasons": list(reasons or []),
        "post_validation_action": "",
        "esql": query,
        "query_ir": {
            "output_shape": "time_series",
            "output_group_fields": list(
                output_group_fields
                if output_group_fields is not None
                else ["time_bucket", "verb"]
            ),
            "warnings": list(warnings or []),
            "semantic_losses": list(semantic_losses or []),
        },
        "visual_ir": {
            "presentation": {"kind": "esql", "config": config},
        },
    }


def _markdown_panel(
    *,
    title: str = "Pie of doom",
    status: str = "migrated",
    reasons: list[str] | None = None,
    post_validation_action: str = "",
) -> dict[str, Any]:
    return {
        "title": title,
        "status": status,
        "reasons": list(reasons or []),
        "post_validation_action": post_validation_action,
        "esql": "",
        "query_ir": {},
        "visual_ir": {
            "presentation": {
                "kind": "markdown",
                "config": {"content": "Migration Required"},
            }
        },
    }


def _categories(findings: list[invariants.Finding]) -> set[InvariantCategory]:
    return {f.category for f in findings}


# --------------------------------------------------------------------- #
# static column inference
# --------------------------------------------------------------------- #


class TestStaticQueryColumns:
    def test_stats_by_columns(self) -> None:
        cols = static_query_columns(_CLEAN_XY_QUERY)
        assert cols is not None
        assert {"value", "time_bucket", "verb"} <= cols

    def test_keep_bounds_columns(self) -> None:
        cols = static_query_columns(_COMPOSITE_QUERY)
        assert cols is not None
        assert "legend" in cols
        assert "value" in cols
        assert "time_bucket" in cols

    def test_native_promql_without_keep_is_unknown(self) -> None:
        # Base columns of a native PROMQL command are not statically knowable.
        assert static_query_columns("PROMQL \"\"\"rate(x[5m])\"\"\" index=metrics-*") is None

    def test_native_promql_with_trailing_keep_is_known(self) -> None:
        q = (
            "PROMQL \"\"\"rate(x[5m])\"\"\" index=metrics-* "
            "| KEEP time_bucket, value, legend"
        )
        cols = static_query_columns(q)
        assert cols == {"time_bucket", "value", "legend"}

    def test_bare_from_without_projection_is_unknown(self) -> None:
        assert static_query_columns("FROM metrics-* | WHERE host == \"a\"") is None

    def test_empty_query_is_unknown(self) -> None:
        assert static_query_columns("") is None


# --------------------------------------------------------------------- #
# Layer 9 - clean panels produce no findings
# --------------------------------------------------------------------- #


class TestCleanPanelsPass:
    def test_single_breakdown_xy_is_clean(self) -> None:
        assert lint_report_panel(_esql_panel(), "Dash") == []

    def test_composite_legend_panel_is_clean(self) -> None:
        panel = _esql_panel(
            query=_COMPOSITE_QUERY,
            breakdown_field="legend",
            output_group_fields=["time_bucket", "verb", "resource_kind"],
        )
        assert lint_report_panel(panel, "Dash") == []

    def test_honest_markdown_placeholder_is_clean(self) -> None:
        panel = _markdown_panel(reasons=["unsupported panel type: flamegraph"])
        assert lint_report_panel(panel, "Dash") == []

    def test_not_feasible_markdown_is_clean(self) -> None:
        panel = _markdown_panel(status="not_feasible", reasons=["needs manual rebuild"])
        assert lint_report_panel(panel, "Dash") == []


# --------------------------------------------------------------------- #
# Layer 9 - detection
# --------------------------------------------------------------------- #


class TestAccessorChecks:
    def test_broken_accessor_field_is_flagged(self) -> None:
        panel = _esql_panel(breakdown_field="does_not_exist")
        findings = lint_report_panel(panel, "Dash")
        assert InvariantCategory.ACCESSOR_BROKEN in _categories(findings)
        broken = next(f for f in findings if f.category is InvariantCategory.ACCESSOR_BROKEN)
        assert broken.severity is Severity.ERROR
        assert broken.evidence["field"] == "does_not_exist"

    def test_legend_breakdown_without_concat_is_flagged(self) -> None:
        # breakdown bound to 'legend' but the query never produces it.
        panel = _esql_panel(
            breakdown_field="legend",
            output_group_fields=["time_bucket", "verb"],
        )
        findings = lint_report_panel(panel, "Dash")
        assert InvariantCategory.BREAKDOWN_LEGEND_MISMATCH in _categories(findings)

    def test_missing_metric_accessor_is_flagged(self) -> None:
        panel = _esql_panel(metrics_field="phantom_metric")
        findings = lint_report_panel(panel, "Dash")
        assert any(
            f.category is InvariantCategory.ACCESSOR_BROKEN and f.evidence["field"] == "phantom_metric"
            for f in findings
        )

    def test_native_promql_panel_is_not_false_flagged(self) -> None:
        # Columns unknown -> accessor check skipped (no false positive).
        panel = _esql_panel(
            query="PROMQL \"\"\"rate(x[5m])\"\"\" index=metrics-*",
            breakdown_field="verb",
            output_group_fields=["time_bucket", "verb"],
        )
        findings = lint_report_panel(panel, "Dash")
        assert InvariantCategory.ACCESSOR_BROKEN not in _categories(findings)

    def test_datadog_esql_query_panel_is_linted(self) -> None:
        panel = {
            "title": "Datadog timeseries",
            "status": "ok",
            "kibana_type": "xy",
            "esql_query": (
                "FROM metrics-* | STATS value = AVG(metric) "
                "BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), host, env"
            ),
            "query_ir": {
                "output_shape": "time_series",
                "output_group_fields": ["time_bucket", "host", "env"],
            },
        }
        findings = lint_report_panel(panel, "Dash")
        assert InvariantCategory.VISUAL_SEMANTIC_DRIFT in _categories(findings)


class TestMergedSeriesChecks:
    def test_silent_merge_is_error(self) -> None:
        panel = _esql_panel(
            query=(
                "TS metrics-* | STATS value = AVG(rate) "
                "BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), verb, resource_kind "
                "| SORT time_bucket"
            ),
            breakdown_field="verb",
            output_group_fields=["time_bucket", "verb", "resource_kind"],
        )
        findings = lint_report_panel(panel, "Dash")
        drift = [f for f in findings if f.category is InvariantCategory.VISUAL_SEMANTIC_DRIFT]
        assert drift, "expected a merged-series finding"
        assert drift[0].severity is Severity.ERROR

    def test_disclosed_merge_is_warning_not_error(self) -> None:
        panel = _esql_panel(
            query=(
                "TS metrics-* | STATS value = AVG(rate) "
                "BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), verb, resource_kind "
                "| SORT time_bucket"
            ),
            breakdown_field="verb",
            output_group_fields=["time_bucket", "verb", "resource_kind"],
            warnings=["additional grouping dimensions are visually merged"],
        )
        findings = lint_report_panel(panel, "Dash")
        drift = [f for f in findings if f.category is InvariantCategory.VISUAL_SEMANTIC_DRIFT]
        assert drift
        assert drift[0].severity is Severity.WARNING

    def test_composite_legend_suppresses_merge_finding(self) -> None:
        panel = _esql_panel(
            query=_COMPOSITE_QUERY,
            breakdown_field="legend",
            output_group_fields=["time_bucket", "verb", "resource_kind"],
        )
        findings = lint_report_panel(panel, "Dash")
        assert InvariantCategory.VISUAL_SEMANTIC_DRIFT not in _categories(findings)

    def test_datatable_with_many_groups_is_not_merge_flagged(self) -> None:
        # datatables use multi-column breakdowns; not an XY merge.
        panel = _esql_panel(
            chart_type="datatable",
            breakdown_field=None,
            output_group_fields=["time_bucket", "verb", "resource_kind"],
        )
        findings = lint_report_panel(panel, "Dash")
        assert InvariantCategory.VISUAL_SEMANTIC_DRIFT not in _categories(findings)


class TestPlaceholderHonesty:
    def test_silent_markdown_placeholder_is_flagged(self) -> None:
        panel = _markdown_panel(status="migrated", reasons=[], post_validation_action="")
        findings = lint_report_panel(panel, "Dash")
        assert InvariantCategory.PLACEHOLDER_DROPPED in _categories(findings)

    def test_placeholder_with_post_validation_action_is_clean(self) -> None:
        panel = _markdown_panel(post_validation_action="placeholder_empty_result")
        assert lint_report_panel(panel, "Dash") == []


# --------------------------------------------------------------------- #
# driver + summary
# --------------------------------------------------------------------- #


class TestDriverAndSummary:
    def test_lint_report_aggregates_panels(self) -> None:
        report = {
            "dashboards": [
                {
                    "title": "Dash A",
                    "panels": [
                        _esql_panel(),  # clean
                        _esql_panel(title="broken", breakdown_field="nope"),  # accessor
                        _markdown_panel(title="silent"),  # placeholder
                    ],
                }
            ]
        }
        findings = lint_report(report)
        cats = _categories(findings)
        assert InvariantCategory.ACCESSOR_BROKEN in cats
        assert InvariantCategory.PLACEHOLDER_DROPPED in cats

    def test_summarize_counts_errors(self) -> None:
        findings = lint_report_panel(_esql_panel(breakdown_field="nope"), "Dash")
        summary = invariants.summarize(findings)
        assert summary["total"] >= 1
        assert summary["error_count"] >= 1

    def test_live_oracle_overrides_static_inference(self) -> None:
        # A native PROMQL panel whose columns the static parser cannot know is
        # checkable once a live oracle reports the real columns.
        panel = _esql_panel(
            query="PROMQL \"\"\"rate(x[5m])\"\"\" index=metrics-*",
            breakdown_field="ghost",
            output_group_fields=["time_bucket", "verb"],
        )

        def oracle(_query: str) -> set[str]:
            return {"time_bucket", "value", "verb"}

        findings = lint_report_panel(panel, "Dash", columns_oracle=oracle)
        assert any(
            f.category is InvariantCategory.ACCESSOR_BROKEN and f.evidence["field"] == "ghost"
            for f in findings
        )


# --------------------------------------------------------------------- #
# Layer 12 - self-test: corrupt a passing panel, assert the check bites
# --------------------------------------------------------------------- #


class TestRealWorldRobustness:
    """Regressions for false-positive classes found by linting real dashboards
    (node-exporter-full, prometheus-all, user corpus) end-to-end."""

    def test_backtick_escaped_columns_do_not_false_flag_accessor(self) -> None:
        # ES|QL escapes dotted fields with backticks; the breakdown references
        # the unescaped name. Must NOT be reported as a broken accessor.
        query = (
            "TS metrics-* "
            "| STATS v = AVG(x) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), "
            "`service.instance.id`, `service.name` "
            "| KEEP time_bucket, v, `service.instance.id`, `service.name`"
        )
        cols = static_query_columns(query)
        assert cols is not None
        assert "service.instance.id" in cols  # backticks stripped
        panel = _esql_panel(
            query=query,
            breakdown_field="service.instance.id",
            metrics_field="v",
            output_group_fields=["time_bucket", "service.instance.id", "service.name"],
            warnings=["additional grouping dimensions are visually merged"],
        )
        cats = _categories(lint_report_panel(panel, "Dash"))
        assert InvariantCategory.ACCESSOR_BROKEN not in cats

    def test_text_panel_to_markdown_is_not_placeholder_drop(self) -> None:
        panel = _markdown_panel(status="migrated", reasons=[], post_validation_action="")
        panel["grafana_type"] = "text"
        assert lint_report_panel(panel, "Dash") == []

    def test_row_panel_to_markdown_is_not_placeholder_drop(self) -> None:
        panel = _markdown_panel(status="migrated", reasons=[], post_validation_action="")
        panel["grafana_type"] = "row"
        assert lint_report_panel(panel, "Dash") == []

    def test_lint_translation_prefers_visual_ir_over_zipped_yaml(self) -> None:
        # The emitter reorders panels; an externally-passed yaml panel may belong
        # to a different panel. lint_translation must use panel_result.visual_ir.
        class _PR:
            title = "Correct Panel"
            status = "migrated"
            reasons: list[str] = []
            post_validation_action = ""
            esql_query = ""
            query_ir = {"output_shape": "time_series", "output_group_fields": ["time_bucket", "verb"]}

            class _VIR:
                @staticmethod
                def to_dict() -> dict:
                    return {
                        "presentation": {
                            "kind": "esql",
                            "config": {
                                "type": "line",
                                "query": _CLEAN_XY_QUERY,
                                "dimension": {"field": "time_bucket"},
                                "metrics": [{"field": "value"}],
                                "breakdown": {"field": "verb"},
                            },
                        }
                    }

            visual_ir = _VIR()

        # Pass a WRONG yaml panel (different, broken). Linter must ignore it in
        # favor of visual_ir and therefore report no findings.
        wrong_yaml = {
            "title": "Wrong Panel",
            "esql": {"type": "line", "query": "TS x | STATS a = AVG(b) BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)", "breakdown": {"field": "ghost"}},
        }
        findings = lint_translation(wrong_yaml, _PR(), "Dash")
        assert findings == []

    def test_lint_translation_falls_back_to_yaml_when_no_visual_ir(self) -> None:
        class _PR:
            title = "P"
            status = "migrated"
            reasons: list[str] = []
            post_validation_action = ""
            esql_query = ""
            query_ir = {"output_group_fields": ["time_bucket", "verb"]}
            visual_ir = None

        yaml_panel = {
            "title": "P",
            "esql": {
                "type": "line",
                "query": _CLEAN_XY_QUERY,
                "breakdown": {"field": "ghost"},
            },
        }
        cats = _categories(lint_translation(yaml_panel, _PR(), "Dash"))
        assert InvariantCategory.ACCESSOR_BROKEN in cats


class TestInvariantSelfTest:
    """Each case: a clean fixture must pass, and a single mutation must trip
    exactly the expected category. If a mutation does NOT trip its check, the
    linter has a blind spot and this test fails."""

    def _clean(self) -> dict[str, Any]:
        return _esql_panel()

    def test_baseline_is_clean(self) -> None:
        assert lint_report_panel(self._clean(), "Dash") == []

    def test_mutation_breaks_accessor(self) -> None:
        panel = deepcopy(self._clean())
        panel["visual_ir"]["presentation"]["config"]["breakdown"]["field"] = "corrupted"
        cats = _categories(lint_report_panel(panel, "Dash"))
        assert InvariantCategory.ACCESSOR_BROKEN in cats

    def test_mutation_adds_hidden_dimension(self) -> None:
        panel = deepcopy(self._clean())
        # Pretend the translator grouped by an extra dimension the XY chart
        # can't show, without disclosing it.
        panel["query_ir"]["output_group_fields"] = ["time_bucket", "verb", "resource_kind"]
        panel["visual_ir"]["presentation"]["config"]["query"] = (
            "TS metrics-* | STATS value = AVG(rate) "
            "BY time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend), verb, resource_kind "
            "| SORT time_bucket"
        )
        findings = lint_report_panel(panel, "Dash")
        drift = [f for f in findings if f.category is InvariantCategory.VISUAL_SEMANTIC_DRIFT]
        assert drift and drift[0].severity is Severity.ERROR

    def test_mutation_silences_placeholder(self) -> None:
        panel = _markdown_panel(reasons=["unsupported"])
        assert lint_report_panel(panel, "Dash") == []  # honest baseline
        panel["reasons"] = []  # corrupt: drop the disclosure
        cats = _categories(lint_report_panel(panel, "Dash"))
        assert InvariantCategory.PLACEHOLDER_DROPPED in cats
