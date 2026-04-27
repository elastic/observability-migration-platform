# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Adapter interfaces and registries."""

from .registries import source_registry, target_registry
from .source_adapter import SourceAdapter
from .target_adapter import TargetAdapter

__all__ = [
    "SourceAdapter",
    "TargetAdapter",
    "source_registry",
    "target_registry",
]
