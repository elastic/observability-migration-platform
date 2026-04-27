# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Entry point for ``python -m observability_migration``."""

from observability_migration.app.cli import main

if __name__ == "__main__":
    main()
