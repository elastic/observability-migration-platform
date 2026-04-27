# Adding a New Asset Type

This guide explains how to add a new asset type to the platform.

## Module Touch Points

Adding a new asset type requires changes in:

1. **Shared contract**: `observability_migration/core/assets/<asset>.py`
2. **Asset init**: `observability_migration/core/assets/__init__.py`
3. **Source adapters**: Extraction and mapping for each source that supports the asset
4. **Target emitter**: If the asset produces output in the dashboard YAML
5. **Tests**: At each layer

## Step-by-Step

### 1. Define the shared contract

Create `observability_migration/core/assets/<asset>.py`:

```python
from dataclasses import asdict, dataclass, field
from typing import Any
from .status import AssetStatus

@dataclass
class MyAssetIR:
    version: int = 1
    asset_id: str = ""
    name: str = ""
    kind: str = ""

    status: AssetStatus = AssetStatus.MANUAL_REQUIRED
    manual_required: bool = True
    target_candidate: str = ""
    losses: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    source_extension: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        d = asdict(self)
        d["status"] = self.status.value
        return d
```

### 2. Register in the assets package

Add to `observability_migration/core/assets/__init__.py`.

### 3. Update source adapters

For each source that can extract this asset, add extraction and mapping logic
in the adapter. The module layout is flexible — existing adapters place asset
handling directly in their top-level package (e.g. `normalize.py`,
`translate.py`) rather than under an `assets/` subpackage. Follow the pattern
of the adapter you are extending.

### 4. Update the target emitter (if needed)

If the asset produces output in dashboard YAML, update:
- `observability_migration/targets/kibana/emit/`

### 5. Add DashboardIR field

Add the asset list to `DashboardIR` if it's dashboard-scoped.

### 6. Test at each layer

- `tests/core/assets/test_<asset>.py` — contract tests
- Source adapter tests — extraction tests
- Target tests — emission tests (if applicable)
