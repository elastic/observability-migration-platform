# obs-migrate panel verifier

A 5-tier verification framework for migrated Grafana → Kibana dashboards. For every panel of a migrated dashboard, the verifier records the exact representation of the panel's query at every stage of the pipeline and surfaces drift between adjacent stages.

## The 5 tiers

| Tier | Source | Purpose |
| --- | --- | --- |
| **T0** | `migration_report.json:panels[*].promql` (else `query_ir.source_expression`) | the original source panel as authored |
| **T1** | `migration_report.json:panels[*].esql` (Datadog: `esql_query`) | what obs-migrate emitted |
| **T2** | `<output>/ir/<dash>.ir.json` | the migration's semantic `DashboardIR` export, as emitted (`visual.presentation.config.query`) |
| **T3** | `<output>/compiled/<dash>/compiled_dashboards.ndjson` | the kb-dashboard-cli output, ready for upload |
| **T4** | `GET /api/saved_objects/dashboard/<id>` (or HAR walker fallback) | what Kibana stores as the saved object |
| **T5** | live `POST /_query` response | what the cluster actually executes |

T0 → T1 is expected to differ (different languages); the verifier only flags drift on `T1=T2`, `T2=T3`, `T3=T4`, `T4=T5`.

T2 used to read `<output>/yaml/<dash>.yaml`. It now reads the IR export, which
is the artifact the YAML was *derived* from (`DashboardIR.to_yaml_dict`), so
`T1=T2` still measures the same thing: post-translator emitter transforms
(composite-legend splice, synthetic gauge bounds) that
`migration_report.json:esql` does not carry. The tier field in the JSON report
is `tiers.t2_ir_esql`; `tiers.t2_yaml_esql` is still accepted when reading
reports written before the move.

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

Launches Chrome headed; SAML through once; the script saves the auth state to `~/.agent-browser/state/obs-migrate-verifier.json`. From then on every headless verifier run reuses it.

### Run the verifier against a migrated dashboard

```bash
set -a; source serverless_creds.env; set +a

obs-migrate verify-panels \
  --migration-out /tmp/obs-migrate-e2e/parity-out-<slug>/dashboards \
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
  --migration-out /tmp/obs-migrate-e2e/parity-out-<slug>/dashboards \
  --output /tmp/verifier-<slug>.json
```

## How drift is classified

The comparator uses a canonical form (stripped + collapsed whitespace) for the pairwise check. Two transforms are known and explicitly suppressed:

- **Composite-legend splice** (T1 → T2): the panel emitter adds `EVAL legend = CONCAT(...)` plus an extended `KEEP` clause that the translator's bare `migration_report.json:esql` does not have, before the query lands in the IR export. Working as designed; not flagged.

To add another known transform, edit `_KNOWN_T1_T2_RIGHT_ONLY_PATTERNS` in `compare.py` with a short comment explaining the source of the transform.

## Multi-dashboard output directories

`--migration-out` is normally a single dashboard's output directory, but
`grafana-migrate --input-dir` / `datadog-migrate --input-dir` write every
dashboard of a run into one `ir/` (and one `compiled/`), and pointing the
verifier at that is supported.

The local tiers are therefore joined **per dashboard** (on the report's
dashboard `uid`, else its `title`), not by panel title alone. Panel titles
repeat across dashboards constantly — `Error Logs`, `CPU Usage`, `Uptime` —
and a title-only join hands one dashboard's panel a *different* dashboard's
query. That does not just lose data, it invents findings: on the in-repo
15-dashboard Datadog corpus both T1=T2 "drift" findings were the Kafka
dashboard's panels being compared against Redis's and RabbitMQ's queries.

When a record's dashboard cannot be matched to any artifact (e.g. an output
set where several dashboards share one title and carry no uid), the tier is
reported as **unavailable** — empty, with a note on the record — rather than
filled from a neighbouring dashboard. A single-dashboard artifact set is
still joined without needing a key match, since there is nothing to confuse
it with.

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
  --migration-out /tmp/obs-migrate-e2e/parity-out-$SLUG/dashboards \
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

- [`docs/contributing/dev-commands.md`](../../docs/contributing/dev-commands.md) — runnable verifier gate commands.
- [`docs/command-contract.md`](../../docs/command-contract.md) — canonical obs-migrate CLIs.
- [`.cursor/skills/debug-uploaded-kibana-dashboard/SKILL.md`](../../.cursor/skills/debug-uploaded-kibana-dashboard/SKILL.md) — interactive panel debugging via Chrome DevTools MCP + agent-browser.
