#!/usr/bin/env python3
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""License compliance gate for obs-migrate.

Runs `pip-licenses` against the checked Python dependency environment and
fails the build if any installed dependency uses a license that is not on
the allowlist or that is explicitly denied. This job is meant to run in CI
against a locked Python 3.11 environment synchronized from `uv.lock`.

Use ``--write-report`` locally to refresh
``docs/licenses/dependencies.md`` before opening a pull request.

Exit codes:
  0  All checked dependency licenses are allowed.
  1  One or more dependencies violate the allowlist or denylist.
  2  The ``pip-licenses`` tool is not importable.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = REPO_ROOT / "docs" / "licenses" / "dependencies.md"

# First-party packages to ignore entirely. The project's own editable install
# has no license metadata exposed to pip-licenses and must not fail the gate.
IGNORE_PACKAGES = {"obs-migrate"}

# Manual license overrides for packages whose PyPI metadata is missing but
# whose upstream LICENSE file has been inspected directly.
#
# Each entry MUST cite the upstream LICENSE URL that was inspected, and the
# check_licenses script refuses to fall back to the manual value unless
# pip-licenses reports the package as UNKNOWN.
LICENSE_OVERRIDES = {
    "promql-parser": {
        "license": "MIT",
        "source": "https://github.com/messense/py-promql-parser/blob/main/LICENSE",
    },
    "verlib2": {
        "license": "BSD-2-Clause",
        "source": "https://github.com/pyveci/verlib2/blob/main/LICENSE",
    },
}

# Allowlist of SPDX-style or pip-licenses-style labels that are compatible
# with Elastic License 2.0 redistribution of source and compiled artifacts.
# Keep this list conservative and prefer explicit additions over wildcards.
#
# LGPL entries are allowlisted for this check because the locked dependency
# environment installs them only as external site-packages; the repository
# does not vendor or modify LGPL source. Revisit this policy with legal
# before any distribution model change (for example vendoring, bundling, or
# shipping standalone binaries).
ALLOWED_LICENSES = {
    "Apache-2.0",
    "Apache 2.0",
    "Apache License 2.0",
    "Apache Software License",
    "Apache Software License; BSD License",
    "Apache-2.0 OR BSD-2-Clause",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "BSD License",
    "ISC",
    "ISC License (ISCL)",
    "MIT",
    "MIT License",
    "Mozilla Public License 2.0 (MPL 2.0)",
    "MPL-2.0",
    "PSF-2.0",
    "Python Software Foundation License",
    "The Unlicense (Unlicense)",
    "Unlicense",
    "Public Domain",
    "CC0-1.0",
    "CC0 1.0 Universal (CC0 1.0) Public Domain Dedication",
    # LGPL — dynamic-linking-only; see comment above.
    "LGPL-2.0",
    "LGPL-2.0-only",
    "LGPL-2.0-or-later",
    "LGPL-2.1",
    "LGPL-2.1-only",
    "LGPL-2.1-or-later",
    "LGPL-3.0",
    "LGPL-3.0-only",
    "LGPL-3.0-or-later",
    "LGPL",
    "GNU Lesser General Public License v2 or later (LGPLv2+)",
    "GNU Lesser General Public License v3 (LGPLv3)",
    "GNU Lesser General Public License v3 or later (LGPLv3+)",
    "GNU Library or Lesser General Public License (LGPL)",
}

# Licenses that must never appear in the checked dependency environment. If any
# dependency reports one of these, the check fails immediately — we cannot
# redistribute them under ELv2 without additional analysis.
DENIED_LICENSES = {
    "AGPL-3.0",
    "AGPL-3.0-only",
    "AGPL-3.0-or-later",
    "GNU Affero General Public License v3",
    "GNU Affero General Public License v3 or later (AGPLv3+)",
    "GPL-2.0",
    "GPL-2.0-only",
    "GPL-2.0-or-later",
    "GPL-3.0",
    "GPL-3.0-only",
    "GPL-3.0-or-later",
    "GNU General Public License v2 (GPLv2)",
    "GNU General Public License v3 (GPLv3)",
    "GNU General Public License v3 or later (GPLv3+)",
    "SSPL-1.0",
    "Server Side Public License",
    "BUSL-1.1",
    "Business Source License 1.1",
}


def _run_pip_licenses() -> list[dict[str, str]]:
    """Invoke pip-licenses and return its JSON payload."""
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "piplicenses",
                "--format=json",
                "--with-system",
                "--with-urls",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        sys.stderr.write(
            "error: pip-licenses is not installed. "
            "Install it via `pip install pip-licenses` or "
            "`pip install -e \".[dev]\"`.\n"
        )
        sys.exit(2)
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"error: pip-licenses failed:\n{exc.stderr}\n")
        sys.exit(2)
    return json.loads(completed.stdout)


def _resolve_license(entry: dict[str, str]) -> tuple[str, str | None]:
    """Return (license_label, override_source) for a package entry."""
    name = entry["Name"]
    reported = (entry.get("License") or "").strip() or "UNKNOWN"
    if reported == "UNKNOWN" and name in LICENSE_OVERRIDES:
        override = LICENSE_OVERRIDES[name]
        return override["license"], override["source"]
    return reported, None


def _classify(license_label: str) -> str:
    """Classify a license label as allowed / denied / unknown."""
    if license_label in DENIED_LICENSES:
        return "denied"
    if license_label in ALLOWED_LICENSES:
        return "allowed"
    return "unknown"


def _write_report(packages: list[dict[str, object]]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# Python Dependency Environment License Inventory",
        "",
        "This file is produced by `scripts/check_licenses.py --write-report`",
        "against the installed environment and is enforced for drift in CI by",
        "`.github/workflows/license-check.yml`. Do not edit by hand; regenerate",
        "it after adding or bumping dependencies in `pyproject.toml` or `uv.lock`.",
        "",
        "| Package | Version | License | Source |",
        "| --- | --- | --- | --- |",
    ]
    for entry in packages:
        name = entry["name"]
        version = entry["version"]
        license_label = entry["license"]
        source_note = entry.get("override_source")
        raw_url = entry.get("url") or ""
        url = source_note if not raw_url or raw_url == "UNKNOWN" else raw_url
        if source_note:
            license_label = f"{license_label} (manual override — see {source_note})"
        url_cell = f"<{url}>" if url and url != "UNKNOWN" else ""
        lines.append(f"| `{name}` | {version} | {license_label} | {url_cell} |")
    lines.append("")
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-report",
        action="store_true",
        help=(
            "Refresh docs/licenses/dependencies.md with the checked Python "
            "dependency environment inventory (use locally before opening a PR)."
        ),
    )
    parser.add_argument(
        "--strict-unknown",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Fail when a package reports an UNKNOWN license and no manual "
            "override exists (default: enabled)."
        ),
    )
    args = parser.parse_args(argv)

    raw_entries = _run_pip_licenses()
    entries = [e for e in raw_entries if e["Name"] not in IGNORE_PACKAGES]
    entries.sort(key=lambda e: e["Name"].lower())

    denied: list[tuple[str, str]] = []
    unknown: list[tuple[str, str]] = []
    report_rows: list[dict[str, object]] = []

    for entry in entries:
        name = entry["Name"]
        version = entry["Version"]
        url = entry.get("URL", "")
        license_label, override_source = _resolve_license(entry)
        verdict = _classify(license_label)
        if verdict == "denied":
            denied.append((name, license_label))
        elif verdict == "unknown":
            unknown.append((name, license_label))
        report_rows.append(
            {
                "name": name,
                "version": version,
                "license": license_label,
                "url": url,
                "override_source": override_source,
            }
        )

    if args.write_report:
        _write_report(report_rows)
        print(f"wrote {REPORT_PATH.relative_to(REPO_ROOT)}")

    violations = denied + (unknown if args.strict_unknown else [])
    if violations:
        print("License compliance check FAILED:", file=sys.stderr)
        for name, lic in denied:
            print(f"  - {name!s}: DENIED license {lic!r}", file=sys.stderr)
        for name, lic in unknown:
            print(
                f"  - {name!s}: UNKNOWN license {lic!r} "
                "(add to LICENSE_OVERRIDES or ALLOWED_LICENSES after review)",
                file=sys.stderr,
            )
        print(
            "\nAllowed license labels:\n  "
            + "\n  ".join(sorted(ALLOWED_LICENSES)),
            file=sys.stderr,
        )
        return 1

    print(
        f"License compliance check passed: {len(entries)} checked dependency packages, "
        f"all within the allowlist."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
