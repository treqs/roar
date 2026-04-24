"""Tests for the `roar tracer` CLI command group."""

import importlib
import json
from unittest.mock import patch

from click.testing import CliRunner

tracer_cli_module = importlib.import_module("roar.cli.commands.tracer")


class TestTracerCli:
    def test_set_default_writes_tracer_default_key(self):
        runner = CliRunner()
        with patch.object(
            tracer_cli_module, "config_set", return_value=("/tmp/config.toml", "ebpf")
        ) as mock_set:
            result = runner.invoke(tracer_cli_module.tracer, ["set-default", "ebpf"])

        assert result.exit_code == 0
        mock_set.assert_called_once_with("tracer.default", "ebpf")
        assert "Default tracer set to: ebpf" in result.output

    def test_set_default_preload_via_alias(self):
        runner = CliRunner()
        with patch.object(
            tracer_cli_module, "config_set", return_value=("/tmp/config.toml", "preload")
        ) as mock_set:
            result = runner.invoke(tracer_cli_module.tracer, ["preload"])

        assert result.exit_code == 0
        mock_set.assert_called_once_with("tracer.default", "preload")
        assert "Default tracer set to: preload" in result.output

    def test_check_uses_strict_backend_preflight(self):
        runner = CliRunner()
        preflight = tracer_cli_module.tracer_backends.TracerPreflightResult(
            backend="ptrace",
            ok=True,
            summary="ptrace preflight succeeded",
            checks=(
                tracer_cli_module.tracer_backends.PreflightCheck(
                    "binary", True, "/bin/roar-tracer"
                ),
            ),
        )
        with patch.object(
            tracer_cli_module,
            "_backend_preflight",
            return_value=preflight,
        ) as mock_preflight:
            result = runner.invoke(tracer_cli_module.tracer, ["check", "--backend", "ptrace"])

        assert result.exit_code == 0
        mock_preflight.assert_called_once_with("ptrace", None)
        assert "Tracer check passed for 'ptrace': ptrace preflight succeeded" in result.output
        assert "binary: ok (/bin/roar-tracer)" in result.output

    def test_check_json_emits_structured_output(self):
        runner = CliRunner()
        preflight = tracer_cli_module.tracer_backends.TracerPreflightResult(
            backend="ebpf",
            ok=False,
            summary="attach failed",
            checks=(
                tracer_cli_module.tracer_backends.PreflightCheck(
                    "load_and_attach", False, "missing tracepoint"
                ),
            ),
        )
        with patch.object(tracer_cli_module, "_backend_preflight", return_value=preflight):
            result = runner.invoke(
                tracer_cli_module.tracer,
                ["check", "--backend", "ebpf", "--json"],
            )

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["backend"] == "ebpf"
        assert payload["ok"] is False
        assert payload["summary"] == "attach failed"

    def test_check_uses_auto_preflight_when_backend_unspecified(self):
        runner = CliRunner()
        auto_result = tracer_cli_module.tracer_backends.AutoPreflightResult(
            ok=True,
            selected_backend="preload",
            summary="selected backend 'preload'",
            results=(
                tracer_cli_module.tracer_backends.TracerPreflightResult(
                    backend="ebpf",
                    ok=False,
                    summary="attach failed",
                ),
                tracer_cli_module.tracer_backends.TracerPreflightResult(
                    backend="preload",
                    ok=True,
                    summary="preload preflight succeeded",
                ),
            ),
        )
        with (
            patch.object(tracer_cli_module, "_get_default_mode", return_value="auto"),
            patch.object(
                tracer_cli_module, "_backend_preflight", return_value=auto_result
            ) as mock_preflight,
        ):
            result = runner.invoke(tracer_cli_module.tracer, ["check"])

        assert result.exit_code == 0
        mock_preflight.assert_called_once_with("auto", None)
        assert "Tracer check passed for 'auto': selected backend 'preload'" in result.output
        assert "ebpf: attach failed" in result.output
        assert "preload: ok" in result.output
