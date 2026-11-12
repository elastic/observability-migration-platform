# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Generated inputs and bounded exhaustive checks of production binding code."""

from itertools import product
from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from observability_migration.adapters.source.grafana import smoke
from observability_migration.adapters.source.grafana.verification import _semantic_gate


@settings(max_examples=200, derandomize=True, deadline=None)
@given(
    dimensions=st.integers(min_value=1, max_value=3),
    split=st.booleans(), quoted=st.booleans(),
    whitespace=st.sampled_from([" ", "\t", "\n"]),
    fallback=st.sampled_from(['""', '"unknown"', '""""""']),
    comment=st.sampled_from(["", "/* label | note */ ", "// label\n"]),
)
def test_every_masked_group_field_is_checked(dimensions, split, quoted, whitespace, fallback, comment):
    fields = ["labels.instance", "labels.db", "job"][:dimensions]
    def identifier(name):
        return f"`{name}`" if quoted else name
    terms = [f"COALESCE({comment}TO_STRING({identifier(name)}), {fallback})" for name in fields]
    stages = ["FROM metrics-*", "STATS used = AVG(x) BY " + ", ".join(fields)]
    if split:
        stages += [f"{comment}EVAL{whitespace}s{i} = {term}" for i, term in enumerate(terms)]
        stages += ["EVAL series = CONCAT(" + ', "/", '.join(f"s{i}" for i in range(dimensions)) + ', " - used")']
    else:
        stages += [f"{comment}EVAL{whitespace}series = CONCAT(" + ', "/", '.join(terms) + ', " - used")']
    stages += ["WHERE used > 0", "KEEP series, used", "LIMIT 20"]
    query, checked_fields = smoke._composite_binding_probe(" | ".join(stages))
    assert set(checked_fields) == set(fields)
    assert "WHERE used > 0" in query
    assert "LIMIT 20" in query


def test_readiness_exhaustively_preserves_stronger_outcomes():
    # 6 panel states x 6 binding states x 4 runtime states x 2 loss states.
    for status, binding, runtime, loss in product(
        ["migrated", "migrated_with_warnings", "skipped", "not_feasible", "requires_manual", "unknown"],
        ["guessed", "missing", "asserted", "verified", "none", "unknown"],
        [[], ["upload_failed"], ["null_series"], ["upload_failed", "null_series"]],
        [[], ["loss"]],
    ):
        panel = SimpleNamespace(status=status, target_binding={"status": binding})
        result = _semantic_gate(panel, None, loss, runtime)
        if status in {"not_feasible", "requires_manual"}:
            assert result == "Red"
        elif status == "skipped":
            assert result == "Skipped"
        elif "upload_failed" in runtime:
            assert result == "Red"
        elif binding in {"guessed", "missing"} or loss or runtime or status == "migrated_with_warnings":
            assert result == "Yellow"
        else:
            assert result == "Green"
