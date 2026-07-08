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
- **Native PROMQL adaptive resolution parity** — migrated native-PROMQL range
  dashboard panels are emitted with **no baked-in `step=`** (Elastic sizes the
  resolution to the dashboard time range at view time, #272) and a
  `rate()`/`increase()` whose Grafana source used an adaptive window macro
  (`$__rate_interval` / `$__interval`) is emitted **windowless** so its lookback
  tracks the view too (#273). This stays adaptive like Grafana but is not
  bit-for-bit identical: Elastic's windowless form uses a window equal to the
  step, while Grafana's `$__rate_interval` is deliberately a bit wider
  (`max(step + scrape_interval, 4 × scrape_interval)`), so expect a small
  magnitude delta and watch for empty points when the auto-sized window is
  narrower than the scrape interval (a window narrower than the data resolution
  returns no data — Grafana's floor prevents this; the windowless form does
  not). Explicit windows (`rate(x[5m])`) are preserved verbatim. Range functions
  other than `rate`/`increase` (`irate`, `*_over_time`, `delta`, …) still
  collapse an adaptive macro to a fixed `1m` window, since a windowless form for
  them is not confirmed; a coarse scrape combined with a tight `irate` window can
  still yield empty buckets on the seeded/test path (production scrape rates
  avoid this). A `rate()`/`increase()` whose range vector also carries an
  `offset` or `@` modifier (`rate(x[$__rate_interval] offset 5m)`) likewise keeps
  a fixed window, since a windowless form combined with those modifiers is not
  confirmed. Alert-rule migration is unaffected — alerts keep their explicit
  or default `step=`.
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
  EOF-extended segment cannot be reliably attributed from the captured DOM, so
  it is **not** hard-failed (its data-readiness still surfaces as a warn). The
  guard intentionally errs away from false failures.
- **`--agent-browser` is tab-selection only** — `--agent-browser` focuses the
  Kibana tab in a live agent-browser session so the session is not left on a
  stray tab, but DOM capture always uses the headless `--user-data-dir` path
  (which reads HTML, navigates to the exact URL, and exposes CSS-class render
  markers). Serverless render audit therefore needs a logged-in
  `--user-data-dir` profile (a one-time SSO login).
- **`obs-migrate verify` cannot self-detect a fully auth-blocked run** — an
  auth/security/quota error from the cluster classifies as `other` (a warn), not
  a hard fail. If a key lacks ES|QL read access, the gate reports `other` rather
  than failing, so confirm the `ok` count is non-zero before trusting a PASS.
  (A dedicated `blocked` bucket was removed: its heuristics misfired — a real
  ES|QL error at column 429 was read as a quota signal, and a transient quota
  error could mask a real bug in the exit code.)
- **Datadog numeric parity** — `--source-execution` numbers are only meaningful
  when the source (Datadog) and target (Elastic) ingest the **same** telemetry;
  otherwise comparisons are structural-only.

## Scope

- **Field profile** — Grafana migration currently supports the `otel` field
  profile only; Datadog adds source-specific built-ins. ECS fallback is not part
  of this pass.
