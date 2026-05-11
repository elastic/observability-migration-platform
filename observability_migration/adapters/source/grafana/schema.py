# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Schema discovery and label resolution helpers."""

from __future__ import annotations

import re

import requests

from observability_migration.core.verification.field_capabilities import (
    field_capability_from_es_field_caps,
    has_conflicting_types,
    infer_type_family,
    is_aggregatable_field,
    is_counter_metric_field,
    is_numeric_field,
    is_searchable_field,
    is_text_like_field,
)


class SchemaResolver:
    """Resolves Prometheus labels to target Elasticsearch field names.

    Resolution order:
    1. RulePackConfig label_rewrites (user overrides via rule-pack files)
    2. Online discovery via ES _field_caps API (when available)
    3. Built-in Prometheus→OTel candidate mappings (offline fallback)
    4. Pass-through (use label as-is)
    """

    PROM_TO_OTEL_CANDIDATES = {
        "instance": ["service.instance.id", "host.name", "host.ip"],
        "service_instance_id": ["service.instance.id"],
        "job": ["service.name"],
        "service_name": ["service.name"],
        "namespace": ["k8s.namespace.name"],
        "namespace_name": ["k8s.namespace.name"],
        "pod": ["k8s.pod.name"],
        "pod_name": ["k8s.pod.name"],
        "container": ["k8s.container.name", "container.name"],
        "container_name": ["k8s.container.name", "container.name"],
        "node": ["k8s.node.name", "host.name"],
        "node_name": ["k8s.node.name", "host.name"],
        "cluster": ["k8s.cluster.name", "orchestrator.cluster.name"],
        "cluster_name": ["k8s.cluster.name", "orchestrator.cluster.name"],
        "region": ["cloud.region"],
        "datacenter": ["cloud.region"],
        "availability_zone": ["cloud.availability_zone"],
        "zone": ["cloud.availability_zone"],
        "deployment": ["k8s.deployment.name"],
        "daemonset": ["k8s.daemonset.name"],
        "replicaset": ["k8s.replicaset.name"],
        "statefulset": ["k8s.statefulset.name"],
        "cronjob": ["k8s.cronjob.name"],
        "job_name": ["k8s.job.name", "service.name"],
        "hostname": ["host.name", "nodename"],
        "nodename": ["nodename", "host.name"],
        "device": ["device"],
        "interface": ["device"],
        "mountpoint": ["mountpoint"],
        "fstype": ["fstype"],
        "cpu": ["cpu"],
        "mode": ["mode"],
    }

    _PROMETHEUS_LABEL_RE = re.compile(r"^prometheus\.labels\.[A-Za-z_][A-Za-z0-9_]*$")
    _PROMETHEUS_METRIC_LEAF_RE = re.compile(r"^prometheus\.[A-Za-z_][A-Za-z0-9_]*\.(counter|value)$")

    def __init__(self, rule_pack, es_url=None, index_pattern=None, es_api_key=None):
        self._rule_pack = rule_pack
        self._es_url = es_url
        self._index_pattern = index_pattern or "metrics-*"
        self._es_api_key = es_api_key
        self._field_cache = None
        self._discovered_mappings = {}
        self._discovery_attempted = False
        self._concrete_index_cache = None
        self._schema_profile = None
        self._schema_profile_cache_id = None

    def _candidate_fields(self, label):
        candidates = []
        for source in (
            self._rule_pack.label_candidates.get(label, []),
            self.PROM_TO_OTEL_CANDIDATES.get(label, []),
        ):
            for field_name in source:
                if field_name not in candidates:
                    candidates.append(field_name)
        return candidates

    def _es_headers(self):
        headers = {}
        if self._es_api_key:
            headers["Authorization"] = f"ApiKey {self._es_api_key}"
        return headers

    def _discover_fields(self):
        if self._discovery_attempted:
            return
        self._discovery_attempted = True
        self._field_cache = {}
        if not self._es_url:
            return
        try:
            resp = requests.get(
                f"{self._es_url}/{self._index_pattern}/_field_caps",
                params={"fields": "*"},
                headers=self._es_headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                self._field_cache = resp.json().get("fields", {})
                self._build_discovered_mappings()
        except Exception:
            pass

    def _current_schema_profile(self):
        """Return the schema profile for the current `_field_cache`.

        Detection runs lazily and re-runs whenever the cache identity changes,
        so callers that seed `_field_cache` directly (e.g. tests) still get a
        correct profile without having to invoke detection manually.
        """
        cache = self._field_cache
        if not cache:
            return None
        cache_id = id(cache)
        if self._schema_profile_cache_id != cache_id:
            self._schema_profile = self._compute_schema_profile(cache)
            self._schema_profile_cache_id = cache_id
        return self._schema_profile

    @classmethod
    def _compute_schema_profile(cls, field_cache):
        """Identify well-known target layouts from `field_cache`.

        Currently recognizes the Elastic Fleet `prometheus.remote_write` data
        stream layout, where labels live under `prometheus.labels.<name>` and
        metrics under `prometheus.<metric>.{counter,value}`.
        """
        has_prom_label = False
        has_prom_metric_leaf = False
        for field_name in field_cache:
            if not has_prom_label and cls._PROMETHEUS_LABEL_RE.match(field_name):
                has_prom_label = True
            if not has_prom_metric_leaf and cls._PROMETHEUS_METRIC_LEAF_RE.match(field_name):
                has_prom_metric_leaf = True
            if has_prom_label and has_prom_metric_leaf:
                return "prometheus_remote_write"
        return None

    def schema_profile(self):
        """Return the detected schema profile identifier, or `None`.

        Triggers field discovery on first access so callers don't need to
        sequence `_discover_fields()` manually.
        """
        self._discover_fields()
        return self._current_schema_profile()

    def _build_discovered_mappings(self):
        known_fields = set((self._field_cache or {}).keys())
        for prom_label in set(self.PROM_TO_OTEL_CANDIDATES) | set(self._rule_pack.label_candidates):
            if prom_label in self._rule_pack.label_rewrites:
                continue
            for otel_field in self._candidate_fields(prom_label):
                if otel_field in known_fields:
                    self._discovered_mappings[prom_label] = otel_field
                    break

    def _discover_concrete_indexes(self):
        if self._concrete_index_cache is not None:
            return
        self._concrete_index_cache = []
        if not self._es_url:
            return
        if not any(token in self._index_pattern for token in ("*", "?", ",")):
            self._concrete_index_cache = [self._index_pattern]
            return
        try:
            resp = requests.get(
                f"{self._es_url}/_resolve/index/{self._index_pattern}",
                headers=self._es_headers(),
                timeout=10,
            )
            if resp.status_code != 200:
                return
            body = resp.json()
            discovered = []
            for bucket in ("data_streams", "indices"):
                for entry in body.get(bucket, []) or []:
                    name = entry.get("name")
                    if name and name not in discovered:
                        discovered.append(name)
            self._concrete_index_cache = discovered
        except Exception:
            pass

    def resolve_label(self, label):
        if label in self._rule_pack.ignored_labels:
            return None
        if label in self._rule_pack.label_rewrites:
            return self._rule_pack.label_rewrites[label]
        self._discover_fields()
        # Source-faithful: if the target advertises the original label as a real
        # field, use it as-is. This keeps PromQL semantics intact when the target
        # has both Prometheus and OTEL aliases (common on dual-shipping clusters).
        if self._field_cache and label in self._field_cache:
            return label
        # Fleet `prometheus.remote_write` data streams store the original
        # Prometheus label `<name>` under `prometheus.labels.<name>`. When that
        # profile is active and the namespaced field exists, prefer it over
        # the OTEL candidates below — the namespaced form is the actual stored
        # field and OTEL fields are not present at all in this layout.
        if self._current_schema_profile() == "prometheus_remote_write":
            namespaced = f"prometheus.labels.{label}"
            if namespaced in self._field_cache:
                return namespaced
        # Otherwise, fall back to OTEL/Prometheus normalization candidates.
        if label in self._discovered_mappings:
            return self._discovered_mappings[label]
        candidates = self._candidate_fields(label)
        if candidates:
            return candidates[0]
        return label

    def resolve_metric_field(self, metric_name, *, prefer=None):
        """Resolve a PromQL metric name to its actual stored field.

        For most layouts this is a passthrough (the metric name is the field
        name). For the Fleet `prometheus.remote_write` layout, metrics are
        stored as `prometheus.<metric>.{counter,value,rate}`; this method
        picks the suffix matching the metric's role.

        The ``prefer`` keyword controls suffix priority:
        - ``"counter"``: counter → rate → value (for RATE/IRATE/INCREASE)
        - ``"rate"``: rate → counter → value (when a precomputed rate field exists)
        - ``"gauge"`` or ``None``: value → counter → rate (default)

        When the profile is active but no matching field exists in the cache,
        returns the expected default-layout name `prometheus.<metric>.value`
        so the contract layer can surface the missing field via preflight.
        """
        self._discover_fields()
        if self._current_schema_profile() != "prometheus_remote_write":
            return metric_name
        if self._field_cache and metric_name in self._field_cache:
            return metric_name
        if prefer == "counter":
            suffixes = (".counter", ".rate", ".value")
        elif prefer == "rate":
            suffixes = (".rate", ".counter", ".value")
        else:
            suffixes = (".value", ".counter", ".rate")
        for suffix in suffixes:
            candidate = f"prometheus.{metric_name}{suffix}"
            if self._field_cache and candidate in self._field_cache:
                return candidate
        default_suffix = ".counter" if prefer == "counter" else (".rate" if prefer == "rate" else ".value")
        return f"prometheus.{metric_name}{default_suffix}"

    def resolve_labels(self, labels):
        resolved = []
        for label in labels or []:
            mapped = self.resolve_label(label)
            if mapped:
                resolved.append(mapped)
        return resolved

    def field_exists(self, field_name):
        self._discover_fields()
        if not self._field_cache:
            return None
        return field_name in self._field_cache

    def field_type(self, field_name):
        capability = self.field_capability(field_name)
        return capability.type if capability else None

    def field_type_family(self, field_name):
        capability = self.field_capability(field_name)
        return capability.type_family if capability else infer_type_family("")

    def field_capability(self, field_name):
        self._discover_fields()
        if not self._field_cache or field_name not in self._field_cache:
            return None
        return field_capability_from_es_field_caps(field_name, self._field_cache[field_name])

    def is_numeric_field(self, field_name):
        return is_numeric_field(self.field_capability(field_name))

    def is_searchable_field(self, field_name):
        return is_searchable_field(self.field_capability(field_name))

    def is_aggregatable_field(self, field_name):
        return is_aggregatable_field(self.field_capability(field_name))

    def is_text_like_field(self, field_name):
        return is_text_like_field(self.field_capability(field_name))

    def has_conflicting_types(self, field_name):
        return has_conflicting_types(self.field_capability(field_name))

    def is_counter(self, metric_name):
        if any(metric_name.endswith(s) for s in self._rule_pack.counter_suffixes):
            return True
        if is_counter_metric_field(self.field_capability(metric_name)):
            return True
        # In the Fleet `prometheus.remote_write` layout the metric leaf is
        # `prometheus.<metric>.counter`; consult that capability when the bare
        # name isn't directly cached.
        if self._current_schema_profile() == "prometheus_remote_write":
            counter_field = f"prometheus.{metric_name}.counter"
            if self._field_cache and counter_field in self._field_cache:
                return is_counter_metric_field(self.field_capability(counter_field))
        return False

    def resolve_control_field(self, variable_name):
        if variable_name in self._rule_pack.control_field_overrides:
            return self._rule_pack.control_field_overrides[variable_name]
        return self.resolve_label(variable_name)

    def concrete_index_candidates(self):
        self._discover_concrete_indexes()
        return list(self._concrete_index_cache or [])


__all__ = ["SchemaResolver"]
