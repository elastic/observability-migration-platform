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
- Skills live in both `.claude/skills/` and `.cursor/skills/` — edit both copies in lockstep (see the mirroring rule in `AGENTS.md` for the `.claude`↔`.cursor` path-prefix caveat).

## Commit Workflow

Follow `AGENTS.md` commit rules. Key points:
- Commit only when the user explicitly asks.
- Conventional-Commits subject (`feat:`, `fix:`, `docs:`) + blank line + why-focused rationale.
- Never `--no-verify`. Never force-push `main`.
