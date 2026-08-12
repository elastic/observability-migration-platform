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

# Canonical Prometheus "info metric" naming convention (``_info``): a gauge
# whose value is always ``1``, published solely so its labels can be joined
# onto a real metric (``node_uname_info``, ``rabbitmq_identity_info``,
# ``kube_pod_info``, ...). A ``group_left``/``group_right`` join against one of
# these is pure label enrichment — multiplying by it never changes the primary
# metric's value — so it is safe to drop the join and aggregate the primary
# metric alone (issue #197).
DEFAULT_INFO_METRIC_SUFFIXES = [
    "_info",
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
    info_metric_suffixes: list = field(default_factory=lambda: list(DEFAULT_INFO_METRIC_SUFFIXES))
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
    # Legacy frozen default kept for direct ``translate_promql_to_esql`` callers /
    # offline unit fixtures. Dashboard panel translation overlays the adaptive
    # ``TBUCKET(75|20, ?_tstart, ?_tend)`` / ``BUCKET(@timestamp, 75|20, ...)``
    # forms via ``_rule_pack_for_panel`` (issue #316; rate panels use 20 —
    # docs/design/esql-time-bucketing-strategy.md).
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
    # Source metrics that a curated dashboard legitimately treats as optional at
    # runtime. When they are absent from the live target mapping, their series
    # is dropped from a multi-target panel without downgrading the whole panel.
    live_optional_metrics: list = field(default_factory=list)
    # Source metric name → MetricMapEntry (shared core). Empty by default.
    metric_map: dict = field(default_factory=dict)
    panel_type_overrides: dict = field(default_factory=dict)
    skip_panel_types: list = field(default_factory=list)
    panel_query_overrides: list = field(default_factory=list)
    panel_layout_overrides: list = field(default_factory=list)
    index_rewrites: list = field(default_factory=list)
    native_promql: bool = False
    runtime_features: dict = field(default_factory=dict)
    # Optional live native-PROMQL validator: a ``callable(query) -> (ok, error)``
    # the CLI attaches when ``--es-url`` is configured. When present, each built
    # native PROMQL query is probed against the target; a parse rejection
    # degrades that panel to ES|QL translation (see
    # panels._native_promql_query_survives_validation). None (the default)
    # preserves offline behavior.
    native_promql_validator: object = None
    # Per-run counters for the native live-validation gate, populated by the gate
    # when ``native_promql_validator`` is attached so the CLI can print an
    # observable summary line: how many native queries were CHECKED, how many
    # DEGRADED to ES|QL on a parse rejection, and how many were KEPT native.
    native_validation_stats: dict = field(
        default_factory=lambda: {"checked": 0, "degraded": 0, "kept": 0}
    )
    # Scalar settings explicitly present in a loaded rules file, even when the
    # chosen value equals the built-in default. This preserves "user pack wins"
    # semantics when a user intentionally resets a curated scalar back to the
    # default value.
    _explicit_scalar_fields: set[str] = field(default_factory=set, repr=False)

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
        raw_payload = _load_structured_file(path)
        payload = validate_rule_pack_payload(raw_payload, source=str(path))
        query_cfg = payload.query
        panel_cfg = payload.panel
        schema_cfg = payload.schema_config
        dashboard_cfg = payload.dashboard
        raw_query_cfg = raw_payload.get("query") if isinstance(raw_payload.get("query"), dict) else {}
        raw_dashboard_cfg = (
            raw_payload.get("dashboard") if isinstance(raw_payload.get("dashboard"), dict) else {}
        )

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
        for suffix in query_cfg.info_metric_suffixes:
            _append_unique(pack.info_metric_suffixes, suffix)
        for skip_type in panel_cfg.skip_types:
            _append_unique(pack.skip_panel_types, skip_type)

        pack.panel_type_overrides.update(panel_cfg.type_map)

        for override in panel_cfg.query_overrides:
            entry = {
                "title_match": override.title_match,
                "esql_query": override.esql_query,
                "status_override": override.status_override,
            }
            if override.kibana_type_override:
                entry["kibana_type_override"] = override.kibana_type_override
            pack.panel_query_overrides.append(entry)
        for override in panel_cfg.layout_overrides:
            pack.panel_layout_overrides.append({
                "title_match": override.title_match,
                "position": {
                    key: value
                    for key, value in {
                        "x": override.position.x,
                        "y": override.position.y,
                    }.items()
                    if value is not None
                },
                "size": {
                    key: value
                    for key, value in {
                        "w": override.size.w,
                        "h": override.size.h,
                    }.items()
                    if value is not None
                },
                "collapsed": override.collapsed,
            })

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
            if field_name in raw_query_cfg or field_name in raw_dashboard_cfg:
                pack._explicit_scalar_fields.add(field_name)
        pack.label_rewrites.update(query_cfg.label_rewrites)
        for metric_name, kind in query_cfg.metric_kinds.items():
            pack.metric_kinds[metric_name] = str(kind).strip().lower()
        for metric_name in query_cfg.live_optional_metrics:
            _append_unique(pack.live_optional_metrics, metric_name)
        from observability_migration.core.metric_mapping import normalize_metric_map

        pack.metric_map.update(normalize_metric_map(query_cfg.metric_map))
        _merge_mapping_lists(pack.label_candidates, query_cfg.label_candidates)
        _merge_mapping_lists(pack.label_candidates, schema_cfg.label_candidates)
        pack.control_field_overrides.update(payload.controls.field_overrides)
        for label_name in query_cfg.ignored_labels:
            _append_unique(pack.ignored_labels, label_name)
    return pack


def _curated_pack_dir() -> Path:
    from observability_migration.adapters.source.grafana import curated_packs as _cp_module
    return Path(_cp_module.__file__).parent


def _load_curated_pack_for(dashboard: dict[str, Any]) -> RulePackConfig | None:
    """Load the curated RulePackConfig for a dashboard, or None if not registered."""
    from observability_migration.adapters.source.grafana.curated_packs import find_curated_pack

    gnet_id = dashboard.get("gnetId")
    if gnet_id is not None:
        try:
            gnet_id = int(gnet_id)
        except (TypeError, ValueError):
            gnet_id = None

    title = str(dashboard.get("title") or "")
    tags = list(dashboard.get("tags") or [])

    entry = find_curated_pack(gnet_id=gnet_id, title=title, tags=tags)
    if entry is None:
        return None

    pack_dir = _curated_pack_dir() / str(entry["path"])
    pack_yaml = pack_dir / "pack.yaml"
    plugin_py = pack_dir / "plugin.py"

    pack = load_rule_pack_files([str(pack_yaml)] if pack_yaml.exists() else [])
    if plugin_py.exists():
        load_python_plugins([str(plugin_py)], pack)

    pack._curated_pack_name = str(entry.get("name") or "")
    return pack


def _merge_curated_into_base(curated: RulePackConfig, user: RulePackConfig) -> RulePackConfig:
    """Build a composed pack: curated as the base layer, user pack wins on collision."""
    import copy
    result = copy.deepcopy(curated)

    _defaults = RulePackConfig()

    explicit_scalar_fields = getattr(user, "_explicit_scalar_fields", set())
    # Scalars: user wins when explicitly set, even if set back to the built-in
    # default; otherwise use the "differs from default" heuristic.
    for field_name in (
        "default_rate_window", "default_gauge_agg", "ts_time_filter", "from_time_filter",
        "ts_bucket", "from_bucket", "logs_index", "metrics_dataset_filter",
        "logs_dataset_filter", "logs_message_field", "logs_timestamp_field", "logs_limit",
        "native_promql", "assume_tsds_gauges",
    ):
        user_val = getattr(user, field_name)
        default_val = getattr(_defaults, field_name)
        if field_name in explicit_scalar_fields or user_val != default_val:
            setattr(result, field_name, user_val)

    # Dicts: user keys win
    result.metric_kinds.update(user.metric_kinds)
    result.metric_map.update(user.metric_map)
    result.label_rewrites.update(user.label_rewrites)
    result.panel_type_overrides.update(user.panel_type_overrides)
    result.control_field_overrides.update(user.control_field_overrides)

    # panel_query_overrides: user overrides win by title_match
    user_override_titles = {o["title_match"] for o in user.panel_query_overrides}
    result.panel_query_overrides = [
        o for o in result.panel_query_overrides
        if o["title_match"] not in user_override_titles
    ]
    result.panel_query_overrides.extend(user.panel_query_overrides)

    # panel_layout_overrides: user overrides win by title_match
    user_layout_titles = {o["title_match"] for o in user.panel_layout_overrides}
    result.panel_layout_overrides = [
        o for o in result.panel_layout_overrides
        if o["title_match"] not in user_layout_titles
    ]
    result.panel_layout_overrides.extend(user.panel_layout_overrides)

    # Lists: append-unique; user entries take precedence by appearing first
    for item in user.not_feasible_patterns:
        if item not in result.not_feasible_patterns:
            result.not_feasible_patterns.append(item)
    for item in user.warning_patterns:
        if item not in result.warning_patterns:
            result.warning_patterns.append(item)
    for suffix in user.counter_suffixes:
        _append_unique(result.counter_suffixes, suffix)
    for suffix in user.info_metric_suffixes:
        _append_unique(result.info_metric_suffixes, suffix)
    for metric_name in user.live_optional_metrics:
        _append_unique(result.live_optional_metrics, metric_name)
    for skip_type in user.skip_panel_types:
        _append_unique(result.skip_panel_types, skip_type)

    # label_candidates: user values prepend (higher resolution priority)
    for label, candidates in user.label_candidates.items():
        bucket = result.label_candidates.setdefault(label, [])
        for c in reversed(candidates):
            if c not in bucket:
                bucket.insert(0, c)

    for item in user.ignored_labels:
        _append_unique(result.ignored_labels, item)
    for item in user.index_rewrites:
        if item not in result.index_rewrites:
            result.index_rewrites.append(item)

    # Runtime state: carry over from user pack (validator, stats, features)
    result.native_promql_validator = user.native_promql_validator
    result.native_validation_stats = user.native_validation_stats
    result.runtime_features = {**result.runtime_features, **user.runtime_features}
    result._explicit_scalar_fields = set(getattr(curated, "_explicit_scalar_fields", set())) | set(
        explicit_scalar_fields
    )

    # Propagate curated pack identity so callers can surface it in the manifest.
    result._curated_pack_name = getattr(curated, "_curated_pack_name", "")

    return result


def resolve_pack_for_dashboard(
    dashboard: dict[str, Any],
    base_pack: RulePackConfig,
    *,
    no_curated: bool = False,
) -> RulePackConfig:
    """Return a per-dashboard composed RulePackConfig.

    Resolution order (each layer wins over the prior):
      RulePackConfig defaults → curated pack → base_pack (user --rules-file)

    Returns base_pack unchanged (same object) when no curated pack matches
    or no_curated=True — zero cost for unregistered dashboards.
    """
    if no_curated:
        return base_pack

    curated = _load_curated_pack_for(dashboard)
    if curated is None:
        return base_pack

    return _merge_curated_into_base(curated, base_pack)


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
                "info_metric_suffixes": list(rule_pack.info_metric_suffixes),
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
    "resolve_pack_for_dashboard",
]
