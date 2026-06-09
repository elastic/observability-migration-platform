# AGENTS.md — Observability Migration Platform

This file is for automation and repo-working guidance. Public user and
contributor documentation lives in `README.md`, `docs/README.md`, and the root
governance files.

## Naming

These names all refer to this project; do not treat them as separate things:

- **Observability Migration Platform** — the product/repo name.
- **`obs-migrate`** — the installable umbrella CLI (`grafana-migrate` and
  `datadog-migrate` are the per-source entry points).
- **`observability_migration`** — the Python package.
- **`mig-to-kbn`** — the engine/upstream identity (and a repo mirror). See the
  Upstream Boundary note in `CLAUDE.md`.

## Repo Pointers

- Public docs index: `docs/README.md`
- Canonical commands: `docs/command-contract.md`
- Architecture: `docs/architecture.md`
- Contributor setup, verification, and PR rules: `CONTRIBUTING.md`

## Build, Test, Lint

Use the `Makefile` targets (they sync the locked `uv` dev environment first);
`uv` must be on `PATH`. Run `make help` to list targets.

```bash
make sync       # sync the dev virtualenv from uv.lock
make test       # unit tests (excludes e2e)
make lint       # ruff + source-header check
make typecheck  # targeted mypy checks
```

`CONTRIBUTING.md` documents the equivalent direct `.venv/bin/...` invocations
and the license/SBOM refresh. Prefer `make` so the environment matches CI.

## Repo-Specific Working Rules

- Keep `README.md` short and public-facing.
- Keep `docs/` canonical for narrative and reference docs.
- Keep `examples/` and `infra/` focused on assets plus local landing READMEs.
- Do not duplicate long command walkthroughs outside `docs/command-contract.md`.
- Do not commit secrets or generated local artifacts.
- Preserve the existing "degrade gracefully" behavior for unsupported translations instead of hiding semantic gaps.
- Skills are mirrored in `.claude/skills/` and `.cursor/skills/` (one `SKILL.md` per skill in each tree). When you add or edit a skill, update **both** copies. They are byte-identical **except** self-referential path prefixes — `~/.claude/...` in the `.claude` copy vs `~/.cursor/...` in the `.cursor` copy — so don't blindly `cp` a skill that links to other skills; rewrite those prefixes for the destination tree.

## Commit And Push Workflow (For Agents)

Follow these rules when committing on the user's behalf, unless the user
explicitly says otherwise.

- Only commit when the user explicitly asks ("commit", "push", etc.).
- Use HEREDOC commit messages (`git commit -m "$(cat <<'EOF' ... EOF)"`)
  with a Conventional-Commits-style subject (e.g. `docs:`, `docs(readme):`,
  `fix:`, `feat:`) followed by a blank line and a short rationale focused
  on the "why".
- Always run pre-commit hooks (do not pass `--no-verify`). If a hook
  modifies files, re-stage and commit again as a NEW commit; never
  `--amend` a pushed commit.
- Before pushing to `main`, fetch and inspect divergence
  (`git log HEAD..origin/main` and `git log origin/main..HEAD`). If `main`
  has diverged, prefer resetting local `main` to `origin/main` and
  cherry-picking the new commits over `git pull --rebase` when local
  commits duplicate remote ones.
- Never force-push `main`. Never push commits that include local-only
  duplicates of remote commits.
- The remote requires PRs for `main`; pushing directly may bypass the
  rule. Only do so when the user has explicitly approved the direct push
  for the current change.
- After pushing, confirm with `git status -sb` and report the new commit
  SHA and the remote ref it advanced.
