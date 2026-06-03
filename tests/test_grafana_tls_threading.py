# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import unittest
from unittest.mock import Mock, patch

from observability_migration.adapters.source.grafana.extract import _grafana_session
from observability_migration.adapters.source.grafana.rules import RulePackConfig
from observability_migration.adapters.source.grafana.schema import SchemaResolver


class TestGrafanaTlsThreading(unittest.TestCase):
    @patch("observability_migration.adapters.source.grafana.schema.requests.get")
    def test_schema_resolver_passes_verify_to_field_caps_request(self, mock_get):
        mock_get.return_value = Mock(status_code=200, json=lambda: {"fields": {}})

        resolver = SchemaResolver(
            rule_pack=RulePackConfig(),
            es_url="https://es",
            index_pattern="m-*",
            es_api_key="k",
            verify="/tmp/ca.pem",
        )
        resolver.field_exists("job")

        mock_get.assert_called_once()
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["verify"], "/tmp/ca.pem")

    @patch("observability_migration.adapters.source.grafana.extract.requests.Session")
    def test_grafana_session_applies_verify_to_session(self, mock_session_cls):
        session = Mock()
        mock_session_cls.return_value = session

        returned = _grafana_session(
            "https://grafana.example",
            user="admin",
            password="secret",
            verify="/tmp/ca.pem",
        )

        self.assertIs(returned, session)
        self.assertEqual(session.verify, "/tmp/ca.pem")


if __name__ == "__main__":
    unittest.main()
