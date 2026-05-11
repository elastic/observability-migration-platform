"""Deterministic Prometheus metrics producer.

Mimics the surface of an express-prometheus-middleware-instrumented Node.js
service: exports http_requests_total, http_request_duration_seconds_bucket /
_count / _sum, process_cpu_seconds_total, process_resident_memory_bytes, plus
nodejs_version_info, node_uname_info, and node_memory_MemTotal_bytes so the
Grafana dashboard's variable queries also resolve.

Determinism: the counters increment at a fixed rate per (method, path, status)
on every scrape. Latency buckets advance proportionally. This means re-running
the rig produces identical numbers given the same start time + duration.

Endpoints:
    GET /metrics       Prometheus text exposition.
    POST /reset         Resets all counters (useful between test runs).
    GET /samples       Returns the producer's last-rendered snapshot as JSON
                        for the parity harness to cross-check against.
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

INSTANCES = (os.environ.get("PRODUCER_INSTANCE", "express-1:3000"),)
JOB = os.environ.get("PRODUCER_JOB", "express-app")
METHODS = ("GET", "POST")
PATHS = ("/users", "/orders", "/health")
STATUSES = ("200", "201", "400", "404", "500")
BUCKETS = ("0.005", "0.01", "0.025", "0.05", "0.1", "0.25", "0.5", "1.0", "2.5", "5.0", "10.0", "+Inf")
RATE_PER_COMBO_PER_SCRAPE = 1.0


class CounterRegistry:
    """Holds the simulated counters.

    The total elapsed wall-clock time at scrape ``t`` drives the values; this
    makes the producer deterministic but still live-looking. We avoid any
    randomness so the parity harness can reason in closed form.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._start_time = time.time()
        self._reset_time = self._start_time
        self._request_counts: dict[tuple[str, str, str, str, str], float] = defaultdict(float)
        self._bucket_counts: dict[tuple[str, str, str, str, str, str], float] = defaultdict(float)
        self._duration_sum: dict[tuple[str, str, str, str, str], float] = defaultdict(float)
        self._duration_count: dict[tuple[str, str, str, str, str], float] = defaultdict(float)

    def reset(self) -> None:
        with self._lock:
            self._reset_time = time.time()
            self._request_counts.clear()
            self._bucket_counts.clear()
            self._duration_sum.clear()
            self._duration_count.clear()

    def _materialize(self) -> dict[str, Any]:
        now = time.time()
        elapsed = max(0.0, now - self._reset_time)
        # 1 request per (instance, method, path, status) combo per second.
        with self._lock:
            for instance in INSTANCES:
                for method in METHODS:
                    for path in PATHS:
                        for status in STATUSES:
                            key = (instance, JOB, method, path, status)
                            self._request_counts[key] = elapsed
                            # Histogram: 0.1 buckets see all requests; everything below 5ms is 30 %; ramp up.
                            cumulative_share = 0.0
                            for bucket in BUCKETS:
                                if bucket == "0.005":
                                    cumulative_share = 0.30
                                elif bucket == "0.01":
                                    cumulative_share = 0.40
                                elif bucket == "0.025":
                                    cumulative_share = 0.60
                                elif bucket == "0.05":
                                    cumulative_share = 0.75
                                elif bucket == "0.1":
                                    cumulative_share = 0.85
                                elif bucket == "0.25":
                                    cumulative_share = 0.92
                                elif bucket == "0.5":
                                    cumulative_share = 0.96
                                elif bucket == "1":
                                    cumulative_share = 0.98
                                elif bucket == "2.5":
                                    cumulative_share = 0.995
                                elif bucket == "5":
                                    cumulative_share = 0.999
                                else:
                                    cumulative_share = 1.0
                                bkey = (instance, JOB, method, path, status, bucket)
                                self._bucket_counts[bkey] = elapsed * cumulative_share
                            dkey = key
                            self._duration_count[dkey] = elapsed
                            # Mean ≈ 30 ms (sum/count): 0.030
                            self._duration_sum[dkey] = elapsed * 0.030

        return {
            "request_counts": dict(self._request_counts),
            "bucket_counts": dict(self._bucket_counts),
            "duration_sum": dict(self._duration_sum),
            "duration_count": dict(self._duration_count),
            "wall_clock_now": now,
            "reset_time": self._reset_time,
            "elapsed": elapsed,
        }

    def render_prometheus(self) -> str:
        snapshot = self._materialize()
        lines: list[str] = []

        lines.append("# HELP http_requests_total Total HTTP requests")
        lines.append("# TYPE http_requests_total counter")
        for (instance, job, method, path, status), v in snapshot["request_counts"].items():
            lines.append(
                f'http_requests_total{{instance="{instance}",job="{job}",method="{method}",'
                f'path="{path}",status="{status}"}} {v}'
            )

        lines.append("# HELP http_request_duration_seconds Histogram of response latency (seconds)")
        lines.append("# TYPE http_request_duration_seconds histogram")
        for (instance, job, method, path, status, bucket), v in snapshot["bucket_counts"].items():
            lines.append(
                f'http_request_duration_seconds_bucket{{instance="{instance}",job="{job}",method="{method}",'
                f'path="{path}",status="{status}",le="{bucket}"}} {v}'
            )
        for (instance, job, method, path, status), v in snapshot["duration_sum"].items():
            lines.append(
                f'http_request_duration_seconds_sum{{instance="{instance}",job="{job}",method="{method}",'
                f'path="{path}",status="{status}"}} {v}'
            )
        for (instance, job, method, path, status), v in snapshot["duration_count"].items():
            lines.append(
                f'http_request_duration_seconds_count{{instance="{instance}",job="{job}",method="{method}",'
                f'path="{path}",status="{status}"}} {v}'
            )

        lines.append("# HELP process_cpu_seconds_total Total CPU seconds used")
        lines.append("# TYPE process_cpu_seconds_total counter")
        for instance in INSTANCES:
            lines.append(f'process_cpu_seconds_total{{instance="{instance}",job="{JOB}"}} {snapshot["elapsed"] * 0.05}')

        lines.append("# HELP process_resident_memory_bytes Resident memory")
        lines.append("# TYPE process_resident_memory_bytes gauge")
        for instance in INSTANCES:
            lines.append(f'process_resident_memory_bytes{{instance="{instance}",job="{JOB}"}} {1024 * 1024 * 128}')

        lines.append("# HELP nodejs_version_info Node version")
        lines.append("# TYPE nodejs_version_info gauge")
        for instance in INSTANCES:
            labels = (
                f'instance="{instance}",job="{JOB}",version="v20.10.0",'
                'major="20",minor="10",patch="0"'
            )
            lines.append(f"nodejs_version_info{{{labels}}} 1")

        lines.append("# HELP node_uname_info node_exporter uname")
        lines.append("# TYPE node_uname_info gauge")
        for instance in INSTANCES:
            labels = (
                f'instance="{instance}",job="{JOB}",nodename="parity-rig",'
                'release="6.10.0",sysname="Linux",machine="x86_64"'
            )
            lines.append(f"node_uname_info{{{labels}}} 1")

        lines.append("# HELP node_memory_MemTotal_bytes Total memory")
        lines.append("# TYPE node_memory_MemTotal_bytes gauge")
        for instance in INSTANCES:
            lines.append(f'node_memory_MemTotal_bytes{{instance="{instance}",job="{JOB}"}} {1024 * 1024 * 1024 * 8}')

        # `up` series for the Prometheus targets variable
        lines.append("# HELP up Whether the target was scraped")
        lines.append("# TYPE up gauge")
        for instance in INSTANCES:
            lines.append(f'up{{instance="{instance}",job="{JOB}"}} 1')

        return "\n".join(lines) + "\n"

    def snapshot_json(self) -> dict[str, Any]:
        snapshot = self._materialize()
        return {
            "elapsed_seconds": snapshot["elapsed"],
            "reset_time": snapshot["reset_time"],
            "now": snapshot["wall_clock_now"],
            "request_counts": [
                {"instance": k[0], "job": k[1], "method": k[2], "path": k[3], "status": k[4], "value": v}
                for k, v in snapshot["request_counts"].items()
            ],
            "bucket_counts": [
                {
                    "instance": k[0], "job": k[1], "method": k[2], "path": k[3],
                    "status": k[4], "le": k[5], "value": v,
                }
                for k, v in snapshot["bucket_counts"].items()
            ],
        }


REGISTRY = CounterRegistry()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:  # silence default access log
        return

    def do_GET(self) -> None:
        if self.path == "/metrics":
            body = REGISTRY.render_prometheus().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/samples":
            import json

            body = json.dumps(REGISTRY.snapshot_json()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        if self.path == "/reset":
            REGISTRY.reset()
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3000"))
    print(f"producer listening on :{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
