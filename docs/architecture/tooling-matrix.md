# Tooling Matrix

This document records where new authoring and parser tools help most in
`observability_migration`, and where plain Python should remain the default.

## Current Best Fit

| Area | Preferred tool | Status | Why |
|---|---|---|---|
| Declarative extension inputs | YAML + `Pydantic` | Active | Human-editable files with strict validation for Grafana rule packs and Datadog field profiles |
| Environment-specific starter configs | `CUE` | Starter examples added | Good for composing per-target overlays before exporting YAML |
| Runtime translation logic | Python registries | Active | The rule registries are executable, testable, and traceable today |
| User-authored imperative logic | `Starlark` | Later | Safer than raw Python when a stable plugin contract exists, but not needed yet |
| Query generation | Typed Python builders / AST helpers | Active | Safer than string templating for ES|QL and Lens output |
| Datadog DSL parsing | `Lark` | Active for log boolean parsing | Better long-term fit than regex-heavy manual parsing when grammar complexity grows |
| Regex-heavy safety checks | Python `re` with narrow validated inputs | Active | Enough for curated rule-pack patterns today |
| Parser hardening | `Hypothesis` | Active | Finds edge-case crashes without filling the suite with low-value fixtures |

## What Landed

- Grafana rule packs now validate through `Pydantic` before merge/load.
- Datadog field-profile YAML now validates through `Pydantic` before use.
- `obs-migrate extensions` now supports:
  - `--template-only`
  - `--template-out`
- Property-based fuzz tests now cover the Datadog metric and log parsers.
- Property-based fuzz tests now cover Datadog metric, log, and formula parsers.
- Datadog log-query boolean composition now runs through a `Lark` grammar as the primary parser path, with token-level recovery before AST translation.
- Starter `CUE` examples live under `examples/cue/`.

## Authoring Flow

Use YAML for the runtime contract because that is what the CLIs and examples load.
Use `CUE` when you want reusable overlays or environment composition, then export
back to YAML before invoking the migration tools.

Examples:

```bash
cue export examples/cue/datadog-field-profile.cue -e profile --out yaml > custom-profile.yaml
cue export examples/cue/grafana-rule-pack.cue -e rule_pack --out yaml > custom-rule-pack.yaml

.venv/bin/obs-migrate extensions --source datadog --format yaml --template-out custom-profile.yaml
.venv/bin/obs-migrate extensions --source grafana --format yaml --template-out custom-rule-pack.yaml
```

## Why Not Ragel

`Ragel` is powerful for scanner generation, but it is not a strong fit for this
repo right now:

- the codebase is Python-first
- the parser maintenance burden matters more than scanner throughput
- the current risk is grammar clarity and correctness, not low-level lexer speed

If the Datadog parsers ever move into a native Rust component, revisiting tools
like `logos`, `pest`, `nom`, or even `Ragel`-style lexer generation can make
sense there. For the current repo, `Lark` is the better next parser step.

## Status

Datadog log-query boolean composition already runs through a `Lark` grammar as
the primary parser path, with token-level recovery before AST translation. The
existing Hypothesis fuzz corpus covers the parser surface. No further migration
step is needed for `log_parser.py`.
