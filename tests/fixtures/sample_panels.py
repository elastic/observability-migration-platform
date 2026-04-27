"""Sample Grafana and Datadog panel fixtures for testing.

These constants provide minimal but complete panel structures that can be
used across test modules without each module re-inventing the same shapes.
"""

GRAFANA_TIMESERIES_PANEL = {
    "type": "timeseries",
    "title": "CPU Usage",
    "datasource": {"type": "prometheus", "uid": "prom1"},
    "targets": [
        {"expr": "rate(node_cpu_seconds_total[5m])", "refId": "A"},
    ],
    "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
    "fieldConfig": {
        "defaults": {"unit": "percent"},
    },
}

GRAFANA_STAT_PANEL = {
    "type": "stat",
    "title": "Uptime",
    "datasource": {"type": "prometheus", "uid": "prom1"},
    "targets": [
        {"expr": "up", "refId": "A"},
    ],
    "gridPos": {"x": 12, "y": 0, "w": 6, "h": 4},
}

GRAFANA_TABLE_PANEL = {
    "type": "table",
    "title": "Top Pods",
    "datasource": {"type": "prometheus", "uid": "prom1"},
    "targets": [
        {"expr": 'topk(10, container_memory_usage_bytes{namespace="default"})', "refId": "A"},
    ],
    "gridPos": {"x": 0, "y": 8, "w": 24, "h": 10},
}

GRAFANA_GAUGE_PANEL = {
    "type": "gauge",
    "title": "Disk Usage",
    "datasource": {"type": "prometheus", "uid": "prom1"},
    "targets": [
        {"expr": "node_filesystem_avail_bytes / node_filesystem_size_bytes * 100", "refId": "A"},
    ],
    "gridPos": {"x": 18, "y": 0, "w": 6, "h": 4},
    "fieldConfig": {
        "defaults": {"unit": "percent", "min": 0, "max": 100},
    },
}

GRAFANA_TEXT_PANEL = {
    "type": "text",
    "title": "Welcome",
    "options": {"content": "# Dashboard\n\nWelcome to the monitoring dashboard."},
    "gridPos": {"x": 0, "y": 18, "w": 24, "h": 4},
}

DATADOG_TIMESERIES_WIDGET = {
    "definition": {
        "type": "timeseries",
        "title": "Request Rate",
        "requests": [
            {
                "queries": [
                    {
                        "data_source": "metrics",
                        "name": "a",
                        "query": "avg:http.request.count{*} by {service}.as_rate()",
                    }
                ],
                "response_format": "timeseries",
                "display_type": "line",
            }
        ],
    },
    "layout": {"x": 0, "y": 0, "width": 4, "height": 2},
}

DATADOG_QUERY_VALUE_WIDGET = {
    "definition": {
        "type": "query_value",
        "title": "Error Rate",
        "requests": [
            {
                "queries": [
                    {
                        "data_source": "metrics",
                        "name": "a",
                        "query": "avg:http.error.rate{*}",
                    }
                ],
                "response_format": "scalar",
            }
        ],
    },
    "layout": {"x": 4, "y": 0, "width": 2, "height": 2},
}
