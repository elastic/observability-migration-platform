# Grafana metric_map prefix lint

**Date:** 2026-09-03
**Status:** Implemented
**Scope:** Fail Grafana migrate when a `metric_map` target already carries the active field-profile prefix, so the resolver cannot emit `metrics.metrics.*` / `prometheus.metrics.prometheus.metrics.*` / `prometheus.prometheus.*`.

---

## Problem

On Grafana, a `metric_map` target is a **bare logical metric name**. The active `--field-profile` then namespaces it (`metrics.*` under `prometheus_native`, `prometheus.metrics.*` under `prometheus_metrics`, `prometheus.<name>.{value,counter,rate}` under `prometheus_remote_write`).

Operators (and a mistaken pack row) who write the physical field as the target get a silent double-prefix offline:

```text
metric_map:
  redis_uptime_in_seconds: metrics.uptime_seconds

# --field-profile prometheus_native emits:
metrics.metrics.uptime_seconds
```

Docs already say not to do this (`docs/command-contract.md`). There is no load-time check. Datadog is the opposite contract (targets are verbatim full fields) and must not share this lint.

The bug is **profile-relative double-prefixing**, not “a prefix appears in YAML”. Under `otel` or `passthrough`, `metrics.uptime_seconds` is emitted as-is and can be correct.

## Goals

- Fail closed on the Grafana migrate path when the **namespacing profile** would prepend a prefix the target already has.
- Check the **merged** Grafana `metric_map` (curated pack + `--rules-file` + `--metric-map-file`), including `variants[].target`.
- Print every bad source→target in one operator error, then exit `1` before any panel is translated.
- Suggest the logical name to write instead.
- Keep the shared `--metric-map-file` parser Datadog-safe.

## Non-goals

- Changing emit or namespacing rules.
- Datadog `metric_map` target lint (full field names are required there).
- Unifying Grafana vs Datadog target semantics.
- Detecting “wrong family” maps (for example `k8s.pod.cpu.usage` under `prometheus_native` → `metrics.k8s.pod.cpu.usage`).
- Failing `otel` / `passthrough` namespaced targets.
- A new CLI flag.
- Rejecting remote_write targets that only end in `.counter` / `.rate` / `.value` without a `prometheus.` prefix (v1 is prefix-only).

## Approach

One pure helper on the **Grafana adapter**, called after pack + profile bind. Not in `observability_migration/core/metric_mapping/files.py`.

---

## Detection rule

Walk every target in `rule_pack.metric_map`: the root entry `target` and each `variants[].target`. Skip empty targets (scaffold placeholders already resolve as unapplied gaps).

Use the same namespacing profile `SchemaResolver` uses to prefix metrics (`_effective_schema_profile()` / `_namespacing_schema_profile()`), not the raw `--field-profile` string when that string is `auto`.

| Namespacing profile | Fail if target starts with | Suggested logical name |
|---|---|---|
| `prometheus_native` | `metrics.` | strip `metrics.` |
| `prometheus_metrics` | `prometheus.metrics.` | strip `prometheus.metrics.` |
| `prometheus_remote_write` | `prometheus.` | strip `prometheus.` and a trailing `.counter` / `.rate` / `.value` if present |
| `otel`, `passthrough`, unresolved `auto` (otel-like) | never | — |

Match is case-sensitive prefix on the stripped target string. Do not treat `metrics_uptime` as `metrics.`.

`auto`: lint only after live caps have resolved the layout (`resolve_auto_profile`). CLI already rejects `auto` without `--es-url`. If auto falls back to otel, do not fail namespaced targets.

## Helper

Place next to Grafana schema/CLI, for example:

`observability_migration/adapters/source/grafana/metric_map_lint.py`

```python
def grafana_metric_map_prefix_errors(
    metric_map: Mapping[str, Any],
    namespacing_profile: str | None,
) -> list[str]:
    """Return operator-facing error lines; empty means OK."""
```

`namespacing_profile` is `prometheus_native` | `prometheus_metrics` | `prometheus_remote_write` | `None` (and any other value is a no-op).

Each error line (stable copy; tests match this shape):

```text
Grafana metric_map target 'metrics.uptime_seconds' for source 'redis_uptime_in_seconds' already uses the prometheus_native prefix; the profile would emit 'metrics.metrics.uptime_seconds'. Use the logical name 'uptime_seconds' instead.
```

For remote_write, the “would emit” example is the default gauge leaf (`prometheus.<target>.value` after double-prefix), so the operator sees the doubled `prometheus.` even if a later `prefer=counter` would have used `.counter`.

Collect **all** errors; do not stop at the first key.

## Call sites

The helper is the only policy. `SchemaResolver` raises `ValueError(joined_error_lines)` so tests and every Grafana emit path fail closed. CLI never shows a traceback for this case: catch that `ValueError`, print each line as `ERROR: …` on stderr, `sys.exit(1)`.

1. **`SchemaResolver.__init__`**, when `--field-profile` / `field_profile=` is already a named Prometheus plan (`prometheus_native`, `prometheus_metrics`, `prometheus_remote_write`). Validates `rule_pack.metric_map`. This catches `--metric-map-file` and `--rules-file` as soon as the CLI builds the resolver (`_build_dashboard_schema_resolver`).

2. **`SchemaResolver.resolve_auto_profile`**, when the resolved layout is a named Prometheus plan. Unresolved or otel-fallback `auto` does not lint.

3. **`SchemaResolver.copy_with_pack`**. Today this clones via `__new__` and skips `__init__`, so a curated pack that adds a namespaced target would otherwise skip the constructor check. Re-run the helper on the new pack's `metric_map` against the clone's namespacing profile.

Grafana CLI `main` does not need a third copy of the loop. It must wrap resolver construction, post-discovery `resolve_auto_profile` (already invoked via `_discover_fields`), and `copy_with_pack` so the operator sees `ERROR:` lines instead of a stack trace. Named Prometheus profiles can fail before dashboard extraction; that is intended.

Do **not** call this from `load_metric_map_files` / `parse_metric_map_entry`. Datadog files and mixed source-neutral YAML may legally use `prometheus.metrics.foo` as a target.

## Operator surface

No new flag. Existing migrate / preflight Grafana dashboard runs grow a hard failure.

`--print-rule-catalog` does not construct a profile-bound resolver today; leave it alone (catalog dump is not an emit path).

Alerts-only `--assets alerts` does not translate Grafana PromQL metric_map into namespaced ES|QL the same way; this lint is dashboard-resolver scoped. Do not add it to the alerts-only early return.

## Tests

Always-on, no cluster.

**Helper** (`tests/adapters/source/grafana/test_metric_map_lint.py` or `tests/core/metric_mapping/test_grafana_metric_map_prefix_lint.py`):

- `prometheus_native` + `metrics.foo` → one error suggesting `foo`; would-emit `metrics.metrics.foo`.
- `prometheus_metrics` + `prometheus.metrics.foo` → error suggesting `foo`.
- `prometheus_remote_write` + `prometheus.foo.counter` → error suggesting `foo`.
- `prometheus_native` + bare `foo` → no error.
- `otel` / `passthrough` / `namespacing_profile=None` + `metrics.foo` → no error.
- Variant-only namespaced target is reported with the parent source key.
- Two bad keys → two error lines.
- Empty / scaffold target skipped.

**Curated packs:** every shipped pack `query.metric_map` target (and variants) is clean under all three named Prometheus profiles (`tests/test_field_profile_portability.py` or the helper test file). Packs already author bare names; this is a ratchet.

**CLI:** Grafana migrate with `--field-profile prometheus_native` and `--metric-map-file` containing `source: metrics.already_prefixed` exits `1` and prints `Use the logical name 'already_prefixed'`. Input may be an empty or one-file dashboard dir; translation must not start. Follow the existing `_load_configured_rule_pack` / `main(argv=...)` patterns in `tests/core/metric_mapping/test_cli_metric_map_file.py`.

**Resolver:** `SchemaResolver(pack, field_profile="prometheus_native")` with a namespaced target raises `ValueError`; `copy_with_pack` with a namespaced pack map raises too.

## Docs

In the same change:

- `docs/command-contract.md` — the paragraph “Do not include the profile prefix in a Grafana target” becomes fail-closed for the three Prometheus profiles, with the logical-name hint.
- `docs/sources/grafana.md` — one sentence next to “metric_map targets are bare logical metric names”: Grafana migrate exits non-zero if a target already has the active profile prefix.

No `docs/command-contract.md` flag table change (no new flag).

## Rollout

Single PR. No emit baseline refresh. No feature flag. Shipped packs are expected to pass unchanged.

## Success

A Grafana operator who writes `metrics.uptime_seconds` under `prometheus_native` never uploads a dashboard that queries `metrics.metrics.uptime_seconds`. The same YAML target under `--field-profile otel` still migrates. Datadog `--metric-map-file` with `prometheus.metrics.cpu` still loads.
