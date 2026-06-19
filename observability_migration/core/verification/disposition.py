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

# Structured semantic-loss marker recorded when a panel/widget is kept as a
# self-healing visualization. Mirrors the placeholder path's marker so coverage
# reports surface the disposition (issue #154).
SELF_HEAL_SEMANTIC_LOSS = "target telemetry not yet ingested (self-healing panel)"


def validation_failure_self_heals(validation_result):
    """True when a failed live validation is a data-timing issue rather than a
    broken query.

    A missing target field (``Unknown column``) or missing target index
    (``Unknown index``) means the telemetry has simply not been ingested yet.
    The translated ES|QL is structurally valid and the panel will populate on
    its own once data arrives, so it should be kept (with a warning) instead of
    being replaced by a markdown placeholder.

    A counter type mismatch is excluded: the field exists but has the wrong
    type, so waiting for data will not fix it.
    """
    analysis = (validation_result or {}).get("analysis") or {}
    if analysis.get("counter_mismatch_metrics"):
        return False
    return bool(analysis.get("unknown_columns") or analysis.get("unknown_indexes"))


def missing_target_field_warning(validation_result):
    """Human-readable warning for a self-healing validation failure, naming the
    target fields/indexes that are not ingested yet.

    The message is deliberately not absolute: a field that is genuinely
    misnamed (rather than not-yet-ingested) is indistinguishable at validation
    time, so the wording invites the reviewer to check the field name if data is
    already flowing.
    """
    analysis = (validation_result or {}).get("analysis") or {}
    names = [col.get("name", "") for col in analysis.get("unknown_columns") or []]
    names.extend(analysis.get("unknown_indexes") or [])
    names = [name for name in names if name]
    if names:
        field_list = ", ".join(f"`{name}`" for name in names)
        return (
            f"Live ES|QL validation could not find target field/index {field_list} "
            "yet; the query is structurally valid and the panel will populate once "
            "this telemetry is ingested (verify the field name if data is already flowing)."
        )
    return (
        "Live ES|QL validation found no matching data yet; the query is "
        "structurally valid and the panel will populate once telemetry is ingested "
        "(verify the query if data is already flowing)."
    )
