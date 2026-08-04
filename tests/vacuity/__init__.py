# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""The vacuity harness: guards that cannot fail are the bug.

A guard is *vacuous* when it is structurally incapable of going red. Five real
ones shipped in this repo, all green, each hiding a defect:

===========  =====================================================  ==============================
Commit       What was vacuous                                       Flavour
===========  =====================================================  ==============================
``458f4e2``  a test pinned the exact palette Kibana rejects          wrong expectation
``07e5829``  Datadog T1 read a key no Datadog report writes, so      empty denominator
             every panel short-circuited to SKIP and drift was 0
``5160d11``  payload-vs-payload oracle ran the same mapper on both   tautological comparison
             sides, so a dropped panel dropped identically
``da25a51``  an idempotence guard compared the last physical         dead branch
             *line*; raw ES|QL is one line, so it never fired
``0c4f3a2``  four gates returned success on a zero denominator       empty denominator
===========  =====================================================  ==============================

One technique does not catch all four flavours, so the harness has four, all
enumerable from :mod:`tests.vacuity.registry`:

``GUARD_CASES``
    Each load-bearing guard is paired with mutations of its *subject* that must
    make it fail, plus a witness counting what it actually examined. A guard that
    stays green under its own mutation is the bug. Generalises the
    report-versus-invariant-linter pattern in ``parity-rig/verifier/mutations.py``
    to arbitrary guards.

``EMPTY_INPUT_GATES``
    Each gate that turns counts into a verdict is invoked on empty input and must
    refuse to report success.

``FIRING_GUARDS``
    Each idempotence / dedup / collision guard must be observed taking its
    interesting branch at least once — on the committed corpus where the corpus
    should exercise it, or through the production entry point where only a
    synthetic collision can.

``tests/vacuity/test_ratio_denominators.py``
    A structural census: every ratio-over-a-count in the gate layer must be
    classified as guarded or display-only. Adding a new one forces the decision.

See ``docs/testing.md`` (Tier 3) for how to add a guard.
"""
