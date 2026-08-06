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
    died and the grandchild ran on — a false failure plus a silent GPU-cost leak.

    We prove the kill by *liveness*, not by waiting out a sleep: the grandchild
    bumps a counter file every 50ms. Once the step times out and the group is
    SIGKILLed, the counter must stop advancing. (A plain ``os.kill(pid, 0)``
    check is unreliable here — a killed-but-unreaped grandchild is a zombie, for
    which ``os.kill`` still reports "alive".) No long fixed sleep, so this stays
    ~1.5s on the slow macOS lane."""
    heartbeat = tmp_path / "grandchild.heartbeat"
    # Real files, not `python -c` payloads — the -c escaping for a multi-line
    # loop is a trap (a single broken literal makes the child a no-op and the
    # test silently inconclusive).
    grand = tmp_path / "grand.py"
    grand.write_text(
        "import time\n"
        "i = 0\n"
        "while True:\n"
        f"    open({str(heartbeat)!r}, 'w').write(str(i))\n"
        "    i += 1\n"
        "    time.sleep(0.05)\n"
    )
    child = tmp_path / "child.py"
    child.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(grand)!r}])\n"
        "time.sleep(30)\n"
    )

    ex = _executor(step_timeout=1)
    env = MagicMock(repo_dir=tmp_path)

    start = time.monotonic()
    result = _drive(ex, f"{sys.executable} {child}", env)
    elapsed = time.monotonic() - start

    assert result is False
    assert elapsed < 8, f"timeout should fire ~1s, took {elapsed:.1f}s"

    # The grandchild starts heartbeating well within the 1s timeout.
    deadline = start + 3
    while not heartbeat.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert heartbeat.exists(), "grandchild never started — test inconclusive"

    # If the whole group was killed the counter is frozen; a survivor keeps
    # advancing it. Two reads 0.5s apart (>> the 50ms heartbeat) settle it.
    before = heartbeat.read_text()
    time.sleep(0.5)
    after = heartbeat.read_text()
    assert before == after, (
        f"grandchild kept running after the timeout ({before!r} -> {after!r}) "
        "— process group not killed"
    )
