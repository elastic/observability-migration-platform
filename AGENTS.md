# AGENTS.md — Observability Migration Platform

This file is for automation and repo-working guidance. Public user and
contributor documentation lives in `README.md`, `docs/README.md`, and the root
governance files.

## Repo Pointers

- Public docs index: `docs/README.md`
- Canonical commands: `docs/command-contract.md`
- Architecture: `docs/architecture.md`

## Repo-Specific Working Rules

- Keep `README.md` short and public-facing.
- Keep `docs/` canonical for narrative and reference docs.
- Keep `examples/` and `infra/` focused on assets plus local landing READMEs.
- Do not duplicate long command walkthroughs outside `docs/command-contract.md`.
- Do not commit secrets or generated local artifacts.
- Preserve the existing "degrade gracefully" behavior for unsupported translations instead of hiding semantic gaps.
