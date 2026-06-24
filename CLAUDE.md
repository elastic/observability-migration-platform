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
  `obs-migrate compare` plus `verifier.corpus_gate` for semantic parity, and
  `verifier.benchmark_gate` for PM benchmark-history regressions. Do not rely
  on a single migrated/clean percentage; also watch denominator drops
  (`panels_total`, `dashboards`, `verification_total`) and filtered datasource
  slices.
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
