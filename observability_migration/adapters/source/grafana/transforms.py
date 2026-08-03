# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Extract, classify, and apply Grafana transformations.

Most Grafana transformations still need human redesign. A small low-complexity
subset (``calculateField`` reduceRow mean/sum, ``organize`` exclude/rename,
``filterFieldsByName`` / ``filterByName``, ``sortBy``, ``limit``) can be
rewritten into ES|QL EVAL/KEEP/DROP/SORT/LIMIT during panel translation.

Unsupported transforms continue to emit redesign tasks in the migration
manifest / feature-gap report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from observability_migration.adapters.source.grafana.promql import (
    _safe_alias,
    _unique_safe_alias,
)
from observability_migration.targets.kibana.emit.esql_utils import (
    extract_esql_shape,
    split_esql_pipeline,
)

TRANSFORM_COMPLEXITY = {
    "merge": "medium",
    "seriesToColumns": "medium",
    "seriesToRows": "low",
    "filterByName": "low",
    "filterByRefId": "low",
    "filterFieldsByName": "low",
    "organize": "low",
    "sortBy": "low",
    "reduce": "medium",
    "calculateField": "medium",
    "configFromData": "high",
    "groupBy": "medium",
    "concatenate": "low",
    "labelsToFields": "medium",
    "extractFields": "medium",
    "renameByRegex": "medium",
    "convertFieldType": "low",
    "joinByField": "medium",
    "histogram": "medium",
    "groupingToMatrix": "high",
    "prepareTimeSeries": "low",
    "limit": "low",
    "filterByValue": "medium",
    "joinByLabels": "medium",
    "regression": "high",
    "partitionByValues": "medium",
    "formatTime": "low",
    "formatString": "low",
    "rowsToFields": "medium",
    "spatial": "high",
}

KIBANA_ALTERNATIVES = {
    "filterByName": "Use ES|QL KEEP/DROP to select columns",
    "filterFieldsByName": "Use ES|QL KEEP/DROP to select columns",
    "filterByRefId": "Not needed — Kibana panels reference a single query",
    "organize": "Use ES|QL RENAME and KEEP for column ordering",
    "sortBy": "Use ES|QL SORT",
    "reduce": "Use ES|QL STATS aggregation",
    "calculateField": "Use ES|QL EVAL for calculated columns",
    "merge": "Use ES|QL ENRICH or Lens formula layer",
    "seriesToColumns": "Use ES|QL STATS ... BY with PIVOT-like aggregation",
    "seriesToRows": "Use ES|QL MV_EXPAND or restructure the query",
    "groupBy": "Use ES|QL STATS ... BY",
    "concatenate": "Use ES|QL CONCAT function in EVAL",
    "labelsToFields": "Labels become columns naturally in ES|QL output",
    "renameByRegex": "Use ES|QL RENAME",
    "convertFieldType": "Use ES|QL TO_* type conversion functions",
    "joinByField": "Use ES|QL ENRICH or Kibana runtime fields",
    "histogram": "Use ES|QL BUCKET function",
    "limit": "Use ES|QL LIMIT",
    "filterByValue": "Use ES|QL WHERE clause",
    "prepareTimeSeries": "Not needed — ES|QL time series are natively structured",
    "formatTime": "Use ES|QL DATE_FORMAT function",
}

_APPLIED_ALTERNATIVE = "Applied automatically as ES|QL EVAL/KEEP/DROP/SORT/LIMIT"
_TIME_FIELD_ALIASES = frozenset({"time", "Time", "timestamp", "@timestamp"})
_TIME_LIKE_COLUMNS = frozenset({"time_bucket", "timestamp_bucket", "step", "@timestamp"})


@dataclass
class TransformApplyResult:
    applied_indices: list[int] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    updated_metric_fields: list[str] = field(default_factory=list)
    updated_metric_label_hints: dict[str, str] = field(default_factory=dict)


def extract_transformations(panel: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract transformations from a Grafana panel."""
    raw = panel.get("transformations") or []
    extracted: list[dict[str, Any]] = []
    for idx, transform in enumerate(raw):
        transform_id = str(transform.get("id", "") or "")
        options = transform.get("options") or {}
        disabled = bool(transform.get("disabled", False))

        entry: dict[str, Any] = {
            "index": idx,
            "id": transform_id,
            "disabled": disabled,
            "complexity": TRANSFORM_COMPLEXITY.get(transform_id, "high"),
            "kibana_alternative": KIBANA_ALTERNATIVES.get(transform_id, "Manual redesign required"),
        }

        if transform_id == "calculateField":
            entry["details"] = {
                "mode": options.get("mode", ""),
                "alias": options.get("alias", ""),
            }
        elif transform_id in ("filterByName", "filterFieldsByName"):
            entry["details"] = {
                "fields": list((options.get("include") or {}).get("names", []) or []),
            }
        elif transform_id == "organize":
            entry["details"] = {
                "renames": options.get("renameByName", {}),
                "excludes": options.get("excludeByName", {}),
            }
        elif transform_id in ("merge", "joinByField"):
            entry["details"] = {
                "field": options.get("byField", ""),
            }
        elif transform_id == "groupBy":
            entry["details"] = {
                "fields": options.get("fields", {}),
            }
        elif transform_id == "sortBy":
            sort_items = options.get("sort", [])
            entry["details"] = {
                "fields": [s.get("field", "") for s in sort_items] if isinstance(sort_items, list) else [],
            }
        else:
            raw_keys = sorted(options.keys()) if isinstance(options, dict) else []
            entry["details"] = {"option_keys": raw_keys[:10]}

        extracted.append(entry)

    return extracted


def build_redesign_tasks(
    panel_title: str,
    dashboard_title: str,
    transformations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert extracted transformations into actionable redesign task entries."""
    tasks: list[dict[str, Any]] = []
    for transform in transformations:
        if transform.get("disabled"):
            continue
        if transform.get("status") == "applied_in_esql":
            continue
        tasks.append({
            "dashboard": dashboard_title,
            "panel": panel_title,
            "task_type": "transformation_redesign",
            "transform_id": transform["id"],
            "complexity": transform["complexity"],
            "kibana_alternative": transform["kibana_alternative"],
            "details": transform.get("details", {}),
            "description": (
                f"Panel '{panel_title}' uses Grafana transformation "
                f"'{transform['id']}' ({transform['complexity']} complexity). "
                f"Kibana alternative: {transform['kibana_alternative']}"
            ),
        })
    return tasks


def build_transform_summary(
    all_tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize transformation redesign tasks across all panels."""
    if not all_tasks:
        return {"total": 0, "by_complexity": {}, "by_type": {}}

    by_complexity: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for task in all_tasks:
        c = task.get("complexity", "high")
        by_complexity[c] = by_complexity.get(c, 0) + 1
        t = task.get("transform_id", "unknown")
        by_type[t] = by_type.get(t, 0) + 1

    return {
        "total": len(all_tasks),
        "by_complexity": dict(sorted(by_complexity.items(), key=lambda x: -x[1])),
        "by_type": dict(sorted(by_type.items(), key=lambda x: -x[1])),
    }


def mark_applied_transformations(
    entries: list[dict[str, Any]],
    applied_indices: list[int] | set[int] | None,
) -> list[dict[str, Any]]:
    """Annotate extracted transform entries that were rewritten into ES|QL."""
    applied = {int(idx) for idx in (applied_indices or [])}
    marked: list[dict[str, Any]] = []
    for entry in entries:
        clone = dict(entry)
        if int(clone.get("index", -1)) in applied:
            clone["status"] = "applied_in_esql"
            clone["kibana_alternative"] = _APPLIED_ALTERNATIVE
        marked.append(clone)
    return marked


def apply_transformations_to_esql(
    panel: dict[str, Any],
    translation: Any,
    *,
    esql_query: str | None = None,
) -> tuple[str, TransformApplyResult]:
    """Apply supported Grafana transforms to a fused ES|QL query.

    Mutates ``translation.metadata`` metric-field bookkeeping when transforms
    succeed. Returns ``(query, result)``; unsupported transforms are skipped
    and should still emit redesign tasks.
    """
    query = str(esql_query if esql_query is not None else getattr(translation, "esql_query", "") or "")
    result = TransformApplyResult()
    raw_transforms = panel.get("transformations") or []
    if not query.strip() or not raw_transforms:
        return query, result
    if query.upper().lstrip().startswith("PROMQL "):
        # Opaque native PROMQL command — cannot splice EVAL/KEEP safely.
        for idx, transform in enumerate(raw_transforms):
            if transform.get("disabled"):
                continue
            result.skipped.append((idx, "native PROMQL query cannot host Grafana transforms"))
        return query, result

    metric_fields = list(
        (getattr(translation, "metadata", {}) or {}).get("multi_series_metric_fields")
        or ([getattr(translation, "output_metric_field", "")] if getattr(translation, "output_metric_field", "") else [])
    )
    metric_label_hints = dict(
        (getattr(translation, "metadata", {}) or {}).get("multi_series_metric_labels") or {}
    )
    shape = extract_esql_shape(query)
    group_fields = list(shape.group_fields or getattr(translation, "output_group_fields", []) or [])
    if not metric_fields and shape.metric_fields:
        metric_fields = list(shape.metric_fields)

    columns = _inventory_columns(query, metric_fields, group_fields)
    used_aliases = set(columns)

    for idx, transform in enumerate(raw_transforms):
        if not isinstance(transform, dict) or transform.get("disabled"):
            continue
        transform_id = str(transform.get("id") or "")
        options = transform.get("options") if isinstance(transform.get("options"), dict) else {}
        try:
            if transform_id == "calculateField":
                query, metric_fields, metric_label_hints, used_aliases = _apply_calculate_field(
                    query,
                    options,
                    panel=panel,
                    metric_fields=metric_fields,
                    metric_label_hints=metric_label_hints,
                    group_fields=group_fields,
                    columns=columns,
                    used_aliases=used_aliases,
                )
            elif transform_id == "organize":
                query, metric_fields, metric_label_hints = _apply_organize(
                    query,
                    options,
                    panel=panel,
                    metric_fields=metric_fields,
                    metric_label_hints=metric_label_hints,
                    group_fields=group_fields,
                    columns=columns,
                )
            elif transform_id in ("filterByName", "filterFieldsByName"):
                query, metric_fields = _apply_filter_fields(
                    query,
                    options,
                    panel=panel,
                    metric_fields=metric_fields,
                    metric_label_hints=metric_label_hints,
                    group_fields=group_fields,
                    columns=columns,
                )
            elif transform_id == "sortBy":
                query = _apply_sort_by(
                    query,
                    options,
                    panel=panel,
                    metric_label_hints=metric_label_hints,
                    group_fields=group_fields,
                    columns=columns,
                )
            elif transform_id == "limit":
                query = _apply_limit(query, options)
            else:
                result.skipped.append((idx, f"unsupported transform '{transform_id}'"))
                continue
        except _TransformSkip as exc:
            result.skipped.append((idx, str(exc)))
            continue

        result.applied_indices.append(idx)
        columns = _inventory_columns(query, metric_fields, group_fields)
        used_aliases = set(columns)
        result.warnings.append(
            f"Applied Grafana transformation '{transform_id}' as ES|QL rewrite"
        )

    result.updated_metric_fields = list(metric_fields)
    result.updated_metric_label_hints = dict(metric_label_hints)

    metadata = getattr(translation, "metadata", None)
    if isinstance(metadata, dict):
        metadata["applied_transform_indices"] = list(result.applied_indices)
        if result.updated_metric_fields:
            metadata["multi_series_metric_fields"] = list(result.updated_metric_fields)
        if result.updated_metric_label_hints:
            metadata["multi_series_metric_labels"] = dict(result.updated_metric_label_hints)
    if result.updated_metric_fields and hasattr(translation, "output_metric_field"):
        translation.output_metric_field = result.updated_metric_fields[0]
    if hasattr(translation, "esql_query"):
        translation.esql_query = query
    return query, result


class _TransformSkip(Exception):
    """Raised when a single transform cannot be applied safely."""


def _inventory_columns(query: str, metric_fields: list[str], group_fields: list[str]) -> set[str]:
    columns: set[str] = set()
    shape = extract_esql_shape(query)
    columns.update(shape.metric_fields or [])
    columns.update(shape.group_fields or [])
    columns.update(shape.projected_fields or [])
    columns.update(_extract_keep_columns(query))
    columns.update(metric_fields or [])
    columns.update(group_fields or [])
    return {str(col) for col in columns if col}


def _extract_keep_columns(esql_query: str) -> list[str]:
    for stage in reversed(split_esql_pipeline(esql_query)):
        body = str(stage or "").strip()
        if body.lower().startswith("keep "):
            return [part.strip().strip("`") for part in _split_csv(body[5:]) if part.strip()]
    return []


def _split_csv(text: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_quote = None
    for char in text or "":
        if in_quote:
            current.append(char)
            if char == in_quote:
                in_quote = None
            continue
        if char in {'"', "'"}:
            in_quote = char
            current.append(char)
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth = max(depth - 1, 0)
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part]


def _esql_ident(name: str) -> str:
    token = str(name or "").strip().strip("`")
    if not token:
        return '""'
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
        return token
    escaped = token.replace("`", "``")
    return f"`{escaped}`"


def _resolve_field(
    field_name: str,
    *,
    panel: dict[str, Any],
    columns: set[str],
    metric_label_hints: dict[str, str],
    group_fields: list[str],
) -> str | None:
    name = str(field_name or "").strip()
    if not name:
        return None
    if name in _TIME_FIELD_ALIASES:
        for candidate in list(group_fields) + sorted(columns):
            if candidate in _TIME_LIKE_COLUMNS or "time" in candidate.lower() or "bucket" in candidate.lower():
                return candidate
        return None
    if name in columns:
        return name
    for col, hint in metric_label_hints.items():
        if str(hint) == name and col in columns:
            return col
    sanitized = _safe_alias(name)
    if sanitized in columns:
        return sanitized
    for target in panel.get("targets") or []:
        if not isinstance(target, dict):
            continue
        legend = str(target.get("legendFormat") or "").strip()
        ref_id = str(target.get("refId") or "").strip()
        if name not in {legend, ref_id}:
            continue
        for col, hint in metric_label_hints.items():
            if str(hint) in {legend, ref_id, name} and col in columns:
                return col
        for candidate in (sanitized, _safe_alias(legend), _safe_alias(ref_id)):
            if candidate and candidate in columns:
                return candidate
    return None


def _append_pipeline_stage(query: str, stage: str) -> str:
    """Insert *stage* after STATS/EVAL work and before trailing KEEP/SORT/LIMIT."""
    stage = stage if stage.strip().startswith("|") else f"| {stage.strip()}"
    lines = str(query).splitlines()
    insert_at = len(lines)
    for idx in range(len(lines) - 1, -1, -1):
        stripped = lines[idx].strip()
        if not stripped.startswith("|"):
            continue
        body = stripped[1:].strip().lower()
        if body.startswith(("keep ", "sort ", "limit ", "drop ")):
            insert_at = idx
            continue
        break
    lines.insert(insert_at, stage)
    return "\n".join(lines)


def _is_dotted_group_field(name: str) -> bool:
    token = str(name or "").strip().strip("`")
    return bool(token) and "." in token and "(" not in token


def _rewrite_keep(query: str, keep_columns: list[str]) -> str:
    """Set/replace the trailing KEEP so it sits after EVAL stages, before SORT.

    Dotted grouping fields (``service.instance.id``) are omitted from KEEP on
    purpose: re-projecting them after EVAL triggers ES|QL's "Output has changed"
    verification_exception. They remain in the row from ``STATS BY``; Lens binds
    metrics by field name and does not need them in KEEP.
    """
    projected = [
        col for col in keep_columns if col and not _is_dotted_group_field(col)
    ]
    if not projected:
        raise _TransformSkip("KEEP projection would be empty after omitting dotted groups")
    keep_line = "| KEEP " + ", ".join(_esql_ident(col) for col in projected)
    lines = [
        line
        for line in str(query).splitlines()
        if not (
            line.strip().startswith("|")
            and line.strip()[1:].strip().lower().startswith("keep ")
        )
    ]
    insert_at = len(lines)
    for idx in range(len(lines) - 1, -1, -1):
        stripped = lines[idx].strip()
        if stripped.startswith("|") and stripped[1:].strip().lower().startswith(
            ("sort ", "limit ")
        ):
            insert_at = idx
            continue
        break
    lines.insert(insert_at, keep_line)
    return "\n".join(lines)


def _drop_columns(query: str, drop_columns: list[str], keep_fallback: list[str]) -> str:
    drops = [col for col in drop_columns if col]
    if not drops:
        return query
    existing_keep = _extract_keep_columns(query)
    if existing_keep:
        remaining = [col.strip("`") for col in existing_keep if col.strip("`") not in set(drops)]
        # Preserve newly computed columns that were not yet in KEEP.
        for col in keep_fallback:
            if col and col not in remaining and col not in set(drops):
                remaining.append(col)
        if not remaining:
            raise _TransformSkip(
                f"dropping {drops} would leave an empty KEEP projection"
            )
        return _rewrite_keep(query, remaining)
    remaining = [col for col in keep_fallback if col not in set(drops)]
    if remaining:
        return _rewrite_keep(query, remaining)
    drop_line = "| DROP " + ", ".join(_esql_ident(col) for col in drops)
    return _append_pipeline_stage(query, drop_line)


def _apply_calculate_field(
    query: str,
    options: dict[str, Any],
    *,
    panel: dict[str, Any],
    metric_fields: list[str],
    metric_label_hints: dict[str, str],
    group_fields: list[str],
    columns: set[str],
    used_aliases: set[str],
) -> tuple[str, list[str], dict[str, str], set[str]]:
    mode = str(options.get("mode") or "")
    if mode != "reduceRow":
        raise _TransformSkip(f"calculateField mode '{mode}' is not auto-applicable")
    reduce = options.get("reduce") if isinstance(options.get("reduce"), dict) else {}
    reducer = str(reduce.get("reducer") or "sum").lower()
    if reducer not in {"sum", "mean"}:
        raise _TransformSkip(f"calculateField reducer '{reducer}' is not auto-applicable")
    alias = str(options.get("alias") or "calculated").strip() or "calculated"
    include = list(reduce.get("include") or [])
    replace_fields = bool(options.get("replaceFields"))

    if include:
        source_cols = []
        for name in include:
            resolved = _resolve_field(
                name,
                panel=panel,
                columns=columns,
                metric_label_hints=metric_label_hints,
                group_fields=group_fields,
            )
            if resolved is None:
                raise _TransformSkip(f"could not resolve calculateField include field '{name}'")
            source_cols.append(resolved)
    else:
        source_cols = [col for col in metric_fields if col and col not in group_fields]
        if not source_cols:
            raise _TransformSkip("calculateField reduceRow has no metric columns to reduce")

    # A lone remaining series after a multi-target drop is not Grafana's
    # multi-field reduceRow (especially with replaceFields). Claiming apply
    # would misrepresent the source dashboard query.
    if len(source_cols) < 2:
        raise _TransformSkip(
            "calculateField reduceRow needs >=2 resolvable source series "
            f"(found {len(source_cols)})"
        )

    result_col = _unique_safe_alias(alias, used_aliases, fallback_suffix="calc")
    # Inline mean/sum — avoid __tx_* helpers that leak when a later normalize
    # pass strips KEEP projections (dotted-group workaround).
    if reducer == "sum":
        expr = " + ".join(f"COALESCE({_esql_ident(col)}, 0)" for col in source_cols)
        eval_line = f"| EVAL {_esql_ident(result_col)} = {expr}"
    else:
        sum_parts = " + ".join(f"COALESCE({_esql_ident(col)}, 0)" for col in source_cols)
        cnt_parts = " + ".join(
            f"CASE({_esql_ident(col)} IS NOT NULL, 1, 0)" for col in source_cols
        )
        eval_line = (
            f"| EVAL {_esql_ident(result_col)} = CASE(({cnt_parts}) > 0, "
            f"({sum_parts}) / ({cnt_parts}), NULL)"
        )
    query = _append_pipeline_stage(query, eval_line)

    metric_fields = list(metric_fields)
    metric_label_hints = dict(metric_label_hints)
    if replace_fields:
        query = _drop_columns(
            query,
            list(source_cols),
            keep_fallback=list(group_fields) + [result_col],
        )
        metric_fields = [result_col]
        metric_label_hints = {result_col: alias}
    else:
        if result_col not in metric_fields:
            metric_fields.append(result_col)
        metric_label_hints[result_col] = alias
        keep_cols = [col.strip("`") for col in _extract_keep_columns(query)]
        if not keep_cols:
            keep_cols = list(dict.fromkeys(list(group_fields) + list(metric_fields)))
        if result_col not in keep_cols:
            keep_cols.append(result_col)
        # Keep source columns until a later organize exclude drops them.
        query = _rewrite_keep(query, keep_cols)
    return query, metric_fields, metric_label_hints, used_aliases


def _apply_organize(
    query: str,
    options: dict[str, Any],
    *,
    panel: dict[str, Any],
    metric_fields: list[str],
    metric_label_hints: dict[str, str],
    group_fields: list[str],
    columns: set[str],
) -> tuple[str, list[str], dict[str, str]]:
    rename_by_name = options.get("renameByName") if isinstance(options.get("renameByName"), dict) else {}
    exclude_by_name = options.get("excludeByName") if isinstance(options.get("excludeByName"), dict) else {}
    metric_fields = list(metric_fields)
    metric_label_hints = dict(metric_label_hints)
    changed = False

    for old_name, new_name in rename_by_name.items():
        if not new_name:
            continue
        resolved = _resolve_field(
            str(old_name),
            panel=panel,
            columns=columns,
            metric_label_hints=metric_label_hints,
            group_fields=group_fields,
        )
        if resolved is None:
            raise _TransformSkip(f"could not resolve organize rename field '{old_name}'")
        new_col = _safe_alias(str(new_name))
        query = _append_pipeline_stage(
            query,
            f"| EVAL {_esql_ident(new_col)} = {_esql_ident(resolved)}",
        )
        query = _drop_columns(query, [resolved], keep_fallback=list(group_fields) + metric_fields + [new_col])
        metric_fields = [new_col if col == resolved else col for col in metric_fields]
        if resolved in metric_label_hints:
            metric_label_hints[new_col] = str(new_name)
            metric_label_hints.pop(resolved, None)
        else:
            metric_label_hints[new_col] = str(new_name)
        columns.discard(resolved)
        columns.add(new_col)
        changed = True

    drop_cols: list[str] = []
    for name, excluded in exclude_by_name.items():
        if not excluded:
            continue
        resolved = _resolve_field(
            str(name),
            panel=panel,
            columns=columns,
            metric_label_hints=metric_label_hints,
            group_fields=group_fields,
        )
        if resolved is None:
            # Excluding an already-absent field is a no-op (common after replaceFields).
            continue
        if resolved in group_fields:
            # Time/group excludes are display-only for Lens; do not drop BY keys.
            continue
        drop_cols.append(resolved)
    if drop_cols:
        remaining_metrics = [col for col in metric_fields if col not in set(drop_cols)]
        if metric_fields and not remaining_metrics:
            raise _TransformSkip(
                "organize exclude would remove all metric columns "
                f"(tried to drop {drop_cols})"
            )
        query = _drop_columns(
            query,
            drop_cols,
            keep_fallback=list(group_fields) + remaining_metrics,
        )
        metric_fields = remaining_metrics
        for col in drop_cols:
            metric_label_hints.pop(col, None)
        changed = True
    if not changed:
        raise _TransformSkip("organize made no resolvable column changes")
    return query, metric_fields, metric_label_hints


def _apply_filter_fields(
    query: str,
    options: dict[str, Any],
    *,
    panel: dict[str, Any],
    metric_fields: list[str],
    metric_label_hints: dict[str, str],
    group_fields: list[str],
    columns: set[str],
) -> tuple[str, list[str]]:
    include = options.get("include") if isinstance(options.get("include"), dict) else {}
    names = list(include.get("names") or [])
    if not names:
        raise _TransformSkip("filterFieldsByName has no include.names")
    keep_cols = list(group_fields)
    kept_metrics: list[str] = []
    for name in names:
        resolved = _resolve_field(
            str(name),
            panel=panel,
            columns=columns,
            metric_label_hints=metric_label_hints,
            group_fields=group_fields,
        )
        if resolved is None:
            raise _TransformSkip(f"could not resolve filterFieldsByName field '{name}'")
        if resolved not in keep_cols:
            keep_cols.append(resolved)
        if resolved in metric_fields and resolved not in kept_metrics:
            kept_metrics.append(resolved)
    query = _rewrite_keep(query, keep_cols)
    return query, kept_metrics or metric_fields


def _apply_sort_by(
    query: str,
    options: dict[str, Any],
    *,
    panel: dict[str, Any],
    metric_label_hints: dict[str, str],
    group_fields: list[str],
    columns: set[str],
) -> str:
    sort_items = options.get("sort") if isinstance(options.get("sort"), list) else []
    if not sort_items:
        raise _TransformSkip("sortBy has no sort entries")
    parts: list[str] = []
    for item in sort_items:
        if not isinstance(item, dict):
            continue
        resolved = _resolve_field(
            str(item.get("field") or ""),
            panel=panel,
            columns=columns,
            metric_label_hints=metric_label_hints,
            group_fields=group_fields,
        )
        if resolved is None:
            raise _TransformSkip(f"could not resolve sortBy field '{item.get('field')}'")
        direction = "DESC" if item.get("desc") else "ASC"
        parts.append(f"{_esql_ident(resolved)} {direction}")
    if not parts:
        raise _TransformSkip("sortBy has no resolvable fields")
    # Prefer keeping time ascending as a leading key when present.
    time_col = next((col for col in group_fields if col in _TIME_LIKE_COLUMNS), None)
    if time_col and not any(part.startswith(_esql_ident(time_col)) for part in parts):
        parts.insert(0, f"{_esql_ident(time_col)} ASC")
    sort_line = "| SORT " + ", ".join(parts)
    lines = [line for line in str(query).splitlines() if not (
        line.strip().startswith("|") and line.strip()[1:].strip().lower().startswith("sort ")
    )]
    insert_at = len(lines)
    for idx in range(len(lines) - 1, -1, -1):
        stripped = lines[idx].strip()
        if stripped.startswith("|") and stripped[1:].strip().lower().startswith("limit "):
            insert_at = idx
            continue
        break
    lines.insert(insert_at, sort_line)
    return "\n".join(lines)


def _apply_limit(query: str, options: dict[str, Any]) -> str:
    limit_value = options.get("limitValue", options.get("limit"))
    try:
        limit_n = int(limit_value)
    except (TypeError, ValueError):
        raise _TransformSkip("limit transform has no integer limitValue") from None
    if limit_n <= 0:
        raise _TransformSkip("limit transform requires a positive limit")
    lines = [line for line in str(query).splitlines() if not (
        line.strip().startswith("|") and line.strip()[1:].strip().lower().startswith("limit ")
    )]
    lines.append(f"| LIMIT {limit_n}")
    return "\n".join(lines)


__all__ = [
    "TransformApplyResult",
    "apply_transformations_to_esql",
    "build_redesign_tasks",
    "build_transform_summary",
    "extract_transformations",
    "mark_applied_transformations",
]
