---
name: verify-label-binding
description: "Contributor verification for changes to Grafana label-binding smoke probes, readiness gates, or synthetic-series emission. Runs symbolic gate contracts, generated query checks, and guard mutations against production Python."
---

# Verify label binding

Audience: contributors with a repository checkout and `uv`. This verification
workflow is separate from the installable operator CLI.

A missing or unproven label must not become verified merely because its
synthetic legend is nonempty. The harm is a dashboard that merges instances or
loses their names while reporting a clean validation result.

## Run

1. Run `make sync` from the repository root.
2. Run `.venv/bin/python scripts/verification/verify_label_bindings.py`.
   It must report three CrossHair confirmations and three caught mutations.
   An unknown result, timeout, failed assertion, or missed mutation is a failure.
3. For read-only runtime verification, set `ES_URL` and, when required,
   `ES_API_KEY`, then rerun the same command. Five `ROW`-based queries check
   comments, assignment order, downstream selection and split conversions;
   they create no indices. Report an omitted live run separately.

## What this verifies

- `scripts/verification/binding_contracts.py` calls the production
  `_semantic_gate` with CrossHair **0.0.113**. The unverified-label contract
  covers six canonical panel-state classes and both missing/warning booleans,
  with no validation record, comparison or semantic losses. Error and skipped
  precedence contracts allow arbitrary binding strings and their boolean flags.
- `tests/test_binding_invariants.py` exhausts **288** combinations of six panel
  states, six binding states, four runtime states and two semantic-loss states.
  Hypothesis (pinned in `uv.lock`) also runs **200** deterministic generated
  query examples with one to three labels, split conversions, identifier
  quoting, whitespace, comments and fallback literals. Generated examples are
  not an exhaustive proof of ES|QL syntax.
- `scripts/verification/check_binding_mutations.py` removes each of three
  guards from disposable copies of the real module. Each symbolic contract must
  reject its corresponding broken variant. The checkout stays unchanged.
- `tests/test_redis_composite_binding.py` exercises the actual native Redis
  two-metric query and malformed/failed probe evidence. Mocked responses cover
  validation behavior; they do not establish Elasticsearch or browser behavior.

These checks do not prove arbitrary ES|QL instrumentation, Elasticsearch
semantics, control interactions, numeric parity, or Kibana rendering. Run the
layered runtime/API/parity/render gates in `docs/contributing/dev-commands.md`
when making a dashboard correctness claim.

## Coverage and drift

Last confirmed source commit: `de55c1a12ed3d74cff749855a86de16cfc2946f7`.

Covered code: `adapters/source/grafana/smoke.py` (`_composite_binding_probe`,
`validate_esql`, `binding_gap_hint`, `inspect_dashboard`, `build_summary`) and
`adapters/source/grafana/verification.py` (`_semantic_gate`), both under
`observability_migration/`. The Redis test also reaches native folding in
`observability_migration/targets/kibana/dashboards_api.py`.

Before trusting a pass after those files change, diff them from the recorded
commit, check that each property still describes the production behavior, and
rerun the command. Keep counterexamples as findings; change a property only
when the documented promise intentionally changes. Advance the sync commit
only after this review and a passing mutation run. Use pstack's
`maintain-formal-verification-skill` workflow for accumulated verification drift.

