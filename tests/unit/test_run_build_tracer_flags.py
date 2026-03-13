"""Tests for the thin run/build CLI wrappers."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.application.run import BuildRequest, RunRequest
from roar.cli.commands.build import build
from roar.cli.commands.run import run


def _ctx():
    obj = MagicMock()
    obj.is_initialized = True
    obj.roar_dir = Path("/tmp/repo/.roar")
    obj.cwd = Path("/tmp/repo")
    return obj


class TestRunTracerFlags:
    def test_run_passes_request_to_application_service(self):
        runner = CliRunner()

        with patch("roar.cli.commands.run.run_command", return_value=0) as mock_run_command:
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
        request = mock_run_command.call_args.args[0]
        assert isinstance(request, RunRequest)
        assert request.roar_dir == Path("/tmp/repo/.roar")
        assert request.cwd == Path("/tmp/repo")
        assert request.args == ("python", "train.py")
        assert request.tracer_mode == "ptrace"
        assert request.tracer_fallback is False
        assert request.step_name == "preprocess"


class TestBuildTracerFlags:
    def test_build_passes_request_to_application_service(self):
        runner = CliRunner()

        with patch("roar.cli.commands.build.build_command", return_value=0) as mock_build_command:
            result = runner.invoke(
                build,
                ["--tracer", "ebpf", "--tracer-fallback", "--name", "bootstrap", "make", "-j4"],
                obj=_ctx(),
            )

        assert result.exit_code == 0
        request = mock_build_command.call_args.args[0]
        assert isinstance(request, BuildRequest)
        assert request.roar_dir == Path("/tmp/repo/.roar")
        assert request.cwd == Path("/tmp/repo")
        assert request.args == ("make", "-j4")
        assert request.tracer_mode == "ebpf"
        assert request.tracer_fallback is True
        assert request.step_name == "bootstrap"

    def test_build_accepts_preload_tracer_mode(self):
        runner = CliRunner()

        with patch("roar.cli.commands.build.build_command", return_value=0) as mock_build_command:
            result = runner.invoke(
                build,
                ["--tracer", "preload", "make", "-j4"],
                obj=_ctx(),
            )

        assert result.exit_code == 0
        request = mock_build_command.call_args.args[0]
        assert isinstance(request, BuildRequest)
        assert request.tracer_mode == "preload"
