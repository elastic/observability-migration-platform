# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Bundled, offline sample dashboards for no-connection migration trials.

These ship in the wheel as package data (see pyproject ``[tool.setuptools.package-data]``)
so a ``pip install`` user can migrate a representative dashboard without any
source credentials. Each sample intentionally includes at least one panel that
does NOT migrate cleanly, to demonstrate honest degrade-gracefully behavior.
"""
