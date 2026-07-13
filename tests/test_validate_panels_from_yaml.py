# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

from __future__ import annotations

import importlib


def test_build_params_does_not_treat_identifier_as_value_param(monkeypatch):
    monkeypatch.setenv("ELASTICSEARCH_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("KEY", "test-key")
    module = importlib.import_module("scripts.validate_panels_from_yaml")

    assert module._build_params(
        "TS metrics-* | STATS value = SUM(metric) BY grouping = ??grouping"
    ) is None


def test_build_params_binds_identifier_from_field_control_default(monkeypatch):
    monkeypatch.setenv("ELASTICSEARCH_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("KEY", "test-key")
    module = importlib.import_module("scripts.validate_panels_from_yaml")
    query = "TS metrics-* | STATS value = SUM(metric) BY grouping = ??grouping"

    assert module._build_params(
        query,
        identifier_params={"grouping": "exporter"},
    ) == [{"grouping": "exporter"}]


def test_field_control_defaults_use_default_then_first_choice(monkeypatch):
    monkeypatch.setenv("ELASTICSEARCH_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("KEY", "test-key")
    module = importlib.import_module("scripts.validate_panels_from_yaml")

    assert module._field_control_defaults([
        {
            "type": "esql",
            "variable_name": "grouping",
            "variable_type": "fields",
            "choices": ["exporter", "transport"],
            "default": "transport",
        },
        {
            "type": "esql",
            "variable_name": "secondary",
            "variable_type": "fields",
            "choices": ["receiver"],
        },
    ]) == {"grouping": "transport", "secondary": "receiver"}
