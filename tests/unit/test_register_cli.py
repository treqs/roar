"""Unit tests for the register CLI output surface."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.application.publish.results import RegisterLineageResponse
from roar.cli.commands.register import register
from roar.integrations.config import config_set
from roar.scope_config import save_repo_scope


def _mock_context(tmp_path: Path) -> MagicMock:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir(exist_ok=True)
    ctx = MagicMock()
    ctx.roar_dir = roar_dir
    ctx.cwd = tmp_path
    ctx.is_initialized = True
    return ctx


def _fake_result() -> RegisterLineageResponse:
    return RegisterLineageResponse(
        success=True,
        aborted_by_user=False,
        error=None,
        session_hash="a" * 64,
        artifact_hash="b" * 64,
        jobs_registered=10,
        artifacts_registered=13,
        links_created=20,
        secrets_redacted=False,
        secrets_detected=[],
    )


def test_register_cli_accepts_s3_uri(tmp_path: Path) -> None:
    runner = CliRunner()

    with (
        patch("roar.cli.commands.register.register_lineage_target") as mock_register,
        patch(
            "roar.cli.commands.register._resolve_glaas_web_url",
            return_value="https://glaas.local",
        ),
    ):
        mock_register.return_value = _fake_result()
        result = runner.invoke(
            register,
            ["s3://output-bucket/results/run123/final_report.json", "--yes"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code == 0, result.output
    mock_register.assert_called_once()
    request = mock_register.call_args.args[0]
    assert request.target == "s3://output-bucket/results/run123/final_report.json"


def test_register_cli_prints_next_steps_for_artifacts(tmp_path: Path) -> None:
    runner = CliRunner()
    artifact_hash = "abcdef0123456789abcdef0123456789"
    response = RegisterLineageResponse(
        success=True,
        session_hash="0123456789abcdef0123456789abcdef",
        artifact_hash=artifact_hash,
        jobs_registered=3,
        artifacts_registered=4,
        links_created=5,
    )

    with (
        patch("roar.cli.commands.register.register_lineage_target", return_value=response),
        patch(
            "roar.cli.commands.register._resolve_glaas_web_url",
            return_value="https://glaas.example",
        ),
    ):
        result = runner.invoke(register, ["model.pt"], obj=_mock_context(tmp_path))

    assert result.exit_code == 0, result.output
    assert "Registered lineage for: model.pt" in result.output
    assert "Session: 0123456789ab..." in result.output
    assert "GLaaS:" in result.output
    assert "https://glaas.example/dag/0123456789abcdef0123456789abcdef" in result.output
    assert "https://glaas.example/artifact/abcdef0123456789abcdef0123456789" in result.output
    assert "Next:" in result.output
    assert f"roar show --artifact {artifact_hash}" in result.output
    assert f"roar reproduce {artifact_hash}" in result.output


def test_register_cli_prefers_returned_session_url(tmp_path: Path) -> None:
    runner = CliRunner()
    response = RegisterLineageResponse(
        success=True,
        session_hash="0123456789abcdef0123456789abcdef",
        session_url="https://glaas.example/sessions/published-session",
        jobs_registered=3,
        artifacts_registered=4,
        links_created=5,
    )

    with (
        patch("roar.cli.commands.register.register_lineage_target", return_value=response),
        patch(
            "roar.cli.commands.register._resolve_glaas_web_url",
            return_value="https://fallback.glaas.example",
        ),
    ):
        result = runner.invoke(register, ["model.pt"], obj=_mock_context(tmp_path))

    assert result.exit_code == 0, result.output
    assert "https://glaas.example/sessions/published-session" in result.output
    assert (
        "https://fallback.glaas.example/dag/0123456789abcdef0123456789abcdef" not in result.output
    )


def test_register_cli_dry_run_mentions_target(tmp_path: Path) -> None:
    runner = CliRunner()
    response = RegisterLineageResponse(
        success=True,
        session_hash="0123456789abcdef0123456789abcdef",
        jobs_registered=2,
        artifacts_registered=3,
        links_created=4,
    )

    with patch("roar.cli.commands.register.register_lineage_target", return_value=response):
        result = runner.invoke(register, ["model.pt", "--dry-run"], obj=_mock_context(tmp_path))

    assert result.exit_code == 0, result.output
    assert "Dry run: would register lineage for: model.pt" in result.output
    assert "Session: 0123456789ab..." in result.output


def test_register_cli_uses_public_default_from_config(tmp_path: Path) -> None:
    runner = CliRunner()
    config_set("registration.public_by_default", "true", start_dir=str(tmp_path))

    with patch("roar.cli.commands.register.register_lineage_target") as mock_register:
        mock_register.return_value = _fake_result()
        result = runner.invoke(register, ["model.pt", "--yes"], obj=_mock_context(tmp_path))

    assert result.exit_code == 0, result.output
    assert (
        "Warning: defaulting to public visibility because registration.public_by_default=true"
        in result.output
    )
    request = mock_register.call_args.args[0]
    assert request.public is True


def test_register_cli_private_flag_overrides_public_default_from_config(tmp_path: Path) -> None:
    runner = CliRunner()
    config_set("registration.public_by_default", "true", start_dir=str(tmp_path))

    with patch("roar.cli.commands.register.register_lineage_target") as mock_register:
        mock_register.return_value = _fake_result()
        result = runner.invoke(
            register, ["model.pt", "--yes", "--private"], obj=_mock_context(tmp_path)
        )

    assert result.exit_code == 0, result.output
    assert (
        "Warning: defaulting to public visibility because registration.public_by_default=true"
        not in result.output
    )
    request = mock_register.call_args.args[0]
    assert request.public is False


def test_register_cli_anonymous_forces_public_without_default_warning(tmp_path: Path) -> None:
    runner = CliRunner()
    config_set("registration.public_by_default", "false", start_dir=str(tmp_path))

    with patch("roar.cli.commands.register.register_lineage_target") as mock_register:
        mock_register.return_value = _fake_result()
        result = runner.invoke(
            register, ["model.pt", "--yes", "--anonymous"], obj=_mock_context(tmp_path)
        )

    assert result.exit_code == 0, result.output
    assert "Warning: defaulting to public visibility" not in result.output
    request = mock_register.call_args.args[0]
    assert request.public is True
    assert request.anonymous is True


def test_register_cli_uses_anonymous_scope_as_default_publish_intent(tmp_path: Path) -> None:
    runner = CliRunner()
    save_repo_scope("anonymous", start_dir=tmp_path)

    with patch("roar.cli.commands.register.register_lineage_target") as mock_register:
        mock_register.return_value = _fake_result()
        result = runner.invoke(register, ["model.pt", "--yes"], obj=_mock_context(tmp_path))

    assert result.exit_code == 0, result.output
    request = mock_register.call_args.args[0]
    assert request.public is True
    assert request.anonymous is True


def test_register_cli_prompts_before_anonymous_scope_publish(tmp_path: Path) -> None:
    runner = CliRunner()
    save_repo_scope("anonymous", start_dir=tmp_path)

    with patch("roar.cli.commands.register.register_lineage_target") as mock_register:
        result = runner.invoke(register, ["model.pt"], input="n\n", obj=_mock_context(tmp_path))

    assert result.exit_code != 0
    assert "Anonymous scope publishes publicly without account attribution." in result.output
    assert "Publish anonymously and publicly?" in result.output
    assert "Registration aborted." in result.output
    mock_register.assert_not_called()


def test_register_cli_anonymous_prompt_previews_publish_url(tmp_path: Path) -> None:
    """The confirmation prompt must show the GLaaS destination URL before
    asking the user to commit — the live prompt matches the --dry-run
    affordance so users can preview the destination before deciding.
    """
    runner = CliRunner()
    save_repo_scope("anonymous", start_dir=tmp_path)

    with patch("roar.cli.commands.register.register_lineage_target") as mock_register:
        result = runner.invoke(register, ["model.pt"], input="n\n", obj=_mock_context(tmp_path))

    assert result.exit_code != 0
    # Preview line should appear above the prompt copy.
    assert "Will publish to: https://glaas.ai/dag/<session-hash>" in result.output
    preview_idx = result.output.index("Will publish to:")
    prompt_idx = result.output.index("Publish anonymously and publicly?")
    assert preview_idx < prompt_idx
    mock_register.assert_not_called()


def test_register_cli_anonymous_prompt_uses_configured_glaas_host(tmp_path: Path) -> None:
    """When glaas.web_url is overridden (e.g. staging), the preview URL
    must reflect the configured host, not the public default.
    """
    runner = CliRunner()
    save_repo_scope("anonymous", start_dir=tmp_path)
    config_set("glaas.web_url", "https://glaas.staging.example", start_dir=str(tmp_path))

    with patch("roar.cli.commands.register.register_lineage_target") as mock_register:
        result = runner.invoke(register, ["model.pt"], input="n\n", obj=_mock_context(tmp_path))

    assert result.exit_code != 0
    assert "Will publish to: https://glaas.staging.example/dag/<session-hash>" in result.output
    mock_register.assert_not_called()


def test_register_cli_uses_private_scope_as_default_publish_intent(tmp_path: Path) -> None:
    runner = CliRunner()
    save_repo_scope("private", start_dir=tmp_path)

    with patch("roar.cli.commands.register.register_lineage_target") as mock_register:
        mock_register.return_value = _fake_result()
        result = runner.invoke(register, ["model.pt", "--yes"], obj=_mock_context(tmp_path))

    assert result.exit_code == 0, result.output
    request = mock_register.call_args.args[0]
    assert request.public is False
    assert request.anonymous is False


def test_register_cli_rejects_anonymous_private(tmp_path: Path) -> None:
    runner = CliRunner()

    with patch("roar.cli.commands.register.register_lineage_target") as mock_register:
        result = runner.invoke(
            register,
            ["model.pt", "--yes", "--anonymous", "--private"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code != 0
    assert "--anonymous requires public visibility" in result.output
    mock_register.assert_not_called()
