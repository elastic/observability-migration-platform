# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import unittest

import yaml

from observability_migration.adapters.source.grafana.routing_artifacts import (
    elastic_agent_copy_fields,
    forward_label_mapping,
    generate_routing_artifacts,
    otel_transform_processor,
    prometheus_write_relabel_configs,
    reverse_field_mapping,
)


class ForwardLabelMappingTests(unittest.TestCase):
    def test_builtin_maps_prometheus_labels_to_otel(self):
        mapping = forward_label_mapping()
        self.assertEqual(mapping["job"], "service.name")
        self.assertEqual(mapping["pod"], "k8s.pod.name")
        self.assertEqual(mapping["namespace"], "k8s.namespace.name")

    def test_identity_mappings_are_excluded(self):
        # ``cpu`` -> ``cpu`` needs no rename, so it must not appear as a rule.
        mapping = forward_label_mapping()
        self.assertNotIn("cpu", mapping)
        self.assertNotIn("device", mapping)

    def test_label_rewrites_override_builtin(self):
        mapping = forward_label_mapping(label_rewrites={"job": "service.foo"})
        self.assertEqual(mapping["job"], "service.foo")


class ReverseFieldMappingTests(unittest.TestCase):
    def test_reverse_collects_collisions(self):
        forward = {"instance": "host.name", "node": "host.name", "job": "service.name"}
        reverse = reverse_field_mapping(forward)
        self.assertEqual(reverse["host.name"], ["instance", "node"])
        self.assertEqual(reverse["service.name"], ["job"])


class RenderTests(unittest.TestCase):
    def test_otel_transform_sets_and_deletes_source(self):
        processor = otel_transform_processor({"job": "service.name"})
        statements = processor["transform/grafana_to_elastic"]["metric_statements"][0][
            "statements"
        ]
        self.assertIn('set(attributes["service.name"], attributes["job"])', statements)
        self.assertIn('delete_key(attributes, "job")', statements)

    def test_prometheus_relabel_sanitizes_dotted_targets(self):
        # Prometheus label names cannot contain dots — they must be underscored.
        configs = prometheus_write_relabel_configs({"job": "service.name"})
        self.assertIn(
            {"source_labels": ["job"], "target_label": "service_name"}, configs
        )

    def test_elastic_agent_copy_fields_from_labels_namespace(self):
        processors = elastic_agent_copy_fields({"job": "service.name"})
        fields = processors[0]["copy_fields"]["fields"]
        self.assertIn({"from": "labels.job", "to": "service.name"}, fields)


class GenerateRoutingArtifactsTests(unittest.TestCase):
    def test_native_profile_emits_prometheus_and_agent_yaml(self):
        artifacts = generate_routing_artifacts(
            {"job": "service.name"}, schema_profile="prometheus_native"
        )
        self.assertIn("prometheus-relabel.yaml", artifacts)
        self.assertIn("elastic-agent-integration.yaml", artifacts)
        self.assertNotIn("otel-collector-migration.yaml", artifacts)
        # Values must be parseable YAML.
        parsed = yaml.safe_load(artifacts["prometheus-relabel.yaml"])
        self.assertIsInstance(parsed, dict)

    def test_unknown_profile_emits_all_formats(self):
        artifacts = generate_routing_artifacts({"job": "service.name"})
        self.assertEqual(
            set(artifacts),
            {
                "otel-collector-migration.yaml",
                "prometheus-relabel.yaml",
                "elastic-agent-integration.yaml",
            },
        )

    def test_empty_mapping_yields_no_artifacts(self):
        self.assertEqual(generate_routing_artifacts({}), {})


if __name__ == "__main__":
    unittest.main()
