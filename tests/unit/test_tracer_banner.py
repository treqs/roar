"""Tests for the tracer-backend explainer (formerly the per-run banner).

The earlier per-machine state file + every-run emission was noise; the
text now only surfaces when the user explicitly picks a backend via
`roar tracer <backend>` / `roar tracer set-default <backend>`. This file
verifies the data lookup and the CLI surfacing.
"""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from roar.execution.runtime.tracer_banner import banner_for


class TestBannerFor:
    def test_preload_text(self) -> None:
        text = banner_for("preload") or ""
        assert "Selected preload tracer" in text
        assert "CAP_BPF" in text
        assert "shell pipelines" in text

    def test_ptrace_text(self) -> None:
        text = banner_for("ptrace") or ""
        assert "Selected ptrace tracer" in text
        assert "overhead" in text

    def test_ebpf_text(self) -> None:
        text = banner_for("ebpf") or ""
        assert "Selected eBPF tracer" in text
        assert "full" in text.lower()

    def test_unknown_backend(self) -> None:
        assert banner_for("voodoo") is None


class TestSetDefaultPrintsBanner:
    """The text only surfaces on `roar tracer <backend>`; not on `roar run`."""

    def test_setting_ebpf_prints_banner(self) -> None:
        from roar.cli.commands.tracer import tracer

        with patch(
            "roar.cli.commands.tracer.config_set",
            return_value=("/tmp/config.toml", "ebpf"),
        ):
            result = CliRunner().invoke(tracer, ["ebpf"])
        assert result.exit_code == 0
        assert "Default tracer set to: ebpf" in result.output
        assert "Selected eBPF tracer" in result.output

    def test_setting_preload_prints_banner(self) -> None:
        from roar.cli.commands.tracer import tracer

        with patch(
            "roar.cli.commands.tracer.config_set",
            return_value=("/tmp/config.toml", "preload"),
        ):
            result = CliRunner().invoke(tracer, ["preload"])
        assert result.exit_code == 0
        assert "Selected preload tracer" in result.output

    def test_setting_auto_does_not_print_banner(self) -> None:
        """`auto` isn't a specific backend — no per-backend text to print."""
        from roar.cli.commands.tracer import tracer

        with patch(
            "roar.cli.commands.tracer.config_set",
            return_value=("/tmp/config.toml", "auto"),
        ):
            result = CliRunner().invoke(tracer, ["auto"])
        assert result.exit_code == 0
        assert "Default tracer set to: auto" in result.output
        assert "Selected " not in result.output
