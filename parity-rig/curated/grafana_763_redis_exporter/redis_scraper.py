#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
#
# Lightweight Prometheus → Elasticsearch ingestion in prometheus_native format.
# Scrapes each configured exporter and writes docs to its own
# metrics-<dataset>-default data stream using the metrics.* + labels.* layout
# that matches the built-in metrics-prometheus@mappings template
# (labels.* = passthrough TSDB dimension). This gives the migration pipeline the
# prometheus_native schema profile so TS queries, label controls, and RATE() all
# work correctly.
#
# Multiple targets exist so the numeric parity gate has real data for more than
# one exporter. Comparing a node_exporter dashboard against a redis-only index
# yields DATA_GAP for every panel, which looks like coverage but verifies
# nothing; the metric names have to actually be present to be compared.

import json
import math
import re
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime

import requests

ES_URL = "http://elasticsearch:9200"
NAMESPACE = "default"
SCRAPE_INTERVAL = 10
RETRY_DELAY = 5

# (exporter URL, dataset, job label, instance label, extra base labels)
TARGETS = [
    (
        "http://redis_exporter:9121/metrics",
        "redis.prometheus",
        "redis_exporter",
        "redis:6379",
        {"pod": "redis-0"},
    ),
    (
        "http://redis_exporter_b:9121/metrics",
        "redis.prometheus",
        "redis_exporter",
        "redis-b:6379",
        {"pod": "redis-1", "namespace": "staging"},
    ),
    ("http://node_exporter:9100/metrics", "node.prometheus", "node_exporter", "node:9100", {}),
    ("http://mysqld_exporter:9104/metrics", "mysql.prometheus", "mysqld_exporter", "mysql:3306", {}),
    (
        "http://postgres_exporter:9187/metrics",
        "postgres.prometheus",
        "postgres_exporter",
        "postgres:5432",
        {},
    ),
    # Synthetic Kubernetes exporter: cAdvisor (container_*/machine_*) for
    # Grafana 315 and kube-state-metrics + node_exporter for Grafana 6417.
    (
        "http://k8s_exporter:9288/metrics",
        "k8s.prometheus",
        "kube-state-metrics",
        "ksm:8080",
        {},
    ),
]

_LABEL_RE = re.compile(r'(\w+)="([^"]*)"')
_TYPE_RE = re.compile(r"^#\s*TYPE\s+(\S+)\s+(\S+)")

# node_exporter marks several cumulative kernel/netstat families as ``untyped``.
# The Node Exporter Full dashboard legitimately applies rate()/irate() to these
# metrics, and Kibana/Elasticsearch require counter typing for IRATE/RATE.
_METRIC_TYPE_OVERRIDES = {
    "node.prometheus": {
        "node_netstat_Icmp_InErrors": "counter",
        "node_netstat_Icmp_InMsgs": "counter",
        "node_netstat_Icmp_OutMsgs": "counter",
        "node_netstat_IpExt_InOctets": "counter",
        "node_netstat_IpExt_OutOctets": "counter",
        "node_netstat_Ip_Forwarding": "counter",
        "node_netstat_TcpExt_ListenDrops": "counter",
        "node_netstat_TcpExt_ListenOverflows": "counter",
        "node_netstat_TcpExt_SyncookiesFailed": "counter",
        "node_netstat_TcpExt_SyncookiesRecv": "counter",
        "node_netstat_TcpExt_SyncookiesSent": "counter",
        "node_netstat_TcpExt_TCPOFOQueue": "counter",
        "node_netstat_TcpExt_TCPSynRetrans": "counter",
        "node_netstat_Tcp_ActiveOpens": "counter",
        "node_netstat_Tcp_InErrs": "counter",
        "node_netstat_Tcp_InSegs": "counter",
        "node_netstat_Tcp_OutRsts": "counter",
        "node_netstat_Tcp_OutSegs": "counter",
        "node_netstat_Tcp_PassiveOpens": "counter",
        "node_netstat_Tcp_RetransSegs": "counter",
        "node_netstat_UdpLite_InErrors": "counter",
        "node_netstat_Udp_InDatagrams": "counter",
        "node_netstat_Udp_InErrors": "counter",
        "node_netstat_Udp_NoPorts": "counter",
        "node_netstat_Udp_OutDatagrams": "counter",
        "node_netstat_Udp_RcvbufErrors": "counter",
        "node_netstat_Udp_SndbufErrors": "counter",
        "node_vmstat_oom_kill": "counter",
        "node_vmstat_pgfault": "counter",
        "node_vmstat_pgmajfault": "counter",
        "node_vmstat_pgpgin": "counter",
        "node_vmstat_pgpgout": "counter",
        "node_vmstat_pswpin": "counter",
        "node_vmstat_pswpout": "counter",
    },
    "mysql.prometheus": {
        # mysqld_exporter # TYPE untyped — Elasticsearch infers gauge without
        # _total. Dashboards that rate() these need counter mapping.
        "mysql_global_status_queries": "counter",
        "mysql_global_status_questions": "counter",
        "mysql_global_status_threads_created": "counter",
        "mysql_global_status_created_tmp_tables": "counter",
        "mysql_global_status_created_tmp_disk_tables": "counter",
        "mysql_global_status_created_tmp_files": "counter",
        "mysql_global_status_select_full_join": "counter",
        "mysql_global_status_select_full_range_join": "counter",
        "mysql_global_status_select_range": "counter",
        "mysql_global_status_select_range_check": "counter",
        "mysql_global_status_select_scan": "counter",
        "mysql_global_status_sort_rows": "counter",
        "mysql_global_status_sort_range": "counter",
        "mysql_global_status_sort_merge_passes": "counter",
        "mysql_global_status_sort_scan": "counter",
        "mysql_global_status_slow_queries": "counter",
        "mysql_global_status_aborted_connects": "counter",
        "mysql_global_status_aborted_clients": "counter",
        "mysql_global_status_table_locks_immediate": "counter",
        "mysql_global_status_table_locks_waited": "counter",
        "mysql_global_status_bytes_received": "counter",
        "mysql_global_status_bytes_sent": "counter",
        "mysql_global_status_opened_files": "counter",
        "mysql_global_status_opened_tables": "counter",
        "mysql_global_status_table_open_cache_hits": "counter",
        "mysql_global_status_table_open_cache_misses": "counter",
        "mysql_global_status_table_open_cache_overflows": "counter",
        "mysql_global_status_opened_table_definitions": "counter",
        "mysql_global_status_innodb_buffer_pool_read_requests": "counter",
        "mysql_global_status_innodb_buffer_pool_reads": "counter",
        "mysql_global_status_innodb_data_reads": "counter",
        "mysql_global_status_innodb_data_writes": "counter",
        "mysql_global_status_qcache_hits": "counter",
        "mysql_global_status_qcache_inserts": "counter",
        "mysql_global_status_qcache_not_cached": "counter",
        "mysql_global_status_qcache_lowmem_prunes": "counter",
    },
}


def render_node_exporter_extras(_instance: str) -> str:
    """Synthetic node-exporter series missing from the Redis validation rig.

    The curated Redis rig runs a stripped-down node_exporter container whose
    host/kernel view cannot expose several collectors that the Node Exporter
    Full dashboard expects. Appending these metrics to the scraped exposition
    keeps the rig useful for panel-by-panel validation without changing the
    translator or pretending the underlying exporter can emit them.
    """
    now = time.time()
    elapsed = max(0.0, now)
    lines: list[str] = []

    lines.append("# HELP node_memory_DirectMap1G_bytes /proc/meminfo DirectMap1G")
    lines.append("# TYPE node_memory_DirectMap1G_bytes gauge")
    lines.append(f"node_memory_DirectMap1G_bytes {2 * 1024 * 1024 * 1024}")
    lines.append("# HELP node_memory_DirectMap2M_bytes /proc/meminfo DirectMap2M")
    lines.append("# TYPE node_memory_DirectMap2M_bytes gauge")
    lines.append(f"node_memory_DirectMap2M_bytes {6 * 1024 * 1024 * 1024}")
    lines.append("# HELP node_memory_DirectMap4k_bytes /proc/meminfo DirectMap4k")
    lines.append("# TYPE node_memory_DirectMap4k_bytes gauge")
    lines.append(f"node_memory_DirectMap4k_bytes {128 * 1024 * 1024}")

    lines.append("# HELP node_cpu_scaling_frequency_hertz CPU current scaling frequency")
    lines.append("# TYPE node_cpu_scaling_frequency_hertz gauge")
    lines.append("# HELP node_cpu_scaling_frequency_max_hertz CPU max scaling frequency")
    lines.append("# TYPE node_cpu_scaling_frequency_max_hertz gauge")
    lines.append("# HELP node_cpu_scaling_frequency_min_hertz CPU min scaling frequency")
    lines.append("# TYPE node_cpu_scaling_frequency_min_hertz gauge")
    for cpu in (0, 1, 2, 3):
        current = 2_000_000_000 + (cpu * 50_000_000) + int(100_000_000 * ((elapsed % 30) / 30))
        lines.append(
            f'node_cpu_scaling_frequency_hertz{{cpu="{cpu}"}} {current}'
        )
        lines.append(
            f'node_cpu_scaling_frequency_max_hertz{{cpu="{cpu}"}} 3000000000'
        )
        lines.append(
            f'node_cpu_scaling_frequency_min_hertz{{cpu="{cpu}"}} 1000000000'
        )

    lines.append("# HELP node_schedstat_waiting_seconds_total /proc/schedstat waiting")
    lines.append("# TYPE node_schedstat_waiting_seconds_total counter")
    lines.append("# HELP node_schedstat_running_seconds_total /proc/schedstat running")
    lines.append("# TYPE node_schedstat_running_seconds_total counter")
    lines.append("# HELP node_schedstat_timeslices_total /proc/schedstat timeslices")
    lines.append("# TYPE node_schedstat_timeslices_total counter")
    for cpu in (0, 1, 2, 3):
        lines.append(
            f'node_schedstat_waiting_seconds_total{{cpu="{cpu}"}} '
            f'{elapsed * (0.001 + 0.0002 * cpu)}'
        )
        lines.append(
            f'node_schedstat_running_seconds_total{{cpu="{cpu}"}} '
            f'{elapsed * (0.02 + 0.005 * cpu)}'
        )
        lines.append(
            f'node_schedstat_timeslices_total{{cpu="{cpu}"}} '
            f'{int(elapsed * (100 + 10 * cpu))}'
        )

    lines.append("# HELP node_interrupts_total Interrupt counts by CPU")
    lines.append("# TYPE node_interrupts_total counter")
    for cpu in (0, 1, 2, 3):
        lines.append(
            f'node_interrupts_total{{cpu="{cpu}",interrupt="timer"}} '
            f'{int(elapsed * (400 + 25 * cpu))}'
        )

    lines.append("# HELP node_hwmon_chip_names Hardware monitor chip names")
    lines.append("# TYPE node_hwmon_chip_names gauge")
    lines.append("# HELP node_hwmon_temp_celsius Hardware monitor temperature")
    lines.append("# TYPE node_hwmon_temp_celsius gauge")
    lines.append("# HELP node_hwmon_temp_crit_celsius Hardware monitor critical temperature")
    lines.append("# TYPE node_hwmon_temp_crit_celsius gauge")
    for chip, chip_name, label, base in (
        ("coretemp-isa-0000", "coretemp", "Core 0", 45.0),
        ("coretemp-isa-0000", "coretemp", "Core 1", 47.0),
        ("nct6775-isa-0290", "nct6775", "Package id 0", 50.0),
    ):
        cycle = 5.0 * ((elapsed % 60) / 60)
        metric_labels = (
            f'chip="{chip}",chip_name="{chip_name}",sensor="temp1",label="{label}"'
        )
        lines.append(f"node_hwmon_chip_names{{{metric_labels}}} 1")
        lines.append(f"node_hwmon_temp_celsius{{{metric_labels}}} {base + cycle:.2f}")
        lines.append(f"node_hwmon_temp_crit_celsius{{{metric_labels}}} 90.0")

    lines.append("# HELP node_cooling_device_cur_state Linux thermal cooling current state")
    lines.append("# TYPE node_cooling_device_cur_state gauge")
    lines.append("# HELP node_cooling_device_max_state Linux thermal cooling max state")
    lines.append("# TYPE node_cooling_device_max_state gauge")
    for zone in (0, 1):
        labels = f'name="thermal_zone{zone}",type="Processor"'
        lines.append(f"node_cooling_device_cur_state{{{labels}}} {zone}")
        lines.append(f"node_cooling_device_max_state{{{labels}}} 4")

    lines.append("# HELP node_power_supply_online Power supply online state")
    lines.append("# TYPE node_power_supply_online gauge")
    for supply in ("AC0", "BAT0"):
        value = 1 if supply.startswith("AC") else 0
        lines.append(f'node_power_supply_online{{power_supply="{supply}"}} {value}')

    lines.append("# HELP node_systemd_units Number of systemd units")
    lines.append("# TYPE node_systemd_units gauge")
    for state, count in (("activating", 1), ("active", 92), ("deactivating", 0), ("failed", 0), ("inactive", 14)):
        lines.append(f'node_systemd_units{{state="{state}"}} {count}')

    lines.append("# HELP node_systemd_socket_current_connections Current systemd socket connections")
    lines.append("# TYPE node_systemd_socket_current_connections gauge")
    lines.append("# HELP node_systemd_socket_accepted_connections_total Accepted systemd socket connections")
    lines.append("# TYPE node_systemd_socket_accepted_connections_total counter")
    lines.append("# HELP node_systemd_socket_refused_connections_total Refused systemd socket connections")
    lines.append("# TYPE node_systemd_socket_refused_connections_total counter")
    for sock in ("dbus.socket", "ssh.socket", "syslog.socket"):
        current = hash(sock) % 5
        accepted = int(elapsed * 0.02 + (hash(sock) % 7))
        refused = int(elapsed * 0.001)
        lines.append(
            f'node_systemd_socket_current_connections{{name="{sock}"}} {current}'
        )
        lines.append(
            f'node_systemd_socket_accepted_connections_total{{name="{sock}"}} {accepted}'
        )
        lines.append(
            f'node_systemd_socket_refused_connections_total{{name="{sock}"}} {refused}'
        )

    return "\n".join(lines) + "\n"


def _scrape_text(url: str, dataset: str, instance: str) -> str:
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    text = resp.text
    if dataset == "node.prometheus":
        text = f"{text.rstrip()}\n{render_node_exporter_extras(instance)}"
    return text


def parse_metric_types(text: str) -> dict:
    """Metric name -> Prometheus type, from the exposition's ``# TYPE`` lines.

    Elasticsearch infers counter-vs-gauge from the ``_total`` suffix when a
    mapping does not say otherwise. Real exporters do not all follow that
    convention: postgres_exporter declares ``pg_stat_database_tup_fetched`` as a
    counter, and without the declaration it lands as a gauge, so every
    ``irate()`` panel over it fails with "first argument of [IRATE(...)] must be
    [counter_long, counter_integer or counter_double]". Honouring ``# TYPE``
    makes the rig behave like a faithful ingest instead of manufacturing a
    counter-typing gap the real world would not have.
    """
    out = {}
    for line in text.splitlines():
        m = _TYPE_RE.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2).lower()
    return out


def apply_metric_type_overrides(dataset: str, metric_types: dict) -> dict:
    """Promote known untyped exporter metrics to their dashboard-contract kind."""
    overrides = _METRIC_TYPE_OVERRIDES.get(dataset) or {}
    if not overrides:
        return metric_types
    merged = dict(metric_types)
    for name, kind in overrides.items():
        observed = str(merged.get(name, "")).strip().lower()
        if observed in ("", "untyped"):
            merged[name] = kind
    return merged


def parse_label_names(text: str) -> set:
    """Every label name the exposition uses."""
    names = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "{" not in line:
            continue
        block = line.split("{", 1)[1].rsplit("}", 1)[0]
        names.update(k for k, _ in _LABEL_RE.findall(block))
    return names


def ensure_index_template(dataset: str, metric_types: dict, label_names=()) -> None:
    """Declare counter/gauge typing for this dataset before its first document.

    Label names are pinned to ``keyword`` too. Elasticsearch maps a field called
    ``ip`` to the ``ip`` DATATYPE, and node_exporter publishes
    ``node_udp_queues{ip="v4"}`` — "v4" is not an IP address, so the whole
    document is rejected into the failure store and the metric silently never
    lands. The bulk response still says created, so the scraper reported "0
    errors" while losing data.
    """
    props = {}
    for name, kind in sorted(metric_types.items()):
        if kind not in ("counter", "gauge"):
            continue
        props[name] = {"type": "double", "time_series_metric": kind}
    if not props:
        return
    # Compose on the built-in metrics component templates rather than replacing
    # them: an index template does not merge with a lower-priority one, and
    # re-declaring TSDB settings here fails with "[index.mode=time_series]
    # requires a non-empty [index.routing_path]". Composing keeps the stock
    # passthrough/dimension setup and adds only the metric typing.
    body = {
        "index_patterns": [f"metrics-{dataset}-*"],
        "data_stream": {},
        "priority": 500,
        # ``index.mode`` must be set here: none of the composed component
        # templates set it, so omitting it yields a standard index and every
        # query fails with "is not a time series index. Use FROM command
        # instead". The routing path comes from the ``labels`` passthrough in
        # ``metrics-prometheus@mappings``, so that component must be composed and
        # ``labels`` must NOT be redefined below -- redefining it without
        # ``time_series_dimension`` leaves TSDB with no routing path and the
        # template is rejected outright.
        "composed_of": [
            "metrics-prometheus@mappings",
            "metrics-prometheus@settings",
            "metrics@mappings",
            "data-streams@mappings",
            "metrics@settings",
        ],
        "template": {
            "settings": {"index.mode": "time_series"},
            "mappings": {"properties": {
                "metrics": {"properties": props},
                # Elasticsearch types a field named ``ip`` as the ip DATATYPE
                # (ECS ``match_ip``), and node_exporter publishes
                # ``node_udp_queues{ip="v4"}``. "v4" is not an IP address, so the
                # WHOLE document is rejected into the failure store -- and the
                # bulk response still returns 201, which is why this went
                # unnoticed while 3459 documents were lost.
                #
                # The override only holds when ``labels`` is redeclared in FULL.
                # A partial ``{"properties": {...}}` loses to the composed
                # passthrough definition, and dynamic_templates do not win either
                # (verified: ordered ahead of ECS's match_ip and still rejected).
                # Repeating type/priority/time_series_dimension here replaces the
                # definition outright, and the subfield type then sticks.
                "labels": {
                    "type": "passthrough",
                    "priority": 10,
                    "time_series_dimension": True,
                    "properties": {
                        name: {"type": "keyword", "time_series_dimension": True}
                        for name in sorted(label_names)
                    },
                },
            }},
        },
    }
    try:
        resp = requests.put(
            f"{ES_URL}/_index_template/rig-{dataset.replace('.', '-')}",
            json=body, timeout=20,
        )
        if resp.status_code >= 300:
            print(f"  template {dataset}: HTTP {resp.status_code} {resp.text[:160]}", flush=True)
        else:
            counters = sum(1 for v in props.values() if v["time_series_metric"] == "counter")
            print(f"  template {dataset}: {len(props)} fields ({counters} counters)", flush=True)
    except Exception as exc:
        print(f"  template {dataset}: {exc}", flush=True)


def ensure_data_stream(dataset: str) -> None:
    """Create the metrics data stream explicitly before the first bulk write.

    Relying on auto-creation is brittle across Elasticsearch builds: some local
    stacks create a plain index for ``metrics-<dataset>-<namespace>`` even when
    the matching template advertises ``data_stream`` + ``index.mode=time_series``.
    Once that happens every ``TS`` query fails with "is not a time series index"
    and the dashboards validate against placeholders instead of real panels.
    """
    name = f"metrics-{dataset}-{NAMESPACE}"
    try:
        resp = requests.put(f"{ES_URL}/_data_stream/{name}", timeout=20)
        if resp.status_code in (200, 201):
            print(f"  data stream {dataset}: ready", flush=True)
            return
        if resp.status_code == 400 and "resource_already_exists_exception" in resp.text:
            print(f"  data stream {dataset}: already exists", flush=True)
            return
        print(f"  data stream {dataset}: HTTP {resp.status_code} {resp.text[:160]}", flush=True)
    except Exception as exc:
        print(f"  data stream {dataset}: {exc}", flush=True)


def parse_prometheus(text: str) -> dict:
    """Parse Prometheus text format. Returns {(labels_frozen): {metric: value}}."""
    groups: dict = defaultdict(dict)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Skip histogram/summary component metrics (le, quantile labels)
        # — only track the gauge/counter leaf metrics used by the dashboard.
        try:
            if " " not in line:
                continue
            parts = line.rsplit(" ", 1)
            if len(parts) != 2:
                continue
            name_labels, raw_value = parts
            try:
                value = float(raw_value)
            except ValueError:
                continue
            # Skip NaN / Inf — not useful for TSDB
            if math.isnan(value) or math.isinf(value):
                continue

            labels: dict[str, str] = {}
            if "{" in name_labels:
                name, label_block = name_labels.split("{", 1)
                label_block = label_block.rstrip("}")
                for k, v in _LABEL_RE.findall(label_block):
                    labels[k] = v
            else:
                name = name_labels

            name = name.strip()
            if not name:
                continue

            label_key = tuple(sorted(labels.items()))
            groups[label_key][name] = value
        except Exception:
            continue
    return groups


def build_bulk_body(
    groups: dict,
    timestamp: str,
    dataset: str,
    job: str,
    instance: str,
    extra_base_labels: dict[str, str] | None = None,
) -> str:
    lines = []
    index = f"metrics-{dataset}-{NAMESPACE}"
    base_labels = {"instance": instance, "job": job, "namespace": NAMESPACE}
    if extra_base_labels:
        base_labels.update(extra_base_labels)
    for label_key, metrics in groups.items():
        if not metrics:
            continue
        extra = dict(label_key)
        doc = {
            "@timestamp": timestamp,
            "data_stream.type": "metrics",
            "data_stream.dataset": dataset,
            "data_stream.namespace": NAMESPACE,
            "labels": {**base_labels, **extra},
            "metrics": metrics,
        }
        lines.append(json.dumps({"create": {"_index": index}}))
        lines.append(json.dumps(doc))
    return "\n".join(lines) + "\n"


def bulk_index(body: str) -> tuple[int, int]:
    if not body.strip():
        return 0, 0
    resp = requests.post(
        f"{ES_URL}/_bulk",
        data=body.encode(),
        headers={"Content-Type": "application/x-ndjson"},
        timeout=15,
    )
    result = resp.json()
    items = [next(iter(item.values())) for item in result.get("items", [])]
    ok = sum(1 for item in items if item.get("status", 0) in (200, 201))
    # A document rejected into the failure store still reports 201, so counting
    # only HTTP status hides real data loss. Count it separately and loudly.
    diverted = sum(1 for item in items if item.get("failure_store") == "used")
    if diverted:
        for item in items:
            if item.get("failure_store") != "used":
                continue
            reason = ((item.get("error") or {}).get("reason") or "").strip()
            doc = item.get("data") or {}
            labels = (doc.get("labels") or {}) if isinstance(doc, dict) else {}
            metrics = sorted(((doc.get("metrics") or {}) if isinstance(doc, dict) else {}).keys())
            print(
                "  failure_store:",
                {
                    "labels": labels,
                    "metrics": metrics,
                    "reason": reason,
                },
                flush=True,
            )
    for item in items:
        if item.get("status", 0) in (200, 201):
            continue
        reason = ((item.get("error") or {}).get("reason") or "").strip()
        doc = item.get("data") or {}
        labels = (doc.get("labels") or {}) if isinstance(doc, dict) else {}
        metrics = sorted(((doc.get("metrics") or {}) if isinstance(doc, dict) else {}).keys())
        print(
            "  bulk_error:",
            {
                "status": item.get("status"),
                "labels": labels,
                "metrics": metrics,
                "reason": reason,
            },
            flush=True,
        )
    err = len(items) - ok
    return ok - diverted, err + diverted


def wait_for_es(url: str, max_wait: int = 120) -> None:
    print(f"Waiting for Elasticsearch at {url}...", flush=True)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            r = requests.get(f"{url}/_cluster/health", timeout=5)
            if r.status_code == 200 and r.json().get("status") in ("green", "yellow"):
                print("Elasticsearch ready.", flush=True)
                return
        except Exception as exc:
            # ES may still be starting; keep polling until deadline.
            print(f"Elasticsearch not ready yet ({exc!r}); retrying...", flush=True)
        time.sleep(3)
    print("ERROR: Elasticsearch did not become ready in time.", flush=True)
    sys.exit(1)


def main() -> None:
    wait_for_es(ES_URL)
    # Declare counter/gauge typing from each exporter's own ``# TYPE`` lines
    # before the first document creates the data stream with inferred mappings.
    for url, dataset, _job, _instance, _extra_base_labels in TARGETS:
        try:
            text = _scrape_text(url, dataset, _instance)
            ensure_index_template(
                dataset,
                apply_metric_type_overrides(dataset, parse_metric_types(text)),
                parse_label_names(text),
            )
            ensure_data_stream(dataset)
        except Exception as exc:
            print(f"  template {dataset}: could not scrape for types: {exc}", flush=True)
    for url, dataset, _job, _instance, _extra_base_labels in TARGETS:
        print(f"Scraping {url} every {SCRAPE_INTERVAL}s → metrics-{dataset}-{NAMESPACE}", flush=True)
    while True:
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        for url, dataset, job, instance, extra_base_labels in TARGETS:
            # One target being down must not stop the others: the redis pack is
            # validated from this same rig and cannot regress because a
            # secondary exporter is unhealthy.
            try:
                text = _scrape_text(url, dataset, instance)
                groups = parse_prometheus(text)
                body = build_bulk_body(
                    groups,
                    ts,
                    dataset,
                    job,
                    instance,
                    extra_base_labels,
                )
                ok, err = bulk_index(body)
                print(f"{ts} {dataset}: {len(groups)} series → indexed {ok}, errors {err}", flush=True)
            except Exception as exc:
                print(f"{ts} {dataset}: scrape error: {exc}", flush=True)
        time.sleep(SCRAPE_INTERVAL)


if __name__ == "__main__":
    main()
