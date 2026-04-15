"""Tests for the ``from roar import require`` guard module."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

# ---------------------------------------------------------------------------
# Helper: run ``from roar import require`` in an isolated subprocess so that
# sys.exit() doesn't kill the test runner.
# ---------------------------------------------------------------------------


def _run_require(*, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ROAR_")}
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", "from roar import require"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGuardEscapeHatch:
    def test_guard_passes_when_escape_hatch_set(self):
        result = _run_require(env_overrides={"ROAR_GUARD": "0"})
        assert result.returncode == 0

    def test_guard_exits_without_roar(self):
        result = _run_require()
        assert result.returncode == 1
        assert "roar run" in result.stderr

    def test_error_message_mentions_bypass(self):
        result = _run_require()
        assert "ROAR_GUARD=0" in result.stderr


class TestProcessTreeDetection:
    @pytest.mark.skipif(sys.platform != "linux", reason="Linux-only")
    def test_walk_linux_finds_no_roar_ancestor(self):
        from roar.require import _walk_linux

        # In a test runner, roar is not an ancestor.
        assert _walk_linux() is False

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")
    def test_walk_macos_finds_no_roar_ancestor(self):
        from roar.require import _walk_macos

        assert _walk_macos() is False


class TestIsRoarProcess:
    def test_roar_cli(self):
        from roar.require import _is_roar_process

        assert _is_roar_process("roar") is True

    def test_tracer_preload(self):
        from roar.require import _is_roar_process

        assert _is_roar_process("roar-tracer-preload") is True

    def test_tracer_ebpf(self):
        from roar.require import _is_roar_process

        assert _is_roar_process("roar-tracer-ebpf") is True

    def test_tracer_ptrace(self):
        from roar.require import _is_roar_process

        assert _is_roar_process("roar-tracer") is True

    def test_truncated_preload(self):
        """Linux /proc comm truncates to 15 chars."""
        from roar.require import _is_roar_process

        assert _is_roar_process("roar-tracer-pre") is True

    def test_truncated_ebpf(self):
        from roar.require import _is_roar_process

        assert _is_roar_process("roar-tracer-ebp") is True

    def test_unrelated_process(self):
        from roar.require import _is_roar_process

        assert _is_roar_process("python3") is False
        assert _is_roar_process("bash") is False
        assert _is_roar_process("roar-something-else") is False


class TestModuleImportIdempotent:
    def test_second_import_does_not_retrigger(self):
        """Python caches modules; second import should be a no-op."""
        result = _run_require(env_overrides={"ROAR_GUARD": "0"})
        assert result.returncode == 0
        # Import twice in the same process
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                textwrap.dedent("""\
                    from roar import require
                    from roar import require
                    print("ok")
                """),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "ROAR_GUARD": "0"},
            timeout=10,
        )
        assert result.returncode == 0
        assert "ok" in result.stdout
