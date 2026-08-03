# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Abstract base for target adapters (Kibana, future targets)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class TargetAdapter(ABC):
    """Contract for target-side artifact upload and post-upload validation.

    The former ``emit_dashboard`` / ``compile`` / ``validate_queries`` members
    described the removed dashboard-YAML artifact path (emit YAML -> compile to
    NDJSON -> lint/validate the YAML). A migration now writes native
    Dashboard-as-Code artifacts and uploads them through the target's typed
    API, so the contract is upload + smoke.
    """

    name: str  # e.g. "kibana"

    @abstractmethod
    def upload(self, artifact_dir: Path, **kwargs: Any) -> dict[str, Any]:
        """Upload the target-native artifacts under ``artifact_dir``.

        Returns a structured ``{"summary": ..., "records": [...]}`` payload.
        """

    @abstractmethod
    def smoke(self, **kwargs: Any) -> dict[str, Any]:
        """Run post-upload smoke validation and return a structured summary."""
