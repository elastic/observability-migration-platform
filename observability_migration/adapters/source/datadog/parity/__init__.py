# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Datadog↔Elasticsearch parity harness.

Seeds deterministic synthetic metric data into both Datadog and
Elasticsearch with matching timestamps, then runs source Datadog
queries against DD and translated ES|QL against ES, and diffs the
results.
"""
