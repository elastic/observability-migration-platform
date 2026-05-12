# mig-to-kbn panel verifier

A 5-tier verification framework for migrated Grafana → Kibana dashboards. For every panel of a migrated dashboard, the verifier records the exact representation of the panel's query at every stage of the pipeline and surfaces drift between adjacent stages.

## The 5 tiers

| Tier | Source | Purpose |
| --- | --- | --- |
| **T0** | `migration_report.json:panels[*].promql` | the original Grafana panel as authored |
| **T1** | `migration_report.json:panels[*].esql` | what mig-to-kbn emitted |
| **T2** | `<output>/yaml/<dash>.yaml` | the kb-dashboard-cli input |
| **T3** | `<output>/compiled/<dash>/compiled_dashboards.ndjson` | the kb-dashboard-cli output, ready for upload |
| **T4** | `GET /api/saved_objects/dashboard/<id>` (or HAR walker fallback) | what Kibana stores as the saved object |
| **T5** | live `POST /_query` response | what the cluster actually executes |

T0 → T1 is expected to differ (different languages); the verifier only flags drift on `T1=T2`, `T2=T3`, `T3=T4`, `T4=T5`.

## Verdicts

| verdict | meaning |
| --- | --- |
| `PASS` | identical (modulo whitespace + known post-translator splices) across all checked axes |
| `DRIFT` | at least one tier transition mutated the query in a way that wasn't expected |
| `FAIL` | live `_query` returned 4xx/5xx |
| `NOT_FEASIBLE` | translator refused to migrate this panel (e.g. `histogram_quantile`); not a regression |
| `NOT_UPLOADED` | local YAML exists but no compiled NDJSON or cluster saved object |
| `SKIP` | panel had no translator output (likely a markdown / manual panel) |
| `ERROR` | unhandled exception during verification |

## Quick start

### One-time bootstrap (per cluster)

```bash
KIBANA_URL=https://<cluster>.kb.us-central1.gcp.staging.elastic.cloud \
  bash parity-rig/verifier/bootstrap.sh
```

Launches Chrome headed; SAML through once; the script saves the auth state to `~/.agent-browser/state/mig-to-kbn-verifier.json`. From then on every headless verifier run reuses it.

### Run the verifier against a migrated dashboard

```bash
set -a; source serverless_creds.env; set +a

obs-migrate verify-panels \
  --migration-out /tmp/mig-to-kbn-e2e/parity-out-<slug>/dashboards \
  --kibana-url "$KIBANA_ENDPOINT" \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" \
  --dashboard-id <kibana-saved-object-id> \
  --output /tmp/verifier-<slug>.json
```

Produces `verifier-<slug>.json` (machine-readable) and `verifier-<slug>.md` (triage doc) and prints an aggregate summary to stdout.

### Local-only mode (no cluster)

Omit `--kibana-url`/`--es-url`/`--api-key`/`--dashboard-id` to run just T0..T3. Useful for catching translator-emit bugs before upload:

```bash
obs-migrate verify-panels \
  --migration-out /tmp/mig-to-kbn-e2e/parity-out-<slug>/dashboards \
  --output /tmp/verifier-<slug>.json
```

## How drift is classified

The comparator uses a canonical form (stripped + collapsed whitespace) for the pairwise check. Two transforms are known and explicitly suppressed:

- **Composite-legend splice** (T1 → T2): the YAML emitter adds `EVAL legend = CONCAT(...)` plus an extended `KEEP` clause that the translator's bare `migration_report.json:esql` does not have. Working as designed; not flagged.

To add another known transform, edit `_KNOWN_T1_T2_RIGHT_ONLY_PATTERNS` in `compare.py` with a short comment explaining the source of the transform.

## Limitations

- **Elastic Serverless saved-objects API is gated.** When the verifier can't fetch the cluster saved object via `GET /api/saved_objects/dashboard/<id>`, it falls back to using the compiled NDJSON as T4. The browser walker (Workflow E1 in the [debug-uploaded-kibana-dashboard skill](../../.cursor/skills/debug-uploaded-kibana-dashboard/SKILL.md)) is the recommended source for a true T4/T5 capture on Serverless.
- **Lens injects `?_tstart` / `?_tend` parameters at runtime.** The verifier auto-supplies a 1-hour window for T5; if you want a specific time range, edit `_autoparams_for_esql` in `collectors.py`.

## File layout

```
parity-rig/verifier/
├── README.md           — this file
├── __init__.py
├── bootstrap.sh        — one-time agent-browser SAML setup
├── records.py          — PanelRecord dataclass + verdict vocabulary
├── collectors.py       — per-tier collectors (local + cluster)
├── compare.py          — pairwise drift detection + classifier
├── cli.py              — standalone `python -m verifier.cli` entrypoint
├── walker.py           — agent-browser-driven HAR + screenshot walker
├── visual_diff.py      — Grafana ↔ Kibana pixel diff wrapper
└── classifier.py       — rule-based root-cause classifier with LLM hook
```

### Walker

The browser walker uses `agent-browser` to fetch what the saved-objects API can't (live Lens `_query` bodies on Elastic Serverless) and to collect per-panel screenshots + optional React Suspense status. Combine it with the verifier in two steps:

```bash
# 1. Run the base verifier (collects T0..T3, optionally T4/T5 if the saved-objects API is open)
obs-migrate verify-panels --migration-out ... --output /tmp/verifier-<slug>.json

# 2. Run the walker to overlay browser-sourced T4/T5 + screenshots
python -m verifier.walker \
  --kibana-url $KIBANA_ENDPOINT \
  --dashboard-id <kibana-uuid> \
  --output-dir /tmp/walker-<slug>/ \
  --merge /tmp/verifier-<slug>.json
```

The walker is **additive** — it overlays evidence without re-running the comparator, so a PASS verdict from step 1 stays PASS after the merge.

### Visual diff

For Grafana ↔ Kibana pixel comparison of paired screenshots:

```bash
python -m verifier.visual_diff \
  --grafana-dir /var/parity/grafana-shots/ \
  --kibana-dir  /var/parity/kibana-shots/ \
  --output-dir  /var/parity/visual-diffs/ \
  --threshold   0.15 \
  --report      /var/parity/visual-diff.json
```

Panels are paired by title (the only stable identity across Grafana and Kibana). Unpaired panels are surfaced in the report's `unpaired_panels` list. Default threshold `0.15` tolerates Lens vs Grafana font / stroke skew; tighten to `0.05` for chart-area-only diffs.

### Classifier

The classifier reads a verifier JSON, inspects each panel's record, and assigns a root-cause category. Rule-based by default; an LLM hook is available via `classifier.LLM_HOOK = my_callable` for cases where the rules are inconclusive:

```bash
python -m verifier.classifier \
  --verifier-report /tmp/verifier-<slug>.json \
  --output /tmp/classified-<slug>.json
```

Categories: `translator_bug`, `schema_resolution`, `data_gap`, `kibana_cache_stale`, `lens_visual_mismatch`, `feasibility_gap`, `transient_cluster`, `unknown`. Each classification carries a `suggested_action` — usually a one-line lead to the file/function that needs a change.

## End-to-end loop

For a complete loop on a single dashboard:

```bash
SLUG=node-exporter-full
DASH_ID=<kibana-saved-object-uuid>
OUT=/tmp/verifier-$SLUG

# 0. one-time bootstrap (skip if already done for this cluster)
KIBANA_URL=$KIBANA_ENDPOINT bash parity-rig/verifier/bootstrap.sh

# 1. tier comparison
obs-migrate verify-panels \
  --migration-out /tmp/mig-to-kbn-e2e/parity-out-$SLUG/dashboards \
  --kibana-url "$KIBANA_ENDPOINT" --es-url "$ELASTICSEARCH_ENDPOINT" \
  --api-key "$KEY" --dashboard-id "$DASH_ID" \
  --output $OUT.json

# 2. browser-sourced evidence overlay (HAR, screenshots, suspense)
python -m verifier.walker \
  --kibana-url "$KIBANA_ENDPOINT" --dashboard-id "$DASH_ID" \
  --output-dir "$OUT-walker/" --merge "$OUT.json"

# 3. (optional) Grafana ↔ Kibana visual diff
python -m verifier.visual_diff \
  --grafana-dir /var/parity/$SLUG/grafana/ \
  --kibana-dir  "$OUT-walker/screenshots/" \
  --output-dir  "$OUT-vdiff/" \
  --report      "$OUT-vdiff.json"

# 4. classify the failures
python -m verifier.classifier \
  --verifier-report "$OUT.json" \
  --output "$OUT-classified.json"
```

The classifier's Markdown output ends up at `$OUT-classified.md` and surfaces the highest-confidence root cause for each non-PASS panel plus a one-line `suggested_action`.

## Tests

```bash
.venv/bin/python -m pytest tests/test_verifier.py tests/test_verifier_walker.py \
  tests/test_verifier_visual_diff.py tests/test_verifier_classifier.py -q
```

86 tests, ~2 seconds. The full suite (`.venv/bin/python -m pytest tests/ -q --ignore=tests/e2e`) reports **1491 passed**.

## See also

- [`docs/command-contract.md`](../../docs/command-contract.md) — canonical mig-to-kbn CLIs.
- [`parity-rig/RESULTS.md`](../RESULTS.md) — known translator gaps catalogue.
- [`.cursor/skills/debug-uploaded-kibana-dashboard/SKILL.md`](../../.cursor/skills/debug-uploaded-kibana-dashboard/SKILL.md) — interactive panel debugging via Chrome DevTools MCP + agent-browser.
