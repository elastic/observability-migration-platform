# Phase 5 — Fresh end-to-end verification

**Date:** 2026-08-07  
**Canonical run:** `/tmp/node-exporter-phase5-20260807-030538`  
**Dashboard SO:** `obs-migrate-node-exporter-full` @ Kibana `http://localhost:5602`  
**Target:** ES `http://localhost:9201`, `metrics-node.prometheus-default`,
`prometheus_native`

This is the single run that both **translates** and **uploads/smokes** the
curated 1860 pack after Phases 1–4 and the section audits.

## Commands

```bash
OUT=/tmp/node-exporter-phase5-YYYYMMDD-HHMMSS
mkdir -p "$OUT/input"
cp /path/to/node-exporter-full.json "$OUT/input/"
obs-migrate migrate \
  --source grafana --input-mode files --input-dir "$OUT/input" \
  --output-dir "$OUT" --assets dashboards \
  --es-url http://localhost:9201 \
  --esql-index metrics-node.prometheus-default \
  --data-view metrics-node.prometheus-default \
  --field-profile prometheus_native \
  --kibana-url http://localhost:5602 \
  --upload --ensure-data-views --smoke
```

## Results

| Gate | Result |
|------|--------|
| Renderable panels | 117 |
| Migrated / warnings / manual / NF | **117 / 0 / 0 / 0** |
| Verification packets | **117 Green / 0 Yellow / 0 Red** |
| Upload | 1/1 |
| Smoke runtime errors | **0** |
| Smoke empty panels | **0** |
| Layout issues | **0** |
| Field discovery | 696 fields, `prometheus_native` |

## Spot checks (post-upload `_query`)

With controls `job=node_exporter`, `node=node:9100`, range ≈15m:

| Panel | Live |
|-------|------|
| Pressure | OK (metric tiles / curated) |
| CPU Busy | OK |
| Memory Stack | OK (incl. Hardware Corrupted after lab seed) |
| TCP Errors | OK (8 series incl. TCPRcvQDrop) |
| TCP Connections | OK (incl. MaxConn) |
| CPU Frequency Scaling | OK (per-CPU + Max/Min) |
| Network Traffic by Packets | OK |

Native `PROMQL` panel count in this run: **0** (multi-target native path still
disabled — Phase 3). Queries are ES|QL `TS` / curated / `FROM` (CPU Cores).

## Related evidence

- Section audits: [`node-exporter-1860-section-audits/`](node-exporter-1860-section-audits/README.md)
- Phase 3 decision: [`node-exporter-1860-phase3-native-promql.md`](node-exporter-1860-phase3-native-promql.md)
- Workplan: [`node-exporter-1860-curation-workplan.md`](node-exporter-1860-curation-workplan.md)

## Honest remaining gaps (not Phase 5 failures)

1. **Phase 3 blocked** — no multi-target native PROMQL until `label_replace`.
2. **Lab seeding** for three optional metrics is validation-only; stock scrapes
   without those collectors still omit series via `live_optional_metrics`
   (curated TCP Errors currently *requires* TCPRcvQDrop once included in pack).
3. **Root FS `/oldroot`** adaptation is host-specific (section 01).
4. **`$node`/`$job` chained controls** still cannot mirror Grafana
   `label_values` dependency.
5. Browser render/interaction audits were not re-run as a full nightly gate in
   this Phase 5 pass; smoke covers ES|QL execution emptiness/errors only.
