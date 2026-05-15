"""Data model for the 5-tier panel verifier.

The :class:`PanelRecord` captures every representation of a panel's
query across the translation/upload/render pipeline so that pairwise
comparisons can pinpoint exactly where a translation regressed.

Verdict vocabulary intentionally mirrors ``parity-rig/harness/parity.py``
so the existing aggregate runner (``run-all-parity.sh``) can fold the
verifier's output into the same dashboard-level summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    """Aggregate per-panel verdict.

    PASS / DRIFT / FAIL are tier-comparison outcomes; the existing
    PromQL-numeric verdicts (STRICT_PASS, FUZZY_PASS, SHAPE_PASS,
    FAIL_NO_OVERLAP) live in ``parity-rig/harness/parity.py`` and are
    surfaced as a sub-axis of :class:`PanelRecord`.
    """

    PASS = "PASS"
    DRIFT = "DRIFT"
    FAIL = "FAIL"
    SKIP = "SKIP"
    NOT_FEASIBLE = "NOT_FEASIBLE"
    NOT_UPLOADED = "NOT_UPLOADED"
    ERROR = "ERROR"


DRIFT_AXES = (
    "T0=T1",  # source -> translator
    "T1=T2",  # translator -> yaml
    "T2=T3",  # yaml -> compiled ndjson
    "T3=T4",  # compiled ndjson -> cluster saved object
    "T4=T5",  # cluster saved object -> live _query body
)


@dataclass
class PanelRecord:
    """All five tiers for a single dashboard panel."""

    panel_id: str
    title: str
    dashboard_uid: str
    dashboard_title: str

    grafana_type: str = ""
    kibana_type: str = ""
    status: str = ""  # "migrated" / "not_feasible" / "manual" / ...
    feasibility: str = ""  # "feasible" / "not_feasible" / ...

    t0_source_promql: str = ""
    t1_translator_esql: str = ""
    t2_yaml_esql: str = ""
    t3_ndjson_esql: str = ""
    t4_cluster_esql: str = ""
    t5_live_query_body: str = ""

    t1_native_promql: bool = False
    t1_index: str = ""
    t1_warnings: list[str] = field(default_factory=list)
    t1_notes: list[str] = field(default_factory=list)

    t4_saved_object_id: str = ""
    t4_saved_object_updated_at: str = ""

    t5_response_status: int = 0
    t5_response_columns: list[str] = field(default_factory=list)
    t5_response_row_count: int = 0
    t5_response_error: str = ""

    drift_axes: list[str] = field(default_factory=list)
    drift_details: dict[str, str] = field(default_factory=dict)
    verdict: Verdict = Verdict.SKIP
    notes: list[str] = field(default_factory=list)

    visual_diff_path: str = ""
    visual_diff_score: float = 0.0
    visual_diff_threshold: float = 0.0
    grafana_screenshot_path: str = ""
    kibana_screenshot_path: str = ""

    suspense_status: str = ""  # "ok" | "stuck" | "n/a"
    har_path: str = ""

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "panel_id": self.panel_id,
            "title": self.title,
            "dashboard_uid": self.dashboard_uid,
            "dashboard_title": self.dashboard_title,
            "grafana_type": self.grafana_type,
            "kibana_type": self.kibana_type,
            "status": self.status,
            "feasibility": self.feasibility,
            "tiers": {
                "t0_source_promql": self.t0_source_promql,
                "t1_translator_esql": self.t1_translator_esql,
                "t2_yaml_esql": self.t2_yaml_esql,
                "t3_ndjson_esql": self.t3_ndjson_esql,
                "t4_cluster_esql": self.t4_cluster_esql,
                "t5_live_query_body": self.t5_live_query_body,
            },
            "translator": {
                "native_promql": self.t1_native_promql,
                "index": self.t1_index,
                "warnings": list(self.t1_warnings),
                "notes": list(self.t1_notes),
            },
            "cluster": {
                "saved_object_id": self.t4_saved_object_id,
                "updated_at": self.t4_saved_object_updated_at,
            },
            "live": {
                "response_status": self.t5_response_status,
                "response_columns": list(self.t5_response_columns),
                "response_row_count": self.t5_response_row_count,
                "response_error": self.t5_response_error,
            },
            "visual": {
                "diff_path": self.visual_diff_path,
                "diff_score": self.visual_diff_score,
                "diff_threshold": self.visual_diff_threshold,
                "grafana_screenshot": self.grafana_screenshot_path,
                "kibana_screenshot": self.kibana_screenshot_path,
            },
            "browser": {
                "suspense_status": self.suspense_status,
                "har_path": self.har_path,
            },
            "drift_axes": list(self.drift_axes),
            "drift_details": dict(self.drift_details),
            "verdict": self.verdict.value,
            "notes": list(self.notes),
        }

    @classmethod
    def from_jsonable(cls, blob: dict[str, Any]) -> PanelRecord:
        tiers = blob.get("tiers", {})
        translator = blob.get("translator", {})
        cluster = blob.get("cluster", {})
        live = blob.get("live", {})
        visual = blob.get("visual", {})
        browser = blob.get("browser", {})
        return cls(
            panel_id=blob.get("panel_id", ""),
            title=blob.get("title", ""),
            dashboard_uid=blob.get("dashboard_uid", ""),
            dashboard_title=blob.get("dashboard_title", ""),
            grafana_type=blob.get("grafana_type", ""),
            kibana_type=blob.get("kibana_type", ""),
            status=blob.get("status", ""),
            feasibility=blob.get("feasibility", ""),
            t0_source_promql=tiers.get("t0_source_promql", ""),
            t1_translator_esql=tiers.get("t1_translator_esql", ""),
            t2_yaml_esql=tiers.get("t2_yaml_esql", ""),
            t3_ndjson_esql=tiers.get("t3_ndjson_esql", ""),
            t4_cluster_esql=tiers.get("t4_cluster_esql", ""),
            t5_live_query_body=tiers.get("t5_live_query_body", ""),
            t1_native_promql=bool(translator.get("native_promql", False)),
            t1_index=translator.get("index", ""),
            t1_warnings=list(translator.get("warnings", [])),
            t1_notes=list(translator.get("notes", [])),
            t4_saved_object_id=cluster.get("saved_object_id", ""),
            t4_saved_object_updated_at=cluster.get("updated_at", ""),
            t5_response_status=int(live.get("response_status", 0)),
            t5_response_columns=list(live.get("response_columns", [])),
            t5_response_row_count=int(live.get("response_row_count", 0)),
            t5_response_error=live.get("response_error", ""),
            visual_diff_path=visual.get("diff_path", ""),
            visual_diff_score=float(visual.get("diff_score", 0.0) or 0.0),
            visual_diff_threshold=float(visual.get("diff_threshold", 0.0) or 0.0),
            grafana_screenshot_path=visual.get("grafana_screenshot", ""),
            kibana_screenshot_path=visual.get("kibana_screenshot", ""),
            suspense_status=browser.get("suspense_status", ""),
            har_path=browser.get("har_path", ""),
            drift_axes=list(blob.get("drift_axes", [])),
            drift_details=dict(blob.get("drift_details", {})),
            verdict=Verdict(blob.get("verdict", Verdict.SKIP.value)),
            notes=list(blob.get("notes", [])),
        )
