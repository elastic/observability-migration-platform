# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Package-level entrypoint for uploaded dashboard validation."""

from .smoke import main

__all__ = ["main"]


if __name__ == "__main__":
    main()
