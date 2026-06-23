# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Source-agnostic disposition of live ES|QL validation failures.

When a translated query fails live validation, the engine decides whether the
failure is a data-timing issue that self-heals once telemetry arrives, or a
genuinely broken query. These helpers classify that distinction and live in
core so both source adapters and the shared reporting layer can use them without
importing an adapter (issue #154).
"""

from __future__ import annotations

SELF_HEAL_SEMANTIC_LOSS = "target telemetry not yet ingested or not confirmed (self-healing panel)"


def _names(values):
    return [str(value or "").strip() for value in values or [] if str(value or "").strip()]


def _backtick_join(names):
    return ", ".join(f"`{name}`" for name in names)


def validation_failure_self_heals(validation_result):
    """True when a failed live validation is a data-timing issue rather than a
    broken query.

    A missing target field (``Unknown column``) or missing target index
    (``Unknown index``) means the telemetry has simply not been ingested yet.
    The translated ES|QL is structurally valid and the panel will populate on
    its own once data arrives, so it should be kept (with a warning) instead of
    being replaced by a markdown placeholder.

    A counter type mismatch only self-heals when validation could not positively
    confirm that the target field is non-counter. If Elasticsearch reports a
    concrete non-counter type, the panel would continue rendering an error and
    must stay manual.
    """
    analysis = (validation_result or {}).get("analysis") or {}
    counter_metrics = _names(analysis.get("counter_mismatch_metrics"))
    if counter_metrics:
        if "counter_mismatch_confirmed_non_counter" not in analysis:
            return False
        confirmed_non_counter = set(_names(analysis.get("counter_mismatch_confirmed_non_counter")))
        return not any(metric in confirmed_non_counter for metric in counter_metrics)

    if analysis.get("unknown_columns") or analysis.get("unknown_indexes"):
        return True

    return False


def missing_target_field_warning(validation_result):
    """Human-readable warning for a self-healing validation failure, naming the
    target fields/indexes that are not ingested yet.

    The message is deliberately not absolute: a field that is genuinely
    misnamed (rather than not-yet-ingested) is indistinguishable at validation
    time, so the wording invites the reviewer to check the field name if data is
    already flowing.
    """
    analysis = (validation_result or {}).get("analysis") or {}
    counter_metrics = _names(analysis.get("counter_mismatch_metrics"))
    confirmed_non_counter = set(_names(analysis.get("counter_mismatch_confirmed_non_counter")))
    unconfirmed_counter_metrics = [metric for metric in counter_metrics if metric not in confirmed_non_counter]
    names = [col.get("name", "") for col in analysis.get("unknown_columns") or []]
    names.extend(analysis.get("unknown_indexes") or [])
    names = _names(names)
    clauses = []
    if unconfirmed_counter_metrics:
        verb = "is" if len(unconfirmed_counter_metrics) == 1 else "are"
        clauses.append(
            f"{_backtick_join(unconfirmed_counter_metrics)} {verb} not confirmed as a counter "
            "in the target yet (the rate query is structurally valid for counter storage; "
            "verify the metric type if data is already flowing)"
        )
    if names:
        clauses.append(
            f"target field/index {_backtick_join(names)} could not be found yet (the "
            "query is structurally valid and the panel will populate once this telemetry "
            "is ingested; verify the field name if data is already flowing)"
        )
    if clauses:
        return "Live ES|QL validation reports " + "; also, ".join(clauses) + "."
    return (
        "Live ES|QL validation found no matching data yet; the query is "
        "structurally valid and the panel will populate once telemetry is ingested "
        "(verify the query if data is already flowing)."
    )
