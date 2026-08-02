# Telemetry Schema Change Report

## Summary

- Artifact directories: 1
- Total panels: 4
- Target streams: metrics-*

## Artifact directories

1. `/tmp/obs-dd-test/dashboards`

## dashboards

Artifact directory: `/tmp/obs-dd-test/dashboards`

| Dashboard | Panel | Source fields | Target stream | Target fields |
|---|---|---|---|---|
| Sample: Host Basics | CPU by host | host, system.cpu.user | metrics-* | host.name, system_cpu_user, timestamp |
| Sample: Host Basics | Avg load | system.load.1 | metrics-* | system_load_1, timestamp |
| Sample: Host Basics | Top memory consumers | host, system.mem.used | metrics-* | host.name, system_mem_used, timestamp |
| Sample: Host Basics | Host Map (unsupported) | host, system.cpu.idle | metrics-* | host.name, system_cpu_idle, timestamp |
