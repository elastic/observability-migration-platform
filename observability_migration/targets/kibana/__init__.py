# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Kibana target adapter.

Compile, emit, validate, upload, and smoke check dashboards
destined for Kibana/Elasticsearch.
"""

from .adapter import KibanaTargetAdapter

__all__ = ["KibanaTargetAdapter"]
