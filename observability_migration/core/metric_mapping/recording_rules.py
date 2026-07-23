# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Prometheus recording-rule metric name heuristics."""

from __future__ import annotations


def looks_like_recording_rule_metric(name: str) -> bool:
    """Return True when ``name`` looks like a Prometheus recording-rule metric.

    Recording rules commonly use colon-separated names (e.g.
    ``job:http_requests:rate5m``). Empty names are excluded.
    """
    metric = str(name or "").strip()
    return bool(metric) and ":" in metric
