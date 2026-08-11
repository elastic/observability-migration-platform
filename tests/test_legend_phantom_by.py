# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Strong regressions for legendFormat → phantom ES|QL BY / Lens breakdown.

Redis Dashboard 763 Network I/O on main emitted::

    BY time_bucket = ..., input

for::

    sum(rate(redis_net_input_bytes_total{...}[5m]))
    legendFormat: "{{ input }}"

``{{ input }}`` is a Grafana series *alias*, not a Prometheus label. PromQL
``sum()`` without ``by()`` already collapsed every label dimension, so ``input``
is not a field. Elasticsearch then fails with ``Unknown column [input]``.

These tests lock:

1. The issue-#99 decision helper (``outer_agg`` + empty ``group_labels`` ⇒ drop).
2. End-to-end panel translation (hand-built + real redis_763.json fixture).
3. Chart-spec invariants: BY identifiers and Lens breakdown must not invent
   legend placeholders that are not prior query columns.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import pytest

from observability_migration.adapters.source.grafana.esql_structural_oracle import (
    check_esql_structure,
    structural_errors,
)
from observability_migration.adapters.source.grafana.panels import (
    SKIP_PANEL_TYPES,
    _flatten_dashboard_panels,
    translate_panel,
)
from observability_migration.adapters.source.grafana.promql import (
    PromQLFragment,
    _drop_legend_labels_if_redundant,
    _legend_grouping_redundant_on_ts,
)
from observability_migration.adapters.source.grafana.translate import (
    translate_promql_to_esql,
)
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver
from observability_migration.targets.kibana.emit.esql_utils import (
    split_esql_pipeline,
    split_top_level_assignment,
    split_top_level_keyword,
)

_REPO = Path(__file__).resolve().parents[1]
_INFRA_DASHBOARDS = _REPO / "infra" / "grafana" / "dashboards"
_REDIS_763 = (
    _REPO
    / "parity-rig"
    / "curated"
    / "grafana_763_redis_exporter"
    / "grafana_provisioning"
    / "dashboards"
    / "redis_763.json"
)
_REDIS_11835 = _INFRA_DASHBOARDS / "redis-11835.json"

# Legend tokens that are series aliases on Redis Network I/O — never BY dims.
_NETWORK_IO_PHANTOMS = frozenset({"input", "output"})

# Display-only legend tokens that are almost never Prometheus label names.
# Real dimensions (device, cmd, instance, mountpoint, …) are intentionally
# excluded — those must remain BY fields when PromQL/series identity needs them.
_ALIAS_LIKE_LEGEND_TOKENS = frozenset(
    {
        "input",
        "output",
        "receive",
        "transmit",
        "received",
        "sent",
        "rx",
        "tx",
        "in",
        "out",
        "read",
        "write",
        "used",
        "free",
        "total",
        "value",
        "series",
        "metric",
        "hits",
        "misses",
    }
)
_LEGEND_TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def _corpus_dashboard_paths() -> list[Path]:
    paths = sorted(_INFRA_DASHBOARDS.glob("*.json"))
    if _REDIS_763.is_file():
        paths.append(_REDIS_763)
    return paths


def _split_csv_top_level(text: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in text:
        if ch in ("(", "["):
            depth += 1
            current.append(ch)
        elif ch in (")", "]"):
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())
    return [p for p in parts if p]


def _strip_backticks(identifier: str) -> str:
    text = identifier.strip()
    if len(text) >= 2 and text.startswith("`") and text.endswith("`"):
        return text[1:-1]
    return text


def _by_identifiers(query: str) -> set[str]:
    """Bare BY grouping keys (aliases or field names), excluding TBUCKET exprs."""
    idents: set[str] = set()
    for cmd in split_esql_pipeline(query):
        if not cmd.lower().startswith("stats "):
            continue
        _body, by_text = split_top_level_keyword(cmd[6:].strip(), "BY")
        if not by_text:
            continue
        for part in _split_csv_top_level(by_text):
            alias, expr = split_top_level_assignment(part)
            token = _strip_backticks(alias or (expr or "").strip())
            if not token:
                continue
            # ``time_bucket = TBUCKET(...)`` → keep the alias; skip raw TBUCKET(...).
            if token.upper().startswith("TBUCKET("):
                continue
            if _IDENT.match(token):
                idents.add(token)
    return idents


def _stats_aliases(query: str) -> set[str]:
    aliases: set[str] = set()
    for cmd in split_esql_pipeline(query):
        if not cmd.lower().startswith("stats "):
            continue
        body, _by = split_top_level_keyword(cmd[6:].strip(), "BY")
        for part in _split_csv_top_level(body):
            alias, _ = split_top_level_assignment(part)
            if alias:
                aliases.add(_strip_backticks(alias))
    return aliases


def _final_output_columns(query: str) -> set[str]:
    cols: set[str] = set()
    for cmd in split_esql_pipeline(query):
        cl = cmd.lower()
        if cl.startswith("stats "):
            body, by_text = split_top_level_keyword(cmd[6:].strip(), "BY")
            cols = set()
            for part in _split_csv_top_level(body):
                alias, _ = split_top_level_assignment(part)
                if alias:
                    cols.add(_strip_backticks(alias))
            for part in _split_csv_top_level(by_text):
                alias, expr = split_top_level_assignment(part)
                field = alias or (expr or "").strip()
                if field and not field.upper().startswith("TBUCKET("):
                    cols.add(_strip_backticks(field))
        elif cl.startswith("eval "):
            for part in _split_csv_top_level(cmd[5:].strip()):
                alias, _ = split_top_level_assignment(part)
                if alias:
                    cols.add(_strip_backticks(alias))
        elif cl.startswith("keep "):
            cols = {
                _strip_backticks(f.strip())
                for f in _split_csv_top_level(cmd[5:].strip())
                if f.strip()
            }
        elif cl.startswith("drop "):
            cols -= {
                _strip_backticks(f.strip())
                for f in _split_csv_top_level(cmd[5:].strip())
                if f.strip()
            }
    return cols


def _spec_fields(esql_block: dict) -> set[str]:
    fields: set[str] = set()

    def _add(value):
        if isinstance(value, dict):
            field = value.get("field")
            if field:
                fields.add(field)
        elif isinstance(value, str) and value:
            fields.add(value)

    _add(esql_block.get("dimension"))
    _add(esql_block.get("breakdown"))
    _add(esql_block.get("primary"))
    _add(esql_block.get("metric"))
    for item in esql_block.get("metrics") or []:
        _add(item)
    for item in esql_block.get("breakdowns") or []:
        _add(item)
    return fields


def _breakdown_fields(esql_block: dict) -> set[str]:
    fields: set[str] = set()
    breakdown = esql_block.get("breakdown")
    if isinstance(breakdown, dict) and breakdown.get("field"):
        fields.add(breakdown["field"])
    elif isinstance(breakdown, str) and breakdown:
        fields.add(breakdown)
    for item in esql_block.get("breakdowns") or []:
        if isinstance(item, dict) and item.get("field"):
            fields.add(item["field"])
        elif isinstance(item, str) and item:
            fields.add(item)
    return fields


def _assert_no_phantom_legend_by(query: str, phantoms: frozenset[str]) -> None:
    """Fail if a legend alias appears as a BY dimension (main Network I/O bug).

    STATS *column* aliases named ``input``/``output`` are fine; BY grouping by
    those names is not.
    """
    by_ids = _by_identifiers(query)
    leaked = phantoms & by_ids
    assert not leaked, (
        f"legend alias(es) {sorted(leaked)} leaked into BY (main Unknown column "
        f"regression): by={sorted(by_ids)}; query=\n{query}"
    )
    # Explicit main failure shapes observed in Kibana edit mode.
    for name in phantoms:
        assert not re.search(
            rf"\bBY\s+time_bucket(?:\s*=\s*TBUCKET\([^)]*\))?\s*,\s*{re.escape(name)}\b",
            query,
            flags=re.IGNORECASE,
        ), f"main failure pattern BY time_bucket, {name} present:\n{query}"


def _assert_chart_spec_matches_query(esql: dict) -> None:
    query = esql.get("query") or ""
    assert query, "expected ES|QL query"
    output = _final_output_columns(query)
    missing = _spec_fields(esql) - output
    assert not missing, (
        f"chart spec fields {sorted(missing)} absent from query output "
        f"{sorted(output)}; query=\n{query}"
    )
    # Breakdown must never point at a STATS metric alias that is not also a BY key.
    by_ids = _by_identifiers(query)
    metric_aliases = _stats_aliases(query)
    for field in _breakdown_fields(esql):
        assert field in by_ids or field == "time_bucket", (
            f"breakdown {field!r} is not a BY dimension {sorted(by_ids)}; "
            f"metric aliases={sorted(metric_aliases)}; query=\n{query}"
        )
    assert structural_errors(check_esql_structure(query)) == []


# ---------------------------------------------------------------------------
# Unit: decision helper (locks the main-branch wrong branch)
# ---------------------------------------------------------------------------


class TestLegendGroupingRedundantDecision(unittest.TestCase):
    def test_outer_sum_without_by_is_redundant(self):
        """Main returned False here → kept ``BY input`` → Unknown column."""
        frag = PromQLFragment(
            metric="redis_net_input_bytes_total",
            outer_agg="sum",
            group_labels=[],
            range_func="rate",
            family="range_agg",
        )
        self.assertTrue(
            _legend_grouping_redundant_on_ts(frag, None, RulePackConfig()),
            "sum(rate(...)) with no by() must drop legendFormat BY labels",
        )

    def test_outer_sum_with_explicit_by_is_not_redundant(self):
        frag = PromQLFragment(
            metric="redis_connected_clients",
            outer_agg="sum",
            group_labels=["instance"],
            family="simple_agg",
        )
        self.assertFalse(
            _legend_grouping_redundant_on_ts(frag, None, RulePackConfig()),
            "explicit by() labels must not be treated as legend-only drop",
        )

    def test_drop_helper_clears_legend_fields_after_outer_collapse(self):
        frag = PromQLFragment(
            metric="redis_net_input_bytes_total",
            outer_agg="sum",
            group_labels=[],
            range_func="rate",
            family="range_agg",
        )
        dropped = _drop_legend_labels_if_redundant(
            frag,
            resolver=None,
            rule_pack=RulePackConfig(),
            group_fields=["input"],
            preferred_origin="legend",
            summary_mode=False,
        )
        self.assertEqual(dropped, [])

    def test_drop_helper_keeps_explicit_promql_group_labels(self):
        frag = PromQLFragment(
            metric="redis_connected_clients",
            outer_agg="sum",
            group_labels=["instance"],
            family="simple_agg",
        )
        kept = _drop_legend_labels_if_redundant(
            frag,
            resolver=None,
            rule_pack=RulePackConfig(),
            group_fields=["service.instance.id"],
            preferred_origin="legend",
            summary_mode=False,
        )
        # Keep-guard: frag.group_labels non-empty → do not strip via legend drop.
        self.assertEqual(kept, ["service.instance.id"])


# ---------------------------------------------------------------------------
# PromQL→ES|QL: exact main screenshot shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr,legend_token",
    [
        (
            'sum(rate(redis_net_input_bytes_total{instance=~"$instance"}[5m]))',
            "input",
        ),
        (
            'sum(rate(redis_net_output_bytes_total{instance=~"$instance"}[5m]))',
            "output",
        ),
        (
            "sum(redis_connected_clients)",
            "instance",
        ),
        (
            'avg(rate(http_requests_total{job="api"}[5m]))',
            "method",
        ),
    ],
)
def test_outer_agg_without_by_never_emits_legend_token_in_by(expr, legend_token):
    ctx = translate_promql_to_esql(
        expr,
        esql_index="metrics-*",
        panel_type="timeseries",
        rule_pack=RulePackConfig(),
        resolver=SchemaResolver(RulePackConfig()),
        translation_hints={
            "preferred_group_labels": [legend_token],
            "preferred_group_labels_origin": "legend",
        },
    )
    assert ctx.esql_query, ctx.warnings
    _assert_no_phantom_legend_by(ctx.esql_query, frozenset({legend_token}))
    assert legend_token not in (ctx.output_group_fields or [])


# ---------------------------------------------------------------------------
# Panel translate: Redis 763 Network I/O (core path, no curated pack)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dashboard_path",
    [
        pytest.param(_REDIS_763, id="redis-763"),
        pytest.param(_REDIS_11835, id="redis-11835"),
    ],
)
def test_redis_network_io_fixture_no_phantom_by_or_breakdown(dashboard_path: Path):
    """Core translator must not invent BY/breakdown ``input``/``output``.

    Covers both Redis marketplace shapes:
    - gnet 763: ``sum(rate(...))`` + ``{{ input }}`` (main Kibana failure)
    - gnet 11835: bare ``rate(...)`` + ``{{ input }}`` (same phantom class)
    """
    assert dashboard_path.is_file(), f"missing fixture {dashboard_path}"
    dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
    panel = next(
        p
        for p in _flatten_dashboard_panels(dashboard)
        if p.get("title") == "Network I/O"
    )

    # Base RulePackConfig — deliberately *not* a curated pack, so this guards
    # the generic PromQL path that failed on main for 763.
    yaml_panel, result = translate_panel(
        panel,
        rule_pack=RulePackConfig(),
        resolver=SchemaResolver(RulePackConfig()),
        datasource_index="metrics-*",
        esql_index="metrics-*",
    )
    assert result.status in {"migrated", "migrated_with_warnings"}, (
        f"{result.status}: {result.reasons}"
    )
    esql = yaml_panel["esql"]
    query = esql["query"]

    _assert_no_phantom_legend_by(query, _NETWORK_IO_PHANTOMS)
    assert _breakdown_fields(esql).isdisjoint(_NETWORK_IO_PHANTOMS), (
        f"Lens breakdown must not use legend phantoms: {_breakdown_fields(esql)}"
    )
    # Fused two-series form: aliases are STATS columns, not dimensions.
    assert "redis_net_input_bytes_total" in query
    assert "redis_net_output_bytes_total" in query
    stats_aliases = _stats_aliases(query)
    assert "input" in stats_aliases and "output" in stats_aliases, query
    _assert_chart_spec_matches_query(esql)


def test_fixture_corpus_alias_like_legends_never_become_by_or_breakdown():
    """Scan infra (+ Redis 763) dashboards for display-only legend → BY leaks.

    Real label dimensions (``device``, ``cmd``, ``instance``, …) are allowed in
    BY when PromQL/series identity needs them. Alias-like tokens (``input``,
    ``output``, …) must only appear as STATS column aliases / series names.
    """
    leaks: list[str] = []
    checked = 0
    for path in _corpus_dashboard_paths():
        dashboard = json.loads(path.read_text(encoding="utf-8"))
        for panel in _flatten_dashboard_panels(dashboard):
            ptype = panel.get("type")
            if ptype in SKIP_PANEL_TYPES or ptype == "row":
                continue
            alias_tokens: set[str] = set()
            for target in panel.get("targets") or []:
                if not isinstance(target, dict):
                    continue
                for tok in _LEGEND_TOKEN_RE.findall(str(target.get("legendFormat") or "")):
                    if tok.lower() in _ALIAS_LIKE_LEGEND_TOKENS:
                        alias_tokens.add(tok)
            if not alias_tokens:
                continue
            checked += 1
            yaml_panel, result = translate_panel(
                panel,
                rule_pack=RulePackConfig(),
                resolver=SchemaResolver(RulePackConfig()),
                datasource_index="metrics-*",
                esql_index="metrics-*",
            )
            if result.status not in {"migrated", "migrated_with_warnings"}:
                continue
            if not yaml_panel or not yaml_panel.get("esql", {}).get("query"):
                continue
            esql = yaml_panel["esql"]
            by_ids = _by_identifiers(esql["query"])
            bd = _breakdown_fields(esql)
            leaked = {
                tok
                for tok in alias_tokens
                if tok in by_ids
                or tok in bd
                or any(name.endswith(f".{tok}") for name in by_ids | bd)
            }
            if leaked:
                leaks.append(
                    f"{path.name} / {panel.get('title')!r}: alias tokens "
                    f"{sorted(leaked)} in BY={sorted(by_ids)} breakdown={sorted(bd)}"
                )
            else:
                _assert_no_phantom_legend_by(esql["query"], frozenset(alias_tokens))
                _assert_chart_spec_matches_query(esql)

    assert checked >= 2, (
        f"expected Redis Network I/O fixtures in corpus scan, checked={checked}"
    )
    assert not leaks, "alias-like legend → BY/breakdown leaks:\n" + "\n".join(leaks)


def test_hand_built_sum_rate_network_io_matches_main_failure_fixture():
    """Hand-built panel matching the Kibana edit-mode single-target failure."""
    panel = {
        "id": 2,
        "type": "timeseries",
        "title": "Network I/O",
        "datasource": {"type": "prometheus", "uid": "prom"},
        "targets": [
            {
                "expr": 'sum(rate(redis_net_input_bytes_total{instance=~"$instance"}[5m]))',
                "legendFormat": "{{ input }}",
                "refId": "A",
            },
        ],
    }
    yaml_panel, result = translate_panel(
        panel,
        rule_pack=RulePackConfig(),
        resolver=SchemaResolver(RulePackConfig()),
        datasource_index="metrics-*",
        esql_index="metrics-*",
    )
    assert result.status in {"migrated", "migrated_with_warnings"}, result.status
    esql = yaml_panel["esql"]
    _assert_no_phantom_legend_by(esql["query"], frozenset({"input"}))
    assert _breakdown_fields(esql).isdisjoint({"input"})
    _assert_chart_spec_matches_query(esql)


def test_two_target_sum_rate_fuses_aliases_without_phantom_breakdown():
    panel = {
        "id": 2,
        "type": "timeseries",
        "title": "Network I/O",
        "datasource": {"type": "prometheus", "uid": "prom"},
        "targets": [
            {
                "expr": 'sum(rate(redis_net_input_bytes_total{instance=~"$instance"}[5m]))',
                "legendFormat": "{{ input }}",
                "refId": "A",
            },
            {
                "expr": 'sum(rate(redis_net_output_bytes_total{instance=~"$instance"}[5m]))',
                "legendFormat": "{{ output }}",
                "refId": "B",
            },
        ],
    }
    yaml_panel, result = translate_panel(
        panel,
        rule_pack=RulePackConfig(),
        resolver=SchemaResolver(RulePackConfig()),
        datasource_index="metrics-*",
        esql_index="metrics-*",
    )
    assert result.status in {"migrated", "migrated_with_warnings"}, result.status
    esql = yaml_panel["esql"]
    query = esql["query"]
    _assert_no_phantom_legend_by(query, _NETWORK_IO_PHANTOMS)
    assert _by_identifiers(query) <= {"time_bucket"}, (
        f"collapsed sum(rate) must BY time_bucket only: {_by_identifiers(query)}"
    )
    assert _breakdown_fields(esql) == set()
    _assert_chart_spec_matches_query(esql)
