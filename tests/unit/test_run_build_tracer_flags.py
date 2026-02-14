"""Tests for run/build tracer flag plumbing into execution helper."""

import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.cli.commands.build import build
from roar.cli.commands.run import run

run_module = importlib.import_module("roar.cli.commands.run")
build_module = importlib.import_module("roar.cli.commands.build")


def _ctx():
    obj = MagicMock()
    obj.is_initialized = True
    obj.roar_dir = Path("/tmp/repo/.roar")
    return obj


class TestRunTracerFlags:
    def test_run_forwards_tracer_flags(self):
        runner = CliRunner()

        with (
            patch.object(run_module, "validate_git_clean", return_value="/tmp/repo"),
            patch.object(run_module, "get_quiet_setting", return_value=False),
            patch.object(run_module, "get_hash_algorithms", return_value=["blake3"]),
            patch.object(run_module, "execute_and_report", return_value=0) as mock_exec,
        ):
            result = runner.invoke(
                run,
                [
                    "--tracer",
                    "ptrace",
                    "--no-tracer-fallback",
                    "--name",
                    "preprocess",
                    "python",
                    "train.py",
                ],
                obj=_ctx(),
            )

        assert result.exit_code == 0
        kwargs = mock_exec.call_args.kwargs
        assert kwargs["tracer_mode"] == "ptrace"
        assert kwargs["tracer_fallback"] is False
        assert kwargs["step_name"] == "preprocess"


class TestBuildTracerFlags:
    def test_build_forwards_tracer_flags(self):
        runner = CliRunner()

        with (
            patch.object(build_module, "validate_git_clean", return_value="/tmp/repo"),
            patch.object(build_module, "get_quiet_setting", return_value=False),
            patch.object(build_module, "get_hash_algorithms", return_value=["blake3"]),
            patch.object(build_module, "execute_and_report", return_value=0) as mock_exec,
        ):
            result = runner.invoke(
                build,
                ["--tracer", "ebpf", "--tracer-fallback", "--name", "bootstrap", "make", "-j4"],
                obj=_ctx(),
            )

        assert result.exit_code == 0
        kwargs = mock_exec.call_args.kwargs
        assert kwargs["tracer_mode"] == "ebpf"
        assert kwargs["tracer_fallback"] is True
        assert kwargs["step_name"] == "bootstrap"

    def test_build_accepts_preload_tracer_mode(self):
        runner = CliRunner()

        with (
            patch.object(build_module, "validate_git_clean", return_value="/tmp/repo"),
            patch.object(build_module, "get_quiet_setting", return_value=False),
            patch.object(build_module, "get_hash_algorithms", return_value=["blake3"]),
            patch.object(build_module, "execute_and_report", return_value=0) as mock_exec,
        ):
            result = runner.invoke(
                build,
                ["--tracer", "preload", "make", "-j4"],
                obj=_ctx(),
            )

        assert result.exit_code == 0
        kwargs = mock_exec.call_args.kwargs
        assert kwargs["tracer_mode"] == "preload"
