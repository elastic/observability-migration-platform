# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Rollout planning helpers for Datadog migrations."""

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
    run_id: str = ""
    timestamp: float = field(default_factory=time.time)
    yaml_paths: list[str] = field(default_factory=list)
    compiled_paths: list[str] = field(default_factory=list)
    report_path: str = ""
    manifest_path: str = ""
    verification_path: str = ""
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
            "smoke_report_path": self.smoke_report_path,
        }


@dataclass
class DashboardLineage:
    source_dashboard_id: str = ""
    source_dashboard_title: str = ""
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
        self.state_history.append(
            {
                "from": self.rollout_state,
                "to": new_state,
                "timestamp": time.time(),
                "reason": reason,
            }
        )
        self.rollout_state = new_state

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_dashboard_id": self.source_dashboard_id,
            "source_dashboard_title": self.source_dashboard_title,
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
            "dashboards": [dashboard.to_dict() for dashboard in self.dashboards],
            "summary": self._summary(),
        }

    def _summary(self) -> dict[str, Any]:
        by_state: dict[str, int] = {}
        for dashboard in self.dashboards:
            by_state[dashboard.rollout_state] = by_state.get(dashboard.rollout_state, 0) + 1
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
            yaml_paths=[str(path) for path in sorted(base.glob("yaml/*.yaml"))],
            compiled_paths=[str(path) for path in sorted(base.glob("compiled/*/compiled_dashboards.ndjson"))],
            report_path=str(base / "migration_report.json"),
            manifest_path=str(base / "migration_manifest.json"),
            verification_path=str(base / "verification_packets.json"),
            smoke_report_path=str(smoke_report_path or ""),
        )

    for result in results:
        gate_summary: dict[str, int] = {}
        for panel_result in getattr(result, "panel_results", []) or []:
            gate = str((getattr(panel_result, "verification_packet", {}) or {}).get("semantic_gate", "") or "")
            if gate:
                gate_summary[gate] = gate_summary.get(gate, 0) + 1

        lineage = DashboardLineage(
            source_dashboard_id=str(getattr(result, "dashboard_id", "") or ""),
            source_dashboard_title=str(getattr(result, "dashboard_title", "") or ""),
            source_file=str(getattr(result, "source_file", "") or ""),
            kibana_saved_object_id=str(getattr(result, "kibana_saved_object_id", "") or ""),
            kibana_space=str(getattr(result, "uploaded_space", "") or ""),
            yaml_path=str(getattr(result, "yaml_path", "") or ""),
            compiled_path=str(getattr(result, "compiled_path", "") or ""),
            panel_count=len(getattr(result, "panel_results", []) or []),
            migrated_panels=sum(
                1
                for panel_result in (getattr(result, "panel_results", []) or [])
                if str(getattr(panel_result, "status", "") or "") in {"ok", "warning"}
            ),
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
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(plan.to_dict(), fh, indent=2)


def promote_dashboard(plan: RolloutPlan, dashboard_id: str, *, reason: str = "") -> bool:
    """Transition a dashboard from shadow_imported/review_approved to promoted."""
    for dashboard in plan.dashboards:
        if dashboard.source_dashboard_id == dashboard_id:
            if dashboard.rollout_state in ("shadow_imported", "review_approved"):
                dashboard.transition("promoted", reason=reason or "manual promotion")
                return True
    return False


def rollback_dashboard(plan: RolloutPlan, dashboard_id: str, *, reason: str = "") -> bool:
    """Transition a dashboard back to rolled_back state."""
    for dashboard in plan.dashboards:
        if dashboard.source_dashboard_id == dashboard_id:
            if dashboard.rollout_state in ("shadow_imported", "review_approved", "promoted"):
                dashboard.transition("rolled_back", reason=reason or "manual rollback")
                return True
    return False


def generate_review_queue(plan: RolloutPlan) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for dashboard in plan.dashboards:
        red = dashboard.semantic_gate_summary.get("Red", 0)
        yellow = dashboard.semantic_gate_summary.get("Yellow", 0)
        green = dashboard.semantic_gate_summary.get("Green", 0)
        risk_score = red * 10 + yellow * 3
        queue.append(
            {
                "dashboard": dashboard.source_dashboard_title,
                "dashboard_id": dashboard.source_dashboard_id,
                "state": dashboard.rollout_state,
                "panels": dashboard.panel_count,
                "migrated": dashboard.migrated_panels,
                "gates": {"green": green, "yellow": yellow, "red": red},
                "risk_score": risk_score,
            }
        )
    queue.sort(key=lambda item: -item["risk_score"])
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
