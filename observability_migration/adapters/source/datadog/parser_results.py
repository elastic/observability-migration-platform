"""Structured parser diagnostics shared across Datadog parser modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ParserDiagnosticCode(str, Enum):
    """Stable diagnostic codes for parser degradation and failures."""

    METRIC_PARSE_ERROR = "METRIC_PARSE_ERROR"
    METRIC_TRAILING_TOKENS = "METRIC_TRAILING_TOKENS"
    FORMULA_PARSE_ERROR = "FORMULA_PARSE_ERROR"
    LOG_PARSE_ERROR = "LOG_PARSE_ERROR"
    LOG_TOKENIZER_SKIPPED_CHARS = "LOG_TOKENIZER_SKIPPED_CHARS"
    LOG_BOOLEAN_FALLBACK = "LOG_BOOLEAN_FALLBACK"


@dataclass(frozen=True)
class ParserDiagnostic:
    """A machine-readable parser diagnostic."""

    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    degraded: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "degraded": self.degraded,
        }
        if self.detail:
            payload["detail"] = dict(self.detail)
        return payload


@dataclass
class ParserResult:
    """Typed parser output with degradation metadata."""

    value: Any = None
    diagnostics: list[ParserDiagnostic] = field(default_factory=list)
    degraded: bool = False
    lossless: bool = True

