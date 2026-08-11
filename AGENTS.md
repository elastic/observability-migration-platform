# AGENTS.md — Observability Migration Platform

This file is for automation and repo-working guidance. Public user and
contributor documentation lives in `README.md`, `docs/README.md`, and the root
governance files.

## Naming

These names all refer to this project; do not treat them as separate things:

- **Observability Migration Platform** — the product and GitHub repo name
  (`elastic/observability-migration-platform`).
- **`obs-migrate`** — the installable umbrella CLI (`grafana-migrate` and
  `datadog-migrate` are per-source compatibility entry points).
- **`observability_migration`** — the Python package.

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

- **Operator-first feature design.** Design and document features for the
  person who only has the installable CLI (`obs-migrate` /
  `grafana-migrate` / `datadog-migrate`), public docs, and their real source
  + Elastic/Kibana environment. A feature is not "done" if the happy path
  depends on eng-only helper scripts, ad-hoc Python one-offs, env-gated
  prototypes, repo harness folders, or `seed-sample-data` / other lab
  tooling. Those may exist for CI and local investigation, but they must
  not be the documented operator journey, a required step in user-facing
  skills/docs, or the only way to make the feature work. Ask: "If a user
  never clones this repo and never runs anything under `scripts/` or a
  dated `*_test_*/` harness, can they still succeed with the CLI + docs
  alone?" If not, redesign the product surface (flags, rule packs,
  artifacts, messages) until they can.
- Keep `README.md` short and public-facing.
- Keep `docs/` canonical for narrative and reference docs.
- Keep `examples/` and `infra/` focused on assets plus local landing READMEs.
- Do not duplicate long command walkthroughs outside `docs/command-contract.md`.
- When a change affects operator-visible behavior, update the matching docs in
  the same PR. Use this checklist:
  - CLI flags, command shapes, env vars, upload/smoke behavior:
    `docs/command-contract.md`.
  - Shared asset/IR/result contracts: `docs/architecture/asset-model.md`.
  - Package maps or cross-source pipeline structure: `docs/architecture.md` and
    `docs/pipeline-trace.tpl.md` (then regenerate `docs/pipeline-trace.md`).
  - Kibana target behavior, native API mapping, review artifacts, upload:
    `docs/targets/kibana.md`.
  - Grafana- or Datadog-specific extraction, translation, validation, or upload
    behavior: `docs/sources/grafana.md` or `docs/sources/datadog.md`.
  - Public install/scope pointers: `README.md` and `docs/README.md`.
  - Importable public helpers/modules: `docs/contributing/import-paths.md`.
  - Verification gates or verifier commands: `docs/testing.md` and the relevant
    command examples in `docs/contributing/dev-commands.md`.
  - Repo-checkout-only commands (`scripts/*`, verifier gates, pytest):
    `docs/contributing/dev-commands.md`, never `docs/command-contract.md`.
  - Generated trace docs: edit the matching `*.tpl.md` or generator and run
    `python scripts/audit_pipeline.py --update-docs`.
- Do not commit secrets or generated local artifacts.
- Preserve the existing "degrade gracefully" behavior for unsupported translations instead of hiding semantic gaps.
- For dashboard migration fixes, prove the emitted dict shape matches
  `docs/dashboards/schema.json` and the uploaded Kibana saved object. Do not
  infer schema support from Kibana UI affordances alone. In particular, a Lens XY
  panel (`line`, `area`, `bar`) has a single `breakdown`; use a synthetic
  composite field when multiple source labels must define one series identity.
  Multi-breakdown arrays are for datatable/pie/treemap-style schemas unless the
  schema and the mapper prove otherwise.
- For user-facing dashboard correctness claims, validate against real migrated
  artifacts and uploaded dashboards: inspect `native/*.native.json` and
  `ir/*.ir.json`, the uploaded saved object,
  run scoped smoke/direct `_query` checks, and browser-check a clean view-mode
  Kibana session. Clear stale dashboard edit state before trusting browser output.
- For dashboard-regression fixes, run the layered verifier gates documented in
  `docs/contributing/dev-commands.md`: `verifier.live_validate` (runtime ES|QL),
  `verifier.dashboards_api` (typed Kibana dashboard contract), `obs-migrate
  compare` + `verifier.corpus_gate` (semantic parity), `verifier.benchmark_gate`
  (PM benchmark-history regression guard), `verifier.scorecard` (Layer-9 fidelity
  ratchet vs committed baseline), and `render_audit_driver` (does each panel
  actually render in Kibana). Track both percentages and denominators
  (`dashboards`, `panels_total`, `verification_total`), including
  datasource-filtered Grafana slices when the benchmark UI is filtered.
- Offline coverage is machine-enforced: when you add or change a supported
  panel/widget type, update `observability_migration/core/coverage/supported_types.py`
  and add a matrix cell (`tests/test_panel_matrix.py` / `tests/test_datadog_panel_matrix.py`),
  or `tests/core/coverage/test_supported_types.py` fails. After an *intentional*
  fidelity change, refresh the committed baselines
  (`parity-rig/benchmark/fidelity_baseline_{grafana,datadog}.json`, procedure in
  `tests/e2e/test_fidelity_ratchet.py`); never refresh to silence an unexpected
  regression.
- The render audit is the only gate that proves panels actually *render* (Lens
  accessor / "invalid column" / empty-state failures that ES|QL execution and the
  schema gate miss). It classifies per-panel: `render_error` (a real bug, fail)
  vs `field_gap`/`data_gap`/`unexpected_empty` (data-readiness, warn). Both gap
  classes are evidence-based against `--es-url` field caps, taken from the index
  each panel's own ES|QL `FROM` names (`--es-index` is only the fallback for
  panels that name none); missing evidence keeps the stricter class and records
  why in `detail` rather than guessing the lenient one. Serverless
  needs a one-time SSO login into a persistent Chrome profile; CI uses the
  local no-SSO stack (`parity-rig/docker-compose.render-audit.yml` +
  `scripts/run_render_audit_local.sh`). A missing target field that breaks a
  breakdown panel is a field/data gap, not a translator bug — seed the
  dashboard's contract (without `--no-recreate`) before trusting an empty render.
  Control interactivity is a separate gate: the interaction audit
  (`scripts/run_interaction_audit_local.sh`, nightly
  `dashboard-interaction-audit.yml`) requires control selection plus
  affected-query evidence; a green default-state render does not prove filters
  work.
- Grow benchmark coverage through pinned, stratified manifests generated by
  `verifier.corpus_manifest` (top dashboards, long-tail slices, datasource
  quotas, and explicit bug seeds). Avoid unpinned "top N today" corpora as merge
  gates because marketplace changes can look like code regressions.
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
