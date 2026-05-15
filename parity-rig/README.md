# Parity Rig

End-to-end correctness harness for mig-to-kbn. Feeds the **same** simulated
Prometheus metrics into Grafana (via Prometheus) and Kibana (via Elastic's
native `/_prometheus/api/v1/write` endpoint), then runs each migrated
panel's query against both stores and diffs the results.

## Architecture

```
                       ┌─────────────────┐
                       │ Producer (Node) │ ── /metrics ──┐
                       └─────────────────┘                │
                                                            │
                                                            ▼
                                                  ┌─────────────────┐
                                                  │  Prometheus     │
                                                  │  (scrape 15s)   │
                                                  └──────┬──────────┘
                                                            │ remote_write
                                                            ▼
            ┌──────────────────────────────────────────────────────┐
            │   Elasticsearch /_prometheus/api/v1/write             │
            │   → metrics-{dataset}.prometheus-{namespace}          │
            │     (Elastic ships an index template that makes the   │
            │      data shape directly queryable via ES|QL PROMQL.) │
            └──────────────────────────────────────────────────────┘
                              ▲                        ▲
                              │                        │
                ┌─────────────┴────────┐   ┌───────────┴────────────┐
                │   Grafana (PromQL)   │   │   Kibana (ES|QL PROMQL │
                │   reads Prometheus   │   │   command, ESQL)       │
                └──────────────────────┘   └────────────────────────┘
                              ▲                        ▲
                              └────────┬───────────────┘
                                       │
                              ┌────────┴───────────────┐
                              │   Parity harness        │
                              │   diff(prom, kibana)    │
                              └─────────────────────────┘
```

Both stores receive **the same numeric source** (the producer's `/metrics`
endpoint). Migration is run with `--native-promql` so the translated
dashboard prefers the ES|QL `PROMQL` source command (byte-for-byte PromQL
identity) and falls back to ES|QL native for constructs the PROMQL command
doesn't yet support (`or`, `histogram_quantile`, etc.).

## Components

- **producer/** — deterministic Prometheus exposition that mimics an
  express-prometheus-middleware instrumented Node.js app. Counters
  increment at a fixed wall-clock-driven rate so re-runs are
  reproducible. Also exposes a second endpoint `/metrics-k8s` that
  emits a self-consistent synthetic slice of kube-state-metrics and
  cAdvisor (nodes, namespaces, pods, containers, restarts, container
  cpu/memory/network), so dashboards that target the Kubernetes
  exporters can be parity-checked without standing up a real cluster.
- **node-exporter/** — real `prom/node-exporter` v1.8.2 running with
  the standard host collectors (cpu, meminfo, diskstats, filesystem,
  loadavg, netdev, netstat, stat, vmstat, hwmon). The Node Exporter
  Full dashboards (id 1860 and our internal fixture) hit this directly.
- **prometheus/** — config + entrypoint that materializes the
  `remote_write` URL with the `ELASTICSEARCH_ENDPOINT` and `KEY` from
  the env at container start. Scrapes 4 jobs: express-app, node-exporter,
  kube-state-metrics (via `/metrics-k8s`), and prometheus itself.
- **grafana/** — pre-provisioned datasource. Dashboards are stamped
  onto the rig at deploy time by `scripts/prepare-dashboards.sh`
  (which pulls the canonical fixtures from `../infra/grafana/dashboards/`
  and optionally also downloads dashboard 1860 from grafana.com).
- **harness/parity.py** — runs each migrated panel's PromQL against both
  Prometheus and Elasticsearch's ES|QL `PROMQL` command, diffs the
  results.

## Bringing it up

```bash
cd parity-rig
set -a; source ../serverless_creds.env; set +a
bash scripts/prepare-dashboards.sh --1860   # stamp fixture dashboards + fetch 1860
docker compose up -d --build
# Wait ~5 minutes so rate windows have enough history.
```

Ports:

- producer: <http://localhost:27300/metrics>
- prometheus: <http://localhost:29090>
- grafana: <http://localhost:23000> (anonymous Admin, dashboards auto-loaded)

## Running parity across every dashboard

The convenience driver migrates each fixture (plus the canonical 1860
from grafana.com) against the rig's data stream and runs the parity
harness against each:

```bash
bash run-all-parity.sh
```

It writes `reports/parity-all/<slug>/parity-report.json` per dashboard
and `reports/parity-all/_combined.json` for the aggregate counts. See
[`RESULTS.md`](./RESULTS.md) for an interpretation of what each
verdict bucket means and which (few) translator gaps remain.

## Migrating the dashboard

The rig writes data to `metrics-express.prometheus-parity` (set by the
`url=…/metrics/express/parity/api/v1/write` path).

```bash
set -a; source serverless_creds.env; set +a
.venv/bin/python -m observability_migration.adapters.source.grafana.cli \
  --input-dir /tmp/mig-to-kbn-e2e/input-express \
  --output-dir /tmp/mig-to-kbn-e2e/parity-express-native \
  --assets dashboards \
  --es-url "$ELASTICSEARCH_ENDPOINT" \
  --es-api-key "$KEY" \
  --data-view metrics-express.prometheus-parity \
  --esql-index metrics-express.prometheus-parity \
  --native-promql \
  --upload --kibana-url "$KIBANA_ENDPOINT" --kibana-api-key "$KEY" \
  --ensure-data-views
```

## Running the harness

```bash
PROM_URL=http://localhost:29090 \
ESQL_INDEX=metrics-express.prometheus-parity \
REPORT_PATH=/tmp/mig-to-kbn-e2e/parity-express-native/dashboards/migration_report.json \
PARITY_WINDOW_MINUTES=8 \
PARITY_STEP_SECONDS=60 \
  python harness/parity.py
```

Output: `reports/parity-report.json`. Verdicts (per panel):

| Verdict | Meaning |
|---|---|
| `STRICT_PASS` | Every comparable bucket within 1 %. |
| `FUZZY_PASS`  | Within 5 %. |
| `SHAPE_PASS`  | Series label sets overlap but numerics diverge > 5 %. |
| `FAIL_NO_OVERLAP` | Disjoint series sets — usually a real translation gap or source-data quirk. |
| `ERROR`       | One side raised an error. |
| `SKIP`        | Translator returned `not_feasible` or the PromQL uses a construct (`topk`, `histogram_quantile`, etc.) without a comparable ES|QL form. |

Each verdict is tagged with its mode:

- `PROMQL_IDENTITY` — both sides ran the exact same PromQL string. The
  Elastic side used the ES|QL `PROMQL` source command.
- `ESQL_FALLBACK` — Elastic side ran a translated ES|QL query (the
  translator's per-panel fallback when PromQL isn't supported by the
  ES|QL `PROMQL` preview, e.g. `or`).

## Latest run (express-prometheus-middleware)

| Verdict | Count | Notes |
|---|---:|---|
| `STRICT_PASS` | 13 | All `PROMQL_IDENTITY`, `rel_err_max = 0.000`. |
| `SHAPE_PASS`  | 1  | `Request rate` — boundary-bucket artifact. |
| `FAIL_NO_OVERLAP` | 6 | 3 are dashboard fidelity (`le="1"` vs `"1.0"`), 1 is `or` fallback, 1 is binary-op limitation in Elastic's PROMQL preview, 1 is divergent-filter arithmetic. |
| `SKIP` | 4 | `topk`, `histogram_quantile`, `vector`, `label_replace`. |

## Known external limitations (not translator bugs)

- **Elastic PROMQL command preview (9.4+)** doesn't yet support
  `or`/`and`/`unless`, `on()/group_left()`, `histogram_quantile`, or
  binary operations between two instant vectors (e.g. `A / B` without
  explicit matching). Dashboards using these constructs fall back to
  ES|QL translation.
- **Calendar-aligned buckets**: the PROMQL command snaps buckets to
  fixed calendar boundaries; PromQL's `query_range` aligns to the
  query start time. Boundary buckets are dropped from the comparison.
- **`le` label canonicalization**: real Prometheus rewrites `le="1"` to
  `le="1.0"`. Dashboards hard-coding `le="1"` render empty on a real
  Prometheus too.

## Tearing down

```bash
docker compose down
```
