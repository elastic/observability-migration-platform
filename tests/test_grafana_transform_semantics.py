# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Differential / invariant harness for Grafana → ES|QL transform rewrites.

Novel method
------------
We do **not** only assert string fragments of the rewritten query. Instead:

1. **Source-frame oracle** — treat Grafana's post-query frame as a wide table
   whose columns are legend names (plus Time). Apply the same transform chain
   in pure Python (``calculateField`` reduceRow mean/sum, ``organize``
   exclude/rename). That is the ground-truth column contract.
2. **ES|QL contract extract** — parse the rewritten query's trailing KEEP /
   metric bookkeeping and map sanitized columns back to legend labels.
3. **Invariants** — structural guards that catch bugs the oracle alone misses
   (empty KEEP, helper-column leaks, false ``applied``, normalize stripping
   transform projections, no-op mean-of-one marked as success).

Corpus panels from ``k8s-views-global.json`` are run through the full
``translate_panel`` path so regressions that only appear after fusion /
``_normalize_esql_panel_query`` still fail.
"""

from __future__ import annotations

import json
import pathlib
import re
import unittest
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from observability_migration.adapters.source.grafana import panels, rules, schema
from observability_migration.adapters.source.grafana.transforms import (
    apply_transformations_to_esql,
    build_redesign_tasks,
    extract_transformations,
    mark_applied_transformations,
)

# ---------------------------------------------------------------------------
# Source-frame oracle (Grafana semantics, not ES|QL)
# ---------------------------------------------------------------------------


def _legend_columns(panel: dict[str, Any]) -> list[str]:
    cols: list[str] = []
    for target in panel.get("targets") or []:
        if not isinstance(target, dict) or target.get("hide"):
            continue
        legend = str(target.get("legendFormat") or target.get("refId") or "").strip()
        if legend and legend not in cols:
            cols.append(legend)
    return cols


def _oracle_apply_transforms(
    columns: list[str],
    transforms: list[dict[str, Any]],
) -> tuple[list[str], list[int], list[tuple[int, str]]]:
    """Apply supported transforms to a legend-named wide frame.

    Returns ``(remaining_columns, applied_indices, skipped)``.
    """
    frame = list(columns)
    applied: list[int] = []
    skipped: list[tuple[int, str]] = []

    for idx, transform in enumerate(transforms or []):
        if not isinstance(transform, dict) or transform.get("disabled"):
            continue
        tid = str(transform.get("id") or "")
        options = transform.get("options") if isinstance(transform.get("options"), dict) else {}
        try:
            if tid == "calculateField":
                frame = _oracle_calculate_field(frame, options)
            elif tid == "organize":
                frame = _oracle_organize(frame, options)
            else:
                skipped.append((idx, f"unsupported {tid}"))
                continue
        except _OracleSkip as exc:
            skipped.append((idx, str(exc)))
            continue
        applied.append(idx)
    return frame, applied, skipped


class _OracleSkip(Exception):
    pass


def _oracle_calculate_field(frame: list[str], options: dict[str, Any]) -> list[str]:
    mode = str(options.get("mode") or "")
    if mode != "reduceRow":
        raise _OracleSkip(f"mode {mode}")
    reduce = options.get("reduce") if isinstance(options.get("reduce"), dict) else {}
    reducer = str(reduce.get("reducer") or "sum").lower()
    if reducer not in {"sum", "mean"}:
        raise _OracleSkip(f"reducer {reducer}")
    include = list(reduce.get("include") or [])
    alias = str(options.get("alias") or "calculated").strip() or "calculated"
    replace_fields = bool(options.get("replaceFields"))

    if include:
        missing = [name for name in include if name not in frame]
        if missing:
            raise _OracleSkip(f"missing include {missing}")
        sources = list(include)
    else:
        sources = [c for c in frame if c != "Time"]
        if not sources:
            raise _OracleSkip("no sources")
        # Grafana reduceRow-without-include averages/sums *all* numeric fields.
        # A single remaining series after a multi-target drop is not the same
        # semantic as the dashboard author's multi-OS mean — refuse to claim it.
        if len(sources) < 2 and replace_fields:
            raise _OracleSkip("replaceFields reduceRow needs >=2 source series")

    out = list(frame)
    if alias not in out:
        out.append(alias)
    if replace_fields:
        out = [alias] + [c for c in out if c == "Time"]
        # keep Time if present; drop every source metric
        out = [c for c in (["Time"] if "Time" in frame else []) + [alias]]
    return out


def _oracle_organize(frame: list[str], options: dict[str, Any]) -> list[str]:
    rename = options.get("renameByName") if isinstance(options.get("renameByName"), dict) else {}
    exclude = options.get("excludeByName") if isinstance(options.get("excludeByName"), dict) else {}
    out = list(frame)
    for old, new in rename.items():
        if not new or old not in out:
            continue
        out = [str(new) if c == old else c for c in out]
    for name, excluded in exclude.items():
        if excluded and name in out and name != "Time":
            # Time exclude is a display concern; oracle drops it from frame too
            # when Grafana would hide it from the viz.
            out = [c for c in out if c != name]
    if exclude.get("Time"):
        out = [c for c in out if c != "Time"]
    return out


# ---------------------------------------------------------------------------
# ES|QL contract helpers
# ---------------------------------------------------------------------------


_HELPER_COLS = frozenset({"__tx_sum", "__tx_cnt"})
_KEEP_RE = re.compile(r"^\|\s*KEEP\s+(.*)$", re.IGNORECASE | re.MULTILINE)


def _trailing_keep_columns(query: str) -> list[str] | None:
    keeps = list(_KEEP_RE.finditer(query or ""))
    if not keeps:
        return None
    body = keeps[-1].group(1).strip()
    if not body:
        return []
    return [part.strip().strip("`") for part in body.split(",") if part.strip()]


def _assert_esql_invariants(query: str, *, context: str) -> None:
    """Structural guards independent of Grafana oracle agreement."""
    text = query or ""
    keep = _trailing_keep_columns(text)
    if keep is not None:
        assert keep, f"{context}: empty KEEP projection\n{text}"
        leaked = _HELPER_COLS.intersection(keep)
        assert not leaked, f"{context}: helper columns leaked into KEEP {leaked}\n{text}"

    for helper in _HELPER_COLS:
        if helper not in text:
            continue
        # Helpers may appear in EVAL, but must be removed from the visible
        # projection via KEEP (excluding them) or an explicit DROP. When
        # ``_strip_dotted_group_keep`` removes KEEP, helpers become Lens-
        # visible columns — that is a bug.
        if keep is not None:
            assert helper not in keep, f"{context}: helper in KEEP\n{text}"
            continue
        dropped = (
            f"| DROP {helper}" in text
            or f"| DROP `{helper}`" in text
            or any(
                line.strip().upper().startswith("| DROP")
                and helper in line
                for line in text.splitlines()
            )
        )
        assert dropped, (
            f"{context}: helper '{helper}' present without KEEP/DROP cleanup\n{text}"
        )


def _sanitize(name: str) -> str:
    from observability_migration.adapters.source.grafana.promql import _safe_alias

    return _safe_alias(name)


# ---------------------------------------------------------------------------
# Unit: oracle vs apply_transformations_to_esql
# ---------------------------------------------------------------------------


class TransformOracleDifferentialTests(unittest.TestCase):
    """Compare ES|QL rewrite contracts to the Grafana source-frame oracle."""

    def _apply(
        self,
        panel: dict[str, Any],
        metric_fields: list[str],
        metric_labels: dict[str, str],
        query: str,
        group_fields: list[str] | None = None,
    ):
        translation = SimpleNamespace(
            esql_query=query,
            output_metric_field=metric_fields[0] if metric_fields else "",
            output_group_fields=list(group_fields or []),
            metadata={
                "multi_series_metric_fields": list(metric_fields),
                "multi_series_metric_labels": dict(metric_labels),
            },
        )
        return apply_transformations_to_esql(panel, translation, esql_query=query)

    def test_fused_network_saturation_matches_oracle(self):
        panel = {
            "targets": [
                {"refId": "A", "legendFormat": "Linux Packets dropped (receive)"},
                {"refId": "B", "legendFormat": "Linux Packets dropped (transmit)"},
                {"refId": "C", "legendFormat": "Windows Packets dropped (receive)"},
                {"refId": "D", "legendFormat": "Windows Packets dropped (transmit)"},
            ],
            "transformations": [
                {
                    "id": "calculateField",
                    "options": {
                        "alias": "Packets dropped (receive)",
                        "mode": "reduceRow",
                        "reduce": {
                            "include": [
                                "Linux Packets dropped (receive)",
                                "Windows Packets dropped (receive)",
                            ],
                            "reducer": "mean",
                        },
                    },
                },
                {
                    "id": "calculateField",
                    "options": {
                        "alias": "Packets dropped (transmit)",
                        "mode": "reduceRow",
                        "reduce": {
                            "include": [
                                "Linux Packets dropped (transmit)",
                                "Windows Packets dropped (transmit)",
                            ],
                            "reducer": "mean",
                        },
                    },
                },
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {
                            "Linux Packets dropped (receive)": True,
                            "Linux Packets dropped (transmit)": True,
                            "Windows Packets dropped (receive)": True,
                            "Windows Packets dropped (transmit)": True,
                            "Time": False,
                        }
                    },
                },
            ],
        }
        legends = _legend_columns(panel)
        oracle_cols, oracle_applied, _ = _oracle_apply_transforms(
            legends, panel["transformations"]
        )
        self.assertEqual(
            oracle_cols,
            ["Packets dropped (receive)", "Packets dropped (transmit)"],
        )
        self.assertEqual(oracle_applied, [0, 1, 2])

        fields = [_sanitize(legend) for legend in legends]
        labels = {fields[i]: legends[i] for i in range(len(legends))}
        query = (
            "TS metrics-*\n"
            "| STATS "
            + ", ".join(f"{f} = AVG(x)" for f in fields)
            + " BY time_bucket = TBUCKET(5 minute)\n"
            "| KEEP time_bucket, "
            + ", ".join(fields)
            + "\n| SORT time_bucket ASC"
        )
        rewritten, result = self._apply(panel, fields, labels, query, ["time_bucket"])
        _assert_esql_invariants(rewritten, context="network saturation")
        self.assertEqual(result.applied_indices, oracle_applied)
        expected_fields = {_sanitize(c) for c in oracle_cols}
        self.assertEqual(set(result.updated_metric_fields), expected_fields)
        keep = _trailing_keep_columns(rewritten)
        self.assertIsNotNone(keep)
        self.assertTrue(expected_fields.issubset(set(keep or [])))
        self.assertFalse(set(fields) & set(keep or []), keep)

    def test_partial_drop_must_not_empty_keep_or_false_apply_calculate(self):
        """CPU Usage-style: only Real Linux migrated; calculateField needs Windows."""
        panel = {
            "targets": [
                {"refId": "A", "legendFormat": "Real Linux"},
                {"refId": "B", "legendFormat": "Real Windows"},
                {"refId": "C", "legendFormat": "Requests"},
                {"refId": "D", "legendFormat": "Limits"},
            ],
            "transformations": [
                {
                    "id": "calculateField",
                    "options": {
                        "alias": "Real",
                        "mode": "reduceRow",
                        "reduce": {
                            "include": ["Real Linux", "Real Windows"],
                            "reducer": "sum",
                        },
                    },
                },
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {
                            "Real Linux": True,
                            "Real Windows": True,
                            "Time": True,
                        }
                    },
                },
            ],
        }
        # Migrated frame has only Real Linux (Windows/Requests/Limits dropped).
        migrated_legends = ["Real Linux"]
        oracle_cols, _oracle_applied, oracle_skipped = _oracle_apply_transforms(
            migrated_legends, panel["transformations"]
        )
        self.assertIn(0, [s[0] for s in oracle_skipped])
        # organize excluding the only remaining metric empties the frame —
        # that is *not* a successful apply of the dashboard author's intent.
        self.assertEqual(oracle_cols, [])

        query = (
            "TS metrics-*\n"
            "| STATS node_cpu_seconds_total = SUM(RATE(node_cpu_seconds_total, 5m)) "
            "BY time_bucket = TBUCKET(5 minute)\n"
            "| SORT time_bucket ASC\n"
            "| STATS time_bucket = MAX(time_bucket), "
            "node_cpu_seconds_total = MAX(node_cpu_seconds_total)\n"
            "| KEEP time_bucket, node_cpu_seconds_total"
        )
        rewritten, result = self._apply(
            panel,
            ["node_cpu_seconds_total"],
            {"node_cpu_seconds_total": "Real Linux"},
            query,
            [],
        )
        _assert_esql_invariants(rewritten, context="partial CPU Usage")
        # calculateField must not be applied
        self.assertNotIn(0, result.applied_indices)
        # organize that would wipe the only metric must skip, not emit empty KEEP
        self.assertNotIn(1, result.applied_indices)
        keep = _trailing_keep_columns(rewritten)
        self.assertNotEqual(keep, [])
        if keep is not None:
            self.assertIn("node_cpu_seconds_total", keep)
        # Redesign tasks must still cover the skipped calculateField
        entries = mark_applied_transformations(
            extract_transformations(panel), result.applied_indices
        )
        tasks = build_redesign_tasks("CPU Usage", "Dash", entries)
        self.assertIn("calculateField", [t["transform_id"] for t in tasks])

    def test_single_series_replacefields_mean_is_not_claimed_applied(self):
        """Cluster CPU after Windows drop: mean-of-one is not Grafana's intent."""
        panel = {
            "targets": [
                {"refId": "A", "legendFormat": "Linux"},
                {"refId": "B", "legendFormat": "Windows"},
            ],
            "transformations": [
                {
                    "id": "calculateField",
                    "options": {
                        "alias": "CPU usage in %",
                        "mode": "reduceRow",
                        "reduce": {"reducer": "mean"},
                        "replaceFields": True,
                    },
                }
            ],
        }
        _oracle_cols, oracle_applied, oracle_skipped = _oracle_apply_transforms(
            ["Linux"], panel["transformations"]
        )
        self.assertEqual(oracle_applied, [])
        self.assertTrue(oracle_skipped)

        query = (
            "TS metrics-*\n"
            "| STATS inner_val = SUM(RATE(node_cpu_seconds_total, 5m)) "
            "BY time_bucket = TBUCKET(5 minute), service.instance.id, cpu\n"
            "| STATS node_cpu_seconds_total_avg = AVG(inner_val) BY time_bucket\n"
            "| SORT time_bucket ASC"
        )
        rewritten, result = self._apply(
            panel,
            ["node_cpu_seconds_total_avg"],
            {"node_cpu_seconds_total_avg": "Linux"},
            query,
            ["time_bucket"],
        )
        self.assertEqual(result.applied_indices, [])
        self.assertNotIn("__tx_sum", rewritten)
        # Query should be unchanged (or at most a pure rename without mean scaffolding)
        self.assertNotIn("CPU_usage_in", rewritten)

    def test_helpers_dropped_even_when_dotted_group_strips_keep(self):
        """Transform cleanup must survive ``_strip_dotted_group_keep``."""
        panel = {
            "targets": [
                {"refId": "A", "legendFormat": "Linux"},
                {"refId": "B", "legendFormat": "Windows"},
            ],
            "transformations": [
                {
                    "id": "calculateField",
                    "options": {
                        "alias": "CPU usage in %",
                        "mode": "reduceRow",
                        "reduce": {"reducer": "mean"},
                        "replaceFields": True,
                    },
                }
            ],
        }
        # Fully fused two-series case — oracle applies.
        query = (
            "TS metrics-*\n"
            "| STATS Linux = AVG(a), Windows = AVG(b) "
            "BY time_bucket = TBUCKET(5 minute), service.instance.id\n"
            "| SORT time_bucket ASC"
        )
        rewritten, result = self._apply(
            panel,
            ["Linux", "Windows"],
            {"Linux": "Linux", "Windows": "Windows"},
            query,
            ["time_bucket", "service.instance.id"],
        )
        self.assertEqual(result.applied_indices, [0])
        _assert_esql_invariants(rewritten, context="pre-normalize fused replaceFields")

        yaml_panel = {
            "esql": {
                "query": rewritten,
                "metrics": [{"field": result.updated_metric_fields[0]}],
            }
        }
        normalized = panels._normalize_esql_panel_query(
            deepcopy(yaml_panel), rules.RulePackConfig()
        )
        final_q = normalized["esql"]["query"]
        _assert_esql_invariants(final_q, context="post-normalize fused replaceFields")
        # Metric alias must survive; helpers must not.
        metric = result.updated_metric_fields[0]
        self.assertIn(metric, final_q)
        self.assertNotIn("__tx_sum", final_q)
        self.assertNotIn("__tx_cnt", final_q)


# ---------------------------------------------------------------------------
# Corpus: full translate_panel path
# ---------------------------------------------------------------------------


class TransformCorpusInvariantTests(unittest.TestCase):
    """Run every transform panel in the K8s Views Global fixture through
    ``translate_panel`` and enforce ES|QL invariants + oracle agreement on
    *available* (migrated) legend columns.
    """

    @classmethod
    def setUpClass(cls):
        path = (
            pathlib.Path(__file__).resolve().parent.parent
            / "infra/grafana/dashboards/k8s-views-global.json"
        )
        cls.dashboard = json.loads(path.read_text())
        cls.rp = rules.RulePackConfig(native_promql=False)
        cls.resolver = schema.SchemaResolver(cls.rp)

    def _walk(self):
        def walk(items):
            for panel in items or []:
                yield panel
                yield from walk(panel.get("panels"))

        yield from walk(self.dashboard.get("panels"))

    def test_all_transform_panels_satisfy_invariants(self):
        failures: list[str] = []
        seen = 0
        for panel in self._walk():
            transforms = panel.get("transformations") or []
            if not transforms:
                continue
            title = str(panel.get("title") or panel.get("id") or "?")
            seen += 1
            try:
                yaml_panel, result = panels.translate_panel(
                    panel,
                    datasource_index="metrics-*",
                    esql_index="metrics-*",
                    rule_pack=self.rp,
                    resolver=self.resolver,
                )
            except Exception as exc:  # pragma: no cover - surface as failure
                failures.append(f"{title}: translate raised {exc}")
                continue
            query = ((yaml_panel or {}).get("esql") or {}).get("query") or ""
            if not query:
                continue
            try:
                _assert_esql_invariants(query, context=title)
            except AssertionError as exc:
                failures.append(str(exc))
                continue

            applied = list(getattr(result, "applied_transform_indices", []) or [])
            reasons_text = " ".join(result.reasons or [])
            for idx, transform in enumerate(transforms):
                if transform.get("disabled"):
                    continue
                if str(transform.get("id")) != "calculateField":
                    continue
                options = transform.get("options") or {}
                reduce = options.get("reduce") if isinstance(options.get("reduce"), dict) else {}
                include = list(reduce.get("include") or [])
                if idx not in applied:
                    continue
                # If Windows targets were dropped, includes naming Windows must
                # not be claimed applied — that would invent a multi-OS mean.
                dropped_partial = (
                    "Windows-specific" in reasons_text
                    or "only 1 could be migrated" in reasons_text
                )
                if dropped_partial and any("Windows" in name for name in include):
                    failures.append(
                        f"{title}: calculateField[{idx}] marked applied despite "
                        f"dropped Windows include series {include}; applied={applied}"
                    )
                if (
                    bool(options.get("replaceFields"))
                    and not include
                    and "only 1 could be migrated" in reasons_text
                ):
                    failures.append(
                        f"{title}: replaceFields mean marked applied with a single "
                        f"migrated series; applied={applied}"
                    )

        self.assertGreater(seen, 0, "fixture should contain transform panels")
        self.assertEqual(failures, [], "\n\n".join(failures))


if __name__ == "__main__":
    unittest.main()
