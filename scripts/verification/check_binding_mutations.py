# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Prove the symbolic binding checks reject removed production guards."""

import inspect
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from observability_migration.adapters.source.grafana.verification import _semantic_gate

ROOT = Path(__file__).resolve().parents[2]
MUTATIONS = {
    "unverified_labels_never_green": 'if (getattr(panel_result, "target_binding", None) or {}).get("status") in {"guessed", "missing"}:',
    "runtime_errors_remain_red": "if any(item in RUNTIME_ERROR_ROLLUPS for item in runtime_rollups):",
    "skipped_panels_remain_skipped": 'if status == "skipped":',
}


def main():
    gate = inspect.getsource(_semantic_gate)
    relative = Path("observability_migration/adapters/source/grafana/verification.py")
    source = (ROOT / relative).read_text()
    with tempfile.TemporaryDirectory(prefix="binding-mutations-") as directory:
        scratch = Path(directory).resolve()
        shutil.copytree(ROOT / "observability_migration", scratch / "observability_migration", ignore=shutil.ignore_patterns("__pycache__"))
        contracts = (ROOT / "scripts/verification/binding_contracts.py").read_text()
        contracts += f"\nassert _semantic_gate.__code__.co_filename.startswith({str(scratch)!r})\n"
        harness = scratch / "binding_contracts.py"
        harness.write_text(contracts)
        env = dict(os.environ, PYTHONPATH=str(scratch) + os.pathsep + str(ROOT))
        for name, condition in MUTATIONS.items():
            assert gate.count(condition) == 1, name
            (scratch / relative).write_text(source.replace(gate, gate.replace(condition, "if False:", 1), 1))
            for cached in (scratch / relative).parent.glob("__pycache__/verification.*.pyc"):
                cached.unlink()
            result = subprocess.run(
                ["uvx", "--from", "crosshair-tool==0.0.113", "--with-editable", str(ROOT), "crosshair", "check", str(harness),
                 "--analysis_kind=PEP316", "--report_all", "--per_condition_timeout=20", "--per_path_timeout=5"],
                cwd=scratch, env=env, capture_output=True, text=True, timeout=90,
            )
            output = result.stdout + result.stderr
            assert result.returncode != 0 and f"when calling {name}(" in output, output
            print(f"CAUGHT {name}: removed production guard")
    print("3/3 broken variants rejected; checkout unchanged")


if __name__ == "__main__":
    main()
