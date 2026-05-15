#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0
"""Reject staged hunks that contain hardcoded local machine paths."""

import re
import subprocess
import sys

_LOCAL_PATH = re.compile(r"(/Users/[a-zA-Z0-9._-]+/|/home/[a-zA-Z0-9._-]+/)")

result = subprocess.run(
    ["git", "diff", "--cached"],
    capture_output=True,
    text=True,
)

hits = []
for line in result.stdout.splitlines():
    if line.startswith("+") and not line.startswith("+++"):
        m = _LOCAL_PATH.search(line)
        if m:
            hits.append(line)

if hits:
    print("ERROR: staged changes contain hardcoded local machine paths:", file=sys.stderr)
    for h in hits[:10]:
        print(f"  {h}", file=sys.stderr)
    print(
        "\nReplace these with relative paths or environment variables before committing.",
        file=sys.stderr,
    )
    sys.exit(1)
