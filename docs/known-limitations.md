# Known limitations

Documented, accepted limitations of the migration engine and its verification
gates. None are correctness regressions; each degrades **gracefully** (a clear
warning, a `not_feasible` "Migration Required" placeholder, or a conservatively
scoped gate) rather than emitting wrong data silently. Track these in release
notes so operators know what to check by hand.

## Translation

- **`metric * on(instance,job) group_left(labels) <info_metric>{filters}` join
  idiom** — the common `*_info` label-enrichment join (e.g. RabbitMQ
  `rabbitmq_identity_info`, kube-state `*_info`) is marked `not_feasible`
  ("Aggregating over a PromQL vector-matching join requires manual redesign").
  Panels using it ship a Migration Required placeholder and must be rebuilt in
  Kibana. High-value future work, not a bug.
- **Offline counter inference** — when migrating **without** `--es-url`, a
  counter whose name carries no recognised suffix (`_total`, `_seconds_total`,
  …) and isn't proven by live field-caps may be typed as a gauge. The
  source-function heuristic now casts `rate()/irate()/increase()`-wrapped fields
  to `TO_DOUBLE` regardless, but a bare counter reference with an un-inferrable
  name can still mistype offline. The live `--es-url` path resolves this from
  field caps; prefer it for production migrations.
- **Native PROMQL `$__interval`** — the native-PROMQL cleaner resolves
  `$__interval` to a fixed `1m`. Fine for typical scrape rates; a very coarse
  scrape interval combined with a tight `irate` window can yield empty buckets
  on the seeded/test path (production scrape rates avoid this).
- **`topk()` / `bottomk()`** — `topk` is approximated as a **latest-bucket**
  ES|QL top-N (per-series latest value, then ranked); `bottomk` per-series
  selection requires manual redesign.

## Verification gates

- **Parity comparator series-key alignment** — when the native PROMQL oracle
  returns labels as a raw `_timeseries` JSON blob and the translated query
  breaks them into GROK'd columns, the comparator can't always key-align the two
  representations (`compared_points = 0`, `max_relative_error = 0.0`). These are
  **alignment artifacts, not numeric mismatches** — no values disagreed. Treat
  `FAIL` rows with `compared_points = 0` as "not numerically compared", not as
  drift.
- **Render audit — trailing untitled panel** — the per-panel render classifier
  attributes error markers to *titled* panels (field gaps → warn; real render
  errors → fail) and flags markers in the unsegmented/prefix region. A trailing
  **untitled** broken panel whose marker is absorbed into the previous panel's
  EOF-extended segment cannot be reliably attributed from the a11y snapshot, so
  it is **not** hard-failed (its data-readiness still surfaces as a warn). The
  guard intentionally errs away from false failures.
- **`--agent-browser` vs headless** — `--agent-browser` captures the
  accessibility snapshot from the logged-in agent-browser session (no
  `--user-data-dir`); the headless-Chrome path uses `--user-data-dir` with a
  logged-in profile. Serverless render audit requires one of these (a one-time
  SSO login).
- **Datadog numeric parity** — `--source-execution` numbers are only meaningful
  when the source (Datadog) and target (Elastic) ingest the **same** telemetry;
  otherwise comparisons are structural-only.

## Scope

- **Field profile** — Grafana migration currently supports the `otel` field
  profile only; Datadog adds source-specific built-ins. ECS fallback is not part
  of this pass.
