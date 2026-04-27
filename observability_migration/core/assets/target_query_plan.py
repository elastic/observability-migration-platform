"""Target query plan — rendered target query details.

Separated from QueryIR so the canonical semantic intent stays
independent of how the query is materialized on a specific target.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class TargetQueryPlan:
    """How a query materializes on a specific target (e.g. Kibana/ES)."""

    version: int = 1
    target_index: str = ""
    target_query: str = ""
    target_language: str = ""
    validation_status: str = "not_run"
    validation_errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
