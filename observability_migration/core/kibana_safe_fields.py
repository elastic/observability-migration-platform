# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Rewrite field names that break Kibana Options List controls.

Kibana data controls resolve fields via ``dataView.getFieldByName(fieldName)``.
Empirically, the bare keyword field name ``key`` fails that lookup (blocking
error: "Could not locate field: key") even when the data view field list and
Elasticsearch mapping both include ``key``. Remapping the same control to
another keyword on the same data view (e.g. ``command``) works.

Fold ``key`` into a dotted ECS-style label so translated ES|QL, dashboard
controls, and seeded documents stay aligned.
"""

from __future__ import annotations

# Bare names that must not appear as Options List ``field_name`` values.
KIBANA_UNSAFE_FIELD_NAMES: dict[str, str] = {
    "key": "labels.key",
}


def kibana_safe_field_name(field_name: str) -> str:
    """Return a Kibana-safe field name, rewriting known unsafe bare names."""
    name = str(field_name or "").strip()
    if not name:
        return name
    return KIBANA_UNSAFE_FIELD_NAMES.get(name, name)
