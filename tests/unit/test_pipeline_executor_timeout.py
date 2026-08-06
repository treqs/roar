"""P0-2: per-step timeout is configurable (default none) and, when it fires,
kills the whole process group — not just the shell — so a grandchild workload
(e.g. train.py) can't keep running past the declared failure."""

import os
import sys
import time
from unittest.mock import MagicMock

from roar.execution.reproduction.pipeline_executor import PipelineExecutor


def _executor(step_timeout=None):
    ex = PipelineExecutor(roar_executable="/bin/true", step_timeout=step_timeout)
    ex._print = lambda *_: None
    return ex


def _drive(ex, wrapped_command, environment):
    """Run one step, forcing the wrapped command and a clean env."""
    ex._wrap_with_roar = lambda *a, **k: wrapped_command
    ex._prepare_environment = lambda *a, **k: dict(os.environ)
    step = {"command": "x", "metadata": {}}
    return ex._run_step(step, environment, is_build=False)


def test_default_step_timeout_is_none():
    assert PipelineExecutor()._step_timeout is None


def test_quick_command_succeeds_with_no_timeout(tmp_path):
    ex = _executor(step_timeout=None)
    env = MagicMock(repo_dir=tmp_path)
    assert _drive(ex, f'{sys.executable} -c "pass"', env) is True


def test_failing_command_returns_false(tmp_path):
    ex = _executor(step_timeout=None)
    env = MagicMock(repo_dir=tmp_path)
    assert _drive(ex, f'{sys.executable} -c "raise SystemExit(3)"', env) is False


def test_timeout_kills_the_whole_process_group(tmp_path):
    """A shell child that spawns a long-lived grandchild must be fully reaped on
    timeout. Before the fix (shell=True + subprocess.run timeout), only the shell
    died and the grandchild ran on. The grandchild here touches a marker after a
    long sleep; if the tree was killed the marker must NOT appear."""
    marker = tmp_path / "grandchild_finished"
    child = tmp_path / "child.py"
    child.write_text(
        "import subprocess, sys, time\n"
        "grand = (\n"
        "    'import time; time.sleep(20); "
        f"open({str(marker)!r}, \"w\").close()'\n"
        ")\n"
        "subprocess.Popen([sys.executable, '-c', grand])\n"
        "time.sleep(20)\n"
    )

    ex = _executor(step_timeout=1)
    env = MagicMock(repo_dir=tmp_path)

    start = time.monotonic()
    result = _drive(ex, f"{sys.executable} {child}", env)
    elapsed = time.monotonic() - start

    assert result is False
    assert elapsed < 8, f"timeout should fire ~1s, took {elapsed:.1f}s"

    # Give any survivor time to reach its marker write; it must not, because the
    # whole process group (incl. the grandchild) was SIGKILLed.
    time.sleep(3)
    assert not marker.exists(), "grandchild survived the timeout — process group not killed"
