"""Post-translation correctness verifier for variable controls.

See docs/roadmap/2026-04-27-kibana-variable-controls-design.md §7.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from observability_migration.core.variable_classifier import (
    AcceptedBinding,
    RejectedBinding,
    VariableBindingMap,
)


@dataclass(frozen=True)
class PanelTranslationRecord:
    panel_id: str
    compiled_esql: str
    source_var_refs: set[str]
    observed_fields: dict[str, str] = field(default_factory=dict)
    observed_ops: dict[str, str] = field(default_factory=dict)
    data_view: str = ""


@dataclass(frozen=True)
class _Downgrade:
    var_name: str
    reason: str


CheckFn = Callable[[list[PanelTranslationRecord], VariableBindingMap], list[_Downgrade]]


def _check_field_consistency(records, binding_map):
    out = []
    for name, binding in binding_map.items():
        if not isinstance(binding, AcceptedBinding):
            continue
        observed = {r.observed_fields[name] for r in records if name in r.observed_fields}
        if observed - {binding.field}:
            out.append(_Downgrade(name, "verifier_failed_field_consistency"))
    return out


def _check_operator_consistency(records, binding_map):
    out = []
    for name, binding in binding_map.items():
        if not isinstance(binding, AcceptedBinding):
            continue
        ops = {r.observed_ops[name] for r in records if name in r.observed_ops}
        if "exact_match" in ops and "multi_value" in ops:
            out.append(_Downgrade(name, "verifier_failed_operator_consistency"))
    return out


def _check_leftover_token(records, binding_map):
    out = []
    for name, binding in binding_map.items():
        if not isinstance(binding, AcceptedBinding):
            continue
        token = re.compile(rf"\${name}\b")
        for r in records:
            if name in r.source_var_refs and token.search(r.compiled_esql):
                out.append(_Downgrade(name, "verifier_failed_leftover_token"))
                break
    return out


def _check_missing_param(records, binding_map):
    out = []
    for name, binding in binding_map.items():
        if not isinstance(binding, AcceptedBinding):
            continue
        token = re.compile(rf"\?{name}\b")
        for r in records:
            if name in r.source_var_refs and not token.search(r.compiled_esql):
                out.append(_Downgrade(name, "verifier_failed_missing_param"))
                break
    return out


def _check_over_application(records, binding_map):
    out = []
    for name, binding in binding_map.items():
        if not isinstance(binding, AcceptedBinding):
            continue
        token = re.compile(rf"\?{name}\b")
        for r in records:
            if name not in r.source_var_refs and token.search(r.compiled_esql):
                out.append(_Downgrade(name, "verifier_failed_over_application"))
                break
    return out


def _check_data_view_split(records, binding_map):
    out = []
    for name, binding in binding_map.items():
        if not isinstance(binding, AcceptedBinding):
            continue
        data_views = {r.data_view for r in records if name in r.source_var_refs and r.data_view}
        if len(data_views) > 1:
            out.append(_Downgrade(name, "verifier_failed_data_view_split"))
    return out


CHECKS: list[CheckFn] = [
    _check_field_consistency,
    _check_operator_consistency,
    _check_leftover_token,
    _check_missing_param,
    _check_over_application,
    _check_data_view_split,
]


def verify_bindings(
    records: list[PanelTranslationRecord],
    binding_map: VariableBindingMap,
) -> VariableBindingMap:
    out: VariableBindingMap = dict(binding_map)
    for check in CHECKS:
        for downgrade in check(records, out):
            out[downgrade.var_name] = RejectedBinding(reason=downgrade.reason)
    return out
