# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Generate producer-side routing configuration that bridges source telemetry.

A migration translates *dashboards* from Prometheus/Grafana label names to the
Elasticsearch target field names. To keep dashboards working against live data,
the *producers* (OTel collectors, Prometheus remote-write, Elastic Agent) must
emit those same target field names. This module turns the source→target label
mapping the migration already knows into ready-to-apply agent configuration.

The mapping is *forward* (source label → target field), which is collision-free:
each source label has exactly one primary target. The reverse direction
(target field → source labels) can collide — e.g. ``instance`` and ``node`` both
map to ``host.name`` — so :func:`reverse_field_mapping` returns a list per target
to surface those collisions rather than silently dropping them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

import yaml

from .schema import SchemaResolver

# Prometheus label names must match [a-zA-Z_][a-zA-Z0-9_]* — dotted OTel field
# names are not valid relabel targets and must be underscored.
_PROM_LABEL_RE = re.compile(r"[^A-Za-z0-9_]")


def forward_label_mapping(
    *,
    label_rewrites: Mapping[str, str] | None = None,
    candidates: Mapping[str, list[str]] | None = None,
) -> dict[str, str]:
    """Return ``{source_label: target_field}`` for labels that need renaming.

    Built from the primary OTel candidate of each Prometheus label, with
    rule-pack ``label_rewrites`` taking precedence. Identity mappings (where the
    label already equals the target field, e.g. ``cpu``) are omitted because no
    relabeling is required.
    """
    source_candidates = candidates if candidates is not None else SchemaResolver.PROM_TO_OTEL_CANDIDATES
    mapping: dict[str, str] = {}
    for label, options in source_candidates.items():
        if options:
            mapping[label] = options[0]
    for label, target in (label_rewrites or {}).items():
        mapping[label] = target
    return {label: target for label, target in mapping.items() if label != target}


def reverse_field_mapping(forward: Mapping[str, str]) -> dict[str, list[str]]:
    """Invert a forward mapping, collecting all source labels per target field.

    A target with more than one source label is a collision the caller may need
    to disambiguate; the sources are returned sorted for determinism.
    """
    reverse: dict[str, list[str]] = {}
    for source, target in forward.items():
        reverse.setdefault(target, []).append(source)
    return {target: sorted(sources) for target, sources in reverse.items()}


def otel_transform_processor(forward: Mapping[str, str]) -> dict:
    """Build an OTel Collector ``transform`` processor renaming attributes."""
    statements: list[str] = []
    for source, target in sorted(forward.items()):
        statements.append(f'set(attributes["{target}"], attributes["{source}"])')
        statements.append(f'delete_key(attributes, "{source}")')
    return {
        "transform/grafana_to_elastic": {
            "metric_statements": [
                {"context": "datapoint", "statements": statements},
            ],
        },
    }


def prometheus_write_relabel_configs(forward: Mapping[str, str]) -> list[dict]:
    """Build Prometheus ``write_relabel_configs`` renaming labels to targets."""
    configs: list[dict] = []
    for source, target in sorted(forward.items()):
        configs.append(
            {
                "source_labels": [source],
                "target_label": _PROM_LABEL_RE.sub("_", target),
            }
        )
    return configs


def elastic_agent_copy_fields(forward: Mapping[str, str]) -> list[dict]:
    """Build an Elastic Agent ``copy_fields`` processor from ``labels.<source>``."""
    fields = [
        {"from": f"labels.{source}", "to": target}
        for source, target in sorted(forward.items())
    ]
    return [
        {
            "copy_fields": {
                "fields": fields,
                "ignore_missing": True,
                "fail_on_error": False,
            }
        }
    ]


_ES_WRITE_ENDPOINT = "${ELASTICSEARCH_ENDPOINT}/_prometheus/api/v1/write"


def _otel_document(forward: Mapping[str, str]) -> dict:
    return {"processors": otel_transform_processor(forward)}


def _prometheus_document(forward: Mapping[str, str]) -> dict:
    return {
        "remote_write": [
            {
                "url": _ES_WRITE_ENDPOINT,
                "authorization": {"credentials": "${ES_API_KEY}"},
                "write_relabel_configs": prometheus_write_relabel_configs(forward),
            }
        ]
    }


def _elastic_agent_document(forward: Mapping[str, str]) -> dict:
    return {
        "inputs": [
            {
                "type": "prometheus/metrics",
                "streams": [{"processors": elastic_agent_copy_fields(forward)}],
            }
        ]
    }


# Which output files apply to which detected target schema profile.
_PROFILE_FORMATS = {
    "prometheus_native": ("prometheus-relabel.yaml", "elastic-agent-integration.yaml"),
    "prometheus_remote_write": ("prometheus-relabel.yaml", "elastic-agent-integration.yaml"),
    "otel": ("otel-collector-migration.yaml",),
}

_FORMAT_BUILDERS = {
    "otel-collector-migration.yaml": _otel_document,
    "prometheus-relabel.yaml": _prometheus_document,
    "elastic-agent-integration.yaml": _elastic_agent_document,
}


def generate_routing_artifacts(
    forward: Mapping[str, str],
    *,
    schema_profile: str | None = None,
) -> dict[str, str]:
    """Render routing config files for the given source→target label mapping.

    Returns ``{filename: yaml_text}``. The file set is chosen by the detected
    target ``schema_profile``; an unknown/None profile emits all three formats so
    the user can pick. An empty mapping yields no artifacts.
    """
    if not forward:
        return {}
    filenames = _PROFILE_FORMATS.get(schema_profile or "", tuple(_FORMAT_BUILDERS))
    artifacts: dict[str, str] = {}
    for filename in filenames:
        document = _FORMAT_BUILDERS[filename](forward)
        artifacts[filename] = yaml.safe_dump(document, sort_keys=False)
    return artifacts


__all__ = [
    "elastic_agent_copy_fields",
    "forward_label_mapping",
    "generate_routing_artifacts",
    "otel_transform_processor",
    "prometheus_write_relabel_configs",
    "reverse_field_mapping",
]
