---
name: explain-migration-gaps
description: Use when the user asks "why didn't this panel migrate", "what does not_feasible mean here", "how do I fix the panels that need manual work", "explain the warnings", or "how do I rebuild this in Kibana" — explains WHY panels and widgets did NOT migrate cleanly, in plain language, with step-by-step guidance to rebuild them in Kibana. Read-only; reads migration artifacts already on disk. For an overall coverage summary use report-migration-coverage; to numerically verify the panels that DID migrate use validate-side-by-side.
---

# Explain migration gaps

Goal: for each panel or widget that **did not migrate cleanly**, explain **why** in plain language and give **step-by-step guidance** to rebuild it manually in Kibana. Read-only — read artifacts a completed migrate run already wrote; do not re-run migration or touch any cluster.

## Which panels to explain

Filter `panels[]` in `<output-dir>/dashboards/migration_manifest.json` by `panels[].status`. Non-clean statuses differ by source:

- **Grafana:** `migrated_with_warnings`, `requires_manual`, `not_feasible`
- **Datadog:** `warning`, `requires_manual`, `not_feasible`, `blocked`

Skip panels whose status is clean (`migrated` on Grafana, `ok` on Datadog). When the user names a specific panel, match by `title`, `source_panel_id`, and dashboard title/id in the same manifest entry.

> **Exception — clean panels that fail numeric parity:** a panel can be clean here yet still `FAIL` `obs-migrate compare`. If `validate-side-by-side` routed you here for a clean (`migrated` / `ok`) panel, do **not** skip it — use the **Parity failures (from validate-side-by-side)** section below.

## Inputs (artifact table)

Assume the user **installed the package** (`obs-migrate` on `PATH`); prefix `.venv/bin/` only for a repo checkout. Every artifact below is written by a normal migrate run — no source checkout required.

| What you want | File | Field(s) |
|---|---|---|
| Per-panel status + reasons (both) | `<output-dir>/dashboards/migration_manifest.json` | `panels[].status`, `panels[].reasons` |
| Grafana extra context | same manifest | `panels[].notes`; `panels[].transformation_redesign_tasks[].kibana_alternative`, `.description`; `panels[].review_explanation.suggested_checks` (when present) |
| Datadog extra context | same manifest | `panels[].warnings`, `panels[].semantic_losses` |
| Target suggestions (both) | manifest + verification packets | `panels[].recommended_target`, `panels[].target_candidates`; packet `recommended_target`, `candidate_targets` in `<output-dir>/dashboards/verification_packets.json` → `packets[]` (and inside each `panels[].verification_packet`) |
| Feature-level gaps (Grafana runs) | `<output-dir>/dashboards/feature_gap_report.json` | dashboard- and feature-level gaps not captured per panel |
| Human worklist (optional) | `<output-dir>/dashboards/migration_summary.md` | must-fix list to prioritize which panels to explain first |

## Workflow

1. **Locate the output dir** — the `--output-dir` from the user's migrate run (or ask which run they mean if several exist).
2. **Read the manifest** — open `<output-dir>/dashboards/migration_manifest.json` and scan `panels[]`.
3. **Filter non-clean panels** — keep entries whose `status` is non-clean for the source (Grafana: `migrated_with_warnings`, `requires_manual`, `not_feasible`; Datadog: `warning`, `requires_manual`, `not_feasible`, `blocked`).
4. **State why each panel failed or degraded** — lead with `panels[].reasons`. Add Grafana `panels[].notes` or Datadog `panels[].warnings` / `panels[].semantic_losses` when they add detail the reasons omit.
5. **Map to a Kibana rebuild path** — use Grafana `panels[].transformation_redesign_tasks[].kibana_alternative` and `.description` when present; otherwise use `panels[].recommended_target`, `panels[].target_candidates`, and the matching packet's `recommended_target` / `candidate_targets` in `verification_packets.json`. Cross-check Grafana `feature_gap_report.json` when the gap is feature-level rather than query-level.
6. **Produce guidance matched to `status`** — branch on the panel's status; do not over-promise a tweak path where the engine marked a hard stop:
   - **`migrated_with_warnings` (Grafana) / `warning` (Datadog) or `requires_manual`:** give concrete finish/rebuild steps — target panel type, ES|QL or Lens sketch from the verification packet / `query_ir`, post-rebuild checks (fields, time range, group-by). Ground steps in Grafana `transformation_redesign_tasks[].kibana_alternative` when present, otherwise `recommended_target` / `target_candidates`.
   - **`not_feasible` or `blocked`:** do **not** walk through a step-by-step tweak — explain the redesign constraint (what semantic capability Kibana lacks or must be re-modeled) and why, citing `reasons` (and Datadog `semantic_losses` when relevant). Only mention an alternative target if `recommended_target` or `target_candidates` genuinely offers one; otherwise say a net-new design is required.
7. **Prioritize when many panels need work** — use `migration_summary.md` must-fix ordering; explain the highest-impact panels first unless the user names a specific one.

## Parity failures (from validate-side-by-side)

When **`validate-side-by-side`** routed a panel here, the manifest status may still be clean (`migrated` / `ok`) — the panel translated but **`obs-migrate compare`** did not pass numeric parity. Read `<output-dir>/dashboards/comparison_report.json` (or the sibling `.md` table with the same columns) and locate the row by dashboard + panel title/id.

1. **Read the row** — start with `verdict`, `reason`, and `max_relative_error` (when present). A **`FAIL`** means bucket values diverged beyond the strict threshold; **`STRUCTURAL`** or **`ERROR`** on a panel you expected numeric proof means the oracle could not run or only a shape check ran.
2. **Rule out data/window/step mismatch first** — a **`FAIL` is NOT automatically a translation defect.** Re-run `obs-migrate compare` with `--window-minutes` and `--step-seconds` aligned to the source panel's time range and resolution. When live telemetry is sparse or mismatched, use **`obs-migrate seed-sample-data`** so both sides read the same synthetic data, compare again, then **`obs-migrate remove-sample-data --confirm`** to tear down seeder-owned streams.
3. **Only after mismatch is ruled out**, treat a persistent **`FAIL`** as a real translation defect — explain what the emitted ES|QL computes versus the source PromQL (from the verification packet / manifest query context) and give the same rebuild/redesign path as any other gap: `transformation_redesign_tasks`, `recommended_target`, `target_candidates`, and verification-packet sketches.
4. **`SKIP` and `ERROR` are not passes** — they mean the oracle could not verify the panel (unsupported construct or ES query error). Say explicitly that numeric parity was **not** proven; do not treat them as green.

## Honest limits

- **`obs-migrate migrate` does not populate the richer heuristic `review_explanation`.** Those fields appear only when the migration was run with `grafana-migrate --review-explanations` (or the equivalent flag on a Grafana migrate invocation). Default migrate output still has `reasons`, `notes`, and `transformation_redesign_tasks` on Grafana — use those first.
- **Datadog has no `review_explanation` or `transformation_redesign_tasks` equivalent.** Datadog panels carry `warnings` and `semantic_losses` instead; reason rebuild steps from `reasons`, `warnings`, `semantic_losses`, and `target_candidates`.
- **When rich fields are absent**, derive rebuild guidance from `panels[].reasons` plus `recommended_target` / `target_candidates` and the source query in the verification packet — do not invent a migration path the engine did not suggest.
- **Never invent a feasible path for a genuinely `not_feasible` panel.** Say it needs a redesign and why (from `reasons` and semantic-loss notes). `blocked` on Datadog similarly means the engine could not proceed — treat as full manual rebuild, not a tweak.
- **This skill does not prove panels render correctly** — empty uploaded panels may be missing telemetry, not a translation bug. Overall coverage counts are `report-migration-coverage`; numerical proof for panels that did migrate is `validate-side-by-side`.

## See also

- `report-migration-coverage` skill — shareable coverage summary and manual-effort buckets from `summary` counts.
- `validate-side-by-side` skill — prove migrated panels match source numerically.
- `docs/command-contract.md` — artifact paths and migrate flags for the installed version.
