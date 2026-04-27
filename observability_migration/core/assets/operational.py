"""Canonical operational IR — lineage and lifecycle metadata.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _safe_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value or fallback)
    except (TypeError, ValueError):
        return fallback


@dataclass
class AssetLineage:
    dashboard_title: str = ""
    dashboard_uid: str = ""
    source_panel_id: str = ""
    source_file: str = ""
    folder_title: str = ""


@dataclass
class ReviewState:
    readiness: str = ""
    recommended_target: str = ""
    semantic_gate: str = ""
    verification_mode: str = ""
    validation_status: str = "not_run"
    post_validation_action: str = ""
    post_validation_message: str = ""


@dataclass
class DeploymentMetadata:
    datasource_type: str = ""
    datasource_uid: str = ""
    datasource_name: str = ""
    query_language: str = ""
    runtime_rollups: list[str] = field(default_factory=list)


@dataclass
class OperationalIR:
    version: int = 1
    status: str = ""
    confidence: float = 0.0
    lineage: AssetLineage = field(default_factory=AssetLineage)
    review: ReviewState = field(default_factory=ReviewState)
    deployment: DeploymentMetadata = field(default_factory=DeploymentMetadata)
    artifacts: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_operational_ir(
    panel_result: Any,
    *,
    dashboard_title: str = "",
    dashboard_uid: str = "",
    source_file: str = "",
    folder_title: str = "",
    semantic_gate: str = "",
    verification_mode: str = "",
    validation_status: str = "not_run",
    artifacts: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> OperationalIR:
    return OperationalIR(
        status=str(getattr(panel_result, "status", "") or ""),
        confidence=_safe_float(getattr(panel_result, "confidence", 0.0)),
        lineage=AssetLineage(
            dashboard_title=str(dashboard_title or ""),
            dashboard_uid=str(dashboard_uid or ""),
            source_panel_id=str(getattr(panel_result, "source_panel_id", "") or ""),
            source_file=str(source_file or ""),
            folder_title=str(folder_title or ""),
        ),
        review=ReviewState(
            readiness=str(getattr(panel_result, "readiness", "") or ""),
            recommended_target=str(getattr(panel_result, "recommended_target", "") or ""),
            semantic_gate=str(semantic_gate or ""),
            verification_mode=str(verification_mode or ""),
            validation_status=str(validation_status or "not_run"),
            post_validation_action=str(getattr(panel_result, "post_validation_action", "") or ""),
            post_validation_message=str(getattr(panel_result, "post_validation_message", "") or ""),
        ),
        deployment=DeploymentMetadata(
            datasource_type=str(getattr(panel_result, "datasource_type", "") or ""),
            datasource_uid=str(getattr(panel_result, "datasource_uid", "") or ""),
            datasource_name=str(getattr(panel_result, "datasource_name", "") or ""),
            query_language=str(getattr(panel_result, "query_language", "") or ""),
            runtime_rollups=[str(item) for item in (getattr(panel_result, "runtime_rollups", []) or [])],
        ),
        artifacts=dict(artifacts or {}),
        metadata=dict(metadata or {}),
    )
