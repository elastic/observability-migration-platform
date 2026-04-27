# Alert Support Reporting

For the directory-level examples inventory, see `examples/README.md`.

This directory holds curated Grafana and Datadog alert/monitor example suites.
The generated support standings derived from real tool output are written under
`examples/alerting/generated/` as local artifacts and are intentionally ignored
by git.

## Source Of Truth

Do not hand-maintain the support matrix in this README.

Regenerate the current standings with:

```bash
.venv/bin/python scripts/generate_alert_support_report.py
```

Verify that every emitted alert payload uploads to Kibana disabled by default:

```bash
set -a && source serverless_creds.env && set +a
.venv/bin/python scripts/verify_alert_rule_uploads.py
```

Audit existing migrated Kibana rules and optionally disable the enabled subset:

```bash
set -a && source serverless_creds.env && set +a
.venv/bin/python scripts/audit_migrated_rules.py
# .venv/bin/python scripts/audit_migrated_rules.py --disable-enabled
```

Notes:
- Audit-only mode exits non-zero when any migrated rules are still enabled.
- `--disable-enabled` exits non-zero only if one or more disable attempts fail.

Generated outputs land in the ignored local artifact directory:

- `examples/alerting/generated/alert_support_standings.md`
- `examples/alerting/generated/alert_support_standings.json`
- `examples/alerting/generated/grafana/alerts/alert_comparison_results.json`
- `examples/alerting/generated/datadog/alerts/monitor_migration_results.json`
- `examples/alerting/generated/datadog/alerts/monitor_comparison_results.json`

Artifact notes:

- Grafana unified alert comparison rows include `target.review_gates`, which
  show exactly which strict-subset checks passed or failed before a rule could
  be promoted from `draft_requires_review` to `automated`. The
  `no_data_only_blocks_strict_automation` gate highlights the common case where
  source-faithful query migration succeeded and only exact Grafana `NoData`
  parity still blocks auto-promotion.
- Datadog manual-only families now preserve explicit `payload_status_reason`
  / `blocked_reasons` values so policy-manual monitor families are
  distinguishable from translation failures.

## Example Suites

- Grafana file-based examples: `examples/alerting/grafana`
- Datadog monitor examples: `examples/alerting/monitors/datadog_monitors.json`
- The Datadog dashboard placeholder used for file-mode reporting is synthesized
  by `scripts/generate_alert_support_report.py`; it is not a tracked source file.
- Direct Datadog file-mode monitor runs use `--assets alerts` with monitor JSON
  under `<input-dir>/monitors/`.

## Purpose

The generated standings document should answer:

- Which alert families are currently `automated`
- Which are only `draft_requires_review`
- Which are `manual_required`
- Which concrete example queries prove each current boundary

When the pipeline improves, regenerate the report and the document will show the
new standing automatically.
