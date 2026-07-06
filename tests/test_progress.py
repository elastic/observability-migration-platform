# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

import io
import unittest
from contextlib import redirect_stderr

from observability_migration.core.progress import null_progress, stderr_progress


class StderrProgressTests(unittest.TestCase):
    def test_prints_prefixed_message_to_stderr(self):
        buf = io.StringIO()
        emit = stderr_progress("seed")
        with redirect_stderr(buf):
            emit("ingested 100 docs so far (errors=0)")
        self.assertEqual(buf.getvalue(), "seed: ingested 100 docs so far (errors=0)\n")

    def test_each_call_gets_its_own_prefixed_line(self):
        buf = io.StringIO()
        emit = stderr_progress("compare")
        with redirect_stderr(buf):
            emit("comparing 10 panels")
            emit("processed 10/10 panels")
        self.assertEqual(
            buf.getvalue(),
            "compare: comparing 10 panels\ncompare: processed 10/10 panels\n",
        )


class NullProgressTests(unittest.TestCase):
    def test_is_a_noop(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            null_progress("anything")
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
