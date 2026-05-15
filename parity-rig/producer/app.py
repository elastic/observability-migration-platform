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


# ---------------------------------------------------------------------------
# Synthetic kube-state-metrics + cAdvisor emulator
# ---------------------------------------------------------------------------
#
# The Kubernetes / Views / Global fixture dashboard expects metrics in the
# shape kube-state-metrics and cAdvisor produce. We emit a small but
# self-consistent slice deterministically so the parity harness can compare
# the same numbers on both sides without standing up a real K8s cluster.
#
# Coverage:
# - kube_node_info, kube_node_role, kube_namespace_labels, kube_namespace_created
# - kube_pod_info, kube_pod_container_info
# - kube_pod_container_status_running / waiting / terminated / restarts_total
# - container_cpu_usage_seconds_total (counter), container_memory_working_set_bytes (gauge)
# - container_network_receive_bytes_total / transmit_bytes_total (counters)
# - node_cpu_core_throttles_total (counter)
# - node_network_receive_bytes_total / transmit_bytes_total / receive_drop_total / transmit_drop_total (counters)

K8S_CLUSTER = "parity-cluster"
K8S_NODES = ("node-1", "node-2")
K8S_NAMESPACES = ("default", "kube-system", "monitoring")
K8S_PODS_BY_NS = {
    "default": ("app-1", "app-2"),
    "kube-system": ("kube-dns",),
    "monitoring": ("prometheus-0", "grafana-0"),
}
K8S_CONTAINERS_BY_POD = {
    "app-1": ("server",),
    "app-2": ("server", "sidecar"),
    "kube-dns": ("coredns",),
    "prometheus-0": ("prometheus", "config-reloader"),
    "grafana-0": ("grafana",),
}
K8S_NETWORK_INTERFACES = ("eth0", "lo")


def render_kube_state_metrics() -> str:
    now = time.time()
    elapsed = max(0.0, now - REGISTRY._reset_time)
    lines: list[str] = []

    lines.append("# HELP kube_node_info Information about each Node")
    lines.append("# TYPE kube_node_info gauge")
    for n in K8S_NODES:
        lines.append(
            f'kube_node_info{{node="{n}",cluster="{K8S_CLUSTER}",'
            f'kernel_version="6.10.0",os_image="Linux"}} 1'
        )

    lines.append("# HELP kube_node_role Node roles")
    lines.append("# TYPE kube_node_role gauge")
    for n in K8S_NODES:
        role = "master" if n == "node-1" else "worker"
        lines.append(f'kube_node_role{{node="{n}",cluster="{K8S_CLUSTER}",role="{role}"}} 1')

    lines.append("# HELP kube_namespace_labels Kubernetes labels on the namespace")
    lines.append("# TYPE kube_namespace_labels gauge")
    for ns in K8S_NAMESPACES:
        lines.append(f'kube_namespace_labels{{namespace="{ns}",cluster="{K8S_CLUSTER}"}} 1')

    lines.append("# HELP kube_namespace_created Unix timestamp when the namespace was created")
    lines.append("# TYPE kube_namespace_created gauge")
    for ns in K8S_NAMESPACES:
        lines.append(f'kube_namespace_created{{namespace="{ns}",cluster="{K8S_CLUSTER}"}} {REGISTRY._reset_time}')

    lines.append("# HELP kube_pod_info Information about each pod")
    lines.append("# TYPE kube_pod_info gauge")
    for ns, pods in K8S_PODS_BY_NS.items():
        for i, pod in enumerate(pods):
            node = K8S_NODES[i % len(K8S_NODES)]
            lines.append(
                f'kube_pod_info{{namespace="{ns}",pod="{pod}",node="{node}",'
                f'cluster="{K8S_CLUSTER}"}} 1'
            )

    lines.append("# HELP kube_pod_container_info Information about each container in each pod")
    lines.append("# TYPE kube_pod_container_info gauge")
    for ns, pods in K8S_PODS_BY_NS.items():
        for pod in pods:
            for container in K8S_CONTAINERS_BY_POD.get(pod, ()):
                lines.append(
                    f'kube_pod_container_info{{namespace="{ns}",pod="{pod}",'
                    f'container="{container}",image="example/{container}:v1",'
                    f'image_id="example/{container}@sha256:deadbeef",'
                    f'cluster="{K8S_CLUSTER}"}} 1'
                )

    lines.append("# HELP kube_pod_container_status_running Containers currently running")
    lines.append("# TYPE kube_pod_container_status_running gauge")
    for ns, pods in K8S_PODS_BY_NS.items():
        for pod in pods:
            for container in K8S_CONTAINERS_BY_POD.get(pod, ()):
                lines.append(
                    f'kube_pod_container_status_running{{namespace="{ns}",pod="{pod}",'
                    f'container="{container}",cluster="{K8S_CLUSTER}"}} 1'
                )

    lines.append("# HELP kube_pod_container_status_waiting Containers currently waiting")
    lines.append("# TYPE kube_pod_container_status_waiting gauge")
    for ns, pods in K8S_PODS_BY_NS.items():
        for pod in pods:
            for container in K8S_CONTAINERS_BY_POD.get(pod, ()):
                lines.append(
                    f'kube_pod_container_status_waiting{{namespace="{ns}",pod="{pod}",'
                    f'container="{container}",cluster="{K8S_CLUSTER}"}} 0'
                )

    lines.append("# HELP kube_pod_container_status_terminated Containers currently terminated")
    lines.append("# TYPE kube_pod_container_status_terminated gauge")
    for ns, pods in K8S_PODS_BY_NS.items():
        for pod in pods:
            for container in K8S_CONTAINERS_BY_POD.get(pod, ()):
                lines.append(
                    f'kube_pod_container_status_terminated{{namespace="{ns}",pod="{pod}",'
                    f'container="{container}",cluster="{K8S_CLUSTER}"}} 0'
                )

    lines.append("# HELP kube_pod_container_status_restarts_total Container restart count")
    lines.append("# TYPE kube_pod_container_status_restarts_total counter")
    for ns, pods in K8S_PODS_BY_NS.items():
        for pod in pods:
            for container in K8S_CONTAINERS_BY_POD.get(pod, ()):
                # 1 restart every 600s, deterministically.
                restarts = int(elapsed / 600)
                lines.append(
                    f'kube_pod_container_status_restarts_total{{namespace="{ns}",pod="{pod}",'
                    f'container="{container}",cluster="{K8S_CLUSTER}"}} {restarts}'
                )

    lines.append("# HELP container_cpu_usage_seconds_total Cumulative CPU usage of the container")
    lines.append("# TYPE container_cpu_usage_seconds_total counter")
    for ns, pods in K8S_PODS_BY_NS.items():
        for pod in pods:
            for container in K8S_CONTAINERS_BY_POD.get(pod, ()):
                cpu_seconds = elapsed * 0.05
                lines.append(
                    f'container_cpu_usage_seconds_total{{namespace="{ns}",pod="{pod}",'
                    f'container="{container}",image="example/{container}:v1",'
                    f'container_id="docker://{pod}-{container}",cluster="{K8S_CLUSTER}"}} {cpu_seconds}'
                )

    lines.append("# HELP container_memory_working_set_bytes Current working set bytes")
    lines.append("# TYPE container_memory_working_set_bytes gauge")
    for ns, pods in K8S_PODS_BY_NS.items():
        for pod in pods:
            for container in K8S_CONTAINERS_BY_POD.get(pod, ()):
                bytes_val = 64 * 1024 * 1024 + (hash(pod + container) % (32 * 1024 * 1024))
                lines.append(
                    f'container_memory_working_set_bytes{{namespace="{ns}",pod="{pod}",'
                    f'container="{container}",image="example/{container}:v1",'
                    f'container_id="docker://{pod}-{container}",cluster="{K8S_CLUSTER}"}} {bytes_val}'
                )

    lines.append("# HELP container_network_receive_bytes_total Cumulative count of bytes received")
    lines.append("# TYPE container_network_receive_bytes_total counter")
    lines.append("# HELP container_network_transmit_bytes_total Cumulative count of bytes transmitted")
    lines.append("# TYPE container_network_transmit_bytes_total counter")
    for ns, pods in K8S_PODS_BY_NS.items():
        for pod in pods:
            rx = elapsed * 1000
            tx = elapsed * 500
            lines.append(
                f'container_network_receive_bytes_total{{namespace="{ns}",pod="{pod}",'
                f'cluster="{K8S_CLUSTER}"}} {rx}'
            )
            lines.append(
                f'container_network_transmit_bytes_total{{namespace="{ns}",pod="{pod}",'
                f'cluster="{K8S_CLUSTER}"}} {tx}'
            )

    lines.append("# HELP node_cpu_core_throttles_total Number of CPU throttling events")
    lines.append("# TYPE node_cpu_core_throttles_total counter")
    for node in K8S_NODES:
        for cpu in (0, 1, 2, 3):
            throttles = int(elapsed * 0.01)
            lines.append(
                f'node_cpu_core_throttles_total{{instance="{node}",cluster="{K8S_CLUSTER}",'
                f'core="{cpu}",package="0"}} {throttles}'
            )

    lines.append("# HELP node_network_receive_bytes_total Network device statistic receive_bytes")
    lines.append("# TYPE node_network_receive_bytes_total counter")
    lines.append("# HELP node_network_transmit_bytes_total Network device statistic transmit_bytes")
    lines.append("# TYPE node_network_transmit_bytes_total counter")
    lines.append("# HELP node_network_receive_drop_total Network device statistic receive_drop")
    lines.append("# TYPE node_network_receive_drop_total counter")
    lines.append("# HELP node_network_transmit_drop_total Network device statistic transmit_drop")
    lines.append("# TYPE node_network_transmit_drop_total counter")
    for node in K8S_NODES:
        for device in K8S_NETWORK_INTERFACES:
            base = 10000 if device == "eth0" else 100
            lines.append(
                f'node_network_receive_bytes_total{{instance="{node}",cluster="{K8S_CLUSTER}",'
                f'device="{device}",job="node-exporter"}} {elapsed * base}'
            )
            lines.append(
                f'node_network_transmit_bytes_total{{instance="{node}",cluster="{K8S_CLUSTER}",'
                f'device="{device}",job="node-exporter"}} {elapsed * base * 0.4}'
            )
            lines.append(
                f'node_network_receive_drop_total{{instance="{node}",cluster="{K8S_CLUSTER}",'
                f'device="{device}",job="node-exporter"}} {int(elapsed * 0.05)}'
            )
            lines.append(
                f'node_network_transmit_drop_total{{instance="{node}",cluster="{K8S_CLUSTER}",'
                f'device="{device}",job="node-exporter"}} {int(elapsed * 0.03)}'
            )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Synthetic node-exporter "extras" — metrics that depend on host kernel
# features or hardware sysfs paths the producer's Docker container can't
# expose (hwmon/thermal sysfs, /proc/schedstat with CONFIG_SCHEDSTATS,
# /proc/meminfo lines like HardwareCorrupted / DirectMap1G that aren't
# in macOS Docker Desktop's VM, systemd dbus, custom textfile exports).
#
# Surfaced by validating the canonical Node Exporter Full (id 1860)
# dashboard against the rig: after enabling every applicable node-exporter
# collector, 10 metrics remained absent because the container simply
# can't see them. Emitting them deterministically lets the corresponding
# panels render against real numbers rather than reporting "Unknown
# column".
def render_node_extras_metrics() -> str:
    now = time.time()
    elapsed = max(0.0, now - REGISTRY._reset_time)
    lines: list[str] = []

    # /proc/meminfo lines that macOS Docker Desktop's VM kernel doesn't
    # export. Stable numbers; the dashboard uses them as gauges in
    # informational panels.
    lines.append("# HELP node_memory_HardwareCorrupted_bytes /proc/meminfo HardwareCorrupted")
    lines.append("# TYPE node_memory_HardwareCorrupted_bytes gauge")
    lines.append('node_memory_HardwareCorrupted_bytes{instance="node-1:9100"} 0')

    lines.append("# HELP node_memory_DirectMap1G_bytes /proc/meminfo DirectMap1G")
    lines.append("# TYPE node_memory_DirectMap1G_bytes gauge")
    lines.append(f'node_memory_DirectMap1G_bytes{{instance="node-1:9100"}} {2 * 1024 * 1024 * 1024}')
    lines.append("# HELP node_memory_DirectMap2M_bytes /proc/meminfo DirectMap2M")
    lines.append("# TYPE node_memory_DirectMap2M_bytes gauge")
    lines.append(f'node_memory_DirectMap2M_bytes{{instance="node-1:9100"}} {6 * 1024 * 1024 * 1024}')
    lines.append("# HELP node_memory_DirectMap4k_bytes /proc/meminfo DirectMap4k")
    lines.append("# TYPE node_memory_DirectMap4k_bytes gauge")
    lines.append(f'node_memory_DirectMap4k_bytes{{instance="node-1:9100"}} {128 * 1024 * 1024}')

    # CPU frequency scaling. cpufreq sysfs is absent on macOS Docker
    # Desktop's VM but every cloud Linux node will have it.
    lines.append("# HELP node_cpu_scaling_frequency_hertz CPU current scaling frequency")
    lines.append("# TYPE node_cpu_scaling_frequency_hertz gauge")
    lines.append("# HELP node_cpu_scaling_frequency_max_hertz CPU max scaling frequency")
    lines.append("# TYPE node_cpu_scaling_frequency_max_hertz gauge")
    lines.append("# HELP node_cpu_scaling_frequency_min_hertz CPU min scaling frequency")
    lines.append("# TYPE node_cpu_scaling_frequency_min_hertz gauge")
    for cpu in (0, 1, 2, 3):
        # 2 GHz base, varying around ±200 MHz per core deterministically.
        cur = 2_000_000_000 + (cpu * 50_000_000) + int(100_000_000 * ((elapsed % 30) / 30))
        lines.append(f'node_cpu_scaling_frequency_hertz{{instance="node-1:9100",cpu="{cpu}"}} {cur}')
        lines.append(f'node_cpu_scaling_frequency_max_hertz{{instance="node-1:9100",cpu="{cpu}"}} 3000000000')
        lines.append(f'node_cpu_scaling_frequency_min_hertz{{instance="node-1:9100",cpu="{cpu}"}} 1000000000')

    # /proc/schedstat (requires CONFIG_SCHEDSTATS). Cumulative cpu-time
    # spent waiting on the run queue, per cpu.
    lines.append("# HELP node_schedstat_waiting_seconds_total /proc/schedstat waiting")
    lines.append("# TYPE node_schedstat_waiting_seconds_total counter")
    lines.append("# HELP node_schedstat_running_seconds_total /proc/schedstat running")
    lines.append("# TYPE node_schedstat_running_seconds_total counter")
    lines.append("# HELP node_schedstat_timeslices_total /proc/schedstat timeslices")
    lines.append("# TYPE node_schedstat_timeslices_total counter")
    for cpu in (0, 1, 2, 3):
        lines.append(f'node_schedstat_waiting_seconds_total{{instance="node-1:9100",cpu="{cpu}"}} {elapsed * (0.001 + 0.0002 * cpu)}')
        lines.append(f'node_schedstat_running_seconds_total{{instance="node-1:9100",cpu="{cpu}"}} {elapsed * (0.02 + 0.005 * cpu)}')
        lines.append(f'node_schedstat_timeslices_total{{instance="node-1:9100",cpu="{cpu}"}} {int(elapsed * (100 + 10 * cpu))}')

    # Hardware sensors. lm-sensors / hwmon sysfs isn't available inside
    # a generic container; cloud Linux hosts expose it via
    # /sys/class/hwmon/*.
    lines.append("# HELP node_hwmon_temp_celsius Hardware monitor temperature")
    lines.append("# TYPE node_hwmon_temp_celsius gauge")
    lines.append("# HELP node_hwmon_fan_rpm Hardware monitor fan RPM")
    lines.append("# TYPE node_hwmon_fan_rpm gauge")
    lines.append("# HELP node_hwmon_temp_max_celsius Hardware monitor max temperature")
    lines.append("# TYPE node_hwmon_temp_max_celsius gauge")
    for sensor, base in (("Core 0", 45.0), ("Core 1", 47.0), ("Package id 0", 50.0)):
        cycle = 5.0 * ((elapsed % 60) / 60)
        lines.append(
            f'node_hwmon_temp_celsius{{instance="node-1:9100",chip="coretemp-isa-0000",'
            f'sensor="temp1",chip_name="coretemp",label="{sensor}"}} {base + cycle:.2f}'
        )
        lines.append(
            f'node_hwmon_temp_max_celsius{{instance="node-1:9100",chip="coretemp-isa-0000",'
            f'sensor="temp1",chip_name="coretemp",label="{sensor}"}} 90.0'
        )
    for fan, rpm_base in (("fan1", 1500), ("fan2", 1700)):
        rpm = rpm_base + int(50 * ((elapsed % 30) / 30))
        lines.append(
            f'node_hwmon_fan_rpm{{instance="node-1:9100",chip="nct6775-isa-0290",'
            f'sensor="{fan}",chip_name="nct6775"}} {rpm}'
        )

    # Thermal zones.
    lines.append("# HELP node_cooling_device_cur_state Linux thermal_zone cur_state")
    lines.append("# TYPE node_cooling_device_cur_state gauge")
    lines.append("# HELP node_cooling_device_max_state Linux thermal_zone max_state")
    lines.append("# TYPE node_cooling_device_max_state gauge")
    for tz in (0, 1):
        lines.append(f'node_cooling_device_cur_state{{instance="node-1:9100",name="thermal_zone{tz}",type="Processor"}} {tz}')
        lines.append(f'node_cooling_device_max_state{{instance="node-1:9100",name="thermal_zone{tz}",type="Processor"}} 4')

    # systemd unit states. Requires systemd dbus in a real install.
    lines.append("# HELP node_systemd_units Number of systemd units")
    lines.append("# TYPE node_systemd_units gauge")
    for state, count in (("active", 92), ("inactive", 14), ("failed", 0)):
        lines.append(f'node_systemd_units{{instance="node-1:9100",state="{state}"}} {count}')
    lines.append("# HELP node_systemd_unit_state Systemd unit state")
    lines.append("# TYPE node_systemd_unit_state gauge")
    for name in ("ssh.service", "cron.service", "rsyslog.service"):
        for state in ("active", "inactive", "failed", "activating", "deactivating"):
            v = 1 if state == "active" else 0
            lines.append(
                f'node_systemd_unit_state{{instance="node-1:9100",name="{name}",state="{state}"}} {v}'
            )

    # netstat lines that some kernels don't expose. The translated ESQL
    # references these directly so a single sample per metric is enough
    # to flip the field from "Unknown column" to "valid".
    lines.append("# HELP node_netstat_TcpExt_TCPRcvQDrop /proc/net/netstat TCPRcvQDrop")
    lines.append("# TYPE node_netstat_TcpExt_TCPRcvQDrop counter")
    lines.append(
        f'node_netstat_TcpExt_TCPRcvQDrop{{instance="node-1:9100"}} {int(elapsed * 0.001)}'
    )
    lines.append("# HELP node_netstat_Tcp_MaxConn /proc/net/netstat Tcp MaxConn")
    lines.append("# TYPE node_netstat_Tcp_MaxConn gauge")
    lines.append('node_netstat_Tcp_MaxConn{instance="node-1:9100"} -1')

    # Custom textfile metric some operators add; not stock node-exporter.
    lines.append("# HELP node_tcp_connection_states TCP connection state counts")
    lines.append("# TYPE node_tcp_connection_states gauge")
    for state, n in (
        ("established", 80),
        ("listen", 22),
        ("time_wait", 14),
        ("close_wait", 2),
        ("syn_sent", 0),
    ):
        lines.append(
            f'node_tcp_connection_states{{instance="node-1:9100",state="{state}"}} {n}'
        )

    # IRQ PSI metric added in node-exporter 1.9+. v1.8 doesn't have it
    # even with --collector.pressure enabled. The Node Exporter Full
    # dashboard's "Pressure" / "Pressure Stall Information" panels reference
    # it directly, so we emit a deterministic ramp so the panel renders.
    lines.append("# HELP node_pressure_irq_stalled_seconds_total IRQ pressure stall")
    lines.append("# TYPE node_pressure_irq_stalled_seconds_total counter")
    lines.append(
        f'node_pressure_irq_stalled_seconds_total{{instance="node-1:9100"}} '
        f'{elapsed * 0.0005:.6f}'
    )
    lines.append("# HELP node_pressure_io_stalled_seconds_total I/O pressure stall")
    lines.append("# TYPE node_pressure_io_stalled_seconds_total counter")
    lines.append(
        f'node_pressure_io_stalled_seconds_total{{instance="node-1:9100"}} '
        f'{elapsed * 0.0015:.6f}'
    )
    lines.append("# HELP node_pressure_memory_stalled_seconds_total Memory pressure stall")
    lines.append("# TYPE node_pressure_memory_stalled_seconds_total counter")
    lines.append(
        f'node_pressure_memory_stalled_seconds_total{{instance="node-1:9100"}} '
        f'{elapsed * 0.0002:.6f}'
    )

    # node-exporter's textfile collector signal. Some panels reference
    # it directly to highlight stale or malformed textfile exports.
    lines.append("# HELP node_textfile_scrape_error Textfile scrape error indicator")
    lines.append("# TYPE node_textfile_scrape_error gauge")
    lines.append('node_textfile_scrape_error{instance="node-1:9100"} 0')

    # Power-supply state (laptop/desktop only, but real). Reflect a
    # plugged-in AC adapter so the panel shows a value rather than
    # "No results found".
    lines.append("# HELP node_power_supply_online Power supply online state")
    lines.append("# TYPE node_power_supply_online gauge")
    for supply in ("AC0", "BAT0"):
        lines.append(
            f'node_power_supply_online{{instance="node-1:9100",power_supply="{supply}"}} '
            f'{1 if supply.startswith("AC") else 0}'
        )

    # ARP table entries per network interface.
    lines.append("# HELP node_arp_entries Number of ARP entries per device")
    lines.append("# TYPE node_arp_entries gauge")
    for device in ("eth0", "wlan0"):
        n = 12 if device == "eth0" else 4
        lines.append(
            f'node_arp_entries{{instance="node-1:9100",device="{device}"}} {n}'
        )

    # Synthetic root filesystem mount so the Node Exporter Full
    # ``Root FS Used`` gauge and ``RootFS Total`` stat render. The
    # producer's node-exporter container only sees its bind-mounted
    # overlay paths (``/etc/resolv.conf`` etc.) — not the host's actual
    # ``/`` mountpoint — so the dashboard's ``mountpoint="/"`` filter
    # matches nothing. Emit a stable ext4 root mountpoint with realistic
    # size/avail values; other panels filter on bind-mount paths so
    # they're unaffected.
    lines.append("# HELP node_filesystem_size_bytes Filesystem size in bytes")
    lines.append("# TYPE node_filesystem_size_bytes gauge")
    lines.append("# HELP node_filesystem_avail_bytes Filesystem space available to non-root users in bytes")
    lines.append("# TYPE node_filesystem_avail_bytes gauge")
    lines.append("# HELP node_filesystem_free_bytes Filesystem free space in bytes")
    lines.append("# TYPE node_filesystem_free_bytes gauge")
    lines.append("# HELP node_filesystem_files Filesystem total file nodes")
    lines.append("# TYPE node_filesystem_files gauge")
    lines.append("# HELP node_filesystem_files_free Filesystem total free file nodes")
    lines.append("# TYPE node_filesystem_files_free gauge")
    lines.append("# HELP node_filesystem_readonly Filesystem read-only state")
    lines.append("# TYPE node_filesystem_readonly gauge")
    lines.append("# HELP node_filesystem_device_error Filesystem device error indicator")
    lines.append("# TYPE node_filesystem_device_error gauge")
    rootfs_total = 256 * 1024 * 1024 * 1024  # 256 GB root partition
    rootfs_used = 134 * 1024 * 1024 * 1024  # ~52% used
    rootfs_avail = rootfs_total - rootfs_used
    rootfs_labels = (
        'instance="node-1:9100",device="/dev/sda1",fstype="ext4",'
        'mountpoint="/"'
    )
    lines.append(f"node_filesystem_size_bytes{{{rootfs_labels}}} {rootfs_total}")
    lines.append(f"node_filesystem_avail_bytes{{{rootfs_labels}}} {rootfs_avail}")
    lines.append(f"node_filesystem_free_bytes{{{rootfs_labels}}} {rootfs_avail}")
    lines.append(f"node_filesystem_files{{{rootfs_labels}}} 16777216")
    lines.append(f"node_filesystem_files_free{{{rootfs_labels}}} 16500000")
    lines.append(f"node_filesystem_readonly{{{rootfs_labels}}} 0")
    lines.append(f"node_filesystem_device_error{{{rootfs_labels}}} 0")
    # And a /boot partition for the "Filesystem fill up time" panels
    # that want a non-root mount too.
    boot_labels = (
        'instance="node-1:9100",device="/dev/sda2",fstype="ext4",'
        'mountpoint="/boot"'
    )
    lines.append(f"node_filesystem_size_bytes{{{boot_labels}}} {1024 * 1024 * 1024}")
    lines.append(f"node_filesystem_avail_bytes{{{boot_labels}}} {800 * 1024 * 1024}")
    lines.append(f"node_filesystem_free_bytes{{{boot_labels}}} {800 * 1024 * 1024}")
    lines.append(f"node_filesystem_readonly{{{boot_labels}}} 0")
    lines.append(f"node_filesystem_device_error{{{boot_labels}}} 0")

    # Per-socket current connections. Synthetic but stable; the panel
    # uses these as gauges and shows one bar per socket name.
    lines.append("# HELP node_systemd_socket_current_connections Current systemd socket connections")
    lines.append("# TYPE node_systemd_socket_current_connections gauge")
    lines.append("# HELP node_systemd_socket_accepted_connections_total Accepted systemd socket connections")
    lines.append("# TYPE node_systemd_socket_accepted_connections_total counter")
    lines.append("# HELP node_systemd_socket_refused_connections_total Refused systemd socket connections")
    lines.append("# TYPE node_systemd_socket_refused_connections_total counter")
    for sock in ("dbus.socket", "ssh.socket", "syslog.socket"):
        cur = (hash(sock) % 5)
        accepted = int(elapsed * 0.02 + (hash(sock) % 7))
        refused = int(elapsed * 0.001)
        lines.append(
            f'node_systemd_socket_current_connections{{instance="node-1:9100",name="{sock}"}} {cur}'
        )
        lines.append(
            f'node_systemd_socket_accepted_connections_total{{instance="node-1:9100",name="{sock}"}} {accepted}'
        )
        lines.append(
            f'node_systemd_socket_refused_connections_total{{instance="node-1:9100",name="{sock}"}} {refused}'
        )

    # /proc/net/udp + /proc/net/udp6 queue sizes (rx/tx) per protocol
    # family. node-exporter exposes this via --collector.udp_queues but
    # the container's /proc/net/udp doesn't show realistic values; the
    # NEF dashboard uses ip="v4" / ip="v6" labels on this metric, so we
    # emit a deterministic synthetic version that includes those labels.
    lines.append("# HELP node_udp_queues UDP queue size per protocol family")
    lines.append("# TYPE node_udp_queues gauge")
    for ip in ("v4", "v6"):
        for q, base in (("rx", 32), ("tx", 8)):
            v = base + int(8 * ((elapsed % 30) / 30))
            lines.append(
                f'node_udp_queues{{instance="node-1:9100",ip="{ip}",queue="{q}"}} {v}'
            )

    return "\n".join(lines) + "\n"


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
        if self.path == "/metrics-k8s":
            body = render_kube_state_metrics().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/metrics-node-extras":
            body = render_node_extras_metrics().encode("utf-8")
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
