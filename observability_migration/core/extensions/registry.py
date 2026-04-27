# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Shared executable rule registry for source adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(order=True)
class RegisteredRule:
    priority: int
    id: str = field(compare=False)
    summary: str = field(compare=False)
    fn: Callable[[Any], str | None] = field(compare=False)


class RuleRegistry:
    """Priority-ordered executable rules with optional trace capture."""

    def __init__(self, name: str, stage: str):
        self.name = name
        self.stage = stage
        self._rules: list[RegisteredRule] = []

    def register(
        self,
        rule_id: str,
        *,
        priority: int = 100,
        summary: str = "",
    ) -> Callable[[Callable[[Any], str | None]], Callable[[Any], str | None]]:
        def decorator(fn: Callable[[Any], str | None]) -> Callable[[Any], str | None]:
            self.add(rule_id, fn, priority=priority, summary=summary or rule_id)
            return fn

        return decorator

    def add(
        self,
        rule_id: str,
        fn: Callable[[Any], str | None],
        *,
        priority: int = 100,
        summary: str = "",
    ) -> Callable[[Any], str | None]:
        self._rules.append(
            RegisteredRule(
                priority=priority,
                id=rule_id,
                summary=summary or rule_id,
                fn=fn,
            )
        )
        self._rules.sort()
        return fn

    def apply(
        self,
        context: Any,
        *,
        stop_when: Callable[[Any, str | None], bool] | None = None,
    ) -> Any:
        for rule in self._rules:
            detail = rule.fn(context)
            if detail is not None and hasattr(context, "trace"):
                context.trace.append(
                    {
                        "stage": self.stage,
                        "rule": rule.id,
                        "detail": detail,
                    }
                )
            if stop_when and stop_when(context, detail):
                break
        return context

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "id": rule.id,
                "summary": rule.summary,
                "priority": rule.priority,
                "registry": self.name,
                "stage": self.stage,
            }
            for rule in self._rules
        ]


__all__ = [
    "RegisteredRule",
    "RuleRegistry",
]
