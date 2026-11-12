# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Run symbolic, generated-input and mutation checks for label readiness."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    env = dict(os.environ, PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    subprocess.run([sys.executable, "-m", "pytest", "tests/test_binding_invariants.py", "tests/test_redis_composite_binding.py", "-q"], cwd=ROOT, env=env, check=True)
    symbolic = subprocess.run(
        ["uvx", "--from", "crosshair-tool==0.0.113", "--with-editable", str(ROOT), "crosshair", "check",
         "scripts/verification/binding_contracts.py", "--analysis_kind=PEP316", "--report_all",
         "--per_condition_timeout=20", "--per_path_timeout=5"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=True,
    )
    output = symbolic.stdout + symbolic.stderr
    print(output, end="")
    if output.count("Confirmed over all paths.") != 3:
        raise SystemExit("All three contracts must be confirmed; timeout/unknown is not a pass.")
    subprocess.run([sys.executable, "scripts/verification/check_binding_mutations.py"], cwd=ROOT, env=env, check=True)
    if env.get("ES_URL"):
        subprocess.run([sys.executable, "-m", "pytest", "tests/e2e/test_composite_binding_runtime.py", "-q"], cwd=ROOT, env=env, check=True)
    else:
        print("Live ES|QL cases not run: set ES_URL (and ES_API_KEY if needed).")


if __name__ == "__main__":
    main()
