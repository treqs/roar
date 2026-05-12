"""Tests for the `roar tracer` CLI command group."""

import importlib
import json
from unittest.mock import patch

from click.testing import CliRunner

tracer_cli_module = importlib.import_module("roar.cli.commands.tracer")


class TestTracerStatusEbpfRow:
    """`roar tracer` should surface the cause-of-not-ready inline under
    the ebpf row — not a separate trailing block."""

    def test_too_restrictive_paranoid_shows_under_ebpf(self):
        runner = CliRunner()
        # Force fixable-eBPF state by stubbing the classifier.
        fixable = tracer_cli_module._BackendStatus("fixable", "perf_event_paranoid=4 (needs <= 1)")
        with patch.dict(
            tracer_cli_module._CLASSIFIERS,
            {
                "ebpf": lambda: fixable,
                "preload": lambda: tracer_cli_module._BackendStatus("ready", None),
                "ptrace": lambda: tracer_cli_module._BackendStatus("ready", None),
            },
        ):
            result = runner.invoke(tracer_cli_module.tracer)
        assert result.exit_code == 0, result.output
        assert "ebpf" in result.output
        assert "not ready" in result.output
        # Cause shows as a `↳` continuation under the ebpf row.
        assert "↳ perf_event_paranoid=4 (needs <= 1)" in result.output


class TestTracerUseVerb:
    def test_use_writes_tracer_default_key(self):
        runner = CliRunner()
        with patch.object(
            tracer_cli_module, "config_set", return_value=("/tmp/config.toml", "ebpf")
        ) as mock_set:
            result = runner.invoke(tracer_cli_module.tracer, ["use", "ebpf"])

        assert result.exit_code == 0, result.output
        mock_set.assert_called_once_with("tracer.default", "ebpf")
        assert "Default tracer set to: ebpf" in result.output

    def test_use_preload(self):
        runner = CliRunner()
        with patch.object(
            tracer_cli_module, "config_set", return_value=("/tmp/config.toml", "preload")
        ) as mock_set:
            result = runner.invoke(tracer_cli_module.tracer, ["use", "preload"])

        assert result.exit_code == 0, result.output
        mock_set.assert_called_once_with("tracer.default", "preload")
        assert "Default tracer set to: preload" in result.output

    def test_use_warns_when_picked_backend_is_fixable(self):
        runner = CliRunner()
        fixable = tracer_cli_module._BackendStatus("fixable", "perf_event_paranoid=4 (needs <= 1)")
        with (
            patch.object(tracer_cli_module, "config_set", return_value=("/tmp/c.toml", "ebpf")),
            patch.dict(tracer_cli_module._CLASSIFIERS, {"ebpf": lambda: fixable}, clear=False),
        ):
            result = runner.invoke(tracer_cli_module.tracer, ["use", "ebpf"])
        assert result.exit_code == 0, result.output
        assert "ebpf is not ready" in result.output
        assert "roar tracer enable ebpf" in result.output

    def test_use_warns_when_picked_backend_unavailable(self):
        runner = CliRunner()
        unavail = tracer_cli_module._BackendStatus("unavailable", "darwin (Linux-only)")
        with (
            patch.object(tracer_cli_module, "config_set", return_value=("/tmp/c.toml", "ebpf")),
            patch.dict(tracer_cli_module._CLASSIFIERS, {"ebpf": lambda: unavail}, clear=False),
        ):
            result = runner.invoke(tracer_cli_module.tracer, ["use", "ebpf"])
        assert result.exit_code == 0, result.output
        assert "unavailable" in result.output
        assert "darwin" in result.output


class TestTracerCli:
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
        assert "Next steps" not in result.output

    def test_check_failure_prints_actionable_next_steps_without_traceback(self):
        runner = CliRunner()
        preflight = tracer_cli_module.tracer_backends.TracerPreflightResult(
            backend="ebpf",
            ok=False,
            summary="failed to attach sys_enter_open tracepoint",
            checks=(
                tracer_cli_module.tracer_backends.PreflightCheck(
                    "load_and_attach", False, "missing tracepoint"
                ),
            ),
        )
        with patch.object(tracer_cli_module, "_backend_preflight", return_value=preflight):
            result = runner.invoke(tracer_cli_module.tracer, ["check", "--backend", "ebpf"])

        assert result.exit_code == 1
        assert "Tracer check failed for 'ebpf'" in result.output
        assert "Next steps:" in result.output
        assert "roar tracer check --backend preload" in result.output
        assert "Traceback" not in result.output

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
        # `auto` is the first call; the new flow then calls back into
        # _backend_preflight with the selected backend to render the deep
        # per-check detail consistently with `roar tracer check <backend>`.
        mock_preflight.assert_any_call("auto", None)
        mock_preflight.assert_any_call("preload", None)
        assert "Tracer check passed for 'auto': selected backend 'preload'" in result.output
        assert "ebpf: attach failed" in result.output
        assert "preload: ok" in result.output
