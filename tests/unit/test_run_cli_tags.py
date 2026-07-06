"""Tests for `roar run`'s --block-tag / --add-tag option parsing.

Click validates repeatable options (callbacks) during argument parsing,
before the command body (and require_init) ever runs — so these can be
exercised without a real RoarContext/roar_dir.
"""

from __future__ import annotations

import importlib

from click.testing import CliRunner

run_cli_module = importlib.import_module("roar.cli.commands.run")


class TestAddTagValidation:
    def test_malformed_pair_is_rejected(self) -> None:
        runner = CliRunner()
        result = runner.invoke(run_cli_module.run, ["--add-tag", "badpair", "echo", "hi"])
        assert result.exit_code != 0
        assert "Expected KIND=VALUE" in result.output

    def test_empty_value_is_rejected(self) -> None:
        runner = CliRunner()
        result = runner.invoke(run_cli_module.run, ["--add-tag", "license=", "echo", "hi"])
        assert result.exit_code != 0
        assert "Value cannot be empty" in result.output

    def test_noncanonical_kind_warns_but_does_not_reject(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            run_cli_module.run,
            ["--add-tag", "not_a_real_kind=value", "echo", "hi"],
            obj=None,
        )
        # Fails later (no RoarContext), but the callback itself must not raise.
        assert "not a canonical tag kind" in result.output

    def test_canonical_kind_does_not_warn(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            run_cli_module.run,
            ["--add-tag", "license=MIT", "echo", "hi"],
            obj=None,
        )
        assert "not a canonical tag kind" not in result.output
