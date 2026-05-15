# CLAUDE.md — mig-to-kbn

Claude-specific guidance. For automation/agent rules see `AGENTS.md`. For public docs see `docs/README.md`.

## Upstream Boundary

**`elastic/mig-to-kbn`** is the canonical source for `grafana-migrate`, `datadog-migrate`, PromQL/Datadog translation, and the shared Kibana YAML/compile path.

- Engine fixes and features belong in **Issues/PRs on this repo**, not in downstream forks.
- The vendored copy at `validation/external_assets/dashboard-alert-migration/mig-to-kbn/` is a **snapshot**. Changes there should be bumps via `./scripts/update_mig_to_kbn.sh`, not long-lived forks.

## Project Conventions

- Architecture overview: `docs/architecture.md`
- Canonical CLI commands: `docs/command-contract.md`
- Preserve "degrade gracefully" behavior for unsupported translations — do not silently hide semantic gaps.
- Do not commit secrets or generated local artifacts.

## Commit Workflow

Follow `AGENTS.md` commit rules. Key points:
- Commit only when the user explicitly asks.
- Conventional-Commits subject (`feat:`, `fix:`, `docs:`) + blank line + why-focused rationale.
- Never `--no-verify`. Never force-push `main`.
