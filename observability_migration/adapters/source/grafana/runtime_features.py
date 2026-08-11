# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Runtime target feature profile helpers for Grafana migrations."""

from __future__ import annotations

from typing import Any

PROMQL_COMMAND_V0 = "promql_command_v0"
PROMQL_LABEL_MATCHER_PARAMS = "promql_label_matcher_params"
# Kibana forwards dashboard control values into named params inside an opaque
# ``PROMQL …`` expression (elastic/kibana#271244). Confirmed on Kibana >= 9.5;
# verified older Kibana (e.g. 9.4) leaves ``?var`` unbound so migration keeps
# the ES|QL ``WHERE … RLIKE ?var`` path. When ``--kibana-url`` is absent or the
# version probe is inconclusive, migration prefers native PROMQL and lets the
# existing translator / live-validator fallthroughs convert panels to ES|QL
# when needed. Not probeable through Elasticsearch alone.
KIBANA_PROMQL_CONTROL_PARAMS = "kibana_promql_control_params"
# Native ``histogram_quantile`` PromQL support. Landed in Elasticsearch 9.5
# (elastic/elasticsearch#150578); on stacks reporting it, histogram_quantile
# panels pass through the native PROMQL path instead of the ES|QL PERCENTILE()
# translation. Gated behind this feature so older stacks keep the ES|QL path.
PROMQL_HISTOGRAM_QUANTILE = "promql_histogram_quantile"
# Plain ES|QL named-parameter binding (``FROM … | WHERE field == ?var`` /
# ``RLIKE ?var``). Unlike ``promql_label_matcher_params`` this does NOT require
# the ES|QL PROMQL command, so it is available on a broader set of targets and
# is probed independently of the PROMQL command's cluster-wide fallback (issue #132).
ESQL_NAMED_PARAM_BINDING = "esql_named_param_binding"

POLICY_ALLOW = "allow"
POLICY_FALLBACK = "fallback"
POLICY_REVIEW = "review"
POLICY_BLOCK = "block"

_UNSAFE_POLICIES = {POLICY_FALLBACK, POLICY_REVIEW, POLICY_BLOCK}


def get_runtime_features(target: Any) -> dict[str, Any]:
    """Return the mutable runtime feature profile attached to a rule pack."""
    if target is None:
        return {}
    if isinstance(target, dict):
        return target
    features = getattr(target, "runtime_features", None)
    if isinstance(features, dict):
        return features
    return {}


def get_feature_state(target: Any, feature: str) -> dict[str, Any]:
    """Return a normalized feature-state dict.

    Tests and plugin code may inject bare booleans for convenience; the runtime
    profile stores richer dicts so reports can explain where each decision came
    from.
    """
    state = get_runtime_features(target).get(feature)
    if isinstance(state, bool):
        return {
            "supported": state,
            "source": "override",
            "confidence": "assumed",
            "level": "runtime",
        }
    if isinstance(state, dict):
        return state
    return {}


def is_feature_supported(target: Any, feature: str) -> bool:
    """Return whether *feature* is enabled for native routing decisions."""
    state = get_feature_state(target, feature)
    if state.get("policy") in _UNSAFE_POLICIES:
        return False
    return state.get("supported") is True


def binds_esql_named_params(target: Any) -> bool:
    """Whether the target can bind Grafana ``$var`` matchers as ES|QL params.

    True when the target advertises native PROMQL label-matcher params
    (``promql_label_matcher_params``) OR plain ES|QL named-parameter binding
    (``esql_named_param_binding``). The ES|QL path (``WHERE field == ?var`` /
    ``RLIKE ?var``) only needs the latter, which is a core ES|QL feature that
    does not depend on the PROMQL command — so a cluster-wide ES|QL fallback
    run can still preserve ``?var`` label filters instead of dropping them
    (issue #132).
    """
    return is_feature_supported(target, ESQL_NAMED_PARAM_BINDING) or is_feature_supported(
        target, PROMQL_LABEL_MATCHER_PARAMS
    )


def set_runtime_feature(
    target: Any,
    feature: str,
    *,
    supported: bool,
    source: str,
    confidence: str = "verified",
    level: str = "runtime",
    reason: str = "",
    policy: str | None = None,
) -> dict[str, Any]:
    """Set a feature state on *target* and return the recorded state."""
    features = get_runtime_features(target)
    state: dict[str, Any] = {
        "supported": bool(supported),
        "source": source,
        "confidence": confidence,
        "level": level,
        "reason": reason,
    }
    if policy is not None:
        state["policy"] = policy
    features[feature] = state
    return state
