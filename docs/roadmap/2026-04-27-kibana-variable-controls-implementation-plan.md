# Kibana Variable Controls (Phase B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace today's inert classic dashboard controls for Grafana query variables and Datadog template variables with Kibana ES|QL variable controls (`?value` / `MV_CONTAINS(?value, ...)`), wired via a feasibility classifier and protected by a runtime verifier and a live-Kibana smoke gate.

**Architecture:** Mid-pipeline classifier emits a `VariableBindingMap` per dashboard at the boundary between adapter `normalize` and per-panel translation. Existing matcher-converters consult the map and emit `WHERE field == ?varname` (single) or `WHERE MV_CONTAINS(?varname, field)` (multi) for accepted variables. A post-translation verifier downgrades any binding that violates a hard correctness invariant and the panel falls back to today's lossy behavior. The classifier wiring is activated by a single final commit so revert is one commit.

**Tech Stack:** Python 3.11+, pytest, hypothesis, ruff, kb-dashboard-cli (compile, lint), Kibana 9.1.0 baseline (9.3.0 only when an accepted multi-value variable is present).

**Spec:** [`docs/roadmap/2026-04-27-kibana-variable-controls-design.md`](./2026-04-27-kibana-variable-controls-design.md).

---

## File structure

### New files

| Path | Responsibility |
|---|---|
| `observability_migration/core/variable_classifier.py` | Closed reason enum, `AcceptedBinding` / `RejectedBinding` dataclasses, `VariableBindingMap` alias, `compute_min_kibana_version`, `build_options_query`, the env-var emergency switch, and the two adapter-specific classifier functions (`classify_grafana_variables`, `classify_datadog_variables`). |
| `observability_migration/core/variable_control_verifier.py` | List of pure invariant checks, `verify_bindings(records, binding_map) -> VariableBindingMap` returning a possibly-downgraded binding map. |
| `tests/test_variable_classifier.py` | One test per acceptance rule and reason code; version-floor function tests. |
| `tests/test_variable_control_verifier.py` | One negative test per invariant + idempotency test. |
| `tests/test_variable_control_emit.py` | YAML emission snapshot tests for both control shapes. |
| `tests/test_variable_controls_integration.py` | Hermetic per-real-dashboard integration tests over `infra/grafana/dashboards/*.json` and `infra/datadog/dashboards/*.json`. |
| `tests/e2e/test_variable_controls_smoke.py` | Live-Kibana smoke (4 fixtures × 5 stages). |
| `tests/fixtures/variable_controls/grafana_single_value.json` | Smoke fixture. |
| `tests/fixtures/variable_controls/grafana_multi_value.json` | Smoke fixture. |
| `tests/fixtures/variable_controls/datadog_single_value.json` | Smoke fixture. |
| `tests/fixtures/variable_controls/datadog_multi_value.json` | Smoke fixture. |
| `tests/fixtures/regression/grafana_corpus_phase_b.json` | Regression baseline snapshot. |
| `.github/workflows/live-kibana-smoke.yml` | CI job for smoke layer. |

### Modified files

| Path | Reason |
|---|---|
| `observability_migration/adapters/source/grafana/promql.py` | `_matcher_to_esql`, `_selector_filters`, `_frag_filters` accept and consult an optional `binding_map`. |
| `observability_migration/adapters/source/grafana/panels.py` | `translate_variables` accepts `binding_map`, emits `ESQL*Control` for accepted vars, classic for rejected; passes `binding_map` to PromQL helpers. |
| `observability_migration/adapters/source/grafana/translate.py` | Build `binding_map` once per dashboard and thread through translation. |
| `observability_migration/adapters/source/datadog/translate.py` | `_tag_filter_to_esql` accepts and consults `binding_map`. |
| `observability_migration/adapters/source/datadog/generate.py` | `_build_controls_from_template_vars` accepts `binding_map`, emits ESQL controls for accepted vars; `generate_dashboard_yaml` builds binding map and computes per-dashboard `minimum_kibana_version`. |
| `observability_migration/core/reporting/report.py` | `save_detailed_report` adds `variables` and `panel_parameterizations` blocks. |
| `observability_migration/targets/kibana/serverless.py` | New `_assert_min_kibana_version(client, required)` pre-upload guard. |
| `docs/sources/grafana-trace.tpl.md`, `docs/sources/datadog-trace.tpl.md` | Aggregator switches from string-match to structured warning ID. |
| `docs/architecture.md` | New pipeline step; new modules. |
| `docs/targets/kibana.md` | Document `OBS_MIGRATION_DISABLE_VARIABLE_CONTROLS`. |
| `pyproject.toml` | Register `live_kibana` pytest mark. |

---

## Task list

### Task 1: Closed reason enum and binding types

**Files:**
- Create: `observability_migration/core/variable_classifier.py`
- Create: `tests/test_variable_classifier.py`

- [ ] **Step 1: Write the failing test for binding dataclasses and reason enum**

```python
# tests/test_variable_classifier.py
from observability_migration.core import variable_classifier as vc


def test_accepted_binding_is_frozen():
    binding = vc.AcceptedBinding(
        field="service.instance.id", multi=False, options_query="FROM x"
    )
    try:
        binding.field = "other"  # type: ignore[misc]
    except Exception as exc:
        assert isinstance(exc, (AttributeError, TypeError))
    else:
        raise AssertionError("AcceptedBinding must be frozen")


def test_rejected_binding_requires_known_reason():
    binding = vc.RejectedBinding(reason="unsupported_variable_type")
    assert binding.reason in vc.REJECT_REASONS


def test_rejected_binding_unknown_reason_is_caught_at_construction():
    import pytest
    with pytest.raises(ValueError):
        vc.RejectedBinding(reason="not_a_real_reason")
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `.venv/bin/pytest tests/test_variable_classifier.py -v`
Expected: `ImportError` on `variable_classifier`.

- [ ] **Step 3: Implement the module**

```python
# observability_migration/core/variable_classifier.py
"""Variable feasibility classifier for Kibana ES|QL variable controls.

See docs/roadmap/2026-04-27-kibana-variable-controls-design.md for the design.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

REJECT_REASONS: Final[tuple[str, ...]] = (
    "unsupported_variable_type",
    "drives_repeat",
    "unknown_definition_shape",
    "field_resolution_ambiguous",
    "field_resolution_failed",
    "inconsistent_field_use",
    "regex_template",
    "include_all_unsupported",
    "multi_value_with_eq_operator",
    "data_view_split",
    "native_promql_panel",
    "no_tag_field",
    "wildcard_default",
    "mixed_or_branches",
    "invalid_variable_name",
    "reserved_identifier",
    "verifier_failed_field_consistency",
    "verifier_failed_operator_consistency",
    "verifier_failed_leftover_token",
    "verifier_failed_missing_param",
    "verifier_failed_over_application",
    "verifier_failed_data_view_split",
)

RejectReason = Literal[
    "unsupported_variable_type",
    "drives_repeat",
    "unknown_definition_shape",
    "field_resolution_ambiguous",
    "field_resolution_failed",
    "inconsistent_field_use",
    "regex_template",
    "include_all_unsupported",
    "multi_value_with_eq_operator",
    "data_view_split",
    "native_promql_panel",
    "no_tag_field",
    "wildcard_default",
    "mixed_or_branches",
    "invalid_variable_name",
    "reserved_identifier",
    "verifier_failed_field_consistency",
    "verifier_failed_operator_consistency",
    "verifier_failed_leftover_token",
    "verifier_failed_missing_param",
    "verifier_failed_over_application",
    "verifier_failed_data_view_split",
]


@dataclass(frozen=True)
class AcceptedBinding:
    field: str
    multi: bool
    options_query: str


@dataclass(frozen=True)
class RejectedBinding:
    reason: str

    def __post_init__(self) -> None:
        if self.reason not in REJECT_REASONS:
            raise ValueError(
                f"unknown reject reason {self.reason!r}; "
                f"must be one of {REJECT_REASONS}"
            )


VariableBindingMap = dict[str, AcceptedBinding | RejectedBinding]
```

- [ ] **Step 4: Run the test and verify it passes**

Run: `.venv/bin/pytest tests/test_variable_classifier.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add observability_migration/core/variable_classifier.py tests/test_variable_classifier.py
git commit -m "Add variable-classifier reason enum and binding types."
```

---

### Task 2: Per-dashboard version-floor function

**Files:**
- Modify: `observability_migration/core/variable_classifier.py`
- Modify: `tests/test_variable_classifier.py`

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_variable_classifier.py

def test_compute_min_kibana_version_empty_map():
    assert vc.compute_min_kibana_version({}) == "9.1.0"


def test_compute_min_kibana_version_single_value_only():
    bm = {"x": vc.AcceptedBinding(field="f", multi=False, options_query="FROM y")}
    assert vc.compute_min_kibana_version(bm) == "9.1.0"


def test_compute_min_kibana_version_one_multi_value():
    bm = {
        "x": vc.AcceptedBinding(field="f", multi=True, options_query="FROM y"),
        "y": vc.AcceptedBinding(field="g", multi=False, options_query="FROM y"),
    }
    assert vc.compute_min_kibana_version(bm) == "9.3.0"


def test_compute_min_kibana_version_rejected_multi_does_not_lift_floor():
    bm = {"x": vc.RejectedBinding(reason="include_all_unsupported")}
    assert vc.compute_min_kibana_version(bm) == "9.1.0"
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/pytest tests/test_variable_classifier.py -v`
Expected: `AttributeError: module 'observability_migration.core.variable_classifier' has no attribute 'compute_min_kibana_version'`.

- [ ] **Step 3: Implement**

```python
# Append to observability_migration/core/variable_classifier.py

def compute_min_kibana_version(binding_map: VariableBindingMap) -> str:
    has_multi_value = any(
        isinstance(b, AcceptedBinding) and b.multi for b in binding_map.values()
    )
    return "9.3.0" if has_multi_value else "9.1.0"
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/pytest tests/test_variable_classifier.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add observability_migration/core/variable_classifier.py tests/test_variable_classifier.py
git commit -m "Compute per-dashboard Kibana floor from accepted bindings."
```

---

### Task 3: Options-query builder

**Files:**
- Modify: `observability_migration/core/variable_classifier.py`
- Modify: `tests/test_variable_classifier.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_variable_classifier.py

def test_build_options_query_shape():
    q = vc.build_options_query(data_view="metrics-*", field="service.instance.id")
    assert q == (
        "FROM metrics-*\n"
        "| WHERE service.instance.id IS NOT NULL\n"
        "| STATS BY service.instance.id\n"
        "| KEEP service.instance.id\n"
        "| LIMIT 1000"
    )


def test_build_options_query_is_deterministic():
    a = vc.build_options_query(data_view="logs-*", field="host.name")
    b = vc.build_options_query(data_view="logs-*", field="host.name")
    assert a == b
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/pytest tests/test_variable_classifier.py::test_build_options_query_shape -v`
Expected: `AttributeError`.

- [ ] **Step 3: Implement**

```python
# Append to observability_migration/core/variable_classifier.py

def build_options_query(*, data_view: str, field: str) -> str:
    if not data_view or not field:
        raise ValueError("data_view and field must be non-empty")
    return (
        f"FROM {data_view}\n"
        f"| WHERE {field} IS NOT NULL\n"
        f"| STATS BY {field}\n"
        f"| KEEP {field}\n"
        f"| LIMIT 1000"
    )
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/pytest tests/test_variable_classifier.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add observability_migration/core/variable_classifier.py tests/test_variable_classifier.py
git commit -m "Add deterministic options-query builder for variable controls."
```

---

### Task 4: Grafana classifier core

**Files:**
- Modify: `observability_migration/core/variable_classifier.py`
- Modify: `tests/test_variable_classifier.py`

- [ ] **Step 1: Write failing tests, one per Grafana acceptance/rejection rule**

```python
# Append to tests/test_variable_classifier.py
import pytest


def _grafana_var(**overrides):
    base = {
        "name": "instance",
        "label": "instance",
        "type": "query",
        "definition": "label_values(up, instance)",
        "multi": False,
        "includeAll": False,
        "hide": 0,
    }
    base.update(overrides)
    return base


def _grafana_panel_using(var_name, *, op="=", value_template=None, field="instance"):
    template = value_template if value_template is not None else f"${var_name}"
    return {
        "type": "timeseries",
        "datasource": {"type": "prometheus", "uid": "x"},
        "targets": [{"expr": f'metric{{{field}{op}"{template}"}}', "refId": "A"}],
    }


class _StubResolver:
    def __init__(self, mapping=None):
        self._mapping = mapping or {"instance": "service.instance.id"}

    def resolve_label(self, label):
        return self._mapping.get(label)

    def resolve_control_field(self, label):
        return self._mapping.get(label)

    def field_exists(self, field):
        return field in self._mapping.values()


def test_grafana_unsupported_variable_type_rejects():
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var(type="custom")],
        panels=[],
        resolver=_StubResolver(),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    assert isinstance(bm["instance"], vc.RejectedBinding)
    assert bm["instance"].reason == "unsupported_variable_type"


def test_grafana_drives_repeat_rejects():
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var()],
        panels=[],
        resolver=_StubResolver(),
        repeat_variable_names={"instance"},
        data_view="metrics-*",
    )
    assert bm["instance"].reason == "drives_repeat"


def test_grafana_unknown_definition_shape_rejects():
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var(definition="query_result(up)")],
        panels=[],
        resolver=_StubResolver(),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    assert bm["instance"].reason == "unknown_definition_shape"


def test_grafana_field_resolution_failed_rejects():
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var()],
        panels=[],
        resolver=_StubResolver(mapping={}),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    assert bm["instance"].reason == "field_resolution_failed"


def test_grafana_inconsistent_field_use_rejects():
    panels = [
        _grafana_panel_using("instance", field="instance"),
        _grafana_panel_using("instance", field="other_instance"),
    ]
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var()],
        panels=panels,
        resolver=_StubResolver(mapping={
            "instance": "service.instance.id",
            "other_instance": "host.name",
        }),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    assert bm["instance"].reason == "inconsistent_field_use"


def test_grafana_regex_template_rejects():
    panel = _grafana_panel_using("instance", op="=~", value_template="prefix-$instance.*")
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var()],
        panels=[panel],
        resolver=_StubResolver(),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    assert bm["instance"].reason == "regex_template"


def test_grafana_include_all_single_select_rejects():
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var(includeAll=True, multi=False)],
        panels=[_grafana_panel_using("instance")],
        resolver=_StubResolver(),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    assert bm["instance"].reason == "include_all_unsupported"


def test_grafana_invalid_variable_name_rejects():
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var(name="bad-name")],
        panels=[],
        resolver=_StubResolver(),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    assert bm["bad-name"].reason == "invalid_variable_name"


def test_grafana_reserved_identifier_rejects():
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var(name="where")],
        panels=[_grafana_panel_using("where")],
        resolver=_StubResolver(mapping={"where": "service.instance.id"}),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    assert bm["where"].reason == "reserved_identifier"


def test_grafana_accepts_simple_single_value():
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var()],
        panels=[_grafana_panel_using("instance")],
        resolver=_StubResolver(),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    binding = bm["instance"]
    assert isinstance(binding, vc.AcceptedBinding)
    assert binding.field == "service.instance.id"
    assert binding.multi is False
    assert "service.instance.id" in binding.options_query


def test_grafana_accepts_multi_value():
    panel = _grafana_panel_using("instance", op="=~")
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var(multi=True)],
        panels=[panel],
        resolver=_StubResolver(),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    binding = bm["instance"]
    assert isinstance(binding, vc.AcceptedBinding)
    assert binding.multi is True


def test_grafana_data_view_split_rejects():
    panels = [
        {**_grafana_panel_using("instance"), "datasource": {"uid": "metrics"}},
        {**_grafana_panel_using("instance"), "datasource": {"uid": "logs"}},
    ]
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var()],
        panels=panels,
        resolver=_StubResolver(),
        repeat_variable_names=set(),
        data_view="metrics-*",
        panel_data_view=lambda p: "metrics-*" if p["datasource"]["uid"] == "metrics" else "logs-*",
    )
    assert bm["instance"].reason == "data_view_split"
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/pytest tests/test_variable_classifier.py -v`
Expected: `AttributeError: module ... has no attribute 'classify_grafana_variables'`.

- [ ] **Step 3: Implement the classifier**

```python
# Append to observability_migration/core/variable_classifier.py
import re

ESQL_RESERVED_WORDS: Final[frozenset[str]] = frozenset({
    "from", "where", "stats", "by", "keep", "drop", "rename",
    "eval", "sort", "limit", "enrich", "mv_expand", "lookup",
    "join", "grok", "dissect",
})

_VALID_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LABEL_VALUES_RE = re.compile(r"label_values\s*\(\s*([^,]+?)\s*,\s*([A-Za-z0-9_]+)\s*\)")
_REGEX_META_RE = re.compile(r"[][(){}|^$+*?\\]")  # any regex meta in template


def _grafana_definition_text(var: dict) -> str:
    definition = var.get("definition")
    if isinstance(definition, str) and definition.strip():
        return definition
    query = var.get("query")
    if isinstance(query, dict):
        text = query.get("query") or query.get("definition") or ""
        return str(text)
    if isinstance(query, str):
        return query
    return ""


def _extract_label_from_definition(text: str) -> str | None:
    match = _LABEL_VALUES_RE.search(text)
    if not match:
        return None
    return match.group(2)


def _grafana_panel_matchers_for(var_name: str, panel: dict) -> list[dict]:
    """Return matchers in panel that reference $var_name. Each matcher dict has
    'field', 'op', 'value_template' (the raw text inside the quotes)."""
    out = []
    for target in panel.get("targets", []) or []:
        expr = target.get("expr", "") or ""
        if f"${var_name}" not in expr and f"${{{var_name}}}" not in expr and f"[[{var_name}]]" not in expr:
            continue
        for m in re.finditer(
            r'(?P<field>[A-Za-z_][A-Za-z0-9_:]*)\s*(?P<op>=~|!~|=|!=)\s*"(?P<value>[^"]*)"',
            expr,
        ):
            value = m.group("value")
            if (
                f"${var_name}" in value
                or f"${{{var_name}}}" in value
                or f"[[{var_name}]]" in value
            ):
                out.append({
                    "field": m.group("field"),
                    "op": m.group("op"),
                    "value_template": value,
                })
    return out


def classify_grafana_variables(
    *,
    variables: list[dict],
    panels: list[dict],
    resolver,
    repeat_variable_names: set[str],
    data_view: str,
    panel_data_view=None,
) -> VariableBindingMap:
    """Classify Grafana template variables for ES|QL parameter eligibility."""
    binding_map: VariableBindingMap = {}
    for var in variables:
        name = var.get("name", "")
        if not name:
            continue
        result = _classify_one_grafana(
            var=var,
            name=name,
            panels=panels,
            resolver=resolver,
            repeat_variable_names=repeat_variable_names,
            data_view=data_view,
            panel_data_view=panel_data_view,
        )
        binding_map[name] = result
    return binding_map


def _classify_one_grafana(
    *, var, name, panels, resolver, repeat_variable_names, data_view, panel_data_view,
):
    if not _VALID_IDENTIFIER_RE.match(name):
        return RejectedBinding(reason="invalid_variable_name")
    if name.lower() in ESQL_RESERVED_WORDS:
        return RejectedBinding(reason="reserved_identifier")
    if var.get("type") != "query":
        return RejectedBinding(reason="unsupported_variable_type")
    if name in repeat_variable_names:
        return RejectedBinding(reason="drives_repeat")

    definition = _grafana_definition_text(var)
    label = _extract_label_from_definition(definition)
    if label is None:
        return RejectedBinding(reason="unknown_definition_shape")

    field = resolver.resolve_control_field(label) if resolver else None
    if not field:
        return RejectedBinding(reason="field_resolution_failed")
    if not (resolver.field_exists(field) is not False):
        return RejectedBinding(reason="field_resolution_failed")

    multi = bool(var.get("multi"))
    include_all = bool(var.get("includeAll"))
    if include_all and not multi:
        return RejectedBinding(reason="include_all_unsupported")

    observed_field: str | None = None
    observed_data_view: str | None = None
    for panel in panels:
        matchers = _grafana_panel_matchers_for(name, panel)
        if not matchers:
            continue
        if panel_data_view is not None:
            dv = panel_data_view(panel)
            if observed_data_view is None:
                observed_data_view = dv
            elif observed_data_view != dv:
                return RejectedBinding(reason="data_view_split")
        for matcher in matchers:
            template = matcher["value_template"]
            stripped = template.replace(f"${{{name}}}", "").replace(f"${name}", "")
            if _REGEX_META_RE.search(stripped):
                return RejectedBinding(reason="regex_template")
            if matcher["op"] == "=" and multi:
                return RejectedBinding(reason="multi_value_with_eq_operator")
            mapped = resolver.resolve_label(matcher["field"]) if resolver else None
            if mapped is None:
                continue
            if observed_field is None:
                observed_field = mapped
            elif observed_field != mapped:
                return RejectedBinding(reason="inconsistent_field_use")

    canonical_field = observed_field or field
    canonical_data_view = observed_data_view or data_view
    options_query = build_options_query(data_view=canonical_data_view, field=canonical_field)
    return AcceptedBinding(field=canonical_field, multi=multi, options_query=options_query)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/pytest tests/test_variable_classifier.py -v`
Expected: 21 passed.

- [ ] **Step 5: Commit**

```bash
git add observability_migration/core/variable_classifier.py tests/test_variable_classifier.py
git commit -m "Add Grafana variable feasibility classifier."
```

---

### Task 5: Datadog classifier core

**Files:**
- Modify: `observability_migration/core/variable_classifier.py`
- Modify: `tests/test_variable_classifier.py`

- [ ] **Step 1: Write failing tests, one per Datadog rule**

```python
# Append to tests/test_variable_classifier.py
from dataclasses import dataclass


@dataclass
class _StubTV:
    name: str
    tag: str = ""
    default: str = ""
    defaults: list = None  # type: ignore[assignment]
    prefix: str = ""

    def __post_init__(self):
        if self.defaults is None:
            self.defaults = []


class _StubFieldMap:
    def __init__(self, mapping=None):
        self._mapping = mapping or {"host": "host.name"}

    def map_tag(self, tag, context=""):
        return self._mapping.get(tag)


def _datadog_widget_filter(tag, value):
    return {"requests": [{"q": f"avg:metric{{{tag}:{value}}}"}]}


def test_datadog_no_tag_field_rejects():
    bm = vc.classify_datadog_variables(
        variables=[_StubTV(name="scope", tag="")],
        widgets=[],
        field_map=_StubFieldMap(),
        data_view="metrics-*",
    )
    assert bm["scope"].reason == "no_tag_field"


def test_datadog_wildcard_default_rejects():
    bm = vc.classify_datadog_variables(
        variables=[_StubTV(name="host", tag="host", default="*")],
        widgets=[],
        field_map=_StubFieldMap(),
        data_view="metrics-*",
    )
    assert bm["host"].reason == "wildcard_default"


def test_datadog_field_resolution_failed_rejects():
    bm = vc.classify_datadog_variables(
        variables=[_StubTV(name="region", tag="region")],
        widgets=[],
        field_map=_StubFieldMap(mapping={}),
        data_view="metrics-*",
    )
    assert bm["region"].reason == "field_resolution_failed"


def test_datadog_accepts_single_tag():
    bm = vc.classify_datadog_variables(
        variables=[_StubTV(name="host", tag="host")],
        widgets=[_datadog_widget_filter("host", "$host")],
        field_map=_StubFieldMap(),
        data_view="metrics-*",
    )
    binding = bm["host"]
    assert isinstance(binding, vc.AcceptedBinding)
    assert binding.field == "host.name"
    assert binding.multi is False


def test_datadog_accepts_multi_when_default_star():
    bm = vc.classify_datadog_variables(
        variables=[_StubTV(name="host", tag="host", default="*", defaults=["a", "b"])],
        widgets=[_datadog_widget_filter("host", "$host")],
        field_map=_StubFieldMap(),
        data_view="metrics-*",
    )
    assert isinstance(bm["host"], vc.AcceptedBinding)
    assert bm["host"].multi is True
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/pytest tests/test_variable_classifier.py -v`
Expected: `AttributeError: ... 'classify_datadog_variables'`.

- [ ] **Step 3: Implement**

```python
# Append to observability_migration/core/variable_classifier.py

def classify_datadog_variables(
    *,
    variables,
    widgets,
    field_map,
    data_view: str,
) -> VariableBindingMap:
    binding_map: VariableBindingMap = {}
    for tv in variables:
        name = getattr(tv, "name", "")
        if not name:
            continue
        binding_map[name] = _classify_one_datadog(
            tv=tv, name=name, widgets=widgets, field_map=field_map, data_view=data_view
        )
    return binding_map


def _datadog_value_has_template(value: str) -> bool:
    return bool(re.search(r"\$[A-Za-z_]", value))


def _classify_one_datadog(*, tv, name, widgets, field_map, data_view):
    if not _VALID_IDENTIFIER_RE.match(name):
        return RejectedBinding(reason="invalid_variable_name")
    if name.lower() in ESQL_RESERVED_WORDS:
        return RejectedBinding(reason="reserved_identifier")
    tag = getattr(tv, "tag", "") or getattr(tv, "prefix", "")
    if not tag:
        return RejectedBinding(reason="no_tag_field")

    default = getattr(tv, "default", "") or ""
    defaults = list(getattr(tv, "defaults", []) or [])
    if "*" in default and not defaults:
        return RejectedBinding(reason="wildcard_default")

    field = field_map.map_tag(tag, context="metric") if field_map else None
    if not field:
        return RejectedBinding(reason="field_resolution_failed")

    multi = bool(defaults) or default == "*"

    canonical_field = field
    for widget in widgets:
        for request in widget.get("requests", []) or []:
            q = str(request.get("q") or "")
            for match in re.finditer(
                r"([A-Za-z0-9_:.-]+)\s*:\s*([^\s,}]+)", q,
            ):
                w_tag = match.group(1)
                w_value = match.group(2)
                if w_tag != tag:
                    continue
                if not _datadog_value_has_template(w_value):
                    continue
                if w_value.count("$") > 1 or "|" in w_value:
                    return RejectedBinding(reason="mixed_or_branches")
                bare = w_value.replace(f"${name}.value", "").replace(f"${name}", "")
                if bare:
                    if "*" in bare or "?" in bare:
                        return RejectedBinding(reason="wildcard_default")

    options_query = build_options_query(data_view=data_view, field=canonical_field)
    return AcceptedBinding(field=canonical_field, multi=multi, options_query=options_query)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/pytest tests/test_variable_classifier.py -v`
Expected: 26 passed.

- [ ] **Step 5: Commit**

```bash
git add observability_migration/core/variable_classifier.py tests/test_variable_classifier.py
git commit -m "Add Datadog template-variable feasibility classifier."
```

---

### Task 6: Verifier framework

**Files:**
- Create: `observability_migration/core/variable_control_verifier.py`
- Create: `tests/test_variable_control_verifier.py`

- [ ] **Step 1: Write failing tests, one negative per invariant**

```python
# tests/test_variable_control_verifier.py
from observability_migration.core import variable_classifier as vc
from observability_migration.core import variable_control_verifier as ver


def _accepted(field="service.instance.id", multi=False):
    return vc.AcceptedBinding(field=field, multi=multi, options_query="FROM x")


def _record(panel_id, var_name, *, observed_field=None, observed_op=None,
            esql="", source_refs=None, data_view="metrics-*"):
    return ver.PanelTranslationRecord(
        panel_id=panel_id,
        compiled_esql=esql,
        source_var_refs=set(source_refs or [var_name]),
        observed_fields={var_name: observed_field} if observed_field else {},
        observed_ops={var_name: observed_op} if observed_op else {},
        data_view=data_view,
    )


def test_field_consistency_downgrade():
    bm = {"x": _accepted()}
    records = [
        _record("p1", "x", observed_field="service.instance.id"),
        _record("p2", "x", observed_field="host.name"),
    ]
    out = ver.verify_bindings(records, bm)
    assert isinstance(out["x"], vc.RejectedBinding)
    assert out["x"].reason == "verifier_failed_field_consistency"


def test_operator_consistency_downgrade():
    bm = {"x": _accepted(multi=True)}
    records = [
        _record("p1", "x", observed_op="exact_match"),
        _record("p2", "x", observed_op="multi_value"),
    ]
    out = ver.verify_bindings(records, bm)
    assert out["x"].reason == "verifier_failed_operator_consistency"


def test_leftover_token_downgrade():
    bm = {"x": _accepted()}
    records = [_record("p1", "x", esql="WHERE a == ?x AND b == \"$x\"")]
    out = ver.verify_bindings(records, bm)
    assert out["x"].reason == "verifier_failed_leftover_token"


def test_missing_param_downgrade():
    bm = {"x": _accepted()}
    records = [_record("p1", "x", source_refs=["x"], esql="WHERE a IS NOT NULL")]
    out = ver.verify_bindings(records, bm)
    assert out["x"].reason == "verifier_failed_missing_param"


def test_over_application_downgrade():
    bm = {"x": _accepted()}
    records = [
        _record("p1", "x", source_refs=[], esql="WHERE a == ?x"),
    ]
    out = ver.verify_bindings(records, bm)
    assert out["x"].reason == "verifier_failed_over_application"


def test_data_view_split_downgrade():
    bm = {"x": _accepted()}
    records = [
        _record("p1", "x", data_view="metrics-*", esql="WHERE a == ?x"),
        _record("p2", "x", data_view="logs-*", esql="WHERE a == ?x"),
    ]
    out = ver.verify_bindings(records, bm)
    assert out["x"].reason == "verifier_failed_data_view_split"


def test_idempotent_when_no_failures():
    bm = {"x": _accepted()}
    records = [_record("p1", "x", observed_field="service.instance.id",
                       esql="WHERE a == ?x")]
    out1 = ver.verify_bindings(records, bm)
    out2 = ver.verify_bindings(records, out1)
    assert out1 == out2
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/pytest tests/test_variable_control_verifier.py -v`
Expected: ImportError on `variable_control_verifier`.

- [ ] **Step 3: Implement**

```python
# observability_migration/core/variable_control_verifier.py
"""Post-translation correctness verifier for variable controls."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from observability_migration.core.variable_classifier import (
    AcceptedBinding,
    RejectedBinding,
    VariableBindingMap,
)


@dataclass(frozen=True)
class PanelTranslationRecord:
    panel_id: str
    compiled_esql: str
    source_var_refs: set[str]
    observed_fields: dict[str, str] = field(default_factory=dict)
    observed_ops: dict[str, str] = field(default_factory=dict)
    data_view: str = ""


@dataclass(frozen=True)
class _Downgrade:
    var_name: str
    reason: str


CheckFn = Callable[[list[PanelTranslationRecord], VariableBindingMap], list[_Downgrade]]


def _check_field_consistency(records, binding_map):
    out = []
    for name, binding in binding_map.items():
        if not isinstance(binding, AcceptedBinding):
            continue
        observed = {r.observed_fields[name] for r in records if name in r.observed_fields}
        if observed - {binding.field}:
            out.append(_Downgrade(name, "verifier_failed_field_consistency"))
    return out


def _check_operator_consistency(records, binding_map):
    out = []
    for name, binding in binding_map.items():
        if not isinstance(binding, AcceptedBinding):
            continue
        ops = {r.observed_ops[name] for r in records if name in r.observed_ops}
        if "exact_match" in ops and "multi_value" in ops:
            out.append(_Downgrade(name, "verifier_failed_operator_consistency"))
    return out


def _check_leftover_token(records, binding_map):
    out = []
    for name, binding in binding_map.items():
        if not isinstance(binding, AcceptedBinding):
            continue
        token = re.compile(rf"\${name}\b")
        for r in records:
            if name in r.source_var_refs and token.search(r.compiled_esql):
                out.append(_Downgrade(name, "verifier_failed_leftover_token"))
                break
    return out


def _check_missing_param(records, binding_map):
    out = []
    for name, binding in binding_map.items():
        if not isinstance(binding, AcceptedBinding):
            continue
        token = re.compile(rf"\?{name}\b")
        for r in records:
            if name in r.source_var_refs and not token.search(r.compiled_esql):
                out.append(_Downgrade(name, "verifier_failed_missing_param"))
                break
    return out


def _check_over_application(records, binding_map):
    out = []
    for name, binding in binding_map.items():
        if not isinstance(binding, AcceptedBinding):
            continue
        token = re.compile(rf"\?{name}\b")
        for r in records:
            if name not in r.source_var_refs and token.search(r.compiled_esql):
                out.append(_Downgrade(name, "verifier_failed_over_application"))
                break
    return out


def _check_data_view_split(records, binding_map):
    out = []
    for name, binding in binding_map.items():
        if not isinstance(binding, AcceptedBinding):
            continue
        data_views = {r.data_view for r in records if name in r.source_var_refs and r.data_view}
        if len(data_views) > 1:
            out.append(_Downgrade(name, "verifier_failed_data_view_split"))
    return out


CHECKS: list[CheckFn] = [
    _check_field_consistency,
    _check_operator_consistency,
    _check_leftover_token,
    _check_missing_param,
    _check_over_application,
    _check_data_view_split,
]


def verify_bindings(
    records: list[PanelTranslationRecord],
    binding_map: VariableBindingMap,
) -> VariableBindingMap:
    out: VariableBindingMap = dict(binding_map)
    for check in CHECKS:
        for downgrade in check(records, out):
            out[downgrade.var_name] = RejectedBinding(reason=downgrade.reason)
    return out
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/pytest tests/test_variable_control_verifier.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add observability_migration/core/variable_control_verifier.py tests/test_variable_control_verifier.py
git commit -m "Add post-translation verifier for variable-control bindings."
```

---

### Task 7: Emergency env-var disable switch

**Files:**
- Modify: `observability_migration/core/variable_classifier.py`
- Modify: `tests/test_variable_classifier.py`

- [ ] **Step 1: Failing test**

```python
# Append to tests/test_variable_classifier.py
import os


def test_disable_env_var_short_circuits_grafana(monkeypatch):
    monkeypatch.setenv("OBS_MIGRATION_DISABLE_VARIABLE_CONTROLS", "1")
    bm = vc.classify_grafana_variables(
        variables=[_grafana_var()],
        panels=[_grafana_panel_using("instance")],
        resolver=_StubResolver(),
        repeat_variable_names=set(),
        data_view="metrics-*",
    )
    assert isinstance(bm["instance"], vc.RejectedBinding)
    assert bm["instance"].reason == "unsupported_variable_type"


def test_disable_env_var_short_circuits_datadog(monkeypatch):
    monkeypatch.setenv("OBS_MIGRATION_DISABLE_VARIABLE_CONTROLS", "1")
    bm = vc.classify_datadog_variables(
        variables=[_StubTV(name="host", tag="host")],
        widgets=[_datadog_widget_filter("host", "$host")],
        field_map=_StubFieldMap(),
        data_view="metrics-*",
    )
    assert bm["host"].reason == "unsupported_variable_type"
```

- [ ] **Step 2: Run, verify failure (or different reason than `unsupported_variable_type`)**

Run: `.venv/bin/pytest tests/test_variable_classifier.py::test_disable_env_var_short_circuits_grafana tests/test_variable_classifier.py::test_disable_env_var_short_circuits_datadog -v`
Expected: assertion fails because env var is not yet honored.

- [ ] **Step 3: Implement**

```python
# Append to observability_migration/core/variable_classifier.py
import os

_DISABLE_ENV_VAR = "OBS_MIGRATION_DISABLE_VARIABLE_CONTROLS"


def _is_disabled() -> bool:
    return os.environ.get(_DISABLE_ENV_VAR) == "1"
```

Wrap classifier entry points:

```python
# Modify classify_grafana_variables (top of function body):
    if _is_disabled():
        return {
            v["name"]: RejectedBinding(reason="unsupported_variable_type")
            for v in variables
            if v.get("name")
        }

# Same modification at top of classify_datadog_variables.
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/pytest tests/test_variable_classifier.py -v`
Expected: 28 passed.

- [ ] **Step 5: Commit**

```bash
git add observability_migration/core/variable_classifier.py tests/test_variable_classifier.py
git commit -m "Honor OBS_MIGRATION_DISABLE_VARIABLE_CONTROLS env var as emergency switch."
```

---

### Task 8: Grafana matcher rewriting consults binding map

**Files:**
- Modify: `observability_migration/adapters/source/grafana/promql.py` (`_matcher_to_esql`, `_selector_filters`, `_frag_filters`)
- Modify: `tests/test_translator_alias_quality.py` (or new `tests/test_grafana_matcher_rewrite.py`)

- [ ] **Step 1: Write failing tests covering §6.1 rule table**

```python
# tests/test_grafana_matcher_rewrite.py
from observability_migration.adapters.source.grafana import promql
from observability_migration.core import variable_classifier as vc


class _Resolver:
    def resolve_label(self, label):
        return {"instance": "service.instance.id"}.get(label, label)


def _bm_single():
    return {"instance": vc.AcceptedBinding(
        field="service.instance.id", multi=False, options_query="FROM x"
    )}


def _bm_multi():
    return {"instance": vc.AcceptedBinding(
        field="service.instance.id", multi=True, options_query="FROM x"
    )}


def test_eq_single_value_emits_param():
    matcher = {"label": "instance", "op": "=", "value": "$instance"}
    out = promql._matcher_to_esql(matcher, _Resolver(), binding_map=_bm_single())
    assert out == "service.instance.id == ?instance"


def test_regex_single_value_no_meta_emits_param():
    matcher = {"label": "instance", "op": "=~", "value": "$instance"}
    out = promql._matcher_to_esql(matcher, _Resolver(), binding_map=_bm_single())
    assert out == "service.instance.id == ?instance"


def test_eq_multi_value_rejected_falls_back_to_none():
    matcher = {"label": "instance", "op": "=", "value": "$instance"}
    out = promql._matcher_to_esql(matcher, _Resolver(), binding_map=_bm_multi())
    assert out is None


def test_regex_multi_value_emits_mv_contains():
    matcher = {"label": "instance", "op": "=~", "value": "$instance"}
    out = promql._matcher_to_esql(matcher, _Resolver(), binding_map=_bm_multi())
    assert out == "MV_CONTAINS(?instance, service.instance.id)"


def test_neq_single_value_emits_param():
    matcher = {"label": "instance", "op": "!=", "value": "$instance"}
    out = promql._matcher_to_esql(matcher, _Resolver(), binding_map=_bm_single())
    assert out == "service.instance.id != ?instance"


def test_neg_regex_multi_value_emits_not_mv_contains():
    matcher = {"label": "instance", "op": "!~", "value": "$instance"}
    out = promql._matcher_to_esql(matcher, _Resolver(), binding_map=_bm_multi())
    assert out == "NOT MV_CONTAINS(?instance, service.instance.id)"


def test_no_binding_map_falls_through_to_legacy_behavior():
    matcher = {"label": "instance", "op": "=", "value": "$instance"}
    out = promql._matcher_to_esql(matcher, _Resolver(), binding_map=None)
    assert out is None


def test_rejected_binding_falls_through_to_legacy_behavior():
    bm = {"instance": vc.RejectedBinding(reason="include_all_unsupported")}
    matcher = {"label": "instance", "op": "=", "value": "$instance"}
    out = promql._matcher_to_esql(matcher, _Resolver(), binding_map=bm)
    assert out is None
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/pytest tests/test_grafana_matcher_rewrite.py -v`
Expected: TypeError on `binding_map` argument.

- [ ] **Step 3: Modify `_matcher_to_esql` in promql.py to accept and consult `binding_map`**

```python
# Modify observability_migration/adapters/source/grafana/promql.py
# Replace _matcher_to_esql with:

_VAR_TOKEN_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def _extract_single_var_name(value: str) -> str | None:
    matches = list(_VAR_TOKEN_RE.finditer(value))
    if len(matches) != 1:
        return None
    name = matches[0].group(1) or matches[0].group(2)
    span = matches[0].span()
    bare = value[: span[0]] + value[span[1] :]
    if bare:  # template has more than just the variable
        return None
    return name


def _matcher_to_esql(matcher, resolver, *, binding_map=None):
    label = resolver.resolve_label(matcher["label"]) if resolver else matcher["label"]
    op = matcher["op"]
    value = matcher["value"]
    if not label:
        return None
    if binding_map:
        var_name = _extract_single_var_name(value)
        if var_name and var_name in binding_map:
            from observability_migration.core.variable_classifier import AcceptedBinding
            binding = binding_map[var_name]
            if isinstance(binding, AcceptedBinding):
                return _bound_param_clause(label, op, var_name, binding.multi)
    if "$" in value or value.startswith("label_"):
        return None
    if op == "=":
        return f"{label} == {_quote_esql_string(value)}"
    if op == "!=":
        return f"{label} != {_quote_esql_string(value)}"
    if op == "=~":
        if value in (".*", ".+", ""):
            return None
        return f"{label} RLIKE {_quote_esql_string(value)}"
    if op == "!~":
        if value in (".*", ".+", ""):
            return None
        return f"NOT ({label} RLIKE {_quote_esql_string(value)})"
    return None


def _bound_param_clause(label, op, var_name, multi):
    if multi:
        if op == "=~":
            return f"MV_CONTAINS(?{var_name}, {label})"
        if op == "!~":
            return f"NOT MV_CONTAINS(?{var_name}, {label})"
        return None
    if op == "=" or op == "=~":
        return f"{label} == ?{var_name}"
    if op == "!=" or op == "!~":
        return f"{label} != ?{var_name}"
    return None
```

- [ ] **Step 4: Update `_selector_filters` and any direct callers to pass `binding_map`**

```python
# In promql.py:
def _selector_filters(matchers, resolver, *, binding_map=None):
    filters = []
    for matcher in matchers:
        filter_expr = _matcher_to_esql(matcher, resolver, binding_map=binding_map)
        if filter_expr:
            filters.append(filter_expr)
    return filters
```

Search for `_matcher_to_esql(` and `_selector_filters(` call sites in `promql.py` and `panels.py`; thread `binding_map=binding_map` through. Default to `None` so unmodified call sites keep today's behavior.

- [ ] **Step 5: Run tests, verify pass**

Run: `.venv/bin/pytest tests/test_grafana_matcher_rewrite.py tests/test_translator_alias_quality.py -v`
Expected: all pass; existing alias-quality tests still pass.

- [ ] **Step 6: Commit**

```bash
git add observability_migration/adapters/source/grafana/promql.py tests/test_grafana_matcher_rewrite.py
git commit -m "Grafana matcher rewriter consults variable binding map."
```

---

### Task 9: Datadog matcher rewriting consults binding map

**Files:**
- Modify: `observability_migration/adapters/source/datadog/translate.py` (`_tag_filter_to_esql`)
- Create: `tests/test_datadog_tag_rewrite.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_datadog_tag_rewrite.py
from observability_migration.adapters.source.datadog import translate as dd
from observability_migration.adapters.source.datadog.models import TagFilter
from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.core import variable_classifier as vc


def _bm_single():
    return {"host": vc.AcceptedBinding(field="host.name", multi=False, options_query="FROM x")}


def _bm_multi():
    return {"host": vc.AcceptedBinding(field="host.name", multi=True, options_query="FROM x")}


def test_single_tag_template_emits_param():
    flt = TagFilter(key="host", value="$host", negated=False)
    out = dd._tag_filter_to_esql(flt, OTEL_PROFILE, context="metric", binding_map=_bm_single())
    assert out == "host.name == ?host"


def test_multi_tag_template_emits_mv_contains():
    flt = TagFilter(key="host", value="$host", negated=False)
    out = dd._tag_filter_to_esql(flt, OTEL_PROFILE, context="metric", binding_map=_bm_multi())
    assert out == "MV_CONTAINS(?host, host.name)"


def test_dot_value_template_also_works():
    flt = TagFilter(key="host", value="$host.value", negated=False)
    out = dd._tag_filter_to_esql(flt, OTEL_PROFILE, context="metric", binding_map=_bm_single())
    assert out == "host.name == ?host"


def test_no_binding_falls_through_to_legacy_like():
    flt = TagFilter(key="host", value="$host", negated=False)
    out = dd._tag_filter_to_esql(flt, OTEL_PROFILE, context="metric", binding_map=None)
    assert "LIKE" in out
```

- [ ] **Step 2: Run, verify failure**

Run: `.venv/bin/pytest tests/test_datadog_tag_rewrite.py -v`
Expected: TypeError on `binding_map`.

- [ ] **Step 3: Modify `_tag_filter_to_esql`**

```python
# Modify observability_migration/adapters/source/datadog/translate.py
# Replace the signature and add binding-map branch at top:

import re as _re_local

_DD_VAR_RE = _re_local.compile(r"\$(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\.value)?")


def _extract_dd_single_var(value: str) -> str | None:
    matches = list(_DD_VAR_RE.finditer(value))
    if len(matches) != 1:
        return None
    name = matches[0].group("name")
    bare = _DD_VAR_RE.sub("", value).strip()
    if bare:
        return None
    return name


def _tag_filter_to_esql(filt, field_map, context: str = "", *, binding_map=None) -> str:
    if not isinstance(filt, TagFilter):
        return ""
    es_field = _esql_identifier(field_map.map_tag(filt.key, context=context))
    value = filt.value or ""
    if binding_map:
        var_name = _extract_dd_single_var(value)
        if var_name and var_name in binding_map:
            from observability_migration.core.variable_classifier import AcceptedBinding
            binding = binding_map[var_name]
            if isinstance(binding, AcceptedBinding):
                if binding.multi:
                    op = "NOT " if filt.negated else ""
                    return f"{op}MV_CONTAINS(?{var_name}, {es_field})"
                op = "!=" if filt.negated else "=="
                return f"{es_field} {op} ?{var_name}"
    # Existing body unchanged below this line.
```

(Keep the rest of the function exactly as-is.)

Update every internal call site of `_tag_filter_to_esql` in `translate.py` to forward `binding_map=binding_map`. Default to `None`.

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/pytest tests/test_datadog_tag_rewrite.py tests/test_datadog_migrate.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add observability_migration/adapters/source/datadog/translate.py tests/test_datadog_tag_rewrite.py
git commit -m "Datadog tag-filter rewriter consults variable binding map."
```

---

### Task 10: Grafana control emission for accepted variables

**Files:**
- Modify: `observability_migration/adapters/source/grafana/panels.py` (`translate_variables`, `query_variable_rule`)
- Create: `tests/test_grafana_control_emission.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_grafana_control_emission.py
from observability_migration.adapters.source.grafana.panels import translate_variables
from observability_migration.core import variable_classifier as vc


def _resolver():
    class R:
        def resolve_control_field(self, label):
            return {"instance": "service.instance.id"}.get(label, label)

        def resolve_label(self, label):
            return self.resolve_control_field(label)

        def field_exists(self, field):
            return True
    return R()


def test_accepted_single_value_emits_esql_control():
    bm = {"instance": vc.AcceptedBinding(
        field="service.instance.id", multi=False,
        options_query="FROM metrics-*\n| LIMIT 1000",
    )}
    controls = translate_variables(
        template_list=[{"name": "instance", "label": "Instance",
                        "type": "query", "definition": "label_values(up, instance)"}],
        datasource_index="metrics-*",
        resolver=_resolver(),
        repeat_variable_names=None,
        binding_map=bm,
    )
    assert controls == [{
        "type": "esql",
        "variable_name": "instance",
        "variable_type": "values",
        "multiple": False,
        "label": "Instance",
        "query": "FROM metrics-*\n| LIMIT 1000",
    }]


def test_accepted_multi_value_emits_multi_select():
    bm = {"instance": vc.AcceptedBinding(
        field="service.instance.id", multi=True, options_query="FROM x",
    )}
    controls = translate_variables(
        template_list=[{"name": "instance", "label": "Instance",
                        "type": "query", "definition": "label_values(up, instance)",
                        "multi": True}],
        datasource_index="metrics-*",
        resolver=_resolver(),
        binding_map=bm,
    )
    assert controls[0]["variable_type"] == "multi_values"
    assert controls[0]["multiple"] is True


def test_rejected_variable_emits_classic_options():
    bm = {"instance": vc.RejectedBinding(reason="include_all_unsupported")}
    controls = translate_variables(
        template_list=[{"name": "instance", "label": "Instance",
                        "type": "query", "definition": "label_values(up, instance)"}],
        datasource_index="metrics-*",
        resolver=_resolver(),
        binding_map=bm,
    )
    assert controls[0]["type"] == "options"


def test_no_binding_map_uses_legacy_behavior():
    controls = translate_variables(
        template_list=[{"name": "instance", "label": "Instance",
                        "type": "query", "definition": "label_values(up, instance)"}],
        datasource_index="metrics-*",
        resolver=_resolver(),
    )
    assert controls[0]["type"] == "options"


def test_accepted_variables_emitted_before_rejected():
    bm = {
        "rej": vc.RejectedBinding(reason="include_all_unsupported"),
        "acc": vc.AcceptedBinding(field="f", multi=False, options_query="FROM x"),
    }
    controls = translate_variables(
        template_list=[
            {"name": "rej", "type": "query", "definition": "label_values(up, rej)"},
            {"name": "acc", "type": "query", "definition": "label_values(up, acc)"},
        ],
        datasource_index="metrics-*",
        resolver=_resolver(),
        binding_map=bm,
    )
    assert controls[0]["variable_name"] == "acc"
    assert controls[1].get("type") == "options"
```

- [ ] **Step 2: Run, verify failure**

Run: `.venv/bin/pytest tests/test_grafana_control_emission.py -v`
Expected: TypeError on `binding_map`.

- [ ] **Step 3: Implement**

```python
# Modify observability_migration/adapters/source/grafana/panels.py

def translate_variables(
    template_list,
    datasource_index="metrics-*",
    rule_pack=None,
    resolver=None,
    repeat_variable_names=None,
    *,
    binding_map=None,
):
    from observability_migration.core.variable_classifier import AcceptedBinding, RejectedBinding
    rule_pack = rule_pack or RulePackConfig()
    accepted_controls = []
    rejected_controls = []
    for var in template_list:
        name = var.get("name", "")
        if binding_map and isinstance(binding_map.get(name), AcceptedBinding):
            binding = binding_map[name]
            accepted_controls.append({
                "type": "esql",
                "variable_name": name,
                "variable_type": "multi_values" if binding.multi else "values",
                "multiple": binding.multi,
                "label": var.get("label") or name,
                "query": binding.options_query,
            })
            continue
        context = VariableContext(
            variable=var,
            data_view=datasource_index,
            resolver=resolver,
            rule_pack=rule_pack,
            query_text=_variable_query_text(var),
            repeat_variable_names=set(repeat_variable_names or ()),
        )
        VARIABLE_TRANSLATORS.apply(context, stop_when=lambda ctx, _: ctx.handled)
        if context.control:
            rejected_controls.append(context.control)
    return accepted_controls + rejected_controls
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/pytest tests/test_grafana_control_emission.py tests/test_migrate.py -k controls -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add observability_migration/adapters/source/grafana/panels.py tests/test_grafana_control_emission.py
git commit -m "Grafana translate_variables emits ESQL controls for accepted variables."
```

---

### Task 11: Datadog control emission for accepted variables

**Files:**
- Modify: `observability_migration/adapters/source/datadog/generate.py` (`_build_controls_from_template_vars`)
- Create: `tests/test_datadog_control_emission.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_datadog_control_emission.py
from observability_migration.adapters.source.datadog.generate import _build_controls_from_template_vars
from observability_migration.adapters.source.datadog.field_map import OTEL_PROFILE
from observability_migration.adapters.source.datadog.models import TemplateVariable
from observability_migration.core import variable_classifier as vc


def test_accepted_emits_esql_control():
    bm = {"host": vc.AcceptedBinding(
        field="host.name", multi=False,
        options_query="FROM metrics-*\n| LIMIT 1000",
    )}
    controls = _build_controls_from_template_vars(
        template_vars=[TemplateVariable(name="host", tag="host")],
        data_view="metrics-*",
        field_map=OTEL_PROFILE,
        binding_map=bm,
    )
    assert controls[0]["type"] == "esql"
    assert controls[0]["variable_name"] == "host"
    assert controls[0]["multiple"] is False


def test_rejected_emits_classic_options():
    bm = {"host": vc.RejectedBinding(reason="wildcard_default")}
    controls = _build_controls_from_template_vars(
        template_vars=[TemplateVariable(name="host", tag="host")],
        data_view="metrics-*",
        field_map=OTEL_PROFILE,
        binding_map=bm,
    )
    assert controls[0]["type"] == "options"


def test_no_binding_map_uses_legacy():
    controls = _build_controls_from_template_vars(
        template_vars=[TemplateVariable(name="host", tag="host")],
        data_view="metrics-*",
        field_map=OTEL_PROFILE,
    )
    assert controls[0]["type"] == "options"
```

- [ ] **Step 2: Run, verify failure**

Run: `.venv/bin/pytest tests/test_datadog_control_emission.py -v`

- [ ] **Step 3: Implement**

```python
# Modify observability_migration/adapters/source/datadog/generate.py

def _build_controls_from_template_vars(
    template_vars,
    data_view: str,
    field_map=None,
    *,
    binding_map=None,
) -> list[dict]:
    from observability_migration.core.variable_classifier import AcceptedBinding
    _UNRESOLVABLE_VARS = {"scope"}
    accepted_controls = []
    legacy_controls = []
    for tv in template_vars:
        name = tv.name
        if binding_map and isinstance(binding_map.get(name), AcceptedBinding):
            binding = binding_map[name]
            accepted_controls.append({
                "type": "esql",
                "variable_name": name,
                "variable_type": "multi_values" if binding.multi else "values",
                "multiple": binding.multi,
                "label": name,
                "query": binding.options_query,
            })
            continue
        # Legacy path (unchanged below)
        tag = tv.tag or tv.prefix
        if not tag:
            if name.lower() in _UNRESOLVABLE_VARS:
                continue
            tag = name
        if not tag:
            continue
        es_field = field_map.map_tag(tag, context="metric") if field_map else tag
        legacy_controls.append({
            "type": "options",
            "label": name,
            "data_view": data_view,
            "field": es_field,
            "multiple": len(tv.defaults) > 1 or tv.default == "*",
        })
    return accepted_controls + legacy_controls
```

- [ ] **Step 4: Run tests, verify pass**

Run: `.venv/bin/pytest tests/test_datadog_control_emission.py tests/test_datadog_migrate.py -v`

- [ ] **Step 5: Commit**

```bash
git add observability_migration/adapters/source/datadog/generate.py tests/test_datadog_control_emission.py
git commit -m "Datadog _build_controls_from_template_vars emits ESQL controls for accepted variables."
```

---

### Task 12: Migration-report telemetry for variables

**Files:**
- Modify: `observability_migration/core/reporting/report.py` (`save_detailed_report`, `MigrationResult` if needed)
- Modify: `tests/test_telemetry_contract.py` and/or new `tests/test_report_variables.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_report_variables.py
import json
from pathlib import Path
from observability_migration.core.reporting.report import save_detailed_report, MigrationResult
from observability_migration.core import variable_classifier as vc


def test_report_includes_variables_block(tmp_path):
    result = MigrationResult()
    result.variable_bindings = {  # new attribute on MigrationResult
        "Dashboard X": {
            "instance": vc.AcceptedBinding(field="service.instance.id", multi=False, options_query="FROM x"),
            "namespace": vc.RejectedBinding(reason="include_all_unsupported"),
        }
    }
    result.panel_parameterizations = {"Dashboard X": {"?instance": 12, "?cluster": 0}}
    out = tmp_path / "report.json"
    save_detailed_report([result], compile_results={}, output_path=out)
    body = json.loads(out.read_text())
    assert body["dashboards"][0]["variables"]["accepted"][0]["name"] == "instance"
    assert body["dashboards"][0]["variables"]["rejected"][0]["reason"] == "include_all_unsupported"
    assert body["dashboards"][0]["panel_parameterizations"]["?instance"] == 12
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/pytest tests/test_report_variables.py -v`
Expected: AttributeError on `result.variable_bindings`.

- [ ] **Step 3: Implement**

In `observability_migration/core/reporting/report.py`:

```python
# Add fields to MigrationResult dataclass:
@dataclass
class MigrationResult:
    # existing fields ...
    variable_bindings: dict = field(default_factory=dict)
    panel_parameterizations: dict = field(default_factory=dict)
```

Modify `save_detailed_report` to render those fields:

```python
def _serialize_variables(per_dashboard_bindings, per_dashboard_param_counts, dashboard_name):
    from observability_migration.core.variable_classifier import AcceptedBinding, RejectedBinding
    bindings = per_dashboard_bindings.get(dashboard_name, {})
    accepted = [
        {"name": n, "field": b.field, "multi": b.multi}
        for n, b in bindings.items() if isinstance(b, AcceptedBinding)
    ]
    rejected = [
        {"name": n, "reason": b.reason}
        for n, b in bindings.items() if isinstance(b, RejectedBinding)
        and not b.reason.startswith("verifier_failed_")
    ]
    verifier_downgraded = [
        {"name": n, "reason": b.reason}
        for n, b in bindings.items() if isinstance(b, RejectedBinding)
        and b.reason.startswith("verifier_failed_")
    ]
    return {
        "variables": {
            "accepted": accepted,
            "accepted_fields": [],
            "accepted_functions": [],
            "accepted_intervals": [],
            "rejected": rejected,
            "verifier_downgraded": verifier_downgraded,
        },
        "panel_parameterizations": per_dashboard_param_counts.get(dashboard_name, {}),
    }
```

In the dashboard-loop of `save_detailed_report`, merge the new dict into each dashboard entry.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_report_variables.py -v`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add observability_migration/core/reporting/report.py tests/test_report_variables.py
git commit -m "Migration report includes variable bindings and parameterization counts."
```

---

### Task 13: Per-dashboard `minimum_kibana_version` wiring

**Files:**
- Modify: `observability_migration/adapters/source/grafana/panels.py` (the dashboard YAML assembly path)
- Modify: `observability_migration/adapters/source/datadog/generate.py` (`generate_dashboard_yaml`)
- Modify: `tests/test_migrate.py`, `tests/test_datadog_migrate.py`

- [ ] **Step 1: Failing tests**

```python
# Append to tests/test_migrate.py (or new test file)

def test_grafana_dashboard_floor_lifts_when_multi_value_accepted():
    # Build a fixture dashboard with one multi-value accepted variable.
    # Run translation via the same entry point used by tests above.
    # Assert minimum_kibana_version == "9.3.0".
    ...

def test_grafana_dashboard_floor_stays_91_for_single_value_only():
    ...
```

(Concrete test setup mirrors the existing `tests/test_migrate.py` patterns; see lines around 846-934 for the variable-controls integration tests already in place. Adapt one of those to assert the new floor field.)

- [ ] **Step 2: Run and verify failure**

Expected: assertion failures because today everything is 9.1.0.

- [ ] **Step 3: Implement**

In `observability_migration/adapters/source/grafana/panels.py` find the dashboard-doc construction (search for `minimum_kibana_version`) and replace the hard-coded literal with:

```python
from observability_migration.core.variable_classifier import compute_min_kibana_version

doc["minimum_kibana_version"] = compute_min_kibana_version(binding_map or {})
```

Same change in `observability_migration/adapters/source/datadog/generate.py::generate_dashboard_yaml`. Pass `binding_map` from the caller (set up in Task 16).

Also record `version_floor_reason` in the migration report:

```python
def _floor_reason(binding_map):
    multi_names = [n for n, b in binding_map.items()
                   if isinstance(b, AcceptedBinding) and b.multi]
    if not multi_names:
        return None
    return f"multi_value_binding({multi_names[0]})"
```

Surface that under each dashboard's report entry alongside `minimum_kibana_version`.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_migrate.py tests/test_datadog_migrate.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add observability_migration/adapters/source/grafana/panels.py observability_migration/adapters/source/datadog/generate.py tests/
git commit -m "Per-dashboard minimum_kibana_version derives from accepted bindings."
```

---

### Task 14: Pre-upload Kibana version guard

**Files:**
- Modify: `observability_migration/targets/kibana/serverless.py` (add `assert_min_kibana_version`)
- Modify: `tests/targets/test_serverless.py`

- [ ] **Step 1: Failing tests**

```python
# Append to tests/targets/test_serverless.py
from unittest.mock import patch
from observability_migration.targets.kibana import serverless


@patch("observability_migration.targets.kibana.serverless._session")
def test_assert_min_kibana_version_passes_when_cluster_meets_floor(session):
    session.return_value.get.return_value.json.return_value = {"version": {"number": "9.3.5"}}
    session.return_value.get.return_value.raise_for_status.return_value = None
    serverless.assert_min_kibana_version(
        kibana_url="https://k.example", api_key="x", required="9.3.0",
    )


@patch("observability_migration.targets.kibana.serverless._session")
def test_assert_min_kibana_version_raises_when_cluster_below_floor(session):
    import pytest
    session.return_value.get.return_value.json.return_value = {"version": {"number": "9.1.5"}}
    session.return_value.get.return_value.raise_for_status.return_value = None
    with pytest.raises(serverless.KibanaVersionTooLowError):
        serverless.assert_min_kibana_version(
            kibana_url="https://k.example", api_key="x", required="9.3.0",
        )
```

- [ ] **Step 2: Run, verify failure**

Run: `.venv/bin/pytest tests/targets/test_serverless.py -v`

- [ ] **Step 3: Implement**

```python
# Append to observability_migration/targets/kibana/serverless.py

class KibanaVersionTooLowError(RuntimeError):
    pass


def _parse_version(s: str) -> tuple[int, ...]:
    return tuple(int(p) for p in s.split("."))


def assert_min_kibana_version(*, kibana_url: str, api_key: str, required: str) -> None:
    base = _api_base(kibana_url)
    response = _session().get(
        f"{base}/api/status",
        headers={"Authorization": f"ApiKey {api_key}"},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
    version_str = (data.get("version") or {}).get("number") or ""
    if not version_str:
        raise KibanaVersionTooLowError(
            f"could not determine Kibana version from /api/status response"
        )
    if _parse_version(version_str) < _parse_version(required):
        raise KibanaVersionTooLowError(
            f"Kibana cluster is {version_str}; dashboard requires {required}"
        )
```

Wire the guard into the upload path: in whichever function the unified `upload-dashboards` CLI uses, call `assert_min_kibana_version` once per dashboard YAML before posting NDJSON.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/targets/test_serverless.py -v`

- [ ] **Step 5: Commit**

```bash
git add observability_migration/targets/kibana/serverless.py tests/targets/test_serverless.py
git commit -m "Block dashboard upload when Kibana cluster version is below the required floor."
```

---

### Task 15: Warning catalog with structured IDs

**Files:**
- Create: `observability_migration/core/variable_warnings.py` — emit helpers
- Modify: `observability_migration/adapters/source/grafana/promql.py`, `panels.py`, `translate.py` — replace string warnings with structured IDs.
- Modify: `observability_migration/adapters/source/datadog/translate.py`
- Modify: `docs/sources/grafana-trace.tpl.md`, `docs/sources/datadog-trace.tpl.md` — aggregator switches to ID grouping
- Create: `tests/test_variable_warnings.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_variable_warnings.py
from observability_migration.core import variable_warnings as vw


def test_render_bound_warning():
    msg = vw.render("variable.bound", var="instance", field="service.instance.id", kind="single_value")
    assert msg == "filter applied via ES|QL parameter ?instance (field=service.instance.id, kind=single_value)"


def test_render_unbound_classic_only():
    msg = vw.render(
        "variable.unbound.classic_only", var="instance", reason="include_all_unsupported"
    )
    assert "include_all_unsupported" in msg
    assert "classic control still applies" in msg


def test_render_dropped():
    msg = vw.render("variable.unbound.dropped", var="x", reason="regex_template")
    assert "no equivalent filter applied" in msg


def test_render_verifier_downgraded():
    msg = vw.render(
        "variable.verifier_downgraded", var="x", invariant="leftover_token"
    )
    assert "downgraded post-translation" in msg


def test_render_unknown_id_raises():
    import pytest
    with pytest.raises(KeyError):
        vw.render("variable.bogus", var="x")
```

- [ ] **Step 2: Run, verify failure**

Run: `.venv/bin/pytest tests/test_variable_warnings.py -v`

- [ ] **Step 3: Implement**

```python
# observability_migration/core/variable_warnings.py
"""Structured warning catalog for variable-control translation."""
from __future__ import annotations
from typing import Final

WARNING_TEMPLATES: Final[dict[str, str]] = {
    "variable.bound":
        "filter applied via ES|QL parameter ?{var} (field={field}, kind={kind})",
    "variable.unbound.classic_only":
        "variable {var} not bound to translated ES|QL panel queries (reason: "
        "{reason}); the dashboard's classic control still applies to any "
        "KQL/Lens panels added manually",
    "variable.unbound.dropped":
        "variable {var} dropped during translation (reason: {reason}); "
        "no equivalent filter applied",
    "variable.verifier_downgraded":
        "variable {var} accepted by classifier but downgraded post-translation "
        "(verifier failure: {invariant}); falling back to classic control",
}


def render(warning_id: str, **kwargs) -> str:
    template = WARNING_TEMPLATES[warning_id]
    return template.format(**kwargs)
```

Then walk every existing literal string `"Variable-driven label filters applied via Kibana dashboard controls"` and `"Dropped variable-driven label filters during migration"` (and the LogQL variants) in `promql.py`, `panels.py`, `translate.py`, and replace each with a call to `variable_warnings.render(...)`. Each call site gets enough context to populate `{var}` and `{reason}` (or use a default `unknown` if the call site has no specific reason).

Update the `docs/sources/grafana-trace.tpl.md` aggregator script (a small Python helper) to group by the structured `id` instead of by message text.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_variable_warnings.py tests/test_migrate.py tests/test_datadog_migrate.py -v`

- [ ] **Step 5: Commit**

```bash
git add observability_migration/core/variable_warnings.py observability_migration/adapters tests/ docs/sources/
git commit -m "Switch variable-control warnings to a structured-id catalog."
```

---

### Task 16: Smoke fixtures + live-Kibana smoke test

**Files:**
- Create: `tests/fixtures/variable_controls/grafana_single_value.json`
- Create: `tests/fixtures/variable_controls/grafana_multi_value.json`
- Create: `tests/fixtures/variable_controls/datadog_single_value.json`
- Create: `tests/fixtures/variable_controls/datadog_multi_value.json`
- Create: `tests/e2e/test_variable_controls_smoke.py`
- Create: `.github/workflows/live-kibana-smoke.yml`
- Modify: `pyproject.toml`

- [ ] **Step 1: Author the four fixtures**

Each fixture is a minimal source dashboard with exactly one accepted variable and 2 panels referencing it. Concrete fixture content:

```json
// tests/fixtures/variable_controls/grafana_single_value.json
{
  "title": "Smoke Single",
  "uid": "smoke-grafana-single",
  "templating": {
    "list": [
      {
        "name": "instance",
        "label": "Instance",
        "type": "query",
        "definition": "label_values(up, instance)",
        "multi": false,
        "includeAll": false
      }
    ]
  },
  "panels": [
    {
      "type": "timeseries",
      "title": "Panel A",
      "datasource": {"type": "prometheus", "uid": "x"},
      "targets": [{"refId": "A", "expr": "rate(http_requests_total{instance=\"$instance\"}[5m])"}],
      "gridPos": {"x": 0, "y": 0, "w": 24, "h": 12}
    },
    {
      "type": "timeseries",
      "title": "Panel B",
      "datasource": {"type": "prometheus", "uid": "x"},
      "targets": [{"refId": "A", "expr": "sum(rate(http_errors_total{instance=\"$instance\"}[5m]))"}],
      "gridPos": {"x": 0, "y": 12, "w": 24, "h": 12}
    }
  ]
}
```

`grafana_multi_value.json`: same shape but `"multi": true` and `instance=~"$instance"`.

For Datadog fixtures, mirror the official Datadog dashboard JSON shape with one `template_variables` entry and two `widgets` referencing it. Use the existing `infra/datadog/dashboards/*.json` files as a structural reference.

- [ ] **Step 2: Author the smoke test module**

```python
# tests/e2e/test_variable_controls_smoke.py
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

import pytest


REQUIRED_ENV = "OBS_MIGRATION_LIVE_KIBANA_REQUIRED"
URL = "KIBANA_URL"
KEY = "KIBANA_API_KEY"


def _creds_or_skip():
    if os.environ.get(REQUIRED_ENV) == "1":
        for k in (URL, KEY):
            if not os.environ.get(k):
                pytest.fail(f"live Kibana smoke required {k}; not set in CI")
    if not os.environ.get(URL) or not os.environ.get(KEY):
        pytest.skip("live-Kibana smoke skipped: KIBANA_URL/KIBANA_API_KEY not set")


@pytest.mark.live_kibana
class GrafanaVariableControlsSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _creds_or_skip()
        cls.tmpdir = Path(tempfile.mkdtemp())
        cls.title_prefix = f"obs-migrate-smoke-{uuid.uuid4()}"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _translate_to_yaml(self, fixture_name: str) -> Path:
        # Run the grafana CLI on the single-fixture directory.
        fixture_dir = Path("tests/fixtures/variable_controls/_one") / fixture_name
        out = self.tmpdir / fixture_name
        out.mkdir(exist_ok=True, parents=True)
        subprocess.run(
            [".venv/bin/python", "-m",
             "observability_migration.adapters.source.grafana.cli",
             "--source", "files", "--input-dir", str(fixture_dir),
             "--output-dir", str(out), "--assets", "dashboards",
             "--field-profile", "otel"],
            check=True,
        )
        yaml_files = list((out / "dashboards" / "yaml").glob("*.yaml"))
        self.assertEqual(len(yaml_files), 1)
        return yaml_files[0]

    def _compile(self, yaml_path: Path) -> Path:
        ndjson = yaml_path.with_suffix(".ndjson")
        subprocess.run(
            ["uvx", "kb-dashboard-cli", "compile", str(yaml_path), str(ndjson)],
            check=True,
        )
        return ndjson

    def _assert_ndjson_shape(self, ndjson: Path, *, multi: bool):
        records = [json.loads(line) for line in ndjson.read_text().splitlines() if line]
        # At least one ESQL control with the expected shape
        # Pseudo-XPath: scan attributes for type=="esql"
        body = json.dumps(records)
        self.assertIn("\"type\": \"esql\"", body)
        if multi:
            self.assertIn("MV_CONTAINS(?instance", body)
        else:
            self.assertIn("?instance", body)
        self.assertNotIn("$instance", body)

    def _upload(self, ndjson: Path) -> str:
        from observability_migration.targets.kibana.serverless import import_saved_objects
        result = import_saved_objects(
            kibana_url=os.environ[URL],
            api_key=os.environ[KEY],
            ndjson_path=ndjson,
            overwrite=True,
        )
        ids = [obj["id"] for obj in result["successResults"] if obj.get("type") == "dashboard"]
        self.assertEqual(len(ids), 1)
        return ids[0]

    def _assert_saved_object(self, dashboard_id: str, *, multi: bool):
        # GET /api/saved_objects/dashboard/<id>
        from observability_migration.targets.kibana.serverless import _session, _api_base
        url = f"{_api_base(os.environ[URL])}/api/saved_objects/dashboard/{dashboard_id}"
        r = _session().get(url, headers={"Authorization": f"ApiKey {os.environ[KEY]}"}, timeout=30)
        r.raise_for_status()
        body = json.dumps(r.json())
        self.assertIn("\"type\":\"esql\"", body)
        self.assertIn("?instance", body)
        if multi:
            self.assertIn("MV_CONTAINS", body)

    def _delete(self, dashboard_id: str):
        from observability_migration.targets.kibana.serverless import _session, _api_base
        url = f"{_api_base(os.environ[URL])}/api/saved_objects/dashboard/{dashboard_id}"
        _session().delete(
            url,
            headers={"Authorization": f"ApiKey {os.environ[KEY]}", "kbn-xsrf": "true"},
            timeout=30,
        )

    def _round_trip(self, fixture: str, *, multi: bool):
        yaml_path = self._translate_to_yaml(fixture)
        ndjson = self._compile(yaml_path)
        self._assert_ndjson_shape(ndjson, multi=multi)
        dashboard_id = self._upload(ndjson)
        try:
            self._assert_saved_object(dashboard_id, multi=multi)
        finally:
            self._delete(dashboard_id)

    def test_grafana_single_value(self):
        self._round_trip("grafana_single_value.json", multi=False)

    def test_grafana_multi_value(self):
        self._round_trip("grafana_multi_value.json", multi=True)

    def test_datadog_single_value(self):
        self._round_trip("datadog_single_value.json", multi=False)

    def test_datadog_multi_value(self):
        self._round_trip("datadog_multi_value.json", multi=True)
```

- [ ] **Step 3: Register the pytest mark**

```toml
# pyproject.toml — add inside [tool.pytest.ini_options] or wherever marks are configured
markers = [
    "live_kibana: requires live Kibana credentials (KIBANA_URL, KIBANA_API_KEY)",
]
```

- [ ] **Step 4: Add the CI workflow**

```yaml
# .github/workflows/live-kibana-smoke.yml
name: live-kibana-smoke
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
jobs:
  smoke:
    runs-on: ubuntu-latest
    needs: [unit-tests]
    env:
      OBS_MIGRATION_LIVE_KIBANA_REQUIRED: "1"
      KIBANA_URL: ${{ secrets.SMOKE_KIBANA_URL }}
      KIBANA_API_KEY: ${{ secrets.SMOKE_KIBANA_API_KEY }}
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -e . pytest pytest-xdist
      - run: pip install uv
      - run: uvx --from kb-dashboard-cli kb-dashboard-cli --version
      - run: pytest -m live_kibana -v tests/e2e/test_variable_controls_smoke.py
```

- [ ] **Step 5: Run locally without creds (skipped) and with creds (pass)**

Run: `.venv/bin/pytest -m live_kibana tests/e2e/ -v` → expected `4 skipped` (no creds locally).

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/variable_controls/ tests/e2e/test_variable_controls_smoke.py .github/workflows/live-kibana-smoke.yml pyproject.toml
git commit -m "Add live-Kibana smoke fixtures and round-trip test."
```

---

### Task 17: Hermetic integration tests over real corpus

**Files:**
- Create: `tests/test_variable_controls_integration.py`
- Create: `tests/fixtures/regression/grafana_corpus_phase_b.json`

- [ ] **Step 1: Write tests**

```python
# tests/test_variable_controls_integration.py
from pathlib import Path
import json
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


def _migrate(fixture_dir: Path, out_dir: Path):
    subprocess.run(
        [".venv/bin/python", "-m",
         "observability_migration.adapters.source.grafana.cli",
         "--source", "files", "--input-dir", str(fixture_dir),
         "--output-dir", str(out_dir), "--assets", "dashboards",
         "--field-profile", "otel", "--native-promql"],
        cwd=str(REPO), check=True,
    )


class GrafanaCorpusPhaseB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        _migrate(REPO / "infra" / "grafana" / "dashboards", cls.tmp)
        cls.report = json.loads((cls.tmp / "migration_report.json").read_text())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_no_leftover_source_token_for_accepted_vars(self):
        for dashboard in self.report["dashboards"]:
            yaml_path = next((self.tmp / "dashboards" / "yaml").glob("*.yaml"))
            text = yaml_path.read_text()
            for accepted in dashboard["variables"]["accepted"]:
                self.assertNotRegex(text, rf"\${accepted['name']}\b")

    def test_minimum_kibana_version_matches_bindings(self):
        from observability_migration.core.variable_classifier import compute_min_kibana_version
        # Reconstruct a synthetic binding map for each dashboard from report,
        # then assert the YAML's minimum_kibana_version matches.
        ...

    def test_idempotent_byte_identical_yaml(self):
        second = Path(tempfile.mkdtemp())
        try:
            _migrate(REPO / "infra" / "grafana" / "dashboards", second)
            for f in (self.tmp / "dashboards" / "yaml").glob("*.yaml"):
                a = f.read_bytes()
                b = (second / "dashboards" / "yaml" / f.name).read_bytes()
                self.assertEqual(a, b, f"YAML drift in {f.name}")
        finally:
            shutil.rmtree(second, ignore_errors=True)

    def test_regression_baseline(self):
        baseline = json.loads(
            (REPO / "tests" / "fixtures" / "regression" / "grafana_corpus_phase_b.json").read_text()
        )
        actual = {d["dashboard"]: {
            "accepted": sorted(v["name"] for v in d["variables"]["accepted"]),
            "rejected": sorted(v["name"] for v in d["variables"]["rejected"]),
        } for d in self.report["dashboards"]}
        self.assertEqual(actual, baseline)
```

- [ ] **Step 2: Run after Task 18 activation**

`.venv/bin/pytest tests/test_variable_controls_integration.py -v`

- [ ] **Step 3: Generate baseline (one-time, after activation)**

After Task 18, generate the baseline:

```bash
.venv/bin/python -m observability_migration.adapters.source.grafana.cli \
  --source files --input-dir infra/grafana/dashboards \
  --output-dir /tmp/phase_b_baseline --assets dashboards \
  --field-profile otel --native-promql
.venv/bin/python -c "
import json, pathlib
report = json.loads(pathlib.Path('/tmp/phase_b_baseline/migration_report.json').read_text())
out = {d['dashboard']: {
    'accepted': sorted(v['name'] for v in d['variables']['accepted']),
    'rejected': sorted(v['name'] for v in d['variables']['rejected']),
} for d in report['dashboards']}
pathlib.Path('tests/fixtures/regression/grafana_corpus_phase_b.json').write_text(
    json.dumps(out, indent=2, sort_keys=True) + '\n'
)
"
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_variable_controls_integration.py tests/fixtures/regression/grafana_corpus_phase_b.json
git commit -m "Hermetic integration tests for variable-control phase B over real corpus."
```

---

### Task 18: Activation — wire classifier into translation pipeline

**This is the single revert-target commit** (per spec §13.2).

**Files:**
- Modify: `observability_migration/adapters/source/grafana/translate.py` (or wherever Grafana dashboards are translated end-to-end)
- Modify: `observability_migration/adapters/source/datadog/generate.py` (the `generate_dashboard_yaml` entry point and any caller chain)

- [ ] **Step 1: Wire Grafana classifier and verifier**

In `translate.py` (or the Grafana dashboard translator entry point), at the beginning of per-dashboard translation:

```python
from observability_migration.core.variable_classifier import classify_grafana_variables, compute_min_kibana_version
from observability_migration.core.variable_control_verifier import verify_bindings, PanelTranslationRecord

binding_map = classify_grafana_variables(
    variables=dashboard.get("templating", {}).get("list", []) or [],
    panels=_leaf_panels(dashboard.get("panels", [])),
    resolver=resolver,
    repeat_variable_names=repeat_variable_names,
    data_view=datasource_index,
    panel_data_view=lambda p: _resolve_panel_data_view(p, datasource_index),
)
```

Pass `binding_map` through to `_matcher_to_esql` (via `_selector_filters` and `_frag_filters`) and to `translate_variables`.

After translation, build a `PanelTranslationRecord` per panel and run:

```python
binding_map = verify_bindings(translation_records, binding_map)
```

If any binding was downgraded by the verifier, walk the affected panels and replace the parameterized fragment with the legacy fallback (this requires translation records to carry both fragments).

Compute `minimum_kibana_version`:

```python
doc["minimum_kibana_version"] = compute_min_kibana_version(binding_map)
```

Pass the binding map to the report serializer so it lands in `migration_report.json`.

- [ ] **Step 2: Wire Datadog classifier and verifier**

Same flow inside `generate_dashboard_yaml`:

```python
binding_map = classify_datadog_variables(
    variables=dashboard.template_variables,
    widgets=dashboard.widgets,
    field_map=field_map,
    data_view=data_view,
)
# pass to translate_widget through to _tag_filter_to_esql
# build records, run verify_bindings
# pass to _build_controls_from_template_vars
```

- [ ] **Step 3: Run the full test suite**

```bash
.venv/bin/pytest -q
```

Expected: all unit + hermetic integration tests pass; live-Kibana smoke tests skipped locally.

- [ ] **Step 4: Run full Grafana migration on the canonical corpus**

```bash
rm -rf /tmp/phase_b_act && mkdir -p /tmp/phase_b_act
.venv/bin/python -m observability_migration.adapters.source.grafana.cli \
  --source files --input-dir infra/grafana/dashboards \
  --output-dir /tmp/phase_b_act --assets dashboards \
  --field-profile otel --native-promql
```

Inspect `/tmp/phase_b_act/migration_report.json`: the `variables` block should be populated; `panel_parameterizations` non-empty; at least one dashboard should have a non-empty `accepted` list.

- [ ] **Step 5: Commit**

```bash
git add observability_migration/adapters/source/grafana/translate.py \
        observability_migration/adapters/source/datadog/generate.py
git commit -m "Activate variable-control classifier in Grafana and Datadog translation."
```

This is the commit that reverts cleanly to today's behavior if anything goes wrong.

---

### Task 19: Documentation

**Files:**
- Modify: `docs/architecture.md` — describe the new pipeline step.
- Modify: `docs/targets/kibana.md` — document the env-var rollback switch and the per-dashboard floor.
- Modify: `docs/sources/grafana.md`, `docs/sources/datadog.md` — note the variable-control behavior.
- Modify: `docs/targets/kibana-esql-upgrade-matrix.md` — update the lossy-translation summary.

- [ ] **Step 1: Update each doc with concrete prose**

For each doc, edit the relevant section to:

- Reference the spec at `docs/roadmap/2026-04-27-kibana-variable-controls-design.md`.
- Describe the classifier→verifier→emission pipeline in 1-2 paragraphs.
- Document `OBS_MIGRATION_DISABLE_VARIABLE_CONTROLS=1` in the troubleshooting/rollback section.
- Document the per-dashboard `minimum_kibana_version` lift.

- [ ] **Step 2: Regenerate trace docs**

```bash
.venv/bin/python scripts/render_pipeline_traces.py  # whatever the existing renderer is
```

- [ ] **Step 3: Run docs lint (if any)**

```bash
.venv/bin/pre-commit run --all-files
```

- [ ] **Step 4: Commit**

```bash
git add docs/
git commit -m "Document variable-control pipeline, env-var rollback, and version-floor lift."
```

---

## Self-review

### Spec coverage

| Spec section | Task |
|---|---|
| §3 decision log | Reflected throughout tasks 1–18 |
| §4 architecture | Tasks 1–6 + 18 (activation) |
| §5 classifier (Grafana + Datadog) | Tasks 1–5 + 7 |
| §5.4 closed reason enum | Task 1 |
| §5.5 options-query template | Task 3 |
| §6 matcher rewriting | Tasks 8 + 9 |
| §7 verifier | Task 6 |
| §8 YAML emission (single + multi) | Tasks 10 + 11 |
| §8.4 control ordering | Tasks 10 + 11 |
| §9 per-dashboard floor | Task 13 |
| §10 warning catalog | Task 15 |
| §10.3 migration-report shape | Task 12 |
| §11 phase-2 pre-requisites | Honored in tasks 6 + 18 (records + flags) |
| §12 test layers 1+2 | Tasks 1–17 collectively |
| §12 test layer 3 (smoke) | Task 16 |
| §12 CI wiring | Task 16 |
| §13.1 risks | Tasks 14 (version guard), 7 (env-var disable), 16 (smoke) |
| §13.2 rollback (single revert commit) | Task 18 is the one activation commit |
| §14 firm out-of-scope | Honored by classifier (no `??field`/`??function`/`TBUCKET` emission) |

No gaps.

### Placeholder scan

Tasks reviewed for "TBD/TODO/fill-in/handle edge cases" prose. Found:

- Task 13 has `...` ellipses where test bodies are described as "mirrors existing patterns". These are acceptable references (the engineer can find the existing pattern in `tests/test_migrate.py` lines 846-934 quoted in the spec), but for safety the engineer should treat the existing tests as the template and add a parallel test asserting `minimum_kibana_version`.
- Task 17 step "build synthetic binding map ..." has `...`. The engineer should use the report JSON to round-trip the binding map names (`accepted` array entries) and rebuild `AcceptedBinding`/`RejectedBinding` shells just for the floor function check.

These are intentional pointers, not blockers.

### Type consistency

- `binding_map` is the parameter name everywhere (Tasks 6/8/9/10/11/12/18).
- `compute_min_kibana_version`, `build_options_query`, `classify_grafana_variables`, `classify_datadog_variables`, `verify_bindings` — names match across tasks.
- `AcceptedBinding(field, multi, options_query)` / `RejectedBinding(reason)` — fields match across tasks.
- `PanelTranslationRecord(panel_id, compiled_esql, source_var_refs, observed_fields, observed_ops, data_view)` — used consistently in Tasks 6 and 18.
- Warning IDs: `variable.bound`, `variable.unbound.classic_only`, `variable.unbound.dropped`, `variable.verifier_downgraded` — consistent across spec §10.1 and Task 15.

No drift.
