# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""CrossHair contracts calling the production readiness gate without a model."""

from dataclasses import dataclass

from observability_migration.adapters.source.grafana.verification import _semantic_gate


@dataclass
class Panel:
    status: str
    target_binding: dict[str, str]


def unverified_labels_never_green(
    status: int,
    missing: bool,
    warning: bool,
) -> str:
    """
    pre: 0 <= status < 6
    post: __return__ != 'Green'
    """
    states = ("migrated", "migrated_with_warnings", "skipped", "not_feasible", "requires_manual", "unknown")
    panel = Panel(states[status], {"status": "missing" if missing else "guessed"})
    return _semantic_gate(panel, None, [], ["null_series"] if warning else [])


def runtime_errors_remain_red(binding: str, warning: bool) -> str:
    """post: __return__ == 'Red'"""
    panel = Panel("migrated", {"status": binding})
    return _semantic_gate(panel, None, [], ["upload_failed", "null_series"] if warning else ["upload_failed"])


def skipped_panels_remain_skipped(binding: str, runtime_error: bool) -> str:
    """post: __return__ == 'Skipped'"""
    panel = Panel("skipped", {"status": binding})
    return _semantic_gate(panel, None, [], ["upload_failed"] if runtime_error else [])
