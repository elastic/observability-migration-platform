#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
#
# Synthetic Kubernetes metrics exporter for the curated-pack validation rig.
#
# Emits a small, coherent cluster in modern shape so the Kubernetes curated
# packs can be validated on real (rig-ingested) data:
#   - Grafana 315 (cAdvisor): container_* + machine_* + container_fs_* with the
#     modern `pod`/`container` labels and a root-cgroup `id="/"` series.
#   - Grafana 6417 (kube-state-metrics + node_exporter): kube_* in the modern
#     resource-split shape (kube_node_status_allocatable{resource=...}, etc.),
#     plus node_filesystem_*_bytes.
#   - Grafana 741 (Deployment metrics): same cAdvisor + KSM deployment series,
#     including kube_deployment_status_replicas_available. Pod names are
#     `{deployment}-{ordinal}` so Grafana's `pod=~"^$Deployment.*$"` prefix
#     matches.
#   - Grafana 3831 (Cluster Autoscaler): cluster_autoscaler_* gauges/counters.
#   - Grafana 1471 (App Metrics): container_memory_usage_bytes, container_cpu_user/
#     system, container_spec_*, plus http_requests_total / nginx / haproxy with
#     app=container name so $container binds both cAdvisor and HTTP panels.
#
# Counters are wall-clock monotonic so RATE()/DELTA() render on the rig. The
# `OutOfDisk` node condition is deliberately NOT emitted (removed in k8s 1.12) so
# the 6417 "Nodes Out of Disk" panel stays an honest empty gap.

import time
from http.server import BaseHTTPRequestHandler, HTTPServer

CLUSTER = "rig-cluster"
NODES = ["node-1", "node-2"]
# namespace -> [(pod, container)]
WORKLOADS = {
    "default": [("web-0", "web"), ("web-1", "web"), ("cache-0", "redis")],
    "staging": [("api-0", "api"), ("api-1", "api")],
    "kube-system": [("coredns-0", "coredns"), ("kube-proxy-0", "kube-proxy")],
}
DEPLOYMENTS = {
    "default": {"web": 2, "cache": 1},
    "staging": {"api": 2},
    "kube-system": {"coredns": 2},
}

_START = time.time()


def _node_for(idx: int) -> str:
    return NODES[idx % len(NODES)]


def render() -> str:
    now = time.time()
    elapsed = max(1.0, now - _START)
    L: list[str] = []

    # ---- cAdvisor: machine_* (per node) ---------------------------------
    L.append("# HELP machine_cpu_cores Number of CPU cores on the machine")
    L.append("# TYPE machine_cpu_cores gauge")
    L.append("# HELP machine_memory_bytes Amount of memory installed on the machine")
    L.append("# TYPE machine_memory_bytes gauge")
    for n in NODES:
        L.append(f'machine_cpu_cores{{instance="{n}",node="{n}"}} 4')
        L.append(f'machine_memory_bytes{{instance="{n}",node="{n}"}} {16 * 1024**3}')

    # ---- cAdvisor: root cgroup (id="/") node totals ---------------------
    L.append("# HELP container_cpu_usage_seconds_total Cumulative cpu time consumed")
    L.append("# TYPE container_cpu_usage_seconds_total counter")
    L.append("# HELP container_memory_working_set_bytes Current working set")
    L.append("# TYPE container_memory_working_set_bytes gauge")
    L.append("# HELP container_network_receive_bytes_total Cumulative bytes received")
    L.append("# TYPE container_network_receive_bytes_total counter")
    L.append("# HELP container_network_transmit_bytes_total Cumulative bytes transmitted")
    L.append("# TYPE container_network_transmit_bytes_total counter")
    L.append("# HELP container_fs_usage_bytes Filesystem bytes consumed")
    L.append("# TYPE container_fs_usage_bytes gauge")
    L.append("# HELP container_fs_limit_bytes Filesystem capacity in bytes")
    L.append("# TYPE container_fs_limit_bytes gauge")

    for ni, n in enumerate(NODES):
        # Root cgroup totals used by the cluster-usage KPIs (id="/").
        root_cpu = elapsed * (1.2 + 0.3 * ni)
        L.append(
            f'container_cpu_usage_seconds_total{{id="/",instance="{n}",node="{n}"}} {root_cpu:.3f}'
        )
        L.append(
            f'container_memory_working_set_bytes{{id="/",instance="{n}",node="{n}"}} '
            f'{int((5.0 + ni) * 1024**3)}'
        )
        L.append(
            f'container_fs_usage_bytes{{id="/",device="/dev/sda1",instance="{n}"}} '
            f'{int((30 + 5 * ni) * 1024**3)}'
        )
        L.append(
            f'container_fs_limit_bytes{{id="/",device="/dev/sda1",instance="{n}"}} '
            f'{(100 * 1024**3)}'
        )

    # ---- cAdvisor: per pod/container series -----------------------------
    idx = 0
    for ns, pods in WORKLOADS.items():
        for pod, container in pods:
            n = _node_for(idx)
            idx += 1
            cgroup_id = f"/kubepods/{pod}/{container}"
            base = (
                f'id="{cgroup_id}",namespace="{ns}",pod="{pod}",container="{container}",'
                f'image="registry/{container}:latest",name="k8s_{container}_{pod}",instance="{n}"'
            )
            cpu = elapsed * (0.05 + 0.02 * (idx % 5))
            L.append(f"container_cpu_usage_seconds_total{{{base}}} {cpu:.4f}")
            L.append(
                f"container_memory_working_set_bytes{{{base}}} "
                f"{int((128 + 40 * (idx % 6)) * 1024**2)}"
            )
            rx = elapsed * (2000 + 300 * (idx % 7))
            tx = elapsed * (1500 + 200 * (idx % 7))
            L.append(f"container_network_receive_bytes_total{{{base}}} {rx:.0f}")
            L.append(f"container_network_transmit_bytes_total{{{base}}} {tx:.0f}")

    # ---- kube-state-metrics: nodes --------------------------------------
    L.append("# HELP kube_node_info Information about a cluster node")
    L.append("# TYPE kube_node_info gauge")
    L.append("# HELP kube_node_spec_unschedulable Whether a node can schedule new pods")
    L.append("# TYPE kube_node_spec_unschedulable gauge")
    L.append("# HELP kube_node_status_condition The condition of a cluster node")
    L.append("# TYPE kube_node_status_condition gauge")
    L.append("# HELP kube_node_status_allocatable Node resources allocatable for scheduling")
    L.append("# TYPE kube_node_status_allocatable gauge")
    L.append("# HELP kube_node_status_capacity Total node resource capacity")
    L.append("# TYPE kube_node_status_capacity gauge")
    for n in NODES:
        L.append(f'kube_node_info{{node="{n}",cluster="{CLUSTER}"}} 1')
        L.append(f'kube_node_spec_unschedulable{{node="{n}"}} 0')
        # Ready condition present; OutOfDisk intentionally absent (removed in 1.12).
        L.append(f'kube_node_status_condition{{node="{n}",condition="Ready",status="true"}} 1')
        for resource, alloc, cap in (("pods", 110, 110), ("cpu", 4, 4), ("memory", 15 * 1024**3, 16 * 1024**3)):
            L.append(f'kube_node_status_allocatable{{node="{n}",resource="{resource}"}} {alloc}')
            L.append(f'kube_node_status_capacity{{node="{n}",resource="{resource}"}} {cap}')

    # ---- kube-state-metrics: pods ---------------------------------------
    L.append("# HELP kube_pod_info Information about pod")
    L.append("# TYPE kube_pod_info gauge")
    L.append("# HELP kube_pod_status_phase The pods current phase")
    L.append("# TYPE kube_pod_status_phase gauge")
    L.append("# HELP kube_pod_container_status_running Whether the container is running")
    L.append("# TYPE kube_pod_container_status_running gauge")
    L.append("# HELP kube_pod_container_status_waiting Whether the container is waiting")
    L.append("# TYPE kube_pod_container_status_waiting gauge")
    L.append("# HELP kube_pod_container_status_terminated Whether the container is terminated")
    L.append("# TYPE kube_pod_container_status_terminated gauge")
    L.append("# HELP kube_pod_container_status_restarts_total Container restart count")
    L.append("# TYPE kube_pod_container_status_restarts_total counter")
    L.append("# HELP kube_pod_container_resource_requests Requested container resources")
    L.append("# TYPE kube_pod_container_resource_requests gauge")
    idx = 0
    for ns, pods in WORKLOADS.items():
        for pod, container in pods:
            n = _node_for(idx)
            idx += 1
            L.append(f'kube_pod_info{{namespace="{ns}",pod="{pod}",node="{n}"}} 1')
            # kube-state-metrics emits every phase as a 0/1 series; 0-valued
            # non-Running phases keep the Pending/Failed/Succeeded/Unknown
            # tiles at 0 instead of Lens N/A.
            for phase_name, value in (
                ("Pending", 0),
                ("Running", 1),
                ("Succeeded", 0),
                ("Failed", 0),
                ("Unknown", 0),
            ):
                L.append(
                    f'kube_pod_status_phase{{namespace="{ns}",pod="{pod}",phase="{phase_name}"}} {value}'
                )
            L.append(
                f'kube_pod_container_status_running{{namespace="{ns}",pod="{pod}",container="{container}"}} 1'
            )
            L.append(
                f'kube_pod_container_status_waiting{{namespace="{ns}",pod="{pod}",container="{container}"}} 0'
            )
            L.append(
                f'kube_pod_container_status_terminated{{namespace="{ns}",pod="{pod}",container="{container}"}} 0'
            )
            restarts = int(elapsed / 600) + (idx % 3)
            L.append(
                f'kube_pod_container_status_restarts_total{{namespace="{ns}",pod="{pod}",container="{container}"}} {restarts}'
            )
            L.append(
                f'kube_pod_container_resource_requests{{namespace="{ns}",pod="{pod}",container="{container}",node="{n}",resource="cpu",unit="core"}} 0.25'
            )
            L.append(
                f'kube_pod_container_resource_requests{{namespace="{ns}",pod="{pod}",container="{container}",node="{n}",resource="memory",unit="byte"}} {int(0.5 * 1024**3)}'
            )

    # ---- kube-state-metrics: deployments --------------------------------
    L.append("# HELP kube_deployment_status_replicas The number of replicas per deployment")
    L.append("# TYPE kube_deployment_status_replicas gauge")
    L.append("# HELP kube_deployment_status_replicas_available The number of available replicas")
    L.append("# TYPE kube_deployment_status_replicas_available gauge")
    L.append("# HELP kube_deployment_status_replicas_updated The number of updated replicas")
    L.append("# TYPE kube_deployment_status_replicas_updated gauge")
    L.append("# HELP kube_deployment_status_replicas_unavailable The number of unavailable replicas")
    L.append("# TYPE kube_deployment_status_replicas_unavailable gauge")
    for ns, deps in DEPLOYMENTS.items():
        for dep, replicas in deps.items():
            L.append(f'kube_deployment_status_replicas{{namespace="{ns}",deployment="{dep}"}} {replicas}')
            L.append(
                f'kube_deployment_status_replicas_available{{namespace="{ns}",deployment="{dep}"}} {replicas}'
            )
            L.append(f'kube_deployment_status_replicas_updated{{namespace="{ns}",deployment="{dep}"}} {replicas}')
            L.append(f'kube_deployment_status_replicas_unavailable{{namespace="{ns}",deployment="{dep}"}} 0')

    # ---- kube-state-metrics: jobs ---------------------------------------
    L.append("# HELP kube_job_status_succeeded The number of pods which reached Complete")
    L.append("# TYPE kube_job_status_succeeded gauge")
    L.append("# HELP kube_job_status_active The number of actively running pods")
    L.append("# TYPE kube_job_status_active gauge")
    L.append("# HELP kube_job_status_failed The number of pods which reached Failed")
    L.append("# TYPE kube_job_status_failed gauge")
    for ns in ("default", "kube-system"):
        L.append(f'kube_job_status_succeeded{{namespace="{ns}",job_name="backup"}} 3')
        L.append(f'kube_job_status_active{{namespace="{ns}",job_name="backup"}} 1')
        L.append(f'kube_job_status_failed{{namespace="{ns}",job_name="backup"}} 0')

    # ---- cAdvisor extras for Grafana 1471 (usage + spec + user/system) --
    L.append("# HELP container_memory_usage_bytes Current memory usage")
    L.append("# TYPE container_memory_usage_bytes gauge")
    L.append("# HELP container_cpu_user_seconds_total Cumulative user cpu time")
    L.append("# TYPE container_cpu_user_seconds_total counter")
    L.append("# HELP container_cpu_system_seconds_total Cumulative system cpu time")
    L.append("# TYPE container_cpu_system_seconds_total counter")
    L.append("# HELP container_spec_cpu_shares CPU share (relative weight)")
    L.append("# TYPE container_spec_cpu_shares gauge")
    L.append("# HELP container_spec_cpu_quota CPU CFS quota period")
    L.append("# TYPE container_spec_cpu_quota gauge")
    L.append("# HELP container_spec_cpu_period CPU CFS period")
    L.append("# TYPE container_spec_cpu_period gauge")
    L.append("# HELP container_spec_memory_limit_bytes Memory limit in bytes")
    L.append("# TYPE container_spec_memory_limit_bytes gauge")
    idx = 0
    for ns, pods in WORKLOADS.items():
        for pod, container in pods:
            n = _node_for(idx)
            idx += 1
            cgroup_id = f"/kubepods/{pod}/{container}"
            base = (
                f'id="{cgroup_id}",namespace="{ns}",pod="{pod}",container="{container}",'
                f'image="registry/{container}:latest",name="k8s_{container}_{pod}",instance="{n}"'
            )
            cpu = elapsed * (0.05 + 0.02 * (idx % 5))
            mem = int((128 + 40 * (idx % 6)) * 1024**2)
            L.append(f"container_memory_usage_bytes{{{base}}} {mem}")
            L.append(f"container_cpu_user_seconds_total{{{base}}} {cpu * 0.7:.4f}")
            L.append(f"container_cpu_system_seconds_total{{{base}}} {cpu * 0.3:.4f}")
            L.append(f"container_spec_cpu_shares{{{base}}} 256")
            L.append(f"container_spec_cpu_quota{{{base}}} 50000")
            L.append(f"container_spec_cpu_period{{{base}}} 100000")
            L.append(f"container_spec_memory_limit_bytes{{{base}}} {512 * 1024**2}")

    # ---- App HTTP metrics for Grafana 1471 (app == container name) ------
    L.append("# HELP http_requests_total Total HTTP requests (native app)")
    L.append("# TYPE http_requests_total counter")
    L.append("# HELP nginx_http_requests_total Total nginx HTTP requests")
    L.append("# TYPE nginx_http_requests_total counter")
    L.append("# HELP haproxy_backend_http_responses_total HAProxy backend responses")
    L.append("# TYPE haproxy_backend_http_responses_total counter")
    L.append("# HELP http_request_duration_seconds Native request duration sample")
    L.append("# TYPE http_request_duration_seconds gauge")
    L.append("# HELP nginx_http_request_duration_seconds Nginx request duration sample")
    L.append("# TYPE nginx_http_request_duration_seconds gauge")
    L.append("# HELP http_request_duration_seconds_bucket Native duration histogram")
    L.append("# TYPE http_request_duration_seconds_bucket counter")
    L.append("# HELP nginx_http_request_duration_seconds_bucket Nginx duration histogram")
    L.append("# TYPE nginx_http_request_duration_seconds_bucket counter")
    idx = 0
    for ns, pods in WORKLOADS.items():
        for pod, container in pods:
            idx += 1
            app = f'app="{container}",namespace="{ns}",kubernetes_namespace="{ns}",pod="{pod}"'
            reqs = elapsed * (20 + 5 * (idx % 4))
            L.append(f'http_requests_total{{{app},handler="app",code="200"}} {reqs:.0f}')
            L.append(f'http_requests_total{{{app},handler="app",code="500"}} {reqs * 0.02:.0f}')
            L.append(f'http_requests_total{{{app},handler="prometheus",code="200"}} {elapsed:.0f}')
            L.append(f'nginx_http_requests_total{{{app},status="200"}} {reqs * 0.8:.0f}')
            L.append(f'nginx_http_requests_total{{{app},status="500"}} {reqs * 0.01:.0f}')
            L.append(f'haproxy_backend_http_responses_total{{{app},code="2xx"}} {reqs * 0.9:.0f}')
            L.append(f'haproxy_backend_http_responses_total{{{app},code="5xx"}} {reqs * 0.015:.0f}')
            latency = 0.04 + 0.01 * (idx % 5)
            L.append(f"http_request_duration_seconds{{{app}}} {latency:.4f}")
            L.append(f"nginx_http_request_duration_seconds{{{app}}} {latency * 0.8:.4f}")
            for le, frac in (("0.05", 0.4), ("0.1", 0.8), ("+Inf", 1.0)):
                L.append(
                    f'http_request_duration_seconds_bucket{{{app},le="{le}"}} {reqs * frac:.0f}'
                )
                L.append(
                    f'nginx_http_request_duration_seconds_bucket{{{app},le="{le}"}} {reqs * 0.8 * frac:.0f}'
                )

    # ---- Cluster autoscaler (Grafana 3831) ------------------------------
    L.append("# HELP cluster_autoscaler_nodes_count Number of nodes in the cluster")
    L.append("# TYPE cluster_autoscaler_nodes_count gauge")
    L.append("# HELP cluster_autoscaler_cluster_safe_to_autoscale Whether CA considers the cluster safe")
    L.append("# TYPE cluster_autoscaler_cluster_safe_to_autoscale gauge")
    L.append("# HELP cluster_autoscaler_unschedulable_pods_count Unschedulable pods")
    L.append("# TYPE cluster_autoscaler_unschedulable_pods_count gauge")
    L.append("# HELP cluster_autoscaler_last_activity Unix time of last CA activity")
    L.append("# TYPE cluster_autoscaler_last_activity gauge")
    L.append("# HELP cluster_autoscaler_unneeded_nodes_count Nodes marked unneeded")
    L.append("# TYPE cluster_autoscaler_unneeded_nodes_count gauge")
    L.append("# HELP cluster_autoscaler_evicted_pods_total Pods evicted by CA")
    L.append("# TYPE cluster_autoscaler_evicted_pods_total counter")
    L.append("# HELP cluster_autoscaler_scaled_up_nodes_total Nodes added by CA")
    L.append("# TYPE cluster_autoscaler_scaled_up_nodes_total counter")
    L.append("# HELP cluster_autoscaler_scaled_down_nodes_total Nodes removed by CA")
    L.append("# TYPE cluster_autoscaler_scaled_down_nodes_total counter")
    L.append(f'cluster_autoscaler_nodes_count{{state="ready"}} {len(NODES)}')
    L.append('cluster_autoscaler_nodes_count{state="unready"} 0')
    L.append('cluster_autoscaler_nodes_count{state="notStarted"} 0')
    L.append("cluster_autoscaler_cluster_safe_to_autoscale 1")
    L.append("cluster_autoscaler_unschedulable_pods_count 1")
    L.append(f"cluster_autoscaler_last_activity{{activity=\"scaleDown\"}} {now - 180:.0f}")
    L.append(f"cluster_autoscaler_last_activity{{activity=\"autoscaling\"}} {now - 45:.0f}")
    L.append("cluster_autoscaler_unneeded_nodes_count 0")
    L.append(f"cluster_autoscaler_evicted_pods_total {int(elapsed / 30)}")
    L.append(f"cluster_autoscaler_scaled_up_nodes_total {5 + int(elapsed / 120)}")
    L.append("cluster_autoscaler_scaled_down_nodes_total 2")

    # ---- node_exporter: filesystem (modern *_bytes names) ---------------
    L.append("# HELP node_filesystem_size_bytes Filesystem size in bytes")
    L.append("# TYPE node_filesystem_size_bytes gauge")
    L.append("# HELP node_filesystem_free_bytes Filesystem free space in bytes")
    L.append("# TYPE node_filesystem_free_bytes gauge")
    for n in NODES:
        labels = f'device="/dev/sda1",fstype="ext4",mountpoint="/",instance="{n}"'
        L.append(f"node_filesystem_size_bytes{{{labels}}} {(100 * 1024**3)}")
        L.append(f"node_filesystem_free_bytes{{{labels}}} {((60 - 5) * 1024**3)}")

    return "\n".join(L) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/metrics", "/"):
            self.send_response(404)
            self.end_headers()
            return
        body = render().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # silence access logs
        return


def main() -> None:
    server = HTTPServer(("0.0.0.0", 9288), Handler)
    print("synthetic k8s exporter on :9288/metrics", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
