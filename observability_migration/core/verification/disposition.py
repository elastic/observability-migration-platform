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
    """True when a failed live validation reflects a target storage gap rather
    than a broken query.

    A missing target field (``Unknown column``) or missing target index
    (``Unknown index``) means the telemetry has simply not been ingested yet.
    The translated ES|QL is structurally valid and the panel will populate on
    its own once data arrives, so it should be kept (with a warning) instead of
    being replaced by a markdown placeholder.

    A counter type mismatch (``RATE``/``IRATE``/``INCREASE``/``DELTA`` rejected
    because the metric is not stored as a counter) is treated the same way. The
    rejection is environment-dependent: the identical query passes against a
    target that stores the metric with counter semantics and fails against one
    that does not. The query form is the faithful translation of a counter
    operation in the source, so the check cannot positively confirm the form is
    wrong -- it only knows this target has not confirmed counter storage. We
    keep the panel with a warning rather than mark the dashboard failed for a
    form that is valid against a correctly-configured target (issue #170).
    """
    analysis = (validation_result or {}).get("analysis") or {}
    return bool(
        analysis.get("unknown_columns")
        or analysis.get("unknown_indexes")
        or analysis.get("counter_mismatch_metrics")
    )


def _backtick_join(names):
    """Render ``names`` as a comma-separated list of backtick-quoted tokens."""
    return ", ".join(f"`{name}`" for name in names)


def missing_target_field_warning(validation_result):
    """Human-readable warning for a self-healing validation failure, naming
    every signal the analysis reported: counter-storage gaps and not-yet-ingested
    target fields/indexes.

    A single live validation can report more than one problem at once (ES|QL
    batches them into one "Found N problems" message), so the warning surfaces
    each present category rather than only the first -- otherwise a reviewer told
    to fix counter storage would never learn a field was also unresolved.

    The message is deliberately not absolute: a field that is genuinely
    misnamed (rather than not-yet-ingested) is indistinguishable at validation
    time, so the wording invites the reviewer to check the field name if data is
    already flowing.
    """
    analysis = (validation_result or {}).get("analysis") or {}
    counter_metrics = [name for name in analysis.get("counter_mismatch_metrics") or [] if name]
    names = [col.get("name", "") for col in analysis.get("unknown_columns") or []]
    names.extend(analysis.get("unknown_indexes") or [])
    names = [name for name in names if name]

    clauses = []
    if counter_metrics:
        verb = "is" if len(counter_metrics) == 1 else "are"
        clauses.append(
            f"{_backtick_join(counter_metrics)} {verb} not stored as a counter in the "
            "target yet (the rate query is structurally valid and will work once the "
            "metric is ingested with counter semantics; verify the metric type if data "
            "is already flowing)"
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
