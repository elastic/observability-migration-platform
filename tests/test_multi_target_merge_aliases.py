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
        "| STATS `IN` = SUM(RATE(haproxy_frontend_bytes_in_total)) "
        "BY time_bucket = TBUCKET(5 minute)\n"
        "| KEEP time_bucket, `IN`\n"
        "| SORT time_bucket ASC"
    )
    q_out = (
        "TS metrics-*\n"
        "| STATS OUT = SUM(RATE(haproxy_frontend_bytes_out_total)) "
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
    assert "IN_A = SUM(RATE(haproxy_frontend_bytes_in_total))" in query
    assert "EVAL `IN` = IN_A" in query
    assert "EVAL `IN` = IN\n" not in query
    assert "EVAL OUT = OUT_B" in query
    _assert_eval_rhs_defined_after_stats(query)


def test_merge_rewrites_reserved_alias_references_inside_eval():
    """Quoted STATS/output aliases must compare and rewrite by column name."""
    q_in = (
        "TS metrics-*\n"
        "| STATS `IN` = SUM(RATE(haproxy_frontend_bytes_in_total)), "
        "tmp = AVG(haproxy_frontend_bytes_in_total) "
        "BY time_bucket = TBUCKET(5 minute)\n"
        "| EVAL `IN` = COALESCE(`IN`, tmp)\n"
        "| KEEP time_bucket, `IN`\n"
        "| SORT time_bucket ASC"
    )
    q_out = (
        "TS metrics-*\n"
        "| STATS OUT = SUM(RATE(haproxy_frontend_bytes_out_total)) "
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
        "denominator = SUM(IRATE(node_cpu_seconds_total)) "
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
        "denominator = SUM(IRATE(node_cpu_seconds_total)) "
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
    assert "CASE(true, IRATE(node_cpu_seconds_total), NULL)" in query
    assert "SUM(IRATE(node_cpu_seconds_total))" not in query
    _assert_no_inner_case_ts_value_args(query)
    _assert_no_bare_ts_alongside_case(query)
    assert structural_errors(check_esql_structure(query)) == []


def test_shared_helper_rewrites_inner_case_irate_for_formula_fusion_and_merge():
    """Formula-plan fusion and pretranslated merge share the CASE-shape helper."""
    assignments = [
        'numerator = SUM(IRATE(CASE((mode == "user"), node_cpu_guest_seconds_total, NULL), 1m))',
        "denominator = SUM(IRATE(node_cpu_seconds_total))",
    ]
    wrapped = _wrap_bare_ts_value_args_when_case_siblings(assignments)
    assert 'CASE((mode == "user"), IRATE(node_cpu_guest_seconds_total, 1m), NULL)' in wrapped[0]
    assert "CASE(true, IRATE(node_cpu_seconds_total), NULL)" in wrapped[1]
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
            stats_expr="SUM(IRATE(node_cpu_seconds_total))",
            final_alias="total",
            metric_field="node_cpu_seconds_total",
        ),
    ]
    result = _build_shared_measure_pipeline("metrics-*", specs)
    assert result is not None
    parts, _, _ = result
    stats_line = next(line for line in parts if line.startswith("| STATS"))
    assert 'CASE((mode == "user"), IRATE(node_cpu_guest_seconds_total, 1m), NULL)' in stats_line
    assert "CASE(true, IRATE(node_cpu_seconds_total), NULL)" in stats_line
    assert "SUM(IRATE(node_cpu_seconds_total))" not in stats_line
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
    assert 'CASE((mode == "user"), IRATE(node_cpu_guest_seconds_total), NULL)' in query
    assert "CASE(true, IRATE(node_cpu_seconds_total), NULL)" in query
    assert "SUM(IRATE(node_cpu_seconds_total))" not in query
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
            # Legend alias appears directly in the STATS term; no separate EVAL
            # needed for a plain aggregate (the optimization collapses the rename).
            assert "CPU =" in query
            # The bare un-suffixed name must not be exposed as an EVAL source.
            assert "EVAL CPU = node_cpu_scaling_frequency_hertz\n" not in query
            _assert_eval_rhs_defined_after_stats(query)
        else:
            assert 'CASE((mode == "user"), IRATE(node_cpu_guest_seconds_total), NULL)' in query
            assert "SUM(IRATE(node_cpu_seconds_total))" not in query
            assert "CASE(true, IRATE(node_cpu_seconds_total), NULL)" in query
            _assert_no_inner_case_ts_value_args(query)
            _assert_no_bare_ts_alongside_case(query)
        assert structural_errors(check_esql_structure(query)) == []


def test_multi_target_simple_renames_folded_into_stats():
    """Pure-rename EVAL steps are eliminated: final alias goes directly into STATS.

    For targets where ``plan.expr`` is the bare intermediate STATS alias (no
    formula, no negation, no post_filter), the translator must rename the STATS
    column in-place rather than emitting ``| EVAL result = intermediate``.
    """
    panel = {
        "id": 1,
        "type": "timeseries",
        "title": "Hits / Misses",
        "datasource": {"type": "prometheus", "uid": "prom"},
        "targets": [
            {
                "expr": "irate(redis_keyspace_hits_total[5m])",
                "legendFormat": "hits",
                "refId": "A",
            },
            {
                "expr": "irate(redis_keyspace_misses_total[5m])",
                "legendFormat": "misses",
                "refId": "B",
            },
        ],
    }
    rule_pack = RulePackConfig()
    rule_pack.metric_kinds.update(
        {"redis_keyspace_hits_total": "counter", "redis_keyspace_misses_total": "counter"}
    )
    resolver = SchemaResolver(rule_pack)
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rule_pack,
        resolver=resolver,
    )
    assert result.status in {"migrated", "migrated_with_warnings"}
    query = yaml_panel["esql"]["query"]

    # Final aliases must appear directly in the STATS term — no separate EVAL.
    assert "hits =" in query
    assert "misses =" in query
    assert "| EVAL hits =" not in query
    assert "| EVAL misses =" not in query
    # Intermediate suffixed aliases must not leak into the final query.
    assert "redis_keyspace_hits_total_A" not in query
    assert "redis_keyspace_misses_total_B" not in query
    # Metric fields must still be wired up correctly.
    metric_fields = [m["field"] for m in yaml_panel["esql"]["metrics"]]
    assert "hits" in metric_fields
    assert "misses" in metric_fields
    assert structural_errors(check_esql_structure(query)) == []


def test_formula_and_simple_target_sharing_same_metric_deduplicates_stats():
    """A formula target and a simple target that both aggregate the same metric
    must not produce two identical STATS terms.

    Example: ``not_expiring = keys - keys_expiring`` (formula, Target A) and
    ``expiring = keys_expiring`` (simple, Target B).  Both require
    ``SUM(keys_expiring)`` — only one STATS term should appear, and the formula
    EVAL must reference the simple target's final alias directly.
    """
    panel = {
        "id": 1,
        "type": "timeseries",
        "title": "Expiring vs Not-Expiring Keys",
        "datasource": {"type": "prometheus", "uid": "prom"},
        "targets": [
            {
                "expr": "sum(redis_db_keys) - sum(redis_db_keys_expiring)",
                "legendFormat": "not expiring",
                "refId": "A",
            },
            {
                "expr": "sum(redis_db_keys_expiring)",
                "legendFormat": "expiring",
                "refId": "B",
            },
        ],
    }
    rule_pack = RulePackConfig()
    resolver = SchemaResolver(rule_pack)
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rule_pack,
        resolver=resolver,
    )
    assert result.status in {"migrated", "migrated_with_warnings"}
    query = yaml_panel["esql"]["query"]

    # SUM(redis_db_keys_expiring) must appear exactly once in the STATS line.
    stats_line = next(
        (line for line in query.splitlines() if "| STATS" in line), ""
    )
    # The field may be prefixed with "metrics." when a live ES resolver is used;
    # assert on the bare name to keep the test resolver-agnostic.
    assert stats_line.count("SUM(redis_db_keys_expiring)") == 1 or \
           stats_line.count("SUM(metrics.redis_db_keys_expiring)") == 1, (
        f"Duplicate SUM in STATS: {stats_line}"
    )
    # The formula EVAL must use the simple target's alias, not an internal alias.
    assert "| EVAL not_expiring = " in query
    eval_line = next(
        line for line in query.splitlines()
        if "not_expiring" in line and "EVAL" in line
    )
    assert "expiring" in eval_line, f"Formula EVAL should use 'expiring': {eval_line}"
    # Ugly intermediate aliases must not appear anywhere.
    assert "expiring_A_rhs" not in query
    assert "expiring_B" not in query
    assert structural_errors(check_esql_structure(query)) == []


def _seed(resolver, fields):
    resolver._discovery_attempted = True
    resolver._field_cache = fields
    resolver._discovered_mappings = {}
    resolver._schema_profile_cache_id = None


_KW = {"keyword": {"type": "keyword", "searchable": True, "aggregatable": True}}
_DBL = {"double": {"type": "double", "aggregatable": True}}


def test_bare_irate_keeps_real_legend_label_as_breakdown():
    """A bare ``irate()`` with a ``{{ label }}`` legend must keep that label in BY.

    ES|QL ``TS`` emits one row per TSID per bucket, so dropping the legend label
    yields N rows per bucket with no column identifying the series. Kibana binds
    series identity to a breakdown *column*, not the TSID, so those render as N
    indistinguishable same-named series (observed on Redis 763 Hits/Misses once a
    second instance existed: two "hits" and two "misses" entries in one tooltip).
    """
    panel = {
        "id": 1, "type": "timeseries", "title": "Hits / Misses per Sec",
        "datasource": {"type": "prometheus", "uid": "prom"},
        "targets": [
            {"expr": 'irate(redis_keyspace_hits_total{instance=~"$instance"}[5m])',
             "legendFormat": "hits, {{ instance }}", "refId": "A"},
            {"expr": 'irate(redis_keyspace_misses_total{instance=~"$instance"}[5m])',
             "legendFormat": "misses, {{ instance }}", "refId": "B"},
        ],
    }
    rule_pack = RulePackConfig()
    resolver = SchemaResolver(rule_pack, field_profile="prometheus_native")
    _seed(resolver, {
        "labels.instance": _KW,
        "metrics.redis_keyspace_hits_total": _DBL,
        "metrics.redis_keyspace_misses_total": _DBL,
    })
    yaml_panel, result = translate_panel(
        panel, datasource_index="metrics-*", esql_index="metrics-*",
        rule_pack=rule_pack, resolver=resolver,
    )
    assert result.status in {"migrated", "migrated_with_warnings"}
    query = yaml_panel["esql"]["query"]
    stats = next(line for line in query.splitlines() if "| STATS" in line)
    assert "labels.instance" in stats, (
        f"real legend label must stay in BY so series are distinguishable: {stats}"
    )
    assert structural_errors(check_esql_structure(query)) == []


def test_bare_rate_drops_phantom_legend_label():
    """A legendFormat placeholder that is not a real target field must still drop.

    Redis 763's Network I/O uses ``{{ input }}`` / ``{{ output }}``, which are not
    Prometheus labels. Grouping by them would reference a non-existent column and
    break target fusion, so the existing drop must be preserved for that case.
    """
    panel = {
        "id": 2, "type": "timeseries", "title": "Network I/O",
        "datasource": {"type": "prometheus", "uid": "prom"},
        "targets": [
            {"expr": 'sum(rate(redis_net_input_bytes_total{instance=~"$instance"}[5m]))',
             "legendFormat": "{{ input }}", "refId": "A"},
        ],
    }
    rule_pack = RulePackConfig()
    resolver = SchemaResolver(rule_pack, field_profile="prometheus_native")
    _seed(resolver, {
        "labels.instance": _KW,
        "metrics.redis_net_input_bytes_total": _DBL,
    })
    yaml_panel, _result = translate_panel(
        panel, datasource_index="metrics-*", esql_index="metrics-*",
        rule_pack=rule_pack, resolver=resolver,
    )
    query = yaml_panel["esql"]["query"]
    assert "input" not in query.split("| STATS")[-1].split("BY")[-1], (
        f"phantom legend label must not become a BY field: {query}"
    )
    assert structural_errors(check_esql_structure(query)) == []


def _panel(targets, title="Hit ratio per instance"):
    return {
        "id": 1, "type": "graph", "title": title,
        "datasource": {"type": "prometheus", "uid": "prom"},
        "targets": targets,
    }


# An explicitly vector-matched join: genuinely unalignable, so still refused.
# (The self-referential hit ratio this used to use is now translated by
# colocated_binary_agg_family, so it no longer exercises the rescue path.)
_RATIO_NF = "sum(node_a / on(x) group_left node_b)"


def _translate(panel):
    rule_pack = RulePackConfig()
    return translate_panel(
        panel, datasource_index="metrics-*", esql_index="metrics-*",
        rule_pack=rule_pack, resolver=SchemaResolver(rule_pack),
    )


def test_scalar_constant_does_not_rescue_a_not_feasible_panel():
    """A Grafana reference line must not make a failed panel look migrated.

    Grafana 14091 "Hit ratio per instance" pairs an unsupported self-referential
    ratio with ``expr: 1``. Filtering not-feasible targets left only the constant,
    so the panel reported success and rendered ``ROW constant_value = 1.0`` — a
    flat line at 1 with the real series silently gone.
    """
    _panel_yaml, result = _translate(_panel([
        {"expr": _RATIO_NF, "refId": "A", "legendFormat": "{{ instance }}"},
        {"expr": "1", "refId": "B"},
    ]))
    assert result.status == "not_feasible", (
        f"constant target must not rescue the panel, got {result.status}"
    )


def test_constant_only_panel_is_still_migrated():
    """A panel whose ONLY target is a constant is legitimately a constant."""
    _panel_yaml, result = _translate(_panel([{"expr": "1", "refId": "A"}], title="Threshold"))
    assert result.status in {"migrated", "migrated_with_warnings"}


def test_constant_alongside_a_feasible_target_still_migrates():
    """The guard must only fire when EVERY substantive target failed."""
    panel_yaml, result = _translate(_panel([
        {"expr": "sum(redis_connected_clients)", "refId": "A"},
        {"expr": "1", "refId": "B"},
    ]))
    assert result.status in {"migrated", "migrated_with_warnings"}
    assert "redis_connected_clients" in panel_yaml["esql"]["query"]


def test_target_specific_filters_are_folded_not_anded_globally():
    """Per-target filters must not become sibling global WHERE stages.

    Each target is translated alone, so its pre-STATS filters describe only
    that target: a presence guard, a label matcher (``mode == "idle"``), a
    device filter. Emitting them as sibling ``| WHERE`` stages ANDs them across
    every target, and because no single document carries every target's metric
    the fused query matches nothing -- the panel renders empty instead of
    erroring, which is the worst failure mode there is.

    Caught live on Node Exporter Dashboard EN "Server Resource Overview": the
    fused query grew 16 ANDed WHERE stages and returned 0 rows; folding the
    target-specific ones into each measure returned 72.
    """
    q_cpu = (
        "TS metrics-*\n"
        '| WHERE job RLIKE ?job\n'
        '| WHERE mode == "idle"\n'
        "| WHERE node_cpu_seconds_total IS NOT NULL\n"
        "| STATS node_cpu_seconds_total = AVG(RATE(node_cpu_seconds_total)) "
        "BY time_bucket = TBUCKET(5 minute)\n"
        "| SORT time_bucket ASC"
    )
    q_load = (
        "TS metrics-*\n"
        "| WHERE job RLIKE ?job\n"
        "| WHERE node_load5 IS NOT NULL\n"
        "| STATS node_load5 = AVG(node_load5) BY time_bucket = TBUCKET(5 minute)\n"
        "| SORT time_bucket ASC"
    )
    merged = _merge_pretranslated_xy_queries(
        [
            _translation("F", "CPU", "node_cpu_seconds_total", q_cpu),
            _translation("L", "Load", "node_load5", q_load),
        ]
    )
    assert merged is not None
    query = merged["query"]

    # The shared filter stays global; nothing target-specific does.
    where_stages = [line.strip() for line in query.splitlines() if line.strip().startswith("| WHERE")]
    assert where_stages == ["| WHERE job RLIKE ?job"], where_stages

    # No metric is required to be present alongside a different target's metric.
    assert "| WHERE node_load5 IS NOT NULL" not in query
    assert "| WHERE node_cpu_seconds_total IS NOT NULL" not in query

    # The filters are preserved, folded into their own measure.
    assert 'mode == "idle"' in query
    assert "CASE(" in query
    stats = " ".join(_stats_assignments(query))
    assert "node_load5" in stats and "node_cpu_seconds_total" in stats


def test_counter_range_siblings_are_wrapped_outer_not_inner():
    """A bare RATE/IRATE sibling must gain an OUTER CASE, never an inner one.

    Inner-CASE value args are exactly what _rewrite_ts_inner_case_to_outer_case
    exists to remove: a time-series function may not take CASE as its value
    argument. Emitting the inner shape when wrapping bare siblings recreated the
    form that pass had just removed, so the fused query was rejected. OVER_TIME
    genuinely uses the inner shape and keeps it.
    """
    from observability_migration.adapters.source.grafana.promql import (
        _wrap_bare_ts_value_args_when_case_siblings,
    )

    out = _wrap_bare_ts_value_args_when_case_siblings([
        'a = SUM(CASE((mode == "idle"), IRATE(cpu_seconds_total), NULL))',
        "b = SUM(IRATE(container_cpu_usage_seconds_total))",
    ])
    assert "CASE(true, IRATE(container_cpu_usage_seconds_total), NULL)" in out[1]
    assert "IRATE(CASE(true," not in out[1]

    over_time = _wrap_bare_ts_value_args_when_case_siblings([
        'a = SUM(CASE((mode == "idle"), AVG_OVER_TIME(x, 2m), NULL))',
        "b = SUM(AVG_OVER_TIME(y, 2m))",
    ])
    assert "AVG_OVER_TIME(CASE(true, y, NULL), 2m)" in over_time[1]


def test_outer_case_is_detected_when_the_condition_contains_a_comma():
    """A comma inside the CASE condition must not hide the outer shape.

    `COALESCE(label, "") RLIKE ?var` is an ordinary label matcher -- PromQL reads
    an absent label as "". The outer-shape detector matched its condition with
    `[^,]+`, so that comma made it stop recognising the shape, and the caller
    then nested an identity CASE inside the outer filter CASE, producing
    `SUM(CASE(cond, RATE(CASE(true, field, w), NULL)))`.
    """
    from observability_migration.adapters.source.grafana.promql import (
        _wrap_bare_ts_value_args_when_case_siblings,
    )

    assignments = [
        'a = SUM(CASE((COALESCE(app, "") RLIKE ?app) and (COALESCE(inst, "") RLIKE ?inst), '
        "RATE(jvm_gc_pause_seconds_sum), NULL))",
        "b = MAX(LAST_OVER_TIME(system_cpu_count))",
    ]
    out = _wrap_bare_ts_value_args_when_case_siblings(assignments)
    assert "RATE(CASE(true," not in out[0]
    assert "RATE(jvm_gc_pause_seconds_sum)" in out[0]


def test_merge_refuses_when_the_output_column_lives_in_a_dropped_stage():
    """A nested aggregation cannot be fused; refusing beats emitting a broken query.

    PromQL ``min(sum(x) by (instance))`` translates to an inner STATS grouped by
    instance plus an outer STATS producing ``<metric>_min``. Only the first STATS
    survives this merge, so binding the series to ``<metric>_min`` emitted
    ``EVAL series = kube_node_status_allocatable_cpu_cores_min`` against a column
    nothing defined -- "Unknown column" in Kibana. Refuse so the panel falls back
    to a path that can express the shape.
    """
    def nested(ref, metric):
        return (
            "FROM metrics-*\n"
            f"| STATS inner_val = SUM({metric}) BY time_bucket = BUCKET(@timestamp, 50), instance\n"
            f"| STATS {metric}_min = MIN(inner_val) BY time_bucket\n"
            f"| KEEP time_bucket, {metric}_min"
        )
    t1 = _translation("A", "Alloc", "kube_node_status_allocatable_cpu_cores_min",
                      nested("A", "kube_node_status_allocatable_cpu_cores"))
    t2 = _translation("B", "Req", "kube_pod_container_resource_requests_cpu_cores_min",
                      nested("B", "kube_pod_container_resource_requests_cpu_cores"))
    assert _merge_pretranslated_xy_queries([t1, t2]) is None


def test_summary_collapse_second_stats_still_merges():
    """A later STATS that re-aggregates the same alias is safe to drop."""
    def collapsed(metric):
        return (
            "TS metrics-*\n"
            f"| STATS {metric} = SUM({metric}) BY time_bucket = TBUCKET(5 minute), instance\n"
            f"| STATS {metric} = MAX({metric}) BY instance\n"
            f"| KEEP instance, {metric}"
        )
    t1 = _translation("A", "One", "metric_one", collapsed("metric_one"))
    t2 = _translation("B", "Two", "metric_two", collapsed("metric_two"))
    assert _merge_pretranslated_xy_queries([t1, t2]) is not None


def test_drop_only_names_columns_that_exist_at_that_point():
    """The DROP replacing a stripped KEEP must not name columns that are gone.

    Two ways it went wrong on Node Exporter Full, each killing the whole query
    with "Unknown column":

    * A later STATS *replaces* the schema, so an alias from an earlier STATS it
      does not re-emit no longer exists ("Speed": ``DROP
      node_network_speed_bytes_instance_job`` after a collapse STATS).
    * The DROP is inserted where the KEEP was, so a column a LATER EVAL creates
      does not exist yet ("Node Exporter Scrape": ``DROP __labels, __pairs,
      label`` above the ``EVAL __labels = ...`` defining them).
    """
    from observability_migration.adapters.source.grafana.panels import (
        _strip_dotted_group_keep,
    )

    query = (
        "TS metrics-*\n"
        "| STATS inner = AVG(metrics.x) BY time_bucket = TBUCKET(1, ?_tstart, ?_tend), labels.device\n"
        "| EVAL computed_value = (inner * 8)\n"
        "| STATS computed_value = MAX(computed_value) BY labels.device\n"
        "| KEEP labels.device, computed_value"
    )
    out = _strip_dotted_group_keep(query)
    assert "inner" not in out.split("| STATS", 2)[-1], out
    # A grouping key expressed as TBUCKET(...) must never leak its ?params into
    # the DROP -- splitting the BY clause on bare commas reached inside the call
    # and produced `DROP ?_tstart`, which failed every such query.
    assert "?_tstart" not in out.replace("TBUCKET(1, ?_tstart, ?_tend)", ""), out


def test_label_join_multi_target_fuses_with_single_eval_concat():
    """Two targets both using label_join produce ONE shared EVAL CONCAT (not two).

    The formula-plan path unwraps label_join to its inner fragment for STATS
    building, then injects a single EVAL CONCAT (deduplicated by expression) and
    extends output_group_fields so the KEEP clause retains the derived column.

    Regression for the bug where destination_workload_var was dropped from KEEP.
    """
    panel = {
        "id": 1,
        "type": "timeseries",
        "title": "TCP Bytes (Istio)",
        "datasource": {"type": "prometheus", "uid": "prom"},
        "targets": [
            {
                "expr": (
                    'label_join('
                    'sum by (destination_workload, destination_workload_namespace, destination_service)'
                    ' (irate(istio_tcp_received_bytes_total{reporter=~"source|waypoint"}[1m])),'
                    ' "destination_workload_var", ".", "destination_workload",'
                    ' "destination_workload_namespace")'
                ),
                "legendFormat": "{{destination_workload_var}} Received",
                "refId": "A",
            },
            {
                "expr": (
                    'label_join('
                    'sum by (destination_workload, destination_workload_namespace, destination_service)'
                    ' (irate(istio_tcp_sent_bytes_total{reporter=~"source|waypoint"}[1m])),'
                    ' "destination_workload_var", ".", "destination_workload",'
                    ' "destination_workload_namespace")'
                ),
                "legendFormat": "{{destination_workload_var}} Sent",
                "refId": "B",
            },
        ],
    }
    rule_pack = RulePackConfig()
    resolver = SchemaResolver(rule_pack)
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rule_pack,
        resolver=resolver,
    )
    assert result.status in {"migrated", "migrated_with_warnings"}, (
        f"Expected migrated, got {result.status}: {result.reasons}"
    )
    query = yaml_panel["esql"]["query"]

    # Exactly one EVAL CONCAT — deduplication by expression must fire.
    concat_count = query.count("CONCAT(destination_workload, ")
    assert concat_count == 1, (
        f"Expected exactly one EVAL CONCAT, found {concat_count}:\n{query}"
    )

    # The derived column must be in the KEEP clause.
    assert "destination_workload_var" in query.split("| KEEP", 1)[-1], (
        f"destination_workload_var missing from KEEP:\n{query}"
    )

    # EVAL must appear after STATS and before SORT.
    stats_pos = query.find("| STATS")
    eval_pos = query.find("| EVAL destination_workload_var")
    sort_pos = query.find("| SORT")
    assert stats_pos != -1, "STATS missing"
    assert eval_pos != -1, "EVAL destination_workload_var missing"
    assert stats_pos < eval_pos < sort_pos, (
        f"Wrong stage order: STATS@{stats_pos} EVAL@{eval_pos} SORT@{sort_pos}"
    )

    # Both metric series must appear in KEEP.
    keep_section = query.split("| KEEP", 1)[-1]
    metric_fields = yaml_panel["esql"]["metrics"]
    for m in metric_fields:
        assert m["field"] in keep_section, (
            f"Metric field {m['field']!r} missing from KEEP:\n{query}"
        )

    assert structural_errors(check_esql_structure(query)) == [], query


def test_label_join_multi_target_group_fields_include_derived_column():
    """The derived label_join column is returned in group_fields of the fused result.

    This ensures downstream KEEP/column handling (including the panel's
    output_group_fields) carries the computed column through to Kibana.
    """
    from observability_migration.adapters.source.grafana.panels import (
        _build_multi_target_series_query,
    )
    from observability_migration.adapters.source.grafana.translate import (
        translate_promql_to_esql,
    )

    rule_pack = RulePackConfig()
    resolver = SchemaResolver(rule_pack)

    exprs = [
        (
            'label_join(sum by (ns, pod) (rate(kube_pod_restarts_total[5m])),'
            ' "ns_pod", "/", "ns", "pod")'
        ),
        (
            'label_join(sum by (ns, pod) (rate(kube_pod_oom_kills_total[5m])),'
            ' "ns_pod", "/", "ns", "pod")'
        ),
    ]
    translations = [
        translate_promql_to_esql(
            e, datasource_index="metrics-*", panel_type="timeseries",
            rule_pack=rule_pack, resolver=resolver,
        )
        for e in exprs
    ]
    merged = _build_multi_target_series_query(translations)
    assert merged is not None, "Multi-target fusion returned None"

    # Derived column in group_fields.
    assert "ns_pod" in merged["group_fields"], (
        f"ns_pod missing from group_fields: {merged['group_fields']}"
    )

    # Derived column appears in KEEP.
    keep_section = merged["query"].split("| KEEP", 1)[-1]
    assert "ns_pod" in keep_section, (
        f"ns_pod missing from KEEP:\n{merged['query']}"
    )

    # Only one EVAL CONCAT (same dst and expression → deduplicated).
    assert merged["query"].count("CONCAT(ns, ") == 1, (
        f"Expected one CONCAT, got multiple:\n{merged['query']}"
    )


def test_label_join_mixed_with_plain_target_fuses_correctly():
    """One label_join target and one plain target fuse via the formula-plan path.

    The EVAL CONCAT must appear only for the label_join target's derived column;
    the plain target contributes normally to the shared STATS without an EVAL.
    """
    panel = {
        "id": 1,
        "type": "timeseries",
        "title": "Requests (labelled) + Total",
        "datasource": {"type": "prometheus", "uid": "prom"},
        "targets": [
            {
                "expr": (
                    'label_join(sum by (destination_workload, namespace)'
                    ' (rate(istio_requests_total[5m])), "workload_ns", "/", "destination_workload",'
                    ' "namespace")'
                ),
                "legendFormat": "{{workload_ns}}",
                "refId": "A",
            },
            {
                "expr": "sum by (destination_workload, namespace) (rate(istio_errors_total[5m]))",
                "legendFormat": "errors",
                "refId": "B",
            },
        ],
    }
    rule_pack = RulePackConfig()
    resolver = SchemaResolver(rule_pack)
    yaml_panel, result = translate_panel(
        panel,
        datasource_index="metrics-*",
        esql_index="metrics-*",
        rule_pack=rule_pack,
        resolver=resolver,
    )
    assert result.status in {"migrated", "migrated_with_warnings"}, (
        f"Expected migrated, got {result.status}: {result.reasons}"
    )
    query = yaml_panel["esql"]["query"]

    # The derived label_join column must be in KEEP.
    keep_section = query.split("| KEEP", 1)[-1]
    assert "workload_ns" in keep_section, (
        f"workload_ns missing from KEEP:\n{query}"
    )

    # EVAL CONCAT must be present exactly once.
    assert query.count("CONCAT(destination_workload") == 1, (
        f"Expected one CONCAT, got:\n{query}"
    )

    # Both metrics must be in the query.
    assert "istio_requests_total" in query
    assert "istio_errors_total" in query

    assert structural_errors(check_esql_structure(query)) == [], query
