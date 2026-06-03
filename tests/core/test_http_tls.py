# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Tests for the shared TLS policy helpers in core/http.py."""

import io
import unittest
from contextlib import redirect_stderr
from unittest import mock

import requests

from observability_migration.core import http as obs_http


class ResolveTlsTests(unittest.TestCase):
    def setUp(self):
        # Reset the one-shot warning guard so each test sees the warning.
        obs_http._reset_insecure_warning_for_tests()

    def test_default_is_verify_true(self):
        with mock.patch.dict("os.environ", {"OBS_MIGRATE_CA_CERT": "", "OBS_MIGRATE_INSECURE": ""}, clear=False):
            self.assertIs(obs_http.resolve_tls(), True)

    def test_ca_cert_returns_path(self):
        with mock.patch.dict("os.environ", {"OBS_MIGRATE_CA_CERT": "", "OBS_MIGRATE_INSECURE": ""}, clear=False):
            self.assertEqual(obs_http.resolve_tls(ca_cert="/etc/ca.pem"), "/etc/ca.pem")

    def test_insecure_returns_false_and_warns_once(self):
        with mock.patch.dict("os.environ", {"OBS_MIGRATE_CA_CERT": "", "OBS_MIGRATE_INSECURE": ""}, clear=False):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                first = obs_http.resolve_tls(insecure=True)
                second = obs_http.resolve_tls(insecure=True)
            self.assertIs(first, False)
            self.assertIs(second, False)
            warning_text = stderr.getvalue()
            # Warning is loud and mentions disabled verification, but only once.
            self.assertIn("verification disabled", warning_text.lower())
            self.assertEqual(warning_text.lower().count("verification disabled"), 1)

    def test_insecure_takes_precedence_over_ca_cert(self):
        with mock.patch.dict("os.environ", {"OBS_MIGRATE_CA_CERT": "", "OBS_MIGRATE_INSECURE": ""}, clear=False):
            with redirect_stderr(io.StringIO()):
                self.assertIs(obs_http.resolve_tls(ca_cert="/etc/ca.pem", insecure=True), False)

    def test_env_ca_cert_used_when_arg_absent(self):
        with mock.patch.dict("os.environ", {"OBS_MIGRATE_CA_CERT": "/env/ca.pem", "OBS_MIGRATE_INSECURE": ""}, clear=False):
            self.assertEqual(obs_http.resolve_tls(), "/env/ca.pem")

    def test_env_insecure_truthy_disables_verification(self):
        with mock.patch.dict("os.environ", {"OBS_MIGRATE_CA_CERT": "", "OBS_MIGRATE_INSECURE": "1"}, clear=False):
            with redirect_stderr(io.StringIO()):
                self.assertIs(obs_http.resolve_tls(), False)

    def test_env_insecure_falsey_keeps_verification(self):
        with mock.patch.dict("os.environ", {"OBS_MIGRATE_CA_CERT": "", "OBS_MIGRATE_INSECURE": "0"}, clear=False):
            self.assertIs(obs_http.resolve_tls(), True)

    def test_explicit_arg_overrides_truthy_env(self):
        # Passing ca_cert explicitly should win over env CA, and explicit
        # insecure=False should not be overridden by an unset env.
        with mock.patch.dict("os.environ", {"OBS_MIGRATE_CA_CERT": "/env/ca.pem", "OBS_MIGRATE_INSECURE": ""}, clear=False):
            self.assertEqual(obs_http.resolve_tls(ca_cert="/explicit/ca.pem"), "/explicit/ca.pem")


class SubprocessTlsEnvTests(unittest.TestCase):
    """The external Node `kb-dashboard-cli` honors NODE_* TLS env vars."""

    def _clean_env(self):
        return mock.patch.dict(
            "os.environ",
            {"NODE_TLS_REJECT_UNAUTHORIZED": "", "NODE_EXTRA_CA_CERTS": ""},
            clear=False,
        )

    def test_verify_true_leaves_node_env_untouched(self):
        import os

        with self._clean_env():
            env = obs_http.apply_subprocess_tls_env(True)
            # Returns the mapping it modified (os.environ by default).
            self.assertNotIn("0", env.get("NODE_TLS_REJECT_UNAUTHORIZED", ""))
            self.assertEqual(os.environ.get("NODE_TLS_REJECT_UNAUTHORIZED", ""), "")
            self.assertEqual(os.environ.get("NODE_EXTRA_CA_CERTS", ""), "")

    def test_verify_false_sets_node_reject_unauthorized_zero(self):
        import os

        with self._clean_env():
            obs_http.apply_subprocess_tls_env(False)
            self.assertEqual(os.environ.get("NODE_TLS_REJECT_UNAUTHORIZED"), "0")

    def test_ca_path_sets_node_extra_ca_certs(self):
        import os

        with self._clean_env():
            obs_http.apply_subprocess_tls_env("/etc/ca.pem")
            self.assertEqual(os.environ.get("NODE_EXTRA_CA_CERTS"), "/etc/ca.pem")
            self.assertNotEqual(os.environ.get("NODE_TLS_REJECT_UNAUTHORIZED"), "0")

    def test_accepts_explicit_env_mapping(self):
        target: dict[str, str] = {}
        obs_http.apply_subprocess_tls_env(False, env=target)
        self.assertEqual(target.get("NODE_TLS_REJECT_UNAUTHORIZED"), "0")


class ApplyTlsTests(unittest.TestCase):
    def test_apply_tls_sets_session_verify_true(self):
        session = requests.Session()
        returned = obs_http.apply_tls(session, True)
        self.assertIs(returned, session)
        self.assertIs(session.verify, True)

    def test_apply_tls_sets_session_verify_path(self):
        session = requests.Session()
        obs_http.apply_tls(session, "/etc/ca.pem")
        self.assertEqual(session.verify, "/etc/ca.pem")

    def test_apply_tls_false_disables_and_suppresses_warning(self):
        session = requests.Session()
        with mock.patch.object(obs_http, "_suppress_insecure_request_warning") as mock_suppress:
            obs_http.apply_tls(session, False)
        self.assertIs(session.verify, False)
        mock_suppress.assert_called_once()


if __name__ == "__main__":
    unittest.main()
