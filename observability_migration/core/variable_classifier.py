"""Variable feasibility classifier for Kibana ES|QL variable controls.

See docs/roadmap/2026-04-27-kibana-variable-controls-design.md for the design.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

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


@dataclass(frozen=True)
class AcceptedBinding:
    field: str
    multi: bool
    options_query: str


@dataclass(frozen=True)
class RejectedBinding:
    reason: str

    def __post_init__(self) -> None:
        if self.reason not in REJECT_REASONS:
            raise ValueError(
                f"unknown reject reason {self.reason!r}; "
                f"must be one of {REJECT_REASONS}"
            )


VariableBindingMap = dict[str, AcceptedBinding | RejectedBinding]
