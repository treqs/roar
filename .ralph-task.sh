#!/bin/bash
~/.openclaw/workspace/scripts/ralph.sh \
  --workdir /home/trevor/dev/roar \
  --task 'You are working ONLY in /home/trevor/dev/roar on branch tb/ray-submit. Do NOT touch any other directory.

TASK: Update _resolve_roar_requirement() in roar/cli/commands/_ray_job_submit.py to prefer
a local wheel file over PyPI when one exists in the working directory.

CONTEXT:
Currently _resolve_roar_requirement() returns "roar-cli==X.Y.Z" (PyPI install).
When running against a local Docker cluster or dev environment, the PyPI version may
not have the latest fixes. If a pre-built wheel exists at a known path relative to
the current working directory, use it instead.

Convention: look for a wheel at ./vendor/roar-cli.whl (relative to cwd where roar run is invoked).
If found: return "roar-cli @ file://<absolute_path_to_wheel>"
If not found: fall back to "roar-cli==X.Y.Z" (existing PyPI behavior)

IMPLEMENTATION:
In roar/cli/commands/_ray_job_submit.py, update _resolve_roar_requirement():
  1. Check Path("vendor/roar-cli.whl").resolve() — if it exists, return the file:// URI
  2. Otherwise fall back to existing PyPI logic
  No new config, no env vars — just the conventional path.

TDD:
Step 1 — Write failing tests in tests/unit/test_ray_job_submit_wheel.py:
  - test: _resolve_roar_requirement returns file:// URI when vendor/roar-cli.whl exists in cwd
    (use tmp_path + monkeypatch os.getcwd, create a fake .whl file)
  - test: _resolve_roar_requirement returns PyPI spec when no vendor wheel exists
  - test: full maybe_rewrite_ray_job_submit uses file:// wheel URI when wheel exists
  Run: uv run pytest tests/unit/test_ray_job_submit_wheel.py -v
  Confirm ALL FAIL first.

Step 2 — Implement. Run tests — ALL PASS.
  Commit: "feat(run): prefer local vendor wheel over PyPI when available"

Step 3 — Full suite:
  uv run pytest tests/unit/ -x -q
  No regressions.

Step 4 — Done:
  git log --oneline -5
  openclaw system event --text "RALPH_DONE: roar local wheel preference implemented" --mode now'
