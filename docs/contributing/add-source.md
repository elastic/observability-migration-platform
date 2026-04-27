# Adding a New Source Adapter

This guide explains how to add support for a new observability source while
staying aligned with the current `observability_migration` package layout.

## Module Touch Points

Adding a new source usually involves:

1. **Adapter package**: `observability_migration/adapters/source/<name>/`
2. **Registration**: `@source_registry.register` in `adapter.py`
3. **Unified CLI bootstrap**: import the adapter module in `observability_migration/app/cli.py`
4. **Fixtures**: lightweight fixtures under `tests/fixtures/` and, when useful, reusable sample dashboards under `infra/<name>/dashboards/`
5. **Tests**: focused tests under `tests/` and `tests/e2e/`
6. **Extension catalog**: implement `build_extension_catalog()` and `build_extension_template()` so `obs-migrate extensions --source <name>` can describe how users should extend the adapter
7. **Documentation**: `docs/sources/<name>.md`

You should NOT need to modify the shared Kibana compile/upload helpers unless
the new source needs a genuinely new target-side panel shape.

## Step-by-Step

### 1. Create the adapter package

Recommended shape:

```text
observability_migration/adapters/source/<name>/
  __init__.py
  adapter.py          # SourceAdapter registration
  cli.py              # Optional but recommended dedicated CLI
  extract.py          # File/API extraction
  normalize.py        # Raw source shape cleanup, if needed
  translate.py        # Query/widget translation entry point
  report.py           # Optional source-specific report helpers
```

The exact module split can follow the source domain. For example, the current
Datadog adapter also has `planner.py`, `query_parser.py`, `log_parser.py`, and
`field_map.py`.

### 2. Implement the `SourceAdapter`

```python
from observability_migration.core.interfaces.registries import source_registry
from observability_migration.core.interfaces.source_adapter import SourceAdapter


@source_registry.register
class MySourceAdapter(SourceAdapter):
    name = "mysource"

    @property
    def supported_assets(self):
        return ["dashboards", "panels", "queries"]

    @property
    def supported_input_modes(self):
        return ["files"]

    def validate_credentials(self, config):
        return []

    def extract_dashboards(self, *, input_mode, input_dir=None, config=None):
        ...

    def normalize_dashboard(self, raw, **kwargs):
        ...

    def translate_queries(self, normalized, **kwargs):
        ...
```

For new adapters, also add:

```python
    def build_extension_catalog(self, **kwargs):
        ...

    def build_extension_template(self, **kwargs):
        ...
```

These methods back the shared `obs-migrate extensions --source <name>` command and
should describe both the current extension surfaces users can rely on today and
any planned plugin or rule-pack contracts you expect to stabilize later.

If the adapter exposes declarative extension files, validate them before load
and make sure `build_extension_template()` returns a starter payload that users
can write out through `obs-migrate extensions --source <name> --template-out ...`.

### 3. Wire the unified CLI

The registry is populated by importing adapter modules. After creating the new
adapter, import it from `observability_migration/app/cli.py` so
`obs-migrate migrate --source <name>` can discover it.

### 4. Add fixture data

- Put small, fast unit-test fixtures under `tests/fixtures/`.
- Put larger or demo-quality source exports under `infra/<name>/dashboards/` when they should be reused by scripts or docs.

### 5. Add tests

Add focused coverage for:

- extraction from files and, if supported, API mode
- normalization into the adapter's stable intermediate shape
- query/widget translation
- registry/bootstrap wiring

Match the current repo style: top-level `tests/test_<source>_*.py` files are
fine, and broader integration checks can live under `tests/e2e/`.

### 6. Document the source

Create `docs/sources/<name>.md` covering:

- supported assets and input modes
- main CLI entry points
- query-language mapping
- known limitations and rollout state
