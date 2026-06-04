# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Rule infrastructure, rule-pack loading, and plugin registration."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from observability_migration.core.extensions import (
    ExtensionCatalog,
    ExtensionRuleCard,
    ExtensionSurface,
)

from .extension_schema import validate_rule_pack_payload

DEFAULT_NOT_FEASIBLE_PATTERNS = [
    (r"\bsubquery\b", "Contains unsupported pattern: subquery"),
    (r"\boffset\b", "Contains unsupported pattern: offset"),
    (r"\bhistogram_quantile\s*\(", "histogram_quantile over Prometheus bucket series requires manual redesign"),
    (r"\b__name__\b", "PromQL metric-name introspection via __name__ requires manual redesign"),
]

DEFAULT_WARNING_PATTERNS = [
    (r"\bpredict_linear\b", "predict_linear has no ES|QL equivalent"),
]

# Canonical Prometheus counter naming conventions. ``_total`` (and the
# unit-qualified ``_seconds_total`` / ``_bytes_total``) plus ``_created`` are the
# explicit-counter spellings; ``_bucket`` / ``_count`` / ``_sum`` are the
# monotonic component series Prometheus emits for histograms and summaries.
# Treating the latter as counters lets rate()/irate()/increase() over them emit
# RATE/IRATE/INCREASE instead of the gauge fallback (AVG_OVER_TIME/MAX_OVER_TIME).
DEFAULT_COUNTER_SUFFIXES = [
    "_total",
    "_seconds_total",
    "_bytes_total",
    "_created",
    "_bucket",
    "_count",
    "_sum",
]


@dataclass
class PatternRule:
    pattern: str
    reason: str


@dataclass
class IndexRewriteRule:
    match: str
    replace: str


@dataclass(order=True)
class RegisteredRule:
    priority: int
    name: str = field(compare=False)
    fn: Callable[[Any], str | None] = field(compare=False)


class RuleRegistry:
    def __init__(self, name: str):
        self.name = name
        self._rules: list[RegisteredRule] = []

    def register(self, name: str | None = None, priority: int = 100):
        def decorator(fn):
            self.add(name or fn.__name__, fn, priority)
            return fn

        return decorator

    def add(self, name: str, fn: Callable[[Any], str | None], priority: int = 100):
        self._rules.append(RegisteredRule(priority=priority, name=name, fn=fn))
        self._rules.sort()
        return fn

    def apply(self, context, stop_when=None):
        for rule in self._rules:
            detail = rule.fn(context)
            if hasattr(context, "trace"):
                context.trace.append(
                    {
                        "stage": self.name,
                        "rule": rule.name,
                        "detail": detail or "",
                    }
                )
            if stop_when and stop_when(context, detail):
                break
        return context

    def describe(self):
        return [{"name": rule.name, "priority": rule.priority} for rule in self._rules]


def _pattern_rules(items):
    return [PatternRule(pattern=pattern, reason=reason) for pattern, reason in items]


@dataclass
class RulePackConfig:
    not_feasible_patterns: list = field(default_factory=lambda: _pattern_rules(DEFAULT_NOT_FEASIBLE_PATTERNS))
    warning_patterns: list = field(default_factory=lambda: _pattern_rules(DEFAULT_WARNING_PATTERNS))
    counter_suffixes: list = field(default_factory=lambda: list(DEFAULT_COUNTER_SUFFIXES))
    default_rate_window: str = "5m"
    default_gauge_agg: str = "AVG"
    # Migration default: target clusters we provision ingest metrics as TSDS, so when
    # we cannot prove a gauge field's TSDS status (offline / empty target / field not yet
    # in the mapping) we assume it IS a TSDS and emit ``TS`` for its aggregations. ``FROM``
    # over a multi-sample TSDS inflates non-idempotent aggregators (SUM/COUNT) by the
    # per-bucket sample count. Set False to target a known non-TSDS index (forces FROM).
    assume_tsds_gauges: bool = True
    ts_time_filter: str = "@timestamp >= ?_tstart AND @timestamp <= ?_tend"
    from_time_filter: str = "@timestamp >= ?_tstart AND @timestamp <= ?_tend"
    ts_bucket: str = "time_bucket = TBUCKET(5 minute)"
    from_bucket: str = "time_bucket = BUCKET(@timestamp, 50, ?_tstart, ?_tend)"
    logs_index: str = "logs-*"
    metrics_dataset_filter: str = "prometheus"
    logs_dataset_filter: str = ""
    logs_message_field: str = "message"
    logs_timestamp_field: str = "@timestamp"
    logs_limit: int = 200
    label_rewrites: dict = field(default_factory=dict)
    label_candidates: dict = field(default_factory=dict)
    ignored_labels: list = field(default_factory=lambda: [
        "origin_prometheus",
        # Prometheus scrape-target metadata labels — these describe the scrape
        # configuration (endpoint path, scheme) and are not stored as metric
        # dimensions in Elastic indices.  Translating them to WHERE clauses
        # produces "Unknown column" errors at validation time.
        "metrics_path",
        "__metrics_path__",
    ])
    control_field_overrides: dict = field(default_factory=dict)
    # Authoritative per-metric counter/gauge classification, keyed by metric name.
    # Overrides every inferred signal when seeding telemetry (see telemetry_contract).
    metric_kinds: dict = field(default_factory=dict)
    panel_type_overrides: dict = field(default_factory=dict)
    skip_panel_types: list = field(default_factory=list)
    index_rewrites: list = field(default_factory=list)
    native_promql: bool = False
    runtime_features: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.native_promql:
            self.metrics_dataset_filter = ""


QUERY_PREPROCESSORS = RuleRegistry("query_preprocessors")
QUERY_CLASSIFIERS = RuleRegistry("query_classifiers")
QUERY_TRANSLATORS = RuleRegistry("query_translators")
QUERY_POSTPROCESSORS = RuleRegistry("query_postprocessors")
QUERY_VALIDATORS = RuleRegistry("query_validators")
PANEL_TRANSLATORS = RuleRegistry("panel_translators")
VARIABLE_TRANSLATORS = RuleRegistry("variable_translators")


def _append_unique(items, value):
    if value and value not in items:
        items.append(value)


def _load_structured_file(path: Path):
    with open(path) as fh:
        if path.suffix.lower() == ".json":
            return json.load(fh)
        return yaml.safe_load(fh) or {}


def _load_pattern_entries(items):
    entries = []
    for item in items or []:
        if isinstance(item, dict) and item.get("pattern") and item.get("reason"):
            entries.append(PatternRule(pattern=item["pattern"], reason=item["reason"]))
    return entries


def _load_index_rewrites(items):
    rewrites = []
    for item in items or []:
        if isinstance(item, dict) and item.get("match") and item.get("replace"):
            rewrites.append(IndexRewriteRule(match=item["match"], replace=item["replace"]))
    return rewrites


def _merge_mapping_lists(target, source):
    for key, values in (source or {}).items():
        items = values if isinstance(values, list) else [values]
        bucket = target.setdefault(key, [])
        for item in items:
            if item not in bucket:
                bucket.append(item)


def load_rule_pack_files(paths: Sequence[str] | None) -> RulePackConfig:
    """Load optional declarative rule packs from YAML or JSON files."""
    pack = RulePackConfig()
    for raw_path in paths or []:
        path = Path(raw_path)
        payload = validate_rule_pack_payload(_load_structured_file(path), source=str(path))
        query_cfg = payload.query
        panel_cfg = payload.panel
        schema_cfg = payload.schema_config
        dashboard_cfg = payload.dashboard

        pack.not_feasible_patterns.extend(
            PatternRule(pattern=item.pattern, reason=item.reason)
            for item in query_cfg.not_feasible_patterns
        )
        pack.warning_patterns.extend(
            PatternRule(pattern=item.pattern, reason=item.reason)
            for item in query_cfg.warning_patterns
        )
        pack.index_rewrites.extend(
            IndexRewriteRule(match=item.match, replace=item.replace)
            for item in query_cfg.index_rewrites
        )

        for suffix in query_cfg.counter_suffixes:
            _append_unique(pack.counter_suffixes, suffix)
        for skip_type in panel_cfg.skip_types:
            _append_unique(pack.skip_panel_types, skip_type)

        pack.panel_type_overrides.update(panel_cfg.type_map)

        for field_name in (
            "default_rate_window",
            "default_gauge_agg",
            "ts_time_filter",
            "from_time_filter",
            "ts_bucket",
            "from_bucket",
            "logs_index",
            "metrics_dataset_filter",
            "logs_dataset_filter",
            "logs_message_field",
            "logs_timestamp_field",
            "logs_limit",
        ):
            query_value = getattr(query_cfg, field_name)
            dashboard_value = getattr(dashboard_cfg, field_name)
            if query_value not in (None, "", []):
                setattr(pack, field_name, query_value)
            elif dashboard_value not in (None, "", []):
                setattr(pack, field_name, dashboard_value)
        pack.label_rewrites.update(query_cfg.label_rewrites)
        for metric_name, kind in query_cfg.metric_kinds.items():
            pack.metric_kinds[metric_name] = str(kind).strip().lower()
        _merge_mapping_lists(pack.label_candidates, query_cfg.label_candidates)
        _merge_mapping_lists(pack.label_candidates, schema_cfg.label_candidates)
        pack.control_field_overrides.update(payload.controls.field_overrides)
        for label_name in query_cfg.ignored_labels:
            _append_unique(pack.ignored_labels, label_name)
    return pack


def build_rule_catalog(rule_pack: RulePackConfig) -> dict[str, Any]:
    registries = {
        "query_preprocessors": QUERY_PREPROCESSORS,
        "query_classifiers": QUERY_CLASSIFIERS,
        "query_translators": QUERY_TRANSLATORS,
        "query_postprocessors": QUERY_POSTPROCESSORS,
        "query_validators": QUERY_VALIDATORS,
        "panel_translators": PANEL_TRANSLATORS,
        "variable_translators": VARIABLE_TRANSLATORS,
    }
    stage_map = {
        "query_preprocessors": "preprocess",
        "query_classifiers": "classify",
        "query_translators": "translate",
        "query_postprocessors": "postprocess",
        "query_validators": "validate",
        "panel_translators": "panel",
        "variable_translators": "variable",
    }
    rule_cards = []
    for registry_name, registry in registries.items():
        for rule in registry.describe():
            rule_cards.append(
                ExtensionRuleCard(
                    id=f"grafana.{registry_name}.{rule['name']}",
                    stage=stage_map.get(registry_name, registry_name),
                    summary=f"{registry_name.replace('_', ' ')} rule `{rule['name']}`",
                    registry=registry_name,
                    priority=rule["priority"],
                    extenders=["rules_file", "python_plugin"],
                )
            )

    catalog = ExtensionCatalog(
        adapter="grafana",
        summary=(
            "Grafana exposes registry-driven query, panel, and variable extension "
            "points backed by declarative rule packs and Python plugins."
        ),
        stages=[
            "preprocess",
            "classify",
            "translate",
            "postprocess",
            "validate",
            "panel",
            "variable",
        ],
        current_surfaces=[
            ExtensionSurface(
                id="grafana.rule_pack",
                kind="declarative",
                summary="YAML or JSON rule packs extend mappings, warnings, and panel behavior.",
                entrypoint="--rules-file",
                format="yaml_or_json",
                example_path="examples/rule-pack.example.yaml",
            ),
            ExtensionSurface(
                id="grafana.plugin",
                kind="python_plugin",
                summary="Python plugins can register new rules into the Grafana registries.",
                entrypoint="register(api)",
                format="python",
                example_path="examples/plugin_example.py",
            ),
        ],
        rules=rule_cards,
        template=build_rule_pack_template(),
        metadata={
            "registries": {name: registry.describe() for name, registry in registries.items()},
            "rule_pack": {
                "counter_suffixes": list(rule_pack.counter_suffixes),
                "label_rewrites": dict(rule_pack.label_rewrites),
                "label_candidates": dict(rule_pack.label_candidates),
                "ignored_labels": list(rule_pack.ignored_labels),
                "panel_type_overrides": dict(rule_pack.panel_type_overrides),
                "skip_panel_types": list(rule_pack.skip_panel_types),
            },
        },
    )
    return catalog.to_dict()


def build_rule_pack_template() -> dict[str, Any]:
    return {
        "query": {
            "default_rate_window": "5m",
            "default_gauge_agg": "AVG",
            "logs_index": "logs-*",
            "label_rewrites": {},
            "label_candidates": {},
            "ignored_labels": ["origin_prometheus"],
        },
        "panel": {
            "type_map": {},
            "skip_types": [],
        },
        "controls": {
            "field_overrides": {},
        },
        "schema": {
            "label_candidates": {},
        },
    }


def load_python_plugins(paths, rule_pack):
    """Load optional Python plugins that register additional migration rules."""
    from .panels import PANEL_TYPE_MAP, PanelContext, VariableContext
    from .promql import (
        AGG_FUNCTION_MAP,
        OUTER_AGG_MAP,
        FormulaPlan,
        MeasureSpec,
        PromQLFragment,
        _build_formula_plan,
        _build_measure_spec,
    )
    from .schema import SchemaResolver
    from .translate import TranslationContext

    api = {
        "rule_pack": rule_pack,
        "rule_pack_cls": RulePackConfig,
        "translation_context_cls": TranslationContext,
        "panel_context_cls": PanelContext,
        "variable_context_cls": VariableContext,
        "query_preprocessors": QUERY_PREPROCESSORS,
        "query_classifiers": QUERY_CLASSIFIERS,
        "query_translators": QUERY_TRANSLATORS,
        "query_postprocessors": QUERY_POSTPROCESSORS,
        "query_validators": QUERY_VALIDATORS,
        "panel_translators": PANEL_TRANSLATORS,
        "variable_translators": VARIABLE_TRANSLATORS,
        "agg_function_map": AGG_FUNCTION_MAP,
        "outer_agg_map": OUTER_AGG_MAP,
        "panel_type_map": PANEL_TYPE_MAP,
        "schema_resolver_cls": SchemaResolver,
        "fragment_cls": PromQLFragment,
        "measure_spec_cls": MeasureSpec,
        "formula_plan_cls": FormulaPlan,
        "build_measure_spec": _build_measure_spec,
        "build_formula_plan": _build_formula_plan,
        "build_rule_catalog": build_rule_catalog,
        "append_unique": _append_unique,
    }
    for idx, raw_path in enumerate(paths or []):
        path = Path(raw_path)
        spec = importlib.util.spec_from_file_location(f"migration_plugin_{idx}_{path.stem}", path)
        if spec is None or spec.loader is None:
            raise ValueError(f"Could not load plugin from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not hasattr(module, "register"):
            raise ValueError(f"Plugin {path} must define register(api)")
        module.register(api)


__all__ = [
    "DEFAULT_COUNTER_SUFFIXES",
    "DEFAULT_NOT_FEASIBLE_PATTERNS",
    "DEFAULT_WARNING_PATTERNS",
    "PANEL_TRANSLATORS",
    "QUERY_CLASSIFIERS",
    "QUERY_POSTPROCESSORS",
    "QUERY_PREPROCESSORS",
    "QUERY_TRANSLATORS",
    "QUERY_VALIDATORS",
    "VARIABLE_TRANSLATORS",
    "IndexRewriteRule",
    "PatternRule",
    "RegisteredRule",
    "RulePackConfig",
    "RuleRegistry",
    "_append_unique",
    "_load_index_rewrites",
    "_load_pattern_entries",
    "_load_structured_file",
    "_merge_mapping_lists",
    "_pattern_rules",
    "build_rule_catalog",
    "build_rule_pack_template",
    "load_python_plugins",
    "load_rule_pack_files",
]
