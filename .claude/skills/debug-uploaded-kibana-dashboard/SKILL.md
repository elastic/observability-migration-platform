---
name: debug-uploaded-kibana-dashboard
description: Use when the user reports a panel rendering empty / "No results found" / "Migration Required" / wrong-shape values after running parity-rig/upload-all.sh or obs-migrate upload, or hands over a Kibana dashboard URL and asks "why is this panel broken" — diagnoses a Kibana dashboard that mig-to-kbn uploaded to the Serverless cluster by driving Chrome via the chrome-devtools MCP server, capturing the per-panel ES|QL the Kibana UI is actually running, the /_query network response, browser console errors, and a screenshot of the failing panel.
---

# Debug an uploaded Kibana dashboard

Pairs the generic `chrome-devtools-debugging` skill with mig-to-kbn's specific workflow: open the uploaded dashboard in Chrome, capture exactly what Kibana's Lens is sending to `/_query`, classify the failure mode against the mig-to-kbn migration report, and feed that back to the translator pipeline if it turns out to be a real bug.

**Prerequisites:** `chrome-devtools` MCP server configured with `--autoConnect` (see [the generic skill](~/.claude/skills/chrome-devtools-debugging/setup-autoconnect.md)). Chrome 144+ open, signed into the target Kibana, on the dashboard in question.

**Faster bulk loops:** For per-panel walks across all panels of a dashboard, structural snapshot diffs, or pixel diffs against a Grafana baseline, the `agent-browser` CLI is the right tool. See [Workflow E](#workflow-e--per-panel-walker-via-agent-browser) below and the [agent-browser companion reference](~/.claude/skills/chrome-devtools-debugging/agent-browser.md). The Chrome DevTools MCP and `agent-browser` coexist; pick whichever has the better primitive for the task.

## Decision tree

Always start by asking three questions in this order:

1. **Does the panel actually render in Kibana right now?** → If yes (just slow / wrong-looking), go to *Workflow A* (capture the actual query).
2. **Does it render "No results found"?** → *Workflow B* (data is missing or filter mismatch).
3. **Does it render "Migration Required" markdown?** → *Workflow C* (translator marked it `not_feasible`).

These three patterns cover ~95% of real reports. The workflow steps below all assume the user already has Chrome open on the dashboard.

## Workflow A — "panel shows wrong values"

The Kibana Lens panel is rendering, but the numbers, labels, series count, or shape don't match what the user expected.

1. **`take_snapshot`** to map the panel. Find the panel's container element.
2. **`list_network_requests`** with `resourceTypes: ["xhr","fetch"]` (Lens dispatches `/api/.../_query` over fetch). Filter the list for entries whose URL ends in `_query`.
3. For each `_query` request: **`get_network_request`** with that `reqid` and `requestFilePath: "/tmp/<panel-slug>.network-request"`, `responseFilePath: "/tmp/<panel-slug>.network-response"`. The request body contains the **exact ES|QL** Lens sent (this is the one source of truth — not the YAML, which is pre-Lens).
4. Compare the request body's `query` field with the YAML at `/tmp/mig-to-kbn-e2e/parity-out-<slug>/dashboards/yaml/<dash>.yaml`. If they differ, Kibana is doing its own column-rewriting (it sometimes adds `BUCKET(@timestamp,...)` or aliases) — note that as a downstream Kibana transform, not a translator bug.
5. Re-run the exact query via the cluster directly to confirm Kibana isn't lying:

   ```bash
   # Point OBS_MIGRATE_CREDS at your own creds file (exports KEY,
   # ELASTICSEARCH_ENDPOINT, KIBANA_ENDPOINT); defaults to ./serverless_creds.env.
   set -a; source "${OBS_MIGRATE_CREDS:-./serverless_creds.env}"; set +a
   curl -s -H "Authorization: ApiKey $KEY" -H 'Content-Type: application/json' \
     "$ELASTICSEARCH_ENDPOINT/_query" \
     -d @/tmp/<panel-slug>.network-request | jq .
   ```

6. If the cluster's response is correct but Lens displays something wrong → Kibana / Lens visual-mapping issue. Inspect with `take_screenshot` and document. Don't blame the translator.
7. If the cluster's response is wrong → real translator/data bug. Move to Workflow B/C as appropriate.

## Workflow B — "No results found"

The panel renders but has no rows. Three causes in order of frequency:

### B1: Filter doesn't match any docs (~70% of cases)

Common in NEF / Kubernetes panels that filter on `mountpoint == "/"`, `device != "rootfs"`, `cluster == "$cluster"`, etc. The metric exists; the filter excludes everything.

1. Capture the query as in Workflow A step 3.
2. Run the query **without the filter** to confirm data exists:
   ```bash
   # Strip the WHERE that's filtering on the dimension you suspect, then re-run.
   ```
3. Inspect the actual distinct values for the filter field:
   ```
   FROM <index> | WHERE <metric> IS NOT NULL | STATS n = COUNT(*) BY <field> | SORT n DESC | LIMIT 20
   ```
4. **Translator fix vs rig fix:** if the dashboard's filter value is a sensible production assumption (`mountpoint="/"`, `fstype="ext4"`) but the rig's data doesn't have it, fix the producer in `parity-rig/producer/app.py` (synthesize a row matching the filter) rather than mutating the translator.

### B2: Field name resolved wrong

The translator's `SchemaResolver` rewrote a label to an OTEL name (`instance` → `service.instance.id`) but the data is using the original name (or vice versa).

1. Pull the cluster's actual fields with `_field_caps`:
   ```bash
   curl -s -H "Authorization: ApiKey $KEY" \
     "$ELASTICSEARCH_ENDPOINT/<index>/_field_caps?fields=instance,service.instance.id" | jq .
   ```
2. Compare against what the YAML query filters on.
3. Fix is in `observability_migration/adapters/source/grafana/schema.py` (`SchemaResolver`) — usually a missing OTEL mapping or a label that should be source-faithful.

### B3: Aggregation collapses everything to null

Seen on multi-target TS queries where each `IRATE` returns null on the rows for *other* metrics. The collapse `LAST(field, time_bucket)` then picks a null-only row. This was fixed in commit `90c3f23` — collapse now uses `MAX(field)` for null safety. If you see this pattern recurring on a *new* shape, replicate the fix in `_collapse_summary_ts_query` (`observability_migration/adapters/source/grafana/promql.py`).

## Workflow C — "Migration Required" markdown placeholder

The translator emitted markdown rather than ES|QL because the source PromQL hit a known unsupported pattern. The markdown body lists the reason.

1. **`take_snapshot`** then `take_screenshot` of just the panel for the user.
2. Read the markdown body via the snapshot or `evaluate_script` if you need the raw text:
   ```
   evaluate_script:
     function: (el) => el.innerText
     args: ["<uid of the markdown panel>"]
   ```
3. Look up the source PromQL in the migration report:
   ```
   /tmp/mig-to-kbn-e2e/parity-out-<slug>/dashboards/migration_report.json
   ```
   Find the panel by title and read `promql` + `notes`.
4. Classify against the documented gaps (from `parity-rig/RESULTS.md`):
   - `histogram_quantile`, `topk`, `bottomk`, `label_replace`, `vector()`, `predict_linear` — out of scope, documented.
   - `A or B`, `A unless B` between different metrics — refused by design (commit `a6f4932`).
   - `A / B` between distinct vectors with no explicit `on()` — should fall through to ES|QL translation (commit `ea49d15`); if it didn't, the gate is missing a case.
   - "Divergent filters/groupings cannot be translated safely" — by design when operands have incompatible filter or BY shapes.
5. If the pattern is a *new* class that mig-to-kbn could plausibly translate, file an issue and link to the captured panel screenshot. Otherwise this is working as intended; report it back to the user as expected.

## Workflow D — runtime error popup (red toast in Kibana)

Kibana shows a red toast like `[parent] Data too large, ...` or `Found 1 problem: line 2:54: Unknown column [...]`.

1. **`list_console_messages` with `types: ["error"]`** — Kibana logs the full error to the console with stack trace.
2. **`get_console_message`** to drill into the message; the response body contains the literal ES error (much richer than the toast text).
3. Classify:
   - `Data too large` / `circuit_breaker` → transient cluster heap pressure. Not a translator bug. Re-run, narrow the time range, or report to the cluster operator.
   - `Unknown column [X]` where X *is* in the query → data gap. If X is a synthetic metric, add it to the producer; if it's a real metric the user has, the index they're pointing the data view at is wrong.
   - `Unknown column [Y]` where Y is *not* in the query → translator emitted an alias-mismatched filter (the canonical symptom of the SchemaResolver bug class).
   - `function [rate] requires a counter metric` → ingest typed the field as gauge. Translator fix in commit `bd68f61` handles this for ES|QL emission; if it's hitting from a native-PROMQL panel, the gate in `_translate_panel_native_promql` is missing the case.
   - `verification_exception` with `cannot infer label set` / `binary operator` → Elastic PROMQL preview can't evaluate this expression shape. `can_use_native_promql` let it through; narrow that gate to reject the shape so the panel degrades to ES|QL translation instead of emitting a PROMQL command the cluster rejects.

## Workflow E — per-panel walker via `agent-browser`

When the question is broader than a single panel — "compare every NEF panel against Grafana", "capture every `/_query` Lens sent for this dashboard", "find which Suspense boundaries are stuck" — switch from the Chrome DevTools MCP to `agent-browser`. It runs as a CLI from a shell or a Python harness; the daemon keeps the browser warm across the loop.

**One-time setup (per cluster):**

```bash
# from repo root
KIBANA_URL=https://<cluster>.kb.us-central1.gcp.staging.elastic.cloud \
  bash parity-rig/verifier/bootstrap.sh
```

The bootstrap script launches Chrome headed, waits for you to SAML through once, then snapshots the auth state to `~/.agent-browser/state/mig-to-kbn-verifier.json`. From then on every verifier loop reuses it without SAML.

### E1: capture every `/_query` Lens dispatches during a dashboard load

```bash
STATE=$HOME/.agent-browser/state/mig-to-kbn-verifier.json
HAR=/tmp/<slug>.har

agent-browser close --all
agent-browser --state "$STATE" network har start
agent-browser open "$KIBANA_URL/app/dashboards#/view/$DASHBOARD_ID"
agent-browser wait --load networkidle
agent-browser wait 5000   # let Lens dispatch its queries
agent-browser network har stop "$HAR"

# extract every _query
jq '.log.entries[] | select(.request.url | test("/_query$")) | {url:.request.url, body:.request.postData.text, status:.response.status}' "$HAR"
```

This is the deterministic equivalent of "page through `list_network_requests` and `get_network_request` for each `_query`" — the HAR file is the single source of truth for every query Lens sent during that load, including ones that fired after `networkidle`.

### E2: pixel-diff a Kibana panel against the Grafana panel

```bash
PANEL="Memory Basic"
# Grafana baseline
agent-browser --state "$STATE" open "http://localhost:3000/d/$GRAFANA_UID?viewPanel=$GRAFANA_PANEL_ID"
agent-browser wait 4000
agent-browser screenshot --selector ".panel-container" /tmp/grafana-$PANEL.png

# Kibana candidate (same time range, same data, same panel)
agent-browser open "$KIBANA_URL/app/dashboards#/view/$KIBANA_DASHBOARD_ID"
agent-browser wait 4000
agent-browser screenshot --selector "[data-test-subj='dashboardPanel-$KIBANA_PANEL_ID']" /tmp/kibana-$PANEL.png

# pixel diff
agent-browser diff screenshot --baseline /tmp/grafana-$PANEL.png /tmp/kibana-$PANEL.png \
  -o /tmp/diff-$PANEL.png -t 0.15
```

Threshold `0.15` is tolerant of Grafana/Kibana's different chrome (legend layout, axis fonts). Tighten when you only care about the chart area.

### E3: which Lens panels are still stuck in a React Suspense fallback

```bash
agent-browser close --all
agent-browser --state "$STATE" --enable react-devtools open \
  "$KIBANA_URL/app/dashboards#/view/$KIBANA_DASHBOARD_ID"
agent-browser wait 8000
agent-browser react suspense --only-dynamic --json | jq .
```

A panel that is *visually* empty but the `_query` returns rows is almost always a Suspense / hydration problem. This points at the exact boundary.

### E4: batch — walk every panel and collect (snapshot, screenshot, `_query` body)

For a comprehensive sweep, use `batch --json` so the daemon processes the whole iteration in one IPC. Build the JSON from a Python loop that knows your panel list:

```bash
python3 -c '
import json, subprocess
panels = [{"id": "p1", "selector": "[data-test-subj=\"dashboardPanel-p1\"]"}, ...]
cmds = [["open", f"{KIBANA_URL}/app/dashboards#/view/{DASH}"], ["wait", "6000"]]
for p in panels:
    cmds.append(["screenshot", "--selector", p["selector"], f"/tmp/{p['id']}.png"])
    cmds.append(["snapshot", "-i", "-s", p["selector"], "--json"])
subprocess.run(["agent-browser", "--state", STATE, "batch", "--json", "--bail"], input=json.dumps(cmds).encode())
'
```

This is the foundation of `parity-rig/verifier/` (the framework in flight). The Python wrapper joins HAR entries with snapshots and screenshots into a single per-panel record.



For any panel, the agent has *two* sources of truth besides Kibana:

1. **The migration report**: `/tmp/mig-to-kbn-e2e/parity-out-<slug>/dashboards/migration_report.json`. Find the panel by `title`. Useful fields: `status`, `feasibility`, `promql`, `esql`, `reasons`, `notes`, `query_ir.source_expression`, `query_ir.target_query`. The `esql` here is what mig-to-kbn emitted; the actual Kibana query (from Workflow A step 3) may differ.
2. **The compiled NDJSON**: `/tmp/mig-to-kbn-e2e/parity-out-<slug>/dashboards/compiled/.../compiled_dashboards.ndjson`. The dashboard saved-object IDs live here. Useful when the agent needs to construct deep-link Kibana URLs (`<kibana>/app/dashboards#/view/<id>`).

## After diagnosis

If a real translator gap is identified:

1. Reproduce in the unit test suite with a red→green test (`tests/test_migrate.py`).
2. Implement the smallest change that makes the new test pass.
3. Run `.venv/bin/python -m pytest tests/ -q` to confirm no regression.
4. Re-migrate just the affected dashboard:
   ```bash
   bash parity-rig/upload-all.sh   # or run the single-dashboard equivalent manually
   ```
5. Re-run validation:
   ```bash
   .venv/bin/grafana-validate-uploaded \
     --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" \
     --es-url "$ELASTICSEARCH_ENDPOINT" --es-api-key "$KEY" \
     --dashboard-title "<dashboard title>" \
     --output /tmp/upload-validate-<slug>.json
   ```
6. Open the dashboard back up in Chrome and re-run the failing panel's workflow to confirm the fix is visible end-to-end.

## Things not to do

- Don't run `evaluate_script` against the user's logged-in Kibana session in a way that reads credentials, API keys, or other org secrets. Tool calls execute in the page's security context.
- Don't loop on `wait_for` past a 30 s timeout — re-snapshot and look at the actual page state.
- Don't blame the translator without first reproducing the failing query via `curl` against `/_query`. Kibana sometimes mutates queries client-side; the YAML alone isn't enough evidence.
- Don't paste full network response bodies into the chat — save with `responseFilePath` and quote a 1–3 sentence summary plus the path.

## See also

- [`~/.claude/skills/chrome-devtools-debugging/SKILL.md`](~/.claude/skills/chrome-devtools-debugging/SKILL.md) — the generic foundation this skill builds on.
- [`~/.claude/skills/chrome-devtools-debugging/agent-browser.md`](~/.claude/skills/chrome-devtools-debugging/agent-browser.md) — `agent-browser` CLI reference for the bulk / diff / HAR primitives used by Workflow E.
- `parity-rig/verifier/bootstrap.sh` — one-time `agent-browser` SAML + persistent state setup.
- `parity-rig/RESULTS.md` — known translator gaps and what each commit fixed.
- `docs/command-contract.md` — canonical mig-to-kbn CLIs (`obs-migrate`, `grafana-validate-uploaded`).
