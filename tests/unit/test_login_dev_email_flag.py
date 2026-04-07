from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from roar.cli.commands.login import login


def test_dev_email_requires_feature_flag(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        login,
        ["--dev-email", "dev@example.com"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "xdg")},
    )

    assert result.exit_code != 0
    assert "ROAR_ENABLE_DEV_EMAIL_LOGIN=1" in result.output


def test_dev_email_works_when_feature_flag_enabled(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        login,
        ["--dev-email", "dev@example.com", "--force"],
        env={
            "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
            "ROAR_ENABLE_DEV_EMAIL_LOGIN": "1",
            "TREQS_API_URL": "https://api.treqs.ai",
        },
    )

    assert result.exit_code == 0, result.output
    assert "Stored auth state for dev@example.com <dev@example.com>" in result.output
