# CLAUDE.md — mig-to-kbn

Claude-specific guidance. For automation/agent rules see `AGENTS.md`. For public docs see `docs/README.md`.

## Upstream Boundary

This repo (the **Observability Migration Platform**, CLI `obs-migrate`) is the
canonical source for the migration **engine** — `grafana-migrate`,
`datadog-migrate`, PromQL/Datadog translation, and the shared Kibana
YAML/compile path. **`mig-to-kbn`** is that engine's upstream identity; treat
this repo as the single source of truth for it. (See the Naming note in
`AGENTS.md`.)

- Engine fixes and features belong in **Issues/PRs on this repo**, not in downstream forks.
- The vendored copy at `validation/external_assets/dashboard-alert-migration/mig-to-kbn/` is a **snapshot**. Changes there should be bumps via `validation/external_assets/dashboard-alert-migration/scripts/update_mig_to_kbn.sh`, not long-lived forks.

## Project Conventions

- Architecture overview: `docs/architecture.md`
- Canonical CLI commands: `docs/command-contract.md`
- Build / test / lint: see `AGENTS.md` (use `make test`, `make lint`, `make typecheck`).
- Keep docs in the same PR as operator-visible behavior changes. Update
  `docs/command-contract.md` for CLI/env/upload/compile/smoke changes,
  `docs/architecture/asset-model.md` for shared IR/result contracts,
  `docs/architecture.md` and `docs/pipeline-trace.tpl.md` for package maps or
  cross-source pipeline structure,
  `docs/targets/kibana.md` for Kibana target/native API/YAML artifact behavior,
  `docs/sources/grafana.md` or `docs/sources/datadog.md` for source-specific
  behavior, `docs/contributing/import-paths.md` for public helper/module moves,
  and `docs/testing.md` for verifier/gate changes. If public install/scope
  changes, also update `README.md` and `docs/README.md`. Generated trace docs
  must be updated through their templates/generators.
- Preserve "degrade gracefully" behavior for unsupported translations — do not silently hide semantic gaps.
- Do not commit secrets or generated local artifacts.
- Dashboard migration fixes must be checked against the schema, compiled saved
  object, and uploaded Kibana behavior. Do not infer YAML support from Kibana UI
  controls alone: Lens XY YAML supports one `breakdown`, while multi-breakdown
  arrays are for schemas such as datatable/pie/treemap unless
  `docs/dashboards/schema.json` and the compiler prove otherwise.
- Before claiming migrated dashboards render correctly, validate real artifacts:
  generated YAML, compiled NDJSON/saved object, scoped smoke or direct `_query`,
  and a clean view-mode browser session. Clear stale dashboard edit state before
  trusting browser observations.
- For dashboard-regression work, use the layered verifier gates documented in
  `docs/command-contract.md`: `verifier.live_validate` for runtime ES|QL errors,
  `verifier.dashboards_api` for typed Kibana UI-contract validation,
  `obs-migrate compare` plus `verifier.corpus_gate` for semantic parity,
  `verifier.benchmark_gate` for PM benchmark-history regressions,
  `verifier.scorecard` for the Layer-9 fidelity ratchet, and
  `render_audit_driver` for whether panels actually render in Kibana. Do not rely
  on a single migrated/clean percentage; also watch denominator drops
  (`panels_total`, `dashboards`, `verification_total`) and filtered datasource
  slices.
- The render audit is the only gate that catches Lens accessor / "invalid
  column" / empty-state failures (which ES|QL execution and the schema gate
  miss). It is per-panel and classifies `render_error` (real bug, fail) vs
  `field_gap`/`data_gap`/`unexpected_empty` (data-readiness, warn). A breakdown
  panel that errors because its label is absent from target data is a field/data
  gap, NOT a translator bug — confirm via seeded data before filing a bug.
- Coverage of supported panel/widget types is machine-enforced
  (`tests/core/coverage/`, the panel matrices, and the kitchen-sink canary). Add
  a type → update `core/coverage/supported_types.py` + a matrix cell, or CI fails.
  Refresh `parity-rig/benchmark/fidelity_baseline_*.json` only for intentional
  changes, never to mask a regression.
- When growing benchmark coverage, prefer pinned stratified manifests from
  `verifier.corpus_manifest` (top dashboards + long-tail + datasource quotas +
  bug seeds) over an unpinned "top N today" sample. Use small deterministic PR
  gates and larger/nightly corpus gates.
- Skills live in both `.claude/skills/` and `.cursor/skills/` — edit both copies in lockstep (see the mirroring rule in `AGENTS.md` for the `.claude`↔`.cursor` path-prefix caveat).

## Commit Workflow

Follow `AGENTS.md` commit rules. Key points:
- Commit only when the user explicitly asks.
- Conventional-Commits subject (`feat:`, `fix:`, `docs:`) + blank line + why-focused rationale.
- Never `--no-verify`. Never force-push `main`.
