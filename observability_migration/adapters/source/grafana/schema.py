"""Schema discovery and label resolution helpers."""

from __future__ import annotations

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

    def __init__(self, rule_pack, es_url=None, index_pattern=None, es_api_key=None):
        self._rule_pack = rule_pack
        self._es_url = es_url
        self._index_pattern = index_pattern or "metrics-*"
        self._es_api_key = es_api_key
        self._field_cache = None
        self._discovered_mappings = {}
        self._discovery_attempted = False
        self._concrete_index_cache = None

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
        if label in self._discovered_mappings:
            return self._discovered_mappings[label]
        if self._field_cache and label in self._field_cache:
            return label
        candidates = self._candidate_fields(label)
        if candidates:
            return candidates[0]
        return label

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
        return is_counter_metric_field(self.field_capability(metric_name))

    def resolve_control_field(self, variable_name):
        if variable_name in self._rule_pack.control_field_overrides:
            return self._rule_pack.control_field_overrides[variable_name]
        return self.resolve_label(variable_name)

    def concrete_index_candidates(self):
        self._discover_concrete_indexes()
        return list(self._concrete_index_cache or [])


__all__ = ["SchemaResolver"]
