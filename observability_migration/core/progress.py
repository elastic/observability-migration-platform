# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Bounded stderr progress reporting for long-running CLI commands.

Kept separate from ``logging`` because nothing in this CLI configures a
logging handler today; a bare ``print(file=sys.stderr)`` guarantees the
message is actually visible without introducing that setup.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

ProgressFn = Callable[[str], None]


def stderr_progress(prefix: str) -> ProgressFn:
    """Return a callback that prints ``f"{prefix}: {message}"`` to stderr."""

    def _emit(message: str) -> None:
        print(f"{prefix}: {message}", file=sys.stderr, flush=True)

    return _emit


def null_progress(message: str) -> None:
    """No-op progress callback, used when progress output is suppressed."""
