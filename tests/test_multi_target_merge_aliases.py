# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Regression tests for multi-target ES|QL merge (_merge_pretranslated_xy_queries).

Live smoke against Node Exporter Full caught two bugs in the merge path:

1. CPU Frequency Scaling — STATS aliases were renamed to ``metric_B`` /
   ``metric_A`` but subsequent EVAL legend lines still referenced the
   pre-rename names (Unknown column).
2. CPU spent seconds in guests — fusing two join-ratio targets produced a
   single STATS that mixed ``IRATE(CASE(...))`` with bare ``IRATE(...)`` on a
   different metric, which Elasticsearch rejects with a ClassCastException
   (ReferenceAttribute → Bucket).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

from observability_migration.adapters.source.grafana.esql_structural_oracle import (
    check_esql_structure,
    structural_errors,
)
from observability_migration.adapters.source.grafana.panels import (
    _merge_pretranslated_xy_queries,
    translate_panel,
)
from observability_migration.adapters.source.grafana.promql import (
    _ESQL_FIELD_REFERENCE_PATTERN,
    MeasureSpec,
    _build_shared_measure_pipeline,
    _wrap_bare_ts_value_args_when_case_siblings,
)
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "infra"
    / "grafana"
    / "dashboards"
    / "node-exporter-full.json"
)

# Offline smoke slice: panels that previously failed after upload+seed.
_NODE_EXPORTER_SMOKE_TITLES = (
    "CPU Frequency Scaling",
    "CPU spent seconds in guests (VMs)",
)


def _translation(ref_id, series_alias, metric_field, query):
    return SimpleNamespace(
        esql_query=query,
        feasibility="feasible",
        output_metric_field=metric_field,
        metric_name=metric_field,
        warnings=[],
        metadata={
            "target_ref_id": ref_id,
            "series_alias": series_alias,
            "target_source_expr": metric_field,
        },
    )


def _stats_assignments(query: str) -> list[str]:
    stats_line = next(
        (line for line in query.splitlines() if line.lstrip().startswith("| STATS") or line.startswith("STATS")),
        "",
    )
    body = re.sub(r"^\|?\s*STATS\s+", "", stats_line.strip(), count=1, flags=re.IGNORECASE)
    by_split = re.split(r"\bBY\b", body, maxsplit=1, flags=re.IGNORECASE)
    assignments_text = by_split[0].strip()
    return [part.strip() for part in assignments_text.split(",") if "=" in part]


def _assert_no_inner_case_ts_value_args(query: str) -> None:
    """TS RATE-family funcs must not take CASE(...) as their value argument."""
    assert re.search(
        r"\b(?:RATE|IRATE|INCREASE|DELTA|DERIV)\(\s*CASE\(",
        query,
        flags=re.IGNORECASE,
    ) is None, query


def _assert_no_bare_ts_alongside_case(query: str) -> None:
    assignments = _stats_assignments(query)
    if not any("CASE(" in a for a in assignments):
        return
    for assignment in assignments:
        # Bare IRATE(field) is OK only when already nested in an outer CASE(...).
        if re.search(
            rf"\b(?:RATE|IRATE|INCREASE)\({_ESQL_FIELD_REFERENCE_PATTERN}\s*,",
            assignment,
        ):
            assert "CASE(" in assignment, assignment
        # Reject truly bare measures: SUM(IRATE(field, w)) with no CASE at all.
        if re.search(
            rf"=\s*(?:SUM|AVG|MIN|MAX)?\(?\s*(?:RATE|IRATE|INCREASE)\({_ESQL_FIELD_REFERENCE_PATTERN}\s*,",
            assignment,
        ) and "CASE(" not in assignment:
            raise AssertionError(f"bare TS alongside CASE siblings: {assignment}")


def _assert_eval_rhs_defined_after_stats(query: str) -> None:
    """Every simple EVAL ``alias = column`` must reference a STATS output name."""
    stages = [s.strip() for s in query.split("\n| ") if s.strip()]
    defined: set[str] = set()
    for stage in stages:
        text = stage[2:] if stage.startswith("| ") else stage
        if text.upper().startswith("STATS "):
            body = text[len("STATS ") :]
            by_split = re.split(r"\bBY\b", body, maxsplit=1, flags=re.IGNORECASE)
            for part in by_split[0].split(","):
                if "=" in part:
                    defined.add(part.split("=", 1)[0].strip().strip("`"))
            continue
        if text.upper().startswith("EVAL "):
            body = text[len("EVAL ") :]
            if "=" not in body:
                continue
            left, right = body.split("=", 1)
            rhs = right.strip()
            # Legend bind of a renamed STATS column (no operators).
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", rhs):
                assert rhs in defined, f"EVAL {left.strip()} = {rhs} references undefined column; defined={sorted(defined)}"
            defined.add(left.strip().strip("`"))


def test_merge_remaps_stats_alias_into_legend_eval_without_prior_eval_stages():
    """When a target has no EVAL stages, the STATS output name IS the metric.

    After rename (``freq → freq_B``), the legend EVAL must read ``freq_B``.
    """
    q_b = (
        "TS metrics-*\n"
        "| WHERE node_cpu_scaling_frequency_hertz IS NOT NULL\n"
        "| STATS node_cpu_scaling_frequency_hertz = "
        "MAX(LAST_OVER_TIME(node_cpu_scaling_frequency_hertz)) "
        "BY time_bucket = TBUCKET(5 minute)\n"
        "| KEEP time_bucket, node_cpu_scaling_frequency_hertz\n"
        "| SORT time_bucket ASC"
    )
    q_a = (
        "TS metrics-*\n"
        "| WHERE node_cpu_scaling_frequency_max_hertz IS NOT NULL\n"
        "| STATS node_cpu_scaling_frequency_max_hertz = "
        "AVG(node_cpu_scaling_frequency_max_hertz) "
        "BY time_bucket = TBUCKET(5 minute)\n"
        "| KEEP time_bucket, node_cpu_scaling_frequency_max_hertz\n"
        "| SORT time_bucket ASC"
    )
    q_c = (
        "TS metrics-*\n"
        "| WHERE node_cpu_scaling_frequency_min_hertz IS NOT NULL\n"
        "| STATS node_cpu_scaling_frequency_min_hertz = "
        "AVG(node_cpu_scaling_frequency_min_hertz) "
        "BY time_bucket = TBUCKET(5 minute)\n"
        "| KEEP time_bucket, node_cpu_scaling_frequency_min_hertz\n"
        "| SORT time_bucket ASC"
    )
    merged = _merge_pretranslated_xy_queries(
        [
            _translation("B", "CPU", "node_cpu_scaling_frequency_hertz", q_b),
            _translation("A", "Max", "node_cpu_scaling_frequency_max_hertz", q_a),
            _translation("C", "Min", "node_cpu_scaling_frequency_min_hertz", q_c),
        ]
    )
    assert merged is not None
    query = merged["query"]
    assert "node_cpu_scaling_frequency_hertz_B =" in query
    assert "EVAL CPU = node_cpu_scaling_frequency_hertz_B" in query
    assert "EVAL Max = node_cpu_scaling_frequency_max_hertz_A" in query
    assert "EVAL Min = node_cpu_scaling_frequency_min_hertz_C" in query
    # Pre-rename names must not appear as EVAL sources.
    assert "EVAL CPU = node_cpu_scaling_frequency_hertz\n" not in query
    assert "EVAL Max = node_cpu_scaling_frequency_max_hertz\n" not in query
    assert "EVAL Min = node_cpu_scaling_frequency_min_hertz\n" not in query
    _assert_eval_rhs_defined_after_stats(query)
    assert structural_errors(check_esql_structure(query)) == []


def test_merge_remaps_reserved_stats_alias_into_legend_eval():
    """Backtick syntax must not become part of the alias-map key."""
    q_in = (
        "TS metrics-*\n"
        "| STATS `IN` = SUM(RATE(haproxy_frontend_bytes_in_total, 5m)) "
        "BY time_bucket = TBUCKET(5 minute)\n"
        "| KEEP time_bucket, `IN`\n"
        "| SORT time_bucket ASC"
    )
    q_out = (
        "TS metrics-*\n"
        "| STATS OUT = SUM(RATE(haproxy_frontend_bytes_out_total, 5m)) "
        "BY time_bucket = TBUCKET(5 minute)\n"
        "| KEEP time_bucket, OUT\n"
        "| SORT time_bucket ASC"
    )

    merged = _merge_pretranslated_xy_queries(
        [
            _translation("A", "IN", "IN", q_in),
            _translation("B", "OUT", "OUT", q_out),
        ]
    )

    assert merged is not None
    query = merged["query"]
    assert "IN_A = SUM(RATE(haproxy_frontend_bytes_in_total, 5m))" in query
    assert "EVAL `IN` = IN_A" in query
    assert "EVAL `IN` = IN\n" not in query
    assert "EVAL OUT = OUT_B" in query
    _assert_eval_rhs_defined_after_stats(query)


def test_merge_rewrites_reserved_alias_references_inside_eval():
    """Quoted STATS/output aliases must compare and rewrite by column name."""
    q_in = (
        "TS metrics-*\n"
        "| STATS `IN` = SUM(RATE(haproxy_frontend_bytes_in_total, 5m)), "
        "tmp = AVG(haproxy_frontend_bytes_in_total) "
        "BY time_bucket = TBUCKET(5 minute)\n"
        "| EVAL `IN` = COALESCE(`IN`, tmp)\n"
        "| KEEP time_bucket, `IN`\n"
        "| SORT time_bucket ASC"
    )
    q_out = (
        "TS metrics-*\n"
        "| STATS OUT = SUM(RATE(haproxy_frontend_bytes_out_total, 5m)) "
        "BY time_bucket = TBUCKET(5 minute)\n"
        "| KEEP time_bucket, OUT\n"
        "| SORT time_bucket ASC"
    )

    merged = _merge_pretranslated_xy_queries(
        [
            _translation("A", "IN", "IN", q_in),
            _translation("B", "OUT", "OUT", q_out),
        ]
    )

    assert merged is not None
    query = merged["query"]
    assert "COALESCE(`IN`" not in query
    assert "EVAL `IN` = COALESCE(IN_A, tmp_A)" in query
    assert "EVAL `IN` = IN\n" not in query
    _assert_eval_rhs_defined_after_stats(query)


def test_merge_rewrites_inner_case_irate_to_outer_case():
    """Fused guest/total CPU ratios must not emit IRATE(CASE(...)).

    Elasticsearch ClassCasts ``IRATE(CASE(cond, field, NULL), window)``. The
    legal shape is ``CASE(cond, IRATE(field, window), NULL)``; bare IRATE
    siblings are fine alongside that outer CASE.
    """
    q_user = (
        "TS metrics-*\n"
        "| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend\n"
        "| STATS numerator = SUM(IRATE(CASE((mode == \"user\"), "
        "node_cpu_guest_seconds_total, NULL), 1m)), "
        "denominator = SUM(IRATE(node_cpu_seconds_total, 1m)) "
        "BY time_bucket = TBUCKET(5 minute), instance\n"
        "| EVAL ratio = numerator / denominator\n"
        "| KEEP time_bucket, instance, ratio\n"
        "| SORT time_bucket ASC"
    )
    q_nice = (
        "TS metrics-*\n"
        "| WHERE @timestamp >= ?_tstart AND @timestamp <= ?_tend\n"
        "| STATS numerator = SUM(IRATE(CASE((mode == \"nice\"), "
        "node_cpu_guest_seconds_total, NULL), 1m)), "
        "denominator = SUM(IRATE(node_cpu_seconds_total, 1m)) "
        "BY time_bucket = TBUCKET(5 minute), instance\n"
        "| EVAL ratio = numerator / denominator\n"
        "| KEEP time_bucket, instance, ratio\n"
        "| SORT time_bucket ASC"
    )
    merged = _merge_pretranslated_xy_queries(
        [
            _translation("A", "Guest", "ratio", q_user),
            _translation("B", "GuestNice", "ratio", q_nice),
        ]
    )
    assert merged is not None
    query = merged["query"]
    assert 'CASE((mode == "user"), IRATE(node_cpu_guest_seconds_total, 1m), NULL)' in query
    assert 'CASE((mode == "nice"), IRATE(node_cpu_guest_seconds_total, 1m), NULL)' in query
    assert "CASE(true, IRATE(node_cpu_seconds_total, 1m), NULL)" in query
    assert "SUM(IRATE(node_cpu_seconds_total, 1m))" not in query
    _assert_no_inner_case_ts_value_args(query)
    _assert_no_bare_ts_alongside_case(query)
    assert structural_errors(check_esql_structure(query)) == []


def test_shared_helper_rewrites_inner_case_irate_for_formula_fusion_and_merge():
    """Formula-plan fusion and pretranslated merge share the CASE-shape helper."""
    assignments = [
        'numerator = SUM(IRATE(CASE((mode == "user"), node_cpu_guest_seconds_total, NULL), 1m))',
        "denominator = SUM(IRATE(node_cpu_seconds_total, 1m))",
    ]
    wrapped = _wrap_bare_ts_value_args_when_case_siblings(assignments)
    assert 'CASE((mode == "user"), IRATE(node_cpu_guest_seconds_total, 1m), NULL)' in wrapped[0]
    assert "CASE(true, IRATE(node_cpu_seconds_total, 1m), NULL)" in wrapped[1]
    assert "IRATE(CASE(" not in wrapped[0]
    assert "IRATE(CASE(" not in wrapped[1]

    specs = [
        MeasureSpec(
            source_type="TS",
            time_filter="@timestamp >= ?_tstart AND @timestamp <= ?_tend",
            bucket_expr="time_bucket = TBUCKET(5 minute)",
            group_fields=["instance"],
            filters=[],
            alias="guest",
            stats_expr='SUM(IRATE(CASE((mode == "user"), node_cpu_guest_seconds_total, NULL), 1m))',
            final_alias="guest",
            metric_field="node_cpu_guest_seconds_total",
        ),
        MeasureSpec(
            source_type="TS",
            time_filter="@timestamp >= ?_tstart AND @timestamp <= ?_tend",
            bucket_expr="time_bucket = TBUCKET(5 minute)",
            group_fields=["instance"],
            filters=[],
            alias="total",
            stats_expr="SUM(IRATE(node_cpu_seconds_total, 1m))",
            final_alias="total",
            metric_field="node_cpu_seconds_total",
        ),
    ]
    result = _build_shared_measure_pipeline("metrics-*", specs)
    assert result is not None
    parts, _, _ = result
    stats_line = next(line for line in parts if line.startswith("| STATS"))
    assert 'CASE((mode == "user"), IRATE(node_cpu_guest_seconds_total, 1m), NULL)' in stats_line
    assert "CASE(true, IRATE(node_cpu_seconds_total, 1m), NULL)" in stats_line
    assert "SUM(IRATE(node_cpu_seconds_total, 1m))" not in stats_line
    assert "IRATE(CASE(" not in stats_line


def test_shared_helper_rewrites_backtick_quoted_recording_rule_field():
    assignments = [
        'numerator = SUM(IRATE(CASE((mode == "user"), `node:cpu:guest`, NULL), 1m))',
        "denominator = SUM(IRATE(`node:cpu:total`, 1m))",
    ]

    wrapped = _wrap_bare_ts_value_args_when_case_siblings(assignments)

    assert wrapped[0] == (
        'numerator = SUM(CASE((mode == "user"), IRATE(`node:cpu:guest`, 1m), NULL))'
    )
    assert wrapped[1] == (
        "denominator = SUM(CASE(true, IRATE(`node:cpu:total`, 1m), NULL))"
    )
    assert "IRATE(`node:cpu:total`, 1m)" not in wrapped[1].replace(
        "CASE(true, IRATE(`node:cpu:total`, 1m), NULL)", ""
    )


def test_merge_normalizes_bare_over_time_when_sibling_is_wrapped():
    """Pretranslated merge must not leave bare+wrapped OVER_TIME in one STATS."""
    q_wrapped = (
        "TS metrics-*\n"
        "| STATS process_virtual_memory_bytes = "
        "AVG(AVG_OVER_TIME(process_virtual_memory_bytes, 5m)) "
        "BY time_bucket = TBUCKET(5 minute), instance\n"
        "| KEEP time_bucket, instance, process_virtual_memory_bytes\n"
        "| SORT time_bucket ASC"
    )
    q_bare = (
        "TS metrics-*\n"
        "| STATS process_resident_memory_max_bytes = "
        "AVG_OVER_TIME(process_resident_memory_max_bytes, 5m) "
        "BY time_bucket = TBUCKET(5 minute), instance\n"
        "| KEEP time_bucket, instance, process_resident_memory_max_bytes\n"
        "| SORT time_bucket ASC"
    )
    merged = _merge_pretranslated_xy_queries(
        [
            _translation("A", "virt", "process_virtual_memory_bytes", q_wrapped),
            _translation("B", "res", "process_resident_memory_max_bytes", q_bare),
        ]
    )
    assert merged is not None
    query = merged["query"]
    assert "AVG(AVG_OVER_TIME(process_resident_memory_max_bytes, 5m))" in query
    assert re.search(
        r"(?:STATS|,)\s*`?process_resident_memory_max_bytes_B`?\s*=\s*AVG_OVER_TIME\(",
        query.replace("\n", " "),
    ) is None
    assert structural_errors(check_esql_structure(query)) == []


def test_join_family_emits_outer_case_irate_for_filtered_numerator():
    """Single-target join-ratio fast path must use outer CASE, not IRATE(CASE)."""
    from observability_migration.adapters.source.grafana.translate import (
        translate_promql_to_esql,
    )

    expr = (
        'sum by(instance) (irate(node_cpu_guest_seconds_total{instance="n", mode="user"}[1m]))'
        " / on(instance) group_left "
        'sum by (instance)(irate(node_cpu_seconds_total{instance="n"}[1m]))'
    )
    ctx = translate_promql_to_esql(
        expr,
        esql_index="metrics-*",
        rule_pack=RulePackConfig(),
        resolver=SchemaResolver(RulePackConfig()),
    )
    assert ctx.feasibility == "feasible"
    query = ctx.esql_query or ""
    assert 'CASE((mode == "user"), IRATE(node_cpu_guest_seconds_total, 1m), NULL)' in query
    assert "CASE(true, IRATE(node_cpu_seconds_total, 1m), NULL)" in query
    assert "SUM(IRATE(node_cpu_seconds_total, 1m))" not in query
    _assert_no_inner_case_ts_value_args(query)
    _assert_no_bare_ts_alongside_case(query)
    assert structural_errors(check_esql_structure(query)) == []


def _walk_panels(panels, found):
    for panel in panels or []:
        title = (panel.get("title") or "").strip()
        if title in _NODE_EXPORTER_SMOKE_TITLES:
            found[title] = panel
        _walk_panels(panel.get("panels"), found)


def test_node_exporter_fixture_smoke_slice_merge_invariants():
    """Offline smoke slice for the two Node Exporter panels that failed live.

    Translates panels from ``infra/grafana/dashboards/node-exporter-full.json``
    (also pinned as community corpus bug_seed id 1860) and asserts the merge
    invariants that live ES|QL smoke previously caught.
    """
    dashboard = json.loads(_FIXTURE.read_text())
    found: dict = {}
    _walk_panels(dashboard.get("panels"), found)
    assert set(found) == set(_NODE_EXPORTER_SMOKE_TITLES)

    rule_pack = RulePackConfig()
    resolver = SchemaResolver(rule_pack)
    for title, panel in found.items():
        yaml_panel, result = translate_panel(
            panel,
            datasource_index="metrics-*",
            esql_index="metrics-*",
            rule_pack=rule_pack,
            resolver=resolver,
        )
        assert result.status in {"migrated", "migrated_with_warnings"}, (
            f"{title}: unexpected status {result.status}: {result.reasons}"
        )
        query = yaml_panel["esql"]["query"]
        if title == "CPU Frequency Scaling":
            assert "EVAL CPU = node_cpu_scaling_frequency_hertz_B" in query
            assert "EVAL CPU = node_cpu_scaling_frequency_hertz\n" not in query
            _assert_eval_rhs_defined_after_stats(query)
        else:
            assert 'CASE((mode == "user"), IRATE(node_cpu_guest_seconds_total, 1m), NULL)' in query
            assert "SUM(IRATE(node_cpu_seconds_total, 1m))" not in query
            assert "CASE(true, IRATE(node_cpu_seconds_total, 1m), NULL)" in query
            _assert_no_inner_case_ts_value_args(query)
            _assert_no_bare_ts_alongside_case(query)
        assert structural_errors(check_esql_structure(query)) == []
