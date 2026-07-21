# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Schema discovery and label resolution helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping

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

    When ``passthrough=True`` (the ``passthrough`` field profile), automatic
    mapping is disabled: explicit rule-pack overrides (``label_rewrites``,
    ``ignored_labels``, ``control_field_overrides``) still apply, but every
    other label and metric name is emitted verbatim. Live ``_field_caps``
    discovery may still run for validation, but OTel/Prometheus candidate
    remapping and auto-detected namespacing (``labels.``/
    ``prometheus.labels.``/``metrics.``/``prometheus.<m>.<suffix>``) are
    skipped. This mirrors the Datadog ``passthrough`` profile and keeps source
    names source-faithful.
    """

    FIELD_PROFILES = frozenset(
        {
            "otel",
            "prometheus_remote_write",
            "prometheus_metrics",
            "prometheus_native",
            "passthrough",
            "auto",
        }
    )

    PROM_TO_OTEL_CANDIDATES = {
        "instance": ["service.instance.id", "host.name", "host.ip"],
        "service": ["service.name"],
        "service_instance_id": ["service.instance.id"],
        "job": ["service.name"],
        "service_name": ["service.name"],
        "namespace": ["k8s.namespace.name"],
        "namespace_name": ["k8s.namespace.name"],
        "pod": ["k8s.pod.name"],
        "pod_name": ["k8s.pod.name"],
        "container": ["k8s.container.name", "container.name"],
        "container_name": ["k8s.container.name", "container.name"],
        "image": ["container.image.name"],
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
    # Fleet / Metricbeat ``use_types`` layout: typed leaves under prometheus.<metric>.
    _PROMETHEUS_METRIC_LEAF_RE = re.compile(r"^prometheus\.[A-Za-z_][A-Za-z0-9_]*\.(counter|value)$")
    # Classic Metricbeat remote_write (use_types=false): prometheus.metrics.<name>.
    _PROMETHEUS_NESTED_METRIC_RE = re.compile(r"^prometheus\.metrics\.[A-Za-z_][A-Za-z0-9_]*$")
    # Native Elastic /_prometheus/api/v1/write endpoint: metrics land under
    # `metrics.<name>` and Prometheus labels land under `labels.<name>`.
    _NATIVE_METRIC_RE = re.compile(r"^metrics\.[A-Za-z_][A-Za-z0-9_]*$")
    _NATIVE_LABEL_RE = re.compile(r"^labels\.[A-Za-z_][A-Za-z0-9_]*$")
    _NAMED_PROMETHEUS_PLANS = frozenset(
        {
            "prometheus_remote_write",
            "prometheus_metrics",
            "prometheus_native",
        }
    )

    def __init__(
        self,
        rule_pack,
        es_url=None,
        index_pattern=None,
        es_api_key=None,
        verify: bool | str = True,
        passthrough: bool = False,
        field_profile: str | None = None,
    ):
        if field_profile is None:
            field_profile = "passthrough" if passthrough else "otel"
        if field_profile not in self.FIELD_PROFILES:
            raise ValueError(f"unsupported Grafana field profile: {field_profile}")
        if passthrough:
            field_profile = "passthrough"
        self._rule_pack = rule_pack
        self._es_url = es_url
        self._index_pattern = index_pattern or "metrics-*"
        self._es_api_key = es_api_key
        self._verify = verify
        self._field_profile = field_profile
        self._passthrough = field_profile == "passthrough"
        self._auto_fallback_warned = False
        self._auto_resolved_profile = None
        self._profile_warnings = []
        self._otel_plan_warning_profiles = set()
        self._field_cache = None
        self._discovered_mappings = {}
        self._discovery_attempted = False
        self._concrete_index_cache = None
        self._schema_profile = None
        self._schema_profile_cache_id = None
        self._discovery_status = "not_attempted"
        self._discovery_error = ""
        self._cooccurrence_cache = {}
        # Set True the first time a label resolves to an unverified OTel/
        # pass-through default (the target neither advertised the source label
        # nor a known schema profile). Drives the run-summary fallback warning
        # for the ambiguous "discovery ok but schema unrecognized" case, where
        # whether fields actually fell back depends on the labels in play
        # (issue #256).
        self._emitted_unverified_otel_default = False
        # Set True when passthrough emits a bare source name that live caps do
        # not advertise. Drives the run-summary empty-panel warning so a live
        # OTel-shaped target is not silently treated as verified.
        self._emitted_unverified_passthrough_field = False

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
        if not self._es_url:
            # Preserve fields already supplied by merge_control_schema().
            if self._field_cache is None:
                self._field_cache = {}
            if not self._field_cache:
                self._discovery_status = "offline"
            self._discovery_error = ""
            return
        self._field_cache = {}
        try:
            resp = requests.get(
                f"{self._es_url}/{self._index_pattern}/_field_caps",
                params={"fields": "*"},
                headers=self._es_headers(),
                timeout=10,
                verify=self._verify,
            )
            if resp.status_code == 200:
                self._field_cache = resp.json().get("fields", {})
                self._discovery_status = "ok" if self._field_cache else "empty"
                self._discovery_error = ""
                self._ensure_auto_profile_resolved()
                if not self._passthrough:
                    self._build_discovered_mappings()
            else:
                self._discovery_status = "error"
                self._discovery_error = f"_field_caps returned HTTP {resp.status_code}: {getattr(resp, 'text', '')}"
        except Exception as exc:
            self._discovery_status = "error"
            self._discovery_error = f"_field_caps request failed: {exc}"

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

        Recognises three layouts (first match wins):

        ``prometheus_remote_write`` — Fleet / Metricbeat ``use_types`` layout:
        labels under ``prometheus.labels.<name>``, metrics under
        ``prometheus.<metric>.{counter,value}``.

        ``prometheus_metrics`` — classic Metricbeat remote_write
        (``use_types=false``): labels under ``prometheus.labels.<name>``,
        metrics under ``prometheus.metrics.<name>``.

        ``prometheus_native`` — native ``/_prometheus/api/v1/write`` endpoint:
        metrics under ``metrics.<name>``, labels under ``labels.<name>``.
        """
        has_prom_label = False
        has_prom_metric_leaf = False
        has_prom_nested_metric = False
        has_native_metric = False
        has_native_label = False
        for field_name in field_cache:
            if not has_prom_label and cls._PROMETHEUS_LABEL_RE.match(field_name):
                has_prom_label = True
            if not has_prom_metric_leaf and cls._PROMETHEUS_METRIC_LEAF_RE.match(field_name):
                has_prom_metric_leaf = True
            if not has_prom_nested_metric and cls._PROMETHEUS_NESTED_METRIC_RE.match(field_name):
                has_prom_nested_metric = True
            if not has_native_metric and cls._NATIVE_METRIC_RE.match(field_name):
                has_native_metric = True
            if not has_native_label and cls._NATIVE_LABEL_RE.match(field_name):
                has_native_label = True
            if has_prom_label and has_prom_metric_leaf:
                return "prometheus_remote_write"
        if has_prom_label and has_prom_nested_metric:
            return "prometheus_metrics"
        if has_native_metric and has_native_label:
            return "prometheus_native"
        return None

    def _ensure_auto_profile_resolved(self):
        if self._field_profile != "auto" or self._auto_resolved_profile is not None:
            return
        # Offline ``auto`` (no discovery yet): behave like otel silently.
        # CLI rejects ``auto`` without ``--es-url``; after discovery runs
        # (including empty/errored caps) we always resolve so operators get the
        # same ambiguous → otel + warn signal.
        if not self._discovery_attempted and not self._field_cache:
            return
        self.resolve_auto_profile()

    def resolve_auto_profile(self):
        """Resolve ``field_profile=auto`` from live caps after discovery.

        When caps clearly match a named Prometheus layout, the effective emit
        plan follows that layout. Otherwise the effective plan is ``otel`` and a
        warning is recorded once (including empty or unrecognized caps).
        """
        if self._field_profile != "auto":
            return self._field_profile
        if self._auto_resolved_profile is not None:
            return self._auto_resolved_profile
        detected = self._compute_schema_profile(self._field_cache or {})
        if detected in self._NAMED_PROMETHEUS_PLANS:
            self._auto_resolved_profile = detected
        else:
            self._auto_resolved_profile = "otel"
            if not self._auto_fallback_warned:
                self._auto_fallback_warned = True
                self._profile_warnings.append(
                    "field profile auto could not detect a named Prometheus layout; "
                    "falling back to otel"
                )
        return self._auto_resolved_profile

    def _maybe_warn_otel_plan_vs_named_layout(self, detected):
        """Warn when default ``otel`` plan meets a clearly named live layout.

        Emit still follows ``otel`` (plan→verify; no silent remap). The warning
        steers operators toward ``--field-profile auto`` or an explicit
        Prometheus profile when Fleet/native caps are present.
        """
        if self._field_profile != "otel":
            return
        if detected not in self._NAMED_PROMETHEUS_PLANS:
            return
        if detected in self._otel_plan_warning_profiles:
            return
        self._otel_plan_warning_profiles.add(detected)
        self._profile_warnings.append(
            f"field profile otel emits bare/OTel candidate names, but live caps "
            f"look like {detected}; use --field-profile {detected} or auto "
            f"with --es-url if panels query empty"
        )

    def _effective_schema_profile(self):
        """Return the planned schema profile used for emit, if any.

        Named Prometheus layouts emit under their plan. ``otel`` and
        ``passthrough`` emit through the OTel/candidate path (``None``).
        ``auto`` uses live detection when caps are available; offline or
        ambiguous caps behave like ``otel``.
        """
        if self._passthrough:
            return None
        plan = self._field_profile
        if plan == "auto":
            self._ensure_auto_profile_resolved()
            if self._auto_resolved_profile in self._NAMED_PROMETHEUS_PLANS:
                return self._auto_resolved_profile
            return None
        if plan in self._NAMED_PROMETHEUS_PLANS:
            return plan
        return None

    def _namespacing_schema_profile(self):
        """Schema profile that governs label/metric namespacing during emit.

        The operator-selected plan wins over live discovery so caps cannot
        silently remap a dashboard to a different layout.
        """
        if self._passthrough:
            return None
        planned = self._effective_schema_profile()
        if planned is not None:
            return planned
        return None

    def schema_profile(self):
        """Return the detected schema profile identifier, or `None`.

        Triggers field discovery on first access so callers don't need to
        sequence `_discover_fields()` manually.
        """
        self._discover_fields()
        return self._current_schema_profile()

    def has_field_capabilities(self):
        """True when live target field capabilities were successfully fetched
        (non-empty). False when offline, on a discovery error, or when the
        target returned no fields. Callers use this to distinguish "the field
        is genuinely absent from a known target" from "we know nothing about
        the target" — the latter cannot single out any one field."""
        self._discover_fields()
        return bool(self._field_cache)

    def discovery_status(self):
        """Return field-capability discovery status for reporting."""
        self._discover_fields()
        return {
            "status": self._discovery_status,
            "error": self._discovery_error,
            "field_count": len(self._field_cache or {}),
        }

    def field_resolution_summary(self):
        """Summarize how label/metric resolution is backed, for run reporting.

        Returns a dict describing whether resolution is verified against the
        live target or is falling back to the built-in OTel/pass-through
        defaults. ``otel_fallback`` is True when emitted field names
        (e.g. ``service.name``) and the query index are unverified guesses that
        may not match the user's data, so panels can render empty:

        - discovery offline/empty/errored (no live capabilities to verify
          against) — always a fallback;
        - discovery ok but a label actually resolved to a blind OTel/pass-
          through default during translation.

        It is False only when discovery returned live capabilities AND every
        resolved label was source-faithful or backed by a live-confirmed field.
        The signal is driven by what resolution actually emitted
        (``_emitted_unverified_otel_default``) rather than by profile/mapping
        counts, because whether fields fell back depends on the specific labels
        the dashboards used. This holds even when a known Prometheus schema
        profile is detected: a profile match on some fields does not guarantee
        every dashboard label exists in the target, and a label missing from a
        recognized profile still falls through to a blind OTel candidate
        (e.g. ``prometheus_remote_write`` without ``prometheus.labels.namespace``
        resolves ``namespace`` to ``k8s.namespace.name``) — issue #256, PR #262.

        ``automatic_mapping`` retains its compatibility meaning (all profiles
        except strict ``passthrough``); ``automatic_profile_selection`` reports
        the separate ``field_profile=auto`` layout-selection behavior."""
        self._discover_fields()
        self._ensure_auto_profile_resolved()
        detected = None if self._passthrough else self._current_schema_profile()
        planned = self._effective_schema_profile()
        self._maybe_warn_otel_plan_vs_named_layout(detected)
        profile_mismatch = (
            planned is not None
            and detected is not None
            and planned != detected
        )
        has_capabilities = bool(self._field_cache)
        if self._passthrough:
            # Offline/empty discovery is always unverified. Live discovery is
            # unverified when a bare source name was emitted but absent from
            # the target caps.
            otel_fallback = (not has_capabilities) or self._emitted_unverified_passthrough_field
        elif not has_capabilities:
            otel_fallback = True
        else:
            otel_fallback = self._emitted_unverified_otel_default
        summary = {
            "status": self._discovery_status,
            "field_profile": self._field_profile,
            "planned_schema_profile": planned,
            "detected_schema_profile": detected,
            "profile_mismatch": profile_mismatch,
            "automatic_mapping": not self._passthrough,
            "automatic_profile_selection": self._field_profile == "auto",
            "schema_profile": detected,
            "index_pattern": self._index_pattern,
            "field_count": len(self._field_cache or {}),
            "label_mappings": len(self._discovered_mappings),
            "otel_fallback": otel_fallback,
            "error": self._discovery_error,
        }
        if self._field_profile == "auto" and self._auto_resolved_profile == "otel":
            summary["auto_fallback"] = "otel"
        if self._profile_warnings:
            summary["profile_warnings"] = list(self._profile_warnings)
        return summary

    def _build_discovered_mappings(self):
        # Native endpoint indices have no OTel fields at all — skip the scan.
        if self._effective_schema_profile() == "prometheus_native":
            return
        if self._compute_schema_profile(self._field_cache or {}) == "prometheus_native":
            return
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
                verify=self._verify,
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

    def resolve_label(self, label, metric_field=None):
        if label in self._rule_pack.ignored_labels:
            return None
        if label in self._rule_pack.label_rewrites:
            return self._rule_pack.label_rewrites[label]
        # Passthrough profile: emit the source label verbatim, skipping live
        # discovery and OTel/Prometheus normalization. Explicit rule-pack
        # overrides above still win.
        if self._passthrough:
            if self._field_cache and label not in self._field_cache:
                self._emitted_unverified_passthrough_field = True
            return label
        self._discover_fields()
        planned = self._effective_schema_profile()
        # Metric-aware: when the label is scoped to a metric (a
        # `label_values(metric, label)` control, or a panel selector/group-by on
        # `metric{label=...}`), prefer the candidate field that co-occurs with
        # the metric over the index-global short-circuit below. The global check
        # would pick any field that merely *exists* in the index — even one
        # written by unrelated sources — and select a disjoint document set from
        # the metric scope, emptying the control/panel (issue #163).
        # When a named Prometheus plan is active, scoped/co-occurrence must not
        # prefer bare caps over the planned namespaced emit (same constraint as
        # the bare `_field_cache` short-circuit below).
        if metric_field and planned not in self._NAMED_PROMETHEUS_PLANS:
            scoped = self._resolve_label_scoped_to_metric(label, metric_field)
            if scoped is not None:
                return scoped
        # Source-faithful: if the target advertises the original label as a real
        # field, use it as-is. This keeps PromQL semantics intact when the target
        # has both Prometheus and OTEL aliases (common on dual-shipping clusters).
        # When a named Prometheus plan is active, the operator-selected layout
        # wins over bare caps (e.g. OTel-shaped targets that also carry bare
        # source labels) — skip this shortcut so emit follows the plan.
        if (
            self._field_cache
            and label in self._field_cache
            and planned not in self._NAMED_PROMETHEUS_PLANS
        ):
            return label
        # Fleet `prometheus.remote_write` data streams store the original
        # Prometheus label `<name>` under `prometheus.labels.<name>`. When that
        # profile is active, prefer it over the OTEL candidates below — the
        # namespaced form is the actual stored field and OTEL fields are not
        # present at all in this layout.
        profile = planned or self._namespacing_schema_profile()
        if profile in {"prometheus_remote_write", "prometheus_metrics"}:
            return f"prometheus.labels.{label}"
        # Native /_prometheus endpoint: labels are always stored as `labels.<name>`.
        # Return the namespaced form unconditionally — OTel candidates do not exist
        # in this layout, so falling through to them would emit wrong field names.
        # Missing labels surface through preflight rather than silently reverting.
        if profile == "prometheus_native":
            return f"labels.{label}"
        # Otherwise, fall back to OTEL/Prometheus normalization candidates.
        if label in self._discovered_mappings:
            return self._discovered_mappings[label]
        # Reaching here, the source label is absent from the target's live
        # capabilities (or discovery never ran) and no known profile applies, so
        # whatever field we emit below is an unverified guess that may not match
        # the user's data. Record it so the run summary can warn (issue #256).
        self._emitted_unverified_otel_default = True
        candidates = self._candidate_fields(label)
        if candidates:
            return candidates[0]
        return label

    def _resolve_label_scoped_to_metric(self, label, metric_field):
        """Resolve a label to the candidate field that co-occurs with the metric.

        Builds the candidate list (source-faithful label first, then the
        OTel/Prometheus normalization candidates, then the profile-namespaced
        forms) and returns the first candidate that actually co-occurs with the
        scoped metric in the live index. Returns ``None`` — so the caller falls
        back to the index-global resolution — when nothing co-occurs, live caps
        are unavailable, or the probes error.

        Co-occurrence is per-document and cannot be derived from `_field_caps`
        (which is per-index), so it requires a live ES|QL probe.
        """
        if not metric_field or not self.has_field_capabilities():
            return None
        ordered = self._scoped_candidate_fields(label)
        if not ordered:
            return None
        # One batched probe covers every candidate at once (issue #182); pick
        # the first, in priority order, that co-occurs with the scoped metric.
        cooccurrence = self._cooccurring_candidates(metric_field, ordered)
        for candidate in ordered:
            if cooccurrence.get(candidate):
                return candidate
        return None

    def _scoped_candidate_fields(self, label):
        """Advertised candidate fields for ``label``, in resolution priority.

        Mirrors the index-global ``resolve_label`` order so metric-aware
        resolution stays source-faithful: bare label first, then the active
        profile's namespaced label form (so a dual-shipping Prometheus index
        keeps ``prometheus.labels.<x>`` / ``labels.<x>`` over an OTel alias),
        then the OTel/Prometheus normalization candidates, then the remaining
        namespaced forms. De-duplicated in priority order and filtered to fields
        the target actually advertises — an ES|QL probe against an unknown
        column would 400 (→ wasted query, None result).
        """
        candidates = [label]
        profile = self._namespacing_schema_profile()
        if profile in {"prometheus_remote_write", "prometheus_metrics"}:
            candidates.append(f"prometheus.labels.{label}")
        elif profile == "prometheus_native":
            candidates.append(f"labels.{label}")
        candidates.extend(self._candidate_fields(label))
        candidates.append(f"labels.{label}")
        candidates.append(f"prometheus.labels.{label}")
        ordered = []
        seen = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate in (self._field_cache or {}):
                ordered.append(candidate)
        return ordered

    def prime_label_cooccurrence(self, labels, metric_field):
        """Pre-warm the co-occurrence cache for a whole set of labels scoped to
        one metric, in a SINGLE batched probe (issue #182).

        Callers that already know every label resolved against a given metric
        (e.g. all of a panel fragment's selector matchers and group-by labels)
        prime here once. The union of all candidates across the labels is
        counted in one ``/_query``; the subsequent per-label ``resolve_label``
        calls then hit the warm cache and issue no further round-trips. Purely a
        cache pre-fill — it changes nothing about which field a label resolves
        to, only how many probes that resolution costs.
        """
        # Passthrough resolution never probes, so priming would only waste
        # round-trips.
        if self._passthrough:
            return
        if not metric_field or not self.has_field_capabilities():
            return
        candidates = []
        seen = set()
        for label in labels or []:
            # Mirror ``resolve_label``'s short-circuits: ignored and rewritten
            # labels never reach a co-occurrence probe, so priming must skip
            # them too — otherwise it issues round-trips for labels resolution
            # will never probe.
            if label in self._rule_pack.ignored_labels or label in self._rule_pack.label_rewrites:
                continue
            for candidate in self._scoped_candidate_fields(label):
                if candidate not in seen:
                    seen.add(candidate)
                    candidates.append(candidate)
        if candidates:
            self._cooccurring_candidates(metric_field, candidates)

    def _cooccurring_candidates(self, metric_field, candidates):
        """Co-occurrence map ``{candidate: True/False/None}`` for ``candidates``
        against ``metric_field``.

        Cache-first, keyed by ``(metric_field, candidate)`` and shared across
        dashboards. Cache misses are resolved with a SINGLE batched ES|QL probe
        that counts every uncached candidate in one round-trip (issue #182),
        collapsing what used to be one blocking probe per candidate. ``None``
        (probe error / unreachable) is cached too, matching the prior per-pair
        behaviour so a transient failure is not re-probed mid-run.

        A batched ``STATS`` couples every candidate's fate: a single
        incompatible field (e.g. a type conflict across dual-shipping
        ``metrics-*`` indices → ``verification_exception``) fails the whole
        query, which would otherwise cache ``None`` for *every* candidate and
        silently revert the label to index-global resolution — re-introducing
        the disjoint-document-set bug #163 was written to prevent. So on a
        multi-candidate batch error we re-probe each candidate alone, matching
        the pre-#182 per-pair behaviour where one bad field never suppressed the
        others. This fan-out is the error path only; the happy path still costs
        one probe.
        """
        result = {}
        uncached = []
        for candidate in candidates:
            key = (metric_field, candidate)
            if key in self._cooccurrence_cache:
                result[candidate] = self._cooccurrence_cache[key]
            else:
                uncached.append(candidate)
        if uncached:
            probed = self._probe_cooccurrence_batch(metric_field, uncached)
            if probed is None and len(uncached) > 1:
                probed = {}
                for candidate in uncached:
                    single = self._probe_cooccurrence_batch(metric_field, [candidate])
                    probed[candidate] = None if single is None else single.get(candidate)
            for candidate in uncached:
                value = probed.get(candidate) if probed is not None else None
                self._cooccurrence_cache[(metric_field, candidate)] = value
                result[candidate] = value
        return result

    def _cooccurs(self, metric_field, candidate):
        """Whether ``metric_field`` and ``candidate`` co-occur on any document.

        Thin per-pair wrapper over the batched probe. Returns ``True``/``False``,
        or ``None`` when the target is unreachable or the probe errors. Results
        are cached per ``(metric_field, candidate)``.
        """
        return self._cooccurring_candidates(metric_field, [candidate]).get(candidate)

    def _probe_cooccurrence_batch(self, metric_field, candidates):
        """Single ES|QL probe counting, among documents where ``metric_field``
        is non-null, how many also carry each candidate field.

        Returns ``{candidate: bool}``, or ``None`` when the target is
        unreachable or the probe errors. ``metric_field`` and the candidates are
        unescaped physical field names; this method adds its own backticks. Each
        candidate gets a ``c<i>`` COUNT alias and results are mapped back by
        column name, so the mapping is robust to column reordering.
        """
        if not self._es_url or not candidates:
            return None
        aliases = {f"c{i}": candidate for i, candidate in enumerate(candidates)}
        stats = ", ".join(
            f"{alias} = COUNT(`{candidate}`)" for alias, candidate in aliases.items()
        )
        query = (
            f"FROM {self._index_pattern} "
            f"| WHERE `{metric_field}` IS NOT NULL "
            f"| STATS {stats} | LIMIT 1"
        )
        try:
            resp = requests.post(
                f"{self._es_url}/_query",
                params={"format": "json"},
                json={"query": query},
                headers={**self._es_headers(), "Content-Type": "application/json"},
                timeout=10,
                verify=self._verify,
            )
            if resp.status_code != 200:
                return None
            body = resp.json()
            values = body.get("values") or []
            if not values or not values[0]:
                return {candidate: False for candidate in candidates}
            row = values[0]
            by_alias = {}
            for idx, column in enumerate(body.get("columns") or []):
                if idx < len(row):
                    by_alias[column.get("name")] = row[idx]
            result = {}
            for idx, (alias, candidate) in enumerate(aliases.items()):
                # Prefer the column-name mapping; fall back to positional order
                # when the response omits `columns`.
                count = by_alias.get(alias) if by_alias else (row[idx] if idx < len(row) else None)
                result[candidate] = (count or 0) > 0
            return result
        except Exception:
            return None

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
        # Passthrough profile: emit the source metric name verbatim, skipping
        # discovery and any layout-specific prefixing/suffixing.
        if self._passthrough:
            if self._field_cache and metric_name not in self._field_cache:
                self._emitted_unverified_passthrough_field = True
            return metric_name
        self._discover_fields()
        profile = self._namespacing_schema_profile()
        if profile == "prometheus_native":
            # Native endpoint stores metrics as `metrics.<name>` directly — no
            # suffix variants.  Return the prefixed form unconditionally so the
            # contract layer can surface missing fields via preflight.
            return f"metrics.{metric_name}"
        if profile == "prometheus_metrics":
            # Classic Metricbeat remote_write (use_types=false): nested under
            # prometheus.metrics.<name> with labels under prometheus.labels.*.
            return f"prometheus.metrics.{metric_name}"
        if profile != "prometheus_remote_write":
            # OTel plan (and auto when resolved to otel): field-level candidate
            # selection only — do not switch the planned layout to
            # prometheus_native when caps advertise metrics.* (issue #270).
            cache = self._field_cache or {}
            if metric_name in cache:
                return metric_name
            prefixed = f"metrics.{metric_name}"
            if prefixed in cache:
                return prefixed
            return metric_name
        # Bare metric names in caps must not override the remote_write plan —
        # emit `prometheus.<metric>.<suffix>` even when OTel-shaped targets
        # advertise the logical PromQL name as a field.
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

    def resolve_labels(self, labels, metric_field=None):
        resolved = []
        for label in labels or []:
            mapped = self.resolve_label(label, metric_field=metric_field)
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
        kind = str(self._rule_pack.metric_kinds.get(metric_name, "")).strip().lower()
        if kind == "counter":
            return True
        if kind == "gauge":
            return False
        capability = self.field_capability(metric_name)
        counter_metric = self.resolve_metric_field(metric_name, prefer="counter")
        counter_capability = (
            self.field_capability(counter_metric)
            if counter_metric and counter_metric != metric_name
            else None
        )
        gauge_metric = self.resolve_metric_field(metric_name, prefer="gauge")
        gauge_capability = (
            self.field_capability(gauge_metric)
            if gauge_metric and gauge_metric != metric_name
            else None
        )
        if is_counter_metric_field(capability):
            return True
        if is_counter_metric_field(counter_capability):
            return True
        for field_capability in (capability, counter_capability, gauge_capability):
            if getattr(field_capability, "time_series_metric_kind", "") == "gauge":
                return False
        if capability is not None and counter_capability is None:
            return False
        component_suffixes = ("_bucket", "_count", "_sum")
        has_counter_suffix = any(metric_name.endswith(s) for s in self._rule_pack.counter_suffixes)
        has_component_suffix = any(
            metric_name.endswith(s) and s in self._rule_pack.counter_suffixes
            for s in component_suffixes
        )
        if has_counter_suffix and not has_component_suffix:
            return True
        if has_component_suffix:
            return True
        # Strict passthrough queries the bare source field. Do not classify it
        # from a namespaced field that will not appear in the emitted query.
        if self._passthrough:
            return False
        profile = self._namespacing_schema_profile() or self._current_schema_profile()
        # Fleet layout: metric leaf is `prometheus.<metric>.counter`.
        if profile == "prometheus_remote_write":
            counter_field = f"prometheus.{metric_name}.counter"
            if self._field_cache and counter_field in self._field_cache:
                return is_counter_metric_field(self.field_capability(counter_field))
        if profile == "prometheus_metrics":
            nested = f"prometheus.metrics.{metric_name}"
            if is_counter_metric_field(self.field_capability(nested)):
                return True
        # Native endpoint layout: metric is stored as `metrics.<name>` with
        # time_series_metric: counter|gauge set by ES's name-suffix heuristic.
        if profile == "prometheus_native":
            if is_counter_metric_field(self.field_capability(f"metrics.{metric_name}")):
                return True
        return False

    def declared_gauge(self, metric_name):
        """True when the user's rule pack explicitly pins this metric as a
        gauge (``metric_kinds: <metric>: gauge``). This is the only signal
        strong enough to degrade a counter-only PromQL range function
        (``rate``/``irate``) to its gauge analogue: live caps can be stale,
        and the telemetry contract locks rate()-ed fields as counters."""
        if not metric_name:
            return False
        return str(self._rule_pack.metric_kinds.get(metric_name, "")).strip().lower() == "gauge"

    def refutes_counter(self, metric_name):
        """True when the *target* has positive information that the metric is
        NOT a usable ES|QL counter — an explicit rule-pack ``gauge`` kind, or a
        resolved field that is present in the live capabilities but not
        counter-typed (gauge, or plain numeric without ``time_series_metric``).

        Returns False when the target is silent (offline migrate, or the field
        is absent from the live caps) or when the field genuinely is a counter.
        Callers use this to decide whether a counter-only PromQL range function
        (``rate``/``irate``) may keep its true ES|QL ``RATE``/``IRATE`` form
        (no refutation -> trust the source) or must degrade to a gauge analogue
        (refuted -> emitting ``RATE`` would 400 in Kibana on a non-counter
        field)."""
        if not metric_name:
            return False
        kind = str(self._rule_pack.metric_kinds.get(metric_name, "")).strip().lower()
        if kind == "gauge":
            return True
        if kind == "counter":
            return False
        if self.is_counter(metric_name):
            return False
        # Not a proven counter. Refute only when the target actually knows this
        # field (live caps present and the resolved field exists); stay silent
        # when offline or the field is unknown so the source signal can win.
        for candidate in (
            metric_name,
            self.resolve_metric_field(metric_name, prefer="counter"),
            self.resolve_metric_field(metric_name, prefer="gauge"),
        ):
            if candidate and self.field_exists(candidate):
                return True
        return False

    def resolve_control_field(self, variable_name, metric_field=None):
        if variable_name in self._rule_pack.control_field_overrides:
            return self._rule_pack.control_field_overrides[variable_name]
        return self.resolve_label(variable_name, metric_field=metric_field)

    def concrete_index_candidates(self):
        self._discover_concrete_indexes()
        return list(self._concrete_index_cache or [])

    def merge_control_schema(self, payload: Mapping[str, object] | None) -> None:
        """Merge offline control-schema field/co-occurrence hints into discovery.

        Scenario manifests ship curated ``control_schemas/*.json`` fixtures so
        live migrations preserve Grafana variable semantics even when the target
        cluster has not yet materialized every label field.
        """
        if not isinstance(payload, Mapping):
            return
        keyword = {"keyword": {"type": "keyword", "aggregatable": True, "searchable": True}}
        if self._field_cache is None:
            self._field_cache = {}
        for field_name, spec in (payload.get("field_cache") or {}).items():
            cleaned = str(field_name or "").strip()
            if not cleaned:
                continue
            if cleaned not in self._field_cache:
                self._field_cache[cleaned] = spec if isinstance(spec, dict) and spec else keyword
        for item in payload.get("cooccurrence_cache") or []:
            if not isinstance(item, Mapping):
                continue
            metric = str(item.get("metric") or "").strip()
            field = str(item.get("field") or item.get("label") or "").strip()
            if metric and field:
                self._cooccurrence_cache[(metric, field)] = bool(item.get("cooccurs"))
        positive_alternatives: dict[str, list[str]] = {}
        negative_fields: set[str] = set()
        for item in payload.get("cooccurrence_cache") or []:
            if not isinstance(item, Mapping):
                continue
            metric = str(item.get("metric") or "").strip()
            field = str(item.get("field") or item.get("label") or "").strip()
            if not metric or not field:
                continue
            if item.get("cooccurs"):
                positive_alternatives.setdefault(metric, []).append(field)
            else:
                negative_fields.add(field)
        for field_name in negative_fields:
            if field_name not in self._field_cache:
                continue
            if any(
                field_name != alternative and alternative in self._field_cache
                for alternatives in positive_alternatives.values()
                for alternative in alternatives
            ):
                self._field_cache.pop(field_name, None)
        if self._field_cache:
            self._discovery_status = "ok"
            self._discovery_error = ""
            # Offline merges happen before the first resolve_label() call. Mark
            # discovery as attempted so _discover_fields() does not wipe the
            # curated field_cache when es_url is empty.
            self._discovery_attempted = True
            self._build_discovered_mappings()
            self._schema_profile_cache_id = None


__all__ = ["SchemaResolver"]
