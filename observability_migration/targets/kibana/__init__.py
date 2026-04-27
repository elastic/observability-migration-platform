"""Kibana target adapter.

Compile, emit, validate, upload, and smoke check dashboards
destined for Kibana/Elasticsearch.
"""

from .adapter import KibanaTargetAdapter

__all__ = ["KibanaTargetAdapter"]
