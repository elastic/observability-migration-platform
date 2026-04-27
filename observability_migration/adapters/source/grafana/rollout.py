# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Rollout safety: artifact lineage, rollout states, and shadow-space workflow.

This module provides the infrastructure for safe, traceable migration deployments.
It tracks the lineage from Grafana source assets through translation to Kibana
deployment, manages rollout states, and supports shadow-space workflows where
migrated dashboards are deployed to a non-production Kibana space first.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROLLOUT_STATES = (
    "report_only",
    "shadow_imported",
    "review_approved",
    "promoted",
    "rolled_back",
)


@dataclass
class ArtifactBundle:
    """Tracks all artifacts produced during a single migration run."""
    run_id: str = ""
    timestamp: float = field(default_factory=time.time)
    yaml_paths: list[str] = field(default_factory=list)
    compiled_paths: list[str] = field(default_factory=list)
    report_path: str = ""
    manifest_path: str = ""
    verification_path: str = ""
    preflight_path: str = ""
    contract_path: str = ""
    smoke_report_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "yaml_paths": self.yaml_paths,
            "compiled_paths": self.compiled_paths,
            "report_path": self.report_path,
            "manifest_path": self.manifest_path,
            "verification_path": self.verification_path,
            "preflight_path": self.preflight_path,
            "contract_path": self.contract_path,
            "smoke_report_path": self.smoke_report_path,
        }


@dataclass
class DashboardLineage:
    """Traces a single dashboard from Grafana source to Kibana deployment."""
    grafana_uid: str = ""
    grafana_title: str = ""
    grafana_folder: str = ""
    source_file: str = ""
    kibana_saved_object_id: str = ""
    kibana_space: str = ""
    yaml_path: str = ""
    compiled_path: str = ""
    panel_count: int = 0
    migrated_panels: int = 0
    semantic_gate_summary: dict[str, int] = field(default_factory=dict)
    rollout_state: str = "report_only"
    state_history: list[dict[str, Any]] = field(default_factory=list)

    def transition(self, new_state: str, *, reason: str = "") -> None:
        if new_state not in ROLLOUT_STATES:
            raise ValueError(f"Invalid rollout state: {new_state}")
        self.state_history.append({
            "from": self.rollout_state,
            "to": new_state,
            "timestamp": time.time(),
            "reason": reason,
        })
        self.rollout_state = new_state

    def to_dict(self) -> dict[str, Any]:
        return {
            "grafana_uid": self.grafana_uid,
            "grafana_title": self.grafana_title,
            "grafana_folder": self.grafana_folder,
            "source_file": self.source_file,
            "kibana_saved_object_id": self.kibana_saved_object_id,
            "kibana_space": self.kibana_space,
            "yaml_path": self.yaml_path,
            "compiled_path": self.compiled_path,
            "panel_count": self.panel_count,
            "migrated_panels": self.migrated_panels,
            "semantic_gate_summary": self.semantic_gate_summary,
            "rollout_state": self.rollout_state,
            "state_history": self.state_history,
        }


@dataclass
class RolloutPlan:
    """Aggregates lineage for all dashboards in a migration run."""
    run_id: str = ""
    timestamp: float = field(default_factory=time.time)
    target_space: str = ""
    shadow_space: str = ""
    artifact_bundle: ArtifactBundle = field(default_factory=ArtifactBundle)
    dashboards: list[DashboardLineage] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "target_space": self.target_space,
            "shadow_space": self.shadow_space,
            "artifact_bundle": self.artifact_bundle.to_dict(),
            "dashboards": [d.to_dict() for d in self.dashboards],
            "summary": self._summary(),
        }

    def _summary(self) -> dict[str, Any]:
        by_state: dict[str, int] = {}
        for d in self.dashboards:
            by_state[d.rollout_state] = by_state.get(d.rollout_state, 0) + 1
        return {
            "total_dashboards": len(self.dashboards),
            "by_rollout_state": by_state,
            "total_panels": sum(d.panel_count for d in self.dashboards),
            "total_migrated": sum(d.migrated_panels for d in self.dashboards),
        }


def build_rollout_plan(
    results: list[Any],
    *,
    run_id: str = "",
    target_space: str = "",
    shadow_space: str = "",
    output_dir: str = "",
    smoke_report_path: str = "",
) -> RolloutPlan:
    """Build a rollout plan from migration results."""
    import uuid as _uuid
    plan = RolloutPlan(
        run_id=run_id or str(_uuid.uuid4())[:8],
        target_space=target_space,
        shadow_space=shadow_space,
    )

    if output_dir:
        base = Path(output_dir)
        plan.artifact_bundle = ArtifactBundle(
            run_id=plan.run_id,
            yaml_paths=[str(p) for p in sorted(base.glob("yaml/*.yaml"))],
            compiled_paths=[str(p) for p in sorted(base.glob("compiled/*/compiled_dashboards.ndjson"))],
            report_path=str(base / "migration_report.json"),
            manifest_path=str(base / "migration_manifest.json"),
            verification_path=str(base / "verification_packets.json"),
            preflight_path=str(base / "preflight_report.json") if (base / "preflight_report.json").exists() else "",
            contract_path=str(base / "required_target_contract.json") if (base / "required_target_contract.json").exists() else "",
            smoke_report_path=str(smoke_report_path or ""),
        )

    for result in results:
        gate_summary: dict[str, int] = {}
        for pr in getattr(result, "panel_results", []) or []:
            gate = (getattr(pr, "verification_packet", {}) or {}).get("semantic_gate", "")
            if gate:
                gate_summary[gate] = gate_summary.get(gate, 0) + 1

        lineage = DashboardLineage(
            grafana_uid=str(getattr(result, "dashboard_uid", "")),
            grafana_title=str(getattr(result, "dashboard_title", "")),
            grafana_folder=str(getattr(result, "folder_title", "")),
            source_file=str(getattr(result, "source_file", "")),
            yaml_path=str(getattr(result, "yaml_path", "") or ""),
            compiled_path=str(getattr(result, "compiled_path", "") or ""),
            panel_count=result.total_panels,
            migrated_panels=result.migrated + result.migrated_with_warnings,
            semantic_gate_summary=gate_summary,
        )

        if getattr(result, "uploaded", None):
            uploaded_space = str(getattr(result, "uploaded_space", "") or "")
            lineage.kibana_space = uploaded_space or target_space
            if shadow_space and uploaded_space == shadow_space:
                lineage.transition("shadow_imported", reason="uploaded to shadow space")
            else:
                lineage.transition("promoted", reason="uploaded directly to target space")

        plan.dashboards.append(lineage)

    return plan


def save_rollout_plan(plan: RolloutPlan, output_path: str | Path) -> None:
    output_path = Path(output_path)
    with output_path.open("w") as fh:
        json.dump(plan.to_dict(), fh, indent=2)


def promote_dashboard(plan: RolloutPlan, dashboard_uid: str, *, reason: str = "") -> bool:
    """Transition a dashboard from shadow_imported/review_approved to promoted."""
    for d in plan.dashboards:
        if d.grafana_uid == dashboard_uid:
            if d.rollout_state in ("shadow_imported", "review_approved"):
                d.transition("promoted", reason=reason or "manual promotion")
                return True
    return False


def rollback_dashboard(plan: RolloutPlan, dashboard_uid: str, *, reason: str = "") -> bool:
    """Transition a dashboard back to rolled_back state."""
    for d in plan.dashboards:
        if d.grafana_uid == dashboard_uid:
            if d.rollout_state in ("shadow_imported", "review_approved", "promoted"):
                d.transition("rolled_back", reason=reason or "manual rollback")
                return True
    return False


def generate_review_queue(plan: RolloutPlan) -> list[dict[str, Any]]:
    """Generate a prioritized review queue sorted by risk."""
    queue: list[dict[str, Any]] = []
    for d in plan.dashboards:
        red = d.semantic_gate_summary.get("Red", 0)
        yellow = d.semantic_gate_summary.get("Yellow", 0)
        green = d.semantic_gate_summary.get("Green", 0)
        risk_score = red * 10 + yellow * 3
        queue.append({
            "dashboard": d.grafana_title,
            "uid": d.grafana_uid,
            "state": d.rollout_state,
            "panels": d.panel_count,
            "migrated": d.migrated_panels,
            "gates": {"green": green, "yellow": yellow, "red": red},
            "risk_score": risk_score,
        })
    queue.sort(key=lambda x: -x["risk_score"])
    return queue


__all__ = [
    "ROLLOUT_STATES",
    "ArtifactBundle",
    "DashboardLineage",
    "RolloutPlan",
    "build_rollout_plan",
    "generate_review_queue",
    "promote_dashboard",
    "rollback_dashboard",
    "save_rollout_plan",
]
