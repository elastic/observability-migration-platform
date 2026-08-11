# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Meta-tests over the guard registry: a guard that cannot fail is the bug.

Nothing here knows anything about dashboards. It reads
:mod:`tests.vacuity.registry` and, for every entry:

* runs the guard on a healthy subject and requires it to pass (so a guard cannot
  satisfy the harness by always failing);
* applies each registered mutation and requires the declared outcome;
* checks the guard's witness against its floor (the denominator assertion);
* for gates, requires a refusal on empty input and an acceptance on healthy input;
* for firing guards, requires the interesting branch to execute at least once.

Failure messages carry ``why`` and ``catches`` from the registry, so a red run
tells the next reader which real defect the entry exists to stop.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import pytest

from tests.vacuity.registry import (
    EMPTY_INPUT_GATES,
    FIRING_GUARDS,
    GUARD_CASES,
    GuardCase,
    Mutation,
    resolve_module,
)

_EXPECTATIONS = {"red", "green", "witness_collapse"}


@contextlib.contextmanager
def _patched(mutation: Mutation) -> Iterator[None]:
    """Apply a mutation's patches, restoring them afterwards."""
    restore: list[tuple[Any, str, Any]] = []
    try:
        for patch in mutation.patches:
            module = resolve_module(patch.module)
            original = getattr(module, patch.attr)
            restore.append((module, patch.attr, original))
            setattr(module, patch.attr, patch.factory(original))
        yield
    finally:
        for module, attr, original in reversed(restore):
            setattr(module, attr, original)


def _floor(case: GuardCase, subject: Any) -> int:
    return case.min_witness(subject) if callable(case.min_witness) else case.min_witness


def _guard_ids() -> list[str]:
    return [case.guard for case in GUARD_CASES]


def _mutation_cases() -> list[tuple[GuardCase, Mutation]]:
    return [(case, mutation) for case in GUARD_CASES for mutation in case.mutations]


def _mutation_ids() -> list[str]:
    return [
        f"{case.guard.rsplit('.', 1)[-1]}::{mutation.name}"
        for case, mutation in _mutation_cases()
    ]


# --------------------------------------------------------------------------- #
# Registry hygiene
# --------------------------------------------------------------------------- #


def test_every_registry_entry_is_documented():
    """An entry without a stated reason is an entry nobody can review."""
    for table, label in (
        (GUARD_CASES, "GUARD_CASES"),
        (EMPTY_INPUT_GATES, "EMPTY_INPUT_GATES"),
        (FIRING_GUARDS, "FIRING_GUARDS"),
    ):
        assert table, f"{label} is empty"
        for entry in table:
            name = getattr(entry, "guard", None) or getattr(entry, "gate", "")
            assert len(entry.why) > 60, f"{label} entry {name!r} has no substantive `why`"
            assert entry.catches, f"{label} entry {name!r} does not name what it catches"


def test_every_guard_declares_at_least_one_reddening_mutation():
    """A guard whose only mutations expect green proves nothing about failing."""
    for case in GUARD_CASES:
        assert {mutation.expect for mutation in case.mutations} <= _EXPECTATIONS, (
            f"{case.guard}: unknown expectation"
        )
        reddening = [m for m in case.mutations if m.expect == "red"]
        assert reddening, (
            f"{case.guard} has no mutation that must make it fail, so the harness "
            f"never proves it can go red at all"
        )


def test_every_firing_guard_declares_a_known_flavour():
    for firing in FIRING_GUARDS:
        assert firing.flavour in {"corpus", "path"}, (
            f"{firing.guard}: flavour must be 'corpus' or 'path', got {firing.flavour!r}"
        )


# --------------------------------------------------------------------------- #
# GUARD_CASES
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("case", GUARD_CASES, ids=_guard_ids())
def test_guard_passes_on_a_healthy_subject(case: GuardCase):
    """A guard that fails on healthy input cannot distinguish anything."""
    case.check(case.subject())


@pytest.mark.parametrize("case", GUARD_CASES, ids=_guard_ids())
def test_guard_examined_a_non_zero_denominator(case: GuardCase):
    """The denominator assertion: passing because nothing was inspected is not passing."""
    if case.witness is None:
        pytest.skip(f"{case.guard} declares no witness")
    subject = case.subject()
    floor = _floor(case, subject)
    assert floor > 0, (
        f"{case.guard}: its witness floor is {floor}, so the check is satisfied by a "
        f"guard that inspects nothing. Derive the floor from an independent source."
    )
    examined = case.witness(subject)
    assert examined >= floor, (
        f"{case.guard} examined {examined} subject(s), below its floor of {floor}. "
        f"It is passing because it looked at nothing.\n  why it matters: {case.why}\n"
        f"  stands for: {case.catches}"
    )


@pytest.mark.parametrize(("case", "mutation"), _mutation_cases(), ids=_mutation_ids())
def test_guard_responds_to_its_registered_mutation(case: GuardCase, mutation: Mutation):
    with _patched(mutation):
        # Built *under* the patches: a mutation that corrupts the code producing
        # the subject must be able to corrupt the subject too.
        subject = mutation.apply(case.subject())
        witness = case.witness(subject) if case.witness else None
        floor = _floor(case, subject) if case.witness else 0
        raised: BaseException | None = None
        try:
            case.check(subject)
        except case.fails_with as exc:
            raised = exc

    context = (
        f"\n  guard: {case.guard}\n  mutation: {mutation.name}\n"
        f"  why the mutation matters: {mutation.why}\n  stands for: {case.catches}"
    )
    if mutation.expect == "red":
        assert raised is not None, (
            f"the guard stayed GREEN under a mutation it must catch. A guard that "
            f"survives its own mutation is vacuous.{context}"
        )
    elif mutation.expect == "green":
        assert raised is None, (
            f"the guard went RED on a mutation it must tolerate: {raised}{context}"
        )
    else:  # witness_collapse
        assert witness is not None, (
            f"a witness_collapse mutation needs a witness to collapse{context}"
        )
        assert witness < floor, (
            f"the guard examined {witness} subject(s) (floor {floor}) and stayed "
            f"green, so neither the guard nor its denominator noticed the "
            f"blindness{context}"
        )


# --------------------------------------------------------------------------- #
# EMPTY_INPUT_GATES
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "gate", EMPTY_INPUT_GATES, ids=[gate.gate for gate in EMPTY_INPUT_GATES]
)
def test_gate_refuses_empty_input(gate: Any):
    context = f"\n  gate: {gate.gate}\n  why: {gate.why}\n  stands for: {gate.catches}"
    if gate.refuses_by_raising:
        with pytest.raises(gate.refuses_by_raising) as excinfo:
            result = gate.invoke_empty()
            pytest.fail(
                f"the gate returned {result!r} on empty input instead of refusing"
                f"{context}"
            )
        assert str(excinfo.value).strip(), (
            f"the gate refused but said nothing about why{context}"
        )
        return
    result = gate.invoke_empty()
    assert gate.refused(result), (
        f"the gate reported SUCCESS ({result!r}) with nothing to measure. There is no "
        f"percentage of nothing.{context}"
    )


@pytest.mark.parametrize(
    "gate", EMPTY_INPUT_GATES, ids=[gate.gate for gate in EMPTY_INPUT_GATES]
)
def test_gate_still_accepts_healthy_input(gate: Any):
    """A gate that refuses everything is as useless as one that accepts everything."""
    result = gate.invoke_healthy()
    assert gate.accepted(result), (
        f"{gate.gate} refused healthy input ({result!r}), so its empty-input refusal "
        f"proves nothing"
    )


# --------------------------------------------------------------------------- #
# FIRING_GUARDS
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "firing", FIRING_GUARDS, ids=[firing.guard for firing in FIRING_GUARDS]
)
def test_guard_branch_actually_executes(firing: Any):
    fires = firing.run()
    assert fires >= firing.min_fires, (
        f"{firing.guard} took its interesting branch {fires} time(s), needed "
        f"{firing.min_fires}. Either the branch is dead code or "
        + (
            "the committed corpus stopped exercising it"
            if firing.flavour == "corpus"
            else "it is no longer reachable through the production entry point"
        )
        + f".\n  why it matters: {firing.why}\n  stands for: {firing.catches}"
    )
