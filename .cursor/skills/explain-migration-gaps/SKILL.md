---
name: explain-migration-gaps
description: Use when the user asks "why didn't this panel migrate", "what does not_feasible mean here", "how do I fix the panels that need manual work", "explain the warnings", or "how do I rebuild this in Kibana" — explains WHY panels and widgets did NOT migrate cleanly, in plain language, with step-by-step guidance to rebuild them in Kibana. Read-only; reads migration artifacts already on disk. For an overall coverage summary use report-migration-coverage; to numerically verify the panels that DID migrate use validate-side-by-side.
---

# Explain migration gaps

**Audience:** operators of the published `obs-migrate` CLI (PyPI/`uvx`), using public docs and their real source + Elastic/Kibana — not a repo lab harness.

Goal: for each panel or widget that **did not migrate cleanly**, explain **why** in plain language and give **step-by-step guidance** to rebuild it manually in Kibana. Read-only — read artifacts a completed migrate run already wrote; do not re-run migration or touch any cluster.

## Prerequisites (install)

These skills help **operators** of the published CLI (not a repo checkout).
If `obs-migrate` is missing or `doctor` is not **Ready**, follow
`install-obs-migrate` first — that skill owns PyPI/`uvx`/pip, extras, and
Python/`uv` gotchas. Do not invent alternate install commands here.

```bash
uvx --from 'elastic-observability-migration[all]' obs-migrate doctor
# After a persistent install, the same check is: obs-migrate doctor
```

Source/Elastic credentials: `connect-to-o11y-source` (and your env exports).


## Which panels to explain

Filter `panels[]` in `<output-dir>/dashboards/migration_manifest.json` by `panels[].status`. Non-clean statuses differ by source:

- **Grafana:** `migrated_with_warnings`, `requires_manual`, `not_feasible`
- **Datadog:** `warning`, `requires_manual`, `not_feasible`, `blocked`, and often **`skipped`** (structural/group widgets that are not translated as panels)

Skip panels whose status is clean (`migrated` on Grafana, `ok` on Datadog). When the user names a specific panel, match by `title`, `source_panel_id`, and dashboard title/id in the same manifest entry. Prefer the top-level `panels[]` array when present (flat); otherwise walk `dashboards[].panels[]`.

> **Exception — clean panels that fail numeric parity:** a panel can be clean here yet still `FAIL` / `SOURCE_FAIL` / unexpected `SOURCE_DRIFT` from `obs-migrate compare`. If `validate-side-by-side` routed you here for a clean (`migrated` / `ok`) panel, do **not** skip it — use the **Parity failures (from validate-side-by-side)** section below.

## Inputs (artifact table)

| What you want | File | Field(s) |
|---|---|---|
| Per-panel status + reasons (both) | `<output-dir>/dashboards/migration_manifest.json` | `panels[].status`, `panels[].reasons` |
| Grafana extra context | same manifest | `panels[].notes`; `panels[].transformation_redesign_tasks[].kibana_alternative`, `.description`; `panels[].review_explanation.suggested_checks` (when present) |
| Datadog extra context | same manifest | `panels[].warnings`, `panels[].semantic_losses` |
| Target suggestions (both) | manifest + verification packets | `panels[].recommended_target`, `panels[].target_candidates`; packet `recommended_target`, `candidate_targets` in `<output-dir>/dashboards/verification_packets.json` → `packets[]` (and inside each `panels[].verification_packet`) |
| Feature-level gaps (Grafana runs) | `<output-dir>/dashboards/feature_gap_report.json` | dashboard- and feature-level gaps not captured per panel (written by Grafana migrate when present — open the file if it exists) |
| Human worklist (optional) | `<output-dir>/dashboards/migration_summary.md` | must-fix list to prioritize which panels to explain first |

## Workflow

1. **Locate the output dir** — the `--output-dir` from the user's migrate run (or ask which run they mean if several exist).
2. **Read the manifest** — open `<output-dir>/dashboards/migration_manifest.json` and scan `panels[]`.
3. **Filter non-clean panels** — keep entries whose `status` is non-clean for the source (Grafana: `migrated_with_warnings`, `requires_manual`, `not_feasible`; Datadog: `warning`, `requires_manual`, `not_feasible`, `blocked`, `skipped`).
4. **State why each panel failed or degraded** — lead with `panels[].reasons`. Add Grafana `panels[].notes` or Datadog `panels[].warnings` / `panels[].semantic_losses` when they add detail the reasons omit. Before prescribing rebuild, classify the reason against **Known acceptable approximations** below — many `migrated_with_warnings` / Datadog `warning` panels are intentional fidelity trade-offs, not unfinished migrations.
5. **Map to a Kibana rebuild path** — use Grafana `panels[].transformation_redesign_tasks[].kibana_alternative` and `.description` when present; otherwise use `panels[].recommended_target`, `panels[].target_candidates`, and the matching packet's `recommended_target` / `candidate_targets` in `verification_packets.json`. Cross-check Grafana `feature_gap_report.json` when the gap is feature-level rather than query-level.
6. **Produce guidance matched to `status`** — branch on the panel's status; do not over-promise a tweak path where the engine marked a hard stop:
   - **`migrated_with_warnings` (Grafana) / `warning` (Datadog):** first decide if the warning is an **accepted approximation** (see below). If yes, explain the semantic difference in plain language, what still works in Kibana, and when an operator should still rebuild (e.g. they need exact Prometheus quantile interpolation). If the warning is unfinished work (dropped target, partial fusion, redesign task), give concrete finish steps.
   - **`requires_manual`:** give concrete finish/rebuild steps — target panel type, ES|QL or Lens sketch from the verification packet / `query_ir`, post-rebuild checks (fields, time range, group-by). Ground steps in Grafana `transformation_redesign_tasks[].kibana_alternative` when present, otherwise `recommended_target` / `target_candidates`.
   - **`skipped` (Datadog):** usually structural (e.g. group containers) — say it was intentionally not emitted as a Kibana panel, not that translation "failed".
   - **`not_feasible` or `blocked`:** do **not** walk through a step-by-step tweak — explain the redesign constraint (what semantic capability Kibana lacks or must be re-modeled) and why, citing `reasons` (and Datadog `semantic_losses` when relevant). Only mention an alternative target if `recommended_target` or `target_candidates` genuinely offers one; otherwise say a net-new design is required.
7. **Prioritize when many panels need work** — use `migration_summary.md` must-fix ordering; explain the highest-impact panels first unless the user names a specific one.

## Known acceptable approximations (Grafana)

Treat these as **explained warnings**, not automatic rebuild work. Canonical detail: `https://github.com/elastic/observability-migration-platform/blob/main/docs/sources/grafana.md` → Current Boundaries.

| Pattern in `reasons` / query | What the engine did | Tell the operator |
|---|---|---|
| `histogram_quantile` + `PERCENTILE` / "assumed exponential_histogram" | Translates standard `sum(... by (le))` shapes; unknown field type assumes exponential_histogram and warns | Approximate (t-digest). Pin mapping / re-run with `--es-url` field caps for classic histograms (`TO_TDIGEST`). Prefer ES ≥ 9.5 native `histogram_quantile` when available. Still `not_feasible` for bare `*_bucket` without `sum by (le)` or known-wrong types (e.g. `aggregate_metric_double`). |
| `sum(increase\|rate(m_sum)/increase\|rate(m_count))` / "ratio of aggregates" | Rewrites histogram-mean idiom to `sum(m_sum)/sum(m_count)` | Weighted differently than per-series Prometheus means; unrelated `sum(A/B)` stays `not_feasible`. |
| Multi-target fusion / "summary table" / "Unioned BY" / CASE-scoped filters | Fuses compatible XY and summary panels; QoS nested BY unions; Express-style status counters CASE-inline | Platform is single ES\|QL layer — incompatible targets (e.g. Windows vs Linux) keep the largest group and warn. |
| Label-matcher `$var` → `?var` / late-bound `by ($var)` → `??var` | Named-param binding when dashboard templating + live `esql_named_param_binding` probe succeed | Offline single-panel runs may drop `$var` matchers; enable with full dashboard templating and `--es-url`. |
| Native PROMQL downgraded to ES\|QL | `--translation-mode auto` + `--es-url` probe found no native `PROMQL` support | Warnings may come from the ES\|QL fallback path, not a panel redesign need. |

Hard stops that still need redesign (do not soften): subqueries, `bottomk` / `count_values`, `__name__` introspection, generic non-`_sum`/`_count` `sum(A/B)`, unfusable multi-branch `or`/`join`, and documented platform single-query limits.

## Parity failures (from validate-side-by-side)

When **`validate-side-by-side`** routed a panel here, the manifest status may still be clean (`migrated` / `ok`) — the panel translated but **`obs-migrate compare`** did not prove numeric parity. Read the compare report the user pointed at — default `obs-migrate compare --report-out` writes to **CWD** unless overridden (often `<output-dir>/dashboards/comparison_report.json` when that path was passed explicitly). Locate the row by dashboard + panel title/id.

1. **Read the row** — start with `verdict`, `reason`, and `max_relative_error` (when present). A **`FAIL`** means bucket values diverged beyond the strict threshold; **`SOURCE_FAIL`** / **`SOURCE_DRIFT`** come from live source-vs-target packets (`migrate --source-execution --validate`); **`STRUCTURAL`**, **`SKIP`**, or **`ERROR`** on a panel you expected numeric proof means the oracle could not run or only a shape check ran.
2. **Rule out data/window/step mismatch first** — a **`FAIL` / `SOURCE_DRIFT` is NOT automatically a translation defect.** Re-run `obs-migrate compare` with `--window-minutes` and `--step-seconds` aligned to the source panel's time range and resolution. When live telemetry is sparse or mismatched, use:

```bash
obs-migrate seed-sample-data \
  --artifact-dir <output-dir>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY"
# …compare again…
obs-migrate remove-sample-data \
  --artifact-dir <output-dir>/dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" \
  --confirm
```

   (`seed`/`remove` take **`--api-key`**, not `--es-api-key`; defaults to env `KEY`.)
3. **Only after mismatch is ruled out**, treat a persistent **`FAIL`** as a real translation defect — explain what the emitted ES|QL computes versus the source PromQL (from the verification packet / manifest query context) and give the same rebuild/redesign path as any other gap: `transformation_redesign_tasks`, `recommended_target`, `target_candidates`, and verification-packet sketches.
4. **`SKIP` and `ERROR` are not passes** — they mean the oracle could not verify the panel (unsupported construct or ES query error). Say explicitly that numeric parity was **not** proven; do not treat them as green.

## Honest limits

- **`obs-migrate migrate` does not populate `review_explanation`.** Those fields appear when the migration was run with the **`grafana-migrate`** alias and **`--review-explanations`** (not forwarded on `obs-migrate migrate`). Default migrate output still has `reasons`, `notes`, and often `transformation_redesign_tasks` on Grafana — use those first.
- **Datadog has no `review_explanation` or `transformation_redesign_tasks` equivalent.** Datadog panels carry `warnings` and `semantic_losses` instead; reason rebuild steps from `reasons`, `warnings`, `semantic_losses`, and `target_candidates`.
- **When rich fields are absent**, derive rebuild guidance from `panels[].reasons` plus `recommended_target` / `target_candidates` and the source query in the verification packet — do not invent a migration path the engine did not suggest.
- **Never invent a feasible path for a genuinely `not_feasible` panel.** Say it needs a redesign and why (from `reasons` and semantic-loss notes). `blocked` on Datadog similarly means the engine could not proceed — treat as full manual rebuild, not a tweak.
- **This skill does not prove panels render correctly** — empty uploaded panels may be missing telemetry, not a translation bug. Overall coverage counts are `report-migration-coverage`; numerical proof for panels that did migrate is `validate-side-by-side`. For UI render truth (`render_error` vs `field_gap` / `data_gap`), hand off to `debug-uploaded-kibana-dashboard` / render audit (`https://github.com/elastic/observability-migration-platform/blob/main/docs/testing.md`).
- **Do not treat every `migrated_with_warnings` / Datadog `warning` as rebuild work.** Many are accepted approximations (table above); explain them, then ask whether the operator accepts the fidelity trade-off.

## See also

- `install-obs-migrate` — install/doctor when the CLI is missing or not Ready.
- `report-migration-coverage` skill — shareable coverage summary and manual-effort buckets from `summary` counts.
- `validate-side-by-side` skill — prove migrated panels match source numerically.
- `debug-uploaded-kibana-dashboard` skill — classify empty/wrong UI renders after upload.
- `https://github.com/elastic/observability-migration-platform/blob/main/docs/sources/grafana.md` — Current Boundaries (approximations vs hard stops).
- `https://github.com/elastic/observability-migration-platform/blob/main/docs/command-contract.md` — artifact paths and migrate flags for the installed version.
- `https://github.com/elastic/observability-migration-platform/blob/main/docs/testing.md` — render-audit and layered verifier gates.
