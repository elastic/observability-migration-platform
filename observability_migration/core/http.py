# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Shared TLS policy for outbound HTTP requests.

Every outbound connection (Elasticsearch, Kibana, Grafana, Prometheus/Loki,
Datadog) resolves its certificate-verification behavior through this module so
that the `--ca-cert` / `--insecure` CLI flags (and their `OBS_MIGRATE_CA_CERT` /
`OBS_MIGRATE_INSECURE` environment fallbacks) apply uniformly.

The resolved value is a `requests`-style ``verify`` setting:

- ``True``  — verify against the system CA bundle (default, unchanged behavior)
- ``str``   — verify against the CA bundle/file at this path
- ``False`` — skip certificate verification entirely (insecure; for testing
  and migration against self-signed clusters only)
"""

from __future__ import annotations

import os
import sys

import requests

__all__ = ["apply_subprocess_tls_env", "apply_tls", "resolve_tls"]

_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})

# Emit the insecure warning at most once per process so a multi-request run does
# not spam stderr.
_insecure_warning_emitted = False


def _reset_insecure_warning_for_tests() -> None:
    """Reset the one-shot insecure-warning guard (test helper)."""
    global _insecure_warning_emitted
    _insecure_warning_emitted = False


def _env_truthy(name: str) -> bool:
    return str(os.getenv(name, "") or "").strip().lower() in _TRUTHY_ENV_VALUES


def _warn_insecure_once() -> None:
    global _insecure_warning_emitted
    if _insecure_warning_emitted:
        return
    _insecure_warning_emitted = True
    print(
        "WARNING: TLS certificate verification disabled (--insecure / "
        "OBS_MIGRATE_INSECURE). Connections are vulnerable to interception; "
        "do not use this outside testing or trusted migration environments.",
        file=sys.stderr,
    )


def _suppress_insecure_request_warning() -> None:
    """Silence urllib3's per-request InsecureRequestWarning when verify=False."""
    try:
        from urllib3.exceptions import InsecureRequestWarning

        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - urllib3 internals vary by version
        pass


def resolve_tls(ca_cert: str = "", insecure: bool = False) -> bool | str:
    """Resolve the ``requests`` ``verify`` setting from args + environment.

    Precedence (highest first):

    1. ``insecure=True`` or ``OBS_MIGRATE_INSECURE`` truthy -> ``False``
       (emits a one-time loud stderr warning).
    2. ``ca_cert`` arg, else ``OBS_MIGRATE_CA_CERT`` env -> that path.
    3. Otherwise -> ``True`` (verify against the system CA bundle).
    """
    if insecure or _env_truthy("OBS_MIGRATE_INSECURE"):
        _warn_insecure_once()
        return False
    ca_path = str(ca_cert or "").strip() or str(os.getenv("OBS_MIGRATE_CA_CERT", "") or "").strip()
    if ca_path:
        return ca_path
    return True


def apply_tls(session: requests.Session, verify: bool | str = True) -> requests.Session:
    """Apply a resolved ``verify`` value to a ``requests.Session`` in place.

    Returns the same session for convenient chaining. When verification is
    disabled, urllib3's noisy per-request warning is suppressed.
    """
    session.verify = verify
    if verify is False:
        _suppress_insecure_request_warning()
    return session


def apply_subprocess_tls_env(
    verify: bool | str = True,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Translate a resolved ``verify`` value into Node.js TLS env vars.

    The dashboard compile/upload step shells out to the external Node
    ``kb-dashboard-cli``, which is not driven by ``requests`` and therefore
    cannot read our ``verify`` setting directly. Node honors two standard
    environment variables, so we map onto them for the subprocess it inherits:

    - ``verify is False`` -> ``NODE_TLS_REJECT_UNAUTHORIZED=0`` (disable checks)
    - ``verify`` is a path -> ``NODE_EXTRA_CA_CERTS=<path>`` (trust extra CA)
    - ``verify is True``  -> leave the environment untouched (default behavior)

    Mutates ``env`` (defaults to ``os.environ``) in place and returns it.
    """
    target = env if env is not None else os.environ
    if verify is False:
        target["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
    elif isinstance(verify, str) and verify.strip():
        target["NODE_EXTRA_CA_CERTS"] = verify.strip()
    return target  # type: ignore[return-value]
