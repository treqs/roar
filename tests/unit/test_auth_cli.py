from unittest.mock import patch

from click.testing import CliRunner

from roar.cli.commands.auth import auth


def test_auth_help_lists_key_not_register() -> None:
    result = CliRunner().invoke(auth, ["--help"])

    assert result.exit_code == 0, result.output
    assert "roar auth key" in result.output
    assert "register" not in result.output


@patch("roar.cli.commands.auth._find_ssh_pubkey")
def test_auth_key_outputs_public_key(mock_find_ssh_pubkey) -> None:
    mock_find_ssh_pubkey.return_value = (
        "ssh-ed25519",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey user@example",
        "/tmp/id_ed25519.pub",
    )

    result = CliRunner().invoke(auth, ["key"])

    assert result.exit_code == 0, result.output
    assert "Your SSH public key:" in result.output
    assert "/tmp/id_ed25519.pub" in result.output


@patch("roar.cli.commands.auth._find_ssh_pubkey")
def test_auth_register_alias_matches_key_output(mock_find_ssh_pubkey) -> None:
    mock_find_ssh_pubkey.return_value = (
        "ssh-ed25519",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey user@example",
        "/tmp/id_ed25519.pub",
    )

    runner = CliRunner()
    key_result = runner.invoke(auth, ["key"])
    alias_result = runner.invoke(auth, ["register"])

    assert key_result.exit_code == 0, key_result.output
    assert alias_result.exit_code == 0, alias_result.output
    assert alias_result.output == key_result.output
