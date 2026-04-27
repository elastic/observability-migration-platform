"""Variable feasibility classifier for Kibana ES|QL variable controls.

See docs/roadmap/2026-04-27-kibana-variable-controls-design.md for the design.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Final, Literal, get_args

REJECT_REASONS: Final[tuple[str, ...]] = (
    "unsupported_variable_type",
    "drives_repeat",
    "unknown_definition_shape",
    "field_resolution_ambiguous",
    "field_resolution_failed",
    "inconsistent_field_use",
    "regex_template",
    "include_all_unsupported",
    "multi_value_with_eq_operator",
    "data_view_split",
    "native_promql_panel",
    "no_tag_field",
    "wildcard_default",
    "mixed_or_branches",
    "invalid_variable_name",
    "reserved_identifier",
    "verifier_failed_field_consistency",
    "verifier_failed_operator_consistency",
    "verifier_failed_leftover_token",
    "verifier_failed_missing_param",
    "verifier_failed_over_application",
    "verifier_failed_data_view_split",
)

RejectReason = Literal[
    "unsupported_variable_type",
    "drives_repeat",
    "unknown_definition_shape",
    "field_resolution_ambiguous",
    "field_resolution_failed",
    "inconsistent_field_use",
    "regex_template",
    "include_all_unsupported",
    "multi_value_with_eq_operator",
    "data_view_split",
    "native_promql_panel",
    "no_tag_field",
    "wildcard_default",
    "mixed_or_branches",
    "invalid_variable_name",
    "reserved_identifier",
    "verifier_failed_field_consistency",
    "verifier_failed_operator_consistency",
    "verifier_failed_leftover_token",
    "verifier_failed_missing_param",
    "verifier_failed_over_application",
    "verifier_failed_data_view_split",
]

assert set(get_args(RejectReason)) == set(REJECT_REASONS), (
    "RejectReason Literal and REJECT_REASONS tuple must list the same codes"
)


@dataclass(frozen=True)
class AcceptedBinding:
    field: str
    multi: bool
    options_query: str
    default_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class RejectedBinding:
    reason: RejectReason

    def __post_init__(self) -> None:
        if self.reason not in REJECT_REASONS:
            raise ValueError(
                f"unknown reject reason {self.reason!r}; "
                f"must be one of {REJECT_REASONS}"
            )


VariableBindingMap = dict[str, AcceptedBinding | RejectedBinding]


def compute_min_kibana_version(binding_map: VariableBindingMap) -> str:
    has_multi_value = any(
        isinstance(b, AcceptedBinding) and b.multi for b in binding_map.values()
    )
    return "9.3.0" if has_multi_value else "9.1.0"


def _fetch_default_values(resolver_or_map, field_name: str) -> tuple[str, ...]:
    """Best-effort cluster fetch of distinct values for ``field_name``.

    Returns an empty tuple when the resolver/field-map can't run cluster
    queries. Used to populate ``ESQLQuery*SelectControl.default`` so Kibana
    loads dashboards with values pre-selected; without a default, Kibana shows
    "Select a value" and panel queries that reference ``?param`` crash ES with
    ``DataType.isCounter() because t is null``.
    """
    fetcher = getattr(resolver_or_map, "fetch_distinct_field_values", None)
    if not callable(fetcher):
        return ()
    try:
        values = fetcher(field_name, limit=20) or []
    except Exception:
        return ()
    return tuple(str(v) for v in values if v is not None)


def build_options_query(*, data_view: str, field: str) -> str:
    if not data_view or not field:
        raise ValueError("data_view and field must be non-empty")
    return (
        f"FROM {data_view}\n"
        f"| WHERE {field} IS NOT NULL\n"
        f"| STATS BY {field}\n"
        f"| KEEP {field}\n"
        f"| LIMIT 1000"
    )


ESQL_RESERVED_WORDS: Final[frozenset[str]] = frozenset({
    "from", "where", "stats", "by", "keep", "drop", "rename",
    "eval", "sort", "limit", "enrich", "mv_expand", "lookup",
    "join", "grok", "dissect",
})

_DISABLE_ENV_VAR = "OBS_MIGRATION_DISABLE_VARIABLE_CONTROLS"


def _is_disabled() -> bool:
    return os.environ.get(_DISABLE_ENV_VAR) == "1"


_VALID_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LABEL_VALUES_RE = re.compile(r"label_values\s*\(\s*([^,]+?)\s*,\s*([A-Za-z0-9_]+)\s*\)")
_REGEX_META_RE = re.compile(r"[][(){}|^$+*?\\]")


def _grafana_definition_text(var: dict) -> str:
    definition = var.get("definition")
    if isinstance(definition, str) and definition.strip():
        return definition
    query = var.get("query")
    if isinstance(query, dict):
        text = query.get("query") or query.get("definition") or ""
        return str(text)
    if isinstance(query, str):
        return query
    return ""


def _extract_label_from_definition(text: str) -> str | None:
    match = _LABEL_VALUES_RE.search(text)
    if not match:
        return None
    return match.group(2)


def _grafana_panel_matchers_for(var_name: str, panel: dict) -> list[dict]:
    """Return matchers in `panel` that reference ``$var_name``.

    Each matcher dict carries ``field``, ``op``, and ``value_template``
    (the raw text inside the quotes).
    """
    out: list[dict] = []
    for target in panel.get("targets", []) or []:
        expr = target.get("expr", "") or ""
        if (
            f"${var_name}" not in expr
            and f"${{{var_name}}}" not in expr
            and f"[[{var_name}]]" not in expr
        ):
            continue
        for m in re.finditer(
            r'(?P<field>[A-Za-z_][A-Za-z0-9_:]*)\s*(?P<op>=~|!~|=|!=)\s*"(?P<value>[^"]*)"',
            expr,
        ):
            value = m.group("value")
            if (
                f"${var_name}" in value
                or f"${{{var_name}}}" in value
                or f"[[{var_name}]]" in value
            ):
                out.append({
                    "field": m.group("field"),
                    "op": m.group("op"),
                    "value_template": value,
                })
    return out


def classify_grafana_variables(
    *,
    variables: list[dict],
    panels: list[dict],
    resolver,
    repeat_variable_names: set[str],
    data_view: str,
    panel_data_view=None,
) -> VariableBindingMap:
    """Classify Grafana template variables for ES|QL parameter eligibility."""
    if _is_disabled():
        return {
            v["name"]: RejectedBinding(reason="unsupported_variable_type")
            for v in variables
            if v.get("name")
        }
    binding_map: VariableBindingMap = {}
    for var in variables:
        name = var.get("name", "")
        if not name:
            continue
        result = _classify_one_grafana(
            var=var,
            name=name,
            panels=panels,
            resolver=resolver,
            repeat_variable_names=repeat_variable_names,
            data_view=data_view,
            panel_data_view=panel_data_view,
        )
        binding_map[name] = result
    return binding_map


def _classify_one_grafana(
    *,
    var,
    name,
    panels,
    resolver,
    repeat_variable_names,
    data_view,
    panel_data_view,
):
    if not _VALID_IDENTIFIER_RE.match(name):
        return RejectedBinding(reason="invalid_variable_name")
    if name.lower() in ESQL_RESERVED_WORDS:
        return RejectedBinding(reason="reserved_identifier")
    if var.get("type") != "query":
        return RejectedBinding(reason="unsupported_variable_type")
    if name in repeat_variable_names:
        return RejectedBinding(reason="drives_repeat")

    definition = _grafana_definition_text(var)
    label = _extract_label_from_definition(definition)
    if label is None:
        return RejectedBinding(reason="unknown_definition_shape")

    field = resolver.resolve_control_field(label) if resolver else None
    if not field:
        return RejectedBinding(reason="field_resolution_failed")
    if not (resolver.field_exists(field) is not False):
        return RejectedBinding(reason="field_resolution_failed")

    multi = bool(var.get("multi"))
    include_all = bool(var.get("includeAll"))
    if include_all and not multi:
        return RejectedBinding(reason="include_all_unsupported")

    observed_field: str | None = None
    observed_data_view: str | None = None
    for panel in panels:
        matchers = _grafana_panel_matchers_for(name, panel)
        if not matchers:
            continue
        if panel_data_view is not None:
            dv = panel_data_view(panel)
            if observed_data_view is None:
                observed_data_view = dv
            elif observed_data_view != dv:
                return RejectedBinding(reason="data_view_split")
        for matcher in matchers:
            template = matcher["value_template"]
            stripped = template.replace(f"${{{name}}}", "").replace(f"${name}", "")
            if _REGEX_META_RE.search(stripped):
                return RejectedBinding(reason="regex_template")
            if matcher["op"] == "=" and multi:
                return RejectedBinding(reason="multi_value_with_eq_operator")
            mapped = resolver.resolve_label(matcher["field"]) if resolver else None
            if mapped is None:
                continue
            if observed_field is None:
                observed_field = mapped
            elif observed_field != mapped:
                return RejectedBinding(reason="inconsistent_field_use")

    canonical_field = observed_field or field
    canonical_data_view = observed_data_view or data_view
    options_query = build_options_query(
        data_view=canonical_data_view, field=canonical_field
    )
    return AcceptedBinding(
        field=canonical_field,
        multi=multi,
        options_query=options_query,
        default_values=_fetch_default_values(resolver, canonical_field),
    )


def classify_datadog_variables(
    *,
    variables,
    widgets,
    field_map,
    data_view: str,
) -> VariableBindingMap:
    if _is_disabled():
        return {
            getattr(v, "name", ""): RejectedBinding(reason="unsupported_variable_type")
            for v in variables
            if getattr(v, "name", "")
        }
    binding_map: VariableBindingMap = {}
    for tv in variables:
        name = getattr(tv, "name", "")
        if not name:
            continue
        binding_map[name] = _classify_one_datadog(
            tv=tv, name=name, widgets=widgets, field_map=field_map, data_view=data_view
        )
    return binding_map


def _datadog_value_has_template(value: str) -> bool:
    return bool(re.search(r"\$[A-Za-z_]", value))


def _classify_one_datadog(*, tv, name, widgets, field_map, data_view):
    if not _VALID_IDENTIFIER_RE.match(name):
        return RejectedBinding(reason="invalid_variable_name")
    if name.lower() in ESQL_RESERVED_WORDS:
        return RejectedBinding(reason="reserved_identifier")
    tag = getattr(tv, "tag", "") or getattr(tv, "prefix", "")
    if not tag:
        return RejectedBinding(reason="no_tag_field")

    default = getattr(tv, "default", "") or ""
    defaults = list(getattr(tv, "defaults", []) or [])
    if "*" in default and not defaults:
        return RejectedBinding(reason="wildcard_default")

    field = field_map.map_tag(tag, context="metric") if field_map else None
    if not field:
        return RejectedBinding(reason="field_resolution_failed")

    multi = bool(defaults) or default == "*"

    canonical_field = field
    for widget in widgets:
        for request in widget.get("requests", []) or []:
            q = str(request.get("q") or "")
            for match in re.finditer(
                r"([A-Za-z0-9_:.-]+)\s*:\s*([^\s,}]+)", q,
            ):
                w_tag = match.group(1)
                w_value = match.group(2)
                if w_tag != tag:
                    continue
                if not _datadog_value_has_template(w_value):
                    continue
                if w_value.count("$") > 1 or "|" in w_value:
                    return RejectedBinding(reason="mixed_or_branches")
                bare = w_value.replace(f"${name}.value", "").replace(f"${name}", "")
                if bare:
                    if "*" in bare or "?" in bare:
                        return RejectedBinding(reason="wildcard_default")

    options_query = build_options_query(data_view=data_view, field=canonical_field)
    return AcceptedBinding(
        field=canonical_field,
        multi=multi,
        options_query=options_query,
        default_values=_fetch_default_values(field_map, canonical_field),
    )
