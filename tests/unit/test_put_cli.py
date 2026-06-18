"""Unit tests for the put CLI output surface."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.application.publish.results import PutDryRunItem, PutResponse, PutUploadedFile
from roar.cli.commands.put import put
from roar.integrations.config import config_set
from roar.scope_config import save_repo_scope


def _mock_context(tmp_path: Path) -> MagicMock:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir(exist_ok=True)
    ctx = MagicMock()
    ctx.roar_dir = roar_dir
    ctx.repo_root = tmp_path
    ctx.cwd = tmp_path
    ctx.is_initialized = True
    return ctx


def test_put_cli_prints_structured_success_summary(tmp_path: Path) -> None:
    runner = CliRunner()
    response = PutResponse(
        success=True,
        destination="s3://bucket/release",
        job_id=7,
        job_uid="putjob1234",
        session_hash="0123456789abcdef0123456789abcdef",
        session_url="https://glaas.example/dag/0123456789abcdef0123456789abcdef",
        uploaded_files=[
            PutUploadedFile(
                local_path=str(tmp_path / "model.pt"),
                remote_url="s3://bucket/release/model.pt",
            )
        ],
        git_tag="roar/0123456789ab",
    )

    with patch("roar.cli.commands.put.put_artifacts", return_value=response):
        result = runner.invoke(
            put,
            ["model.pt", "s3://bucket/release", "-m", "publish release", "--yes"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code == 0, result.output
    assert "Published 1 file(s) to s3://bucket/release" in result.output
    assert "Session: 0123456789ab..." in result.output
    assert "Job step: @7" in result.output
    assert "Job UID: putjob1234" in result.output
    assert "Git tag: roar/0123456789ab" in result.output
    assert "GLaaS:" in result.output
    assert "https://glaas.example/dag/0123456789abcdef0123456789abcdef" in result.output
    assert "Next:" in result.output
    assert "roar show --job putjob1234" in result.output
    assert "roar show --session" in result.output
    assert "Created git tag" not in result.output


def test_put_cli_dry_run_mentions_destination_and_count(tmp_path: Path) -> None:
    runner = CliRunner()
    response = PutResponse(
        success=True,
        destination="s3://bucket/release",
        dry_run=True,
        would_upload=[PutDryRunItem(path=str(tmp_path / "model.pt"), exists=True)],
    )

    with patch("roar.cli.commands.put.put_artifacts", return_value=response):
        result = runner.invoke(
            put,
            ["model.pt", "s3://bucket/release", "-m", "publish release", "--dry-run"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code == 0, result.output
    assert "Dry run: would upload 1 file(s) to s3://bucket/release" in result.output
    assert str(tmp_path / "model.pt") in result.output


def test_put_cli_uses_public_default_from_config(tmp_path: Path) -> None:
    runner = CliRunner()
    config_set("registration.public_by_default", "true", start_dir=str(tmp_path))
    response = PutResponse(success=True, destination="s3://bucket/release")

    with patch("roar.cli.commands.put.put_artifacts", return_value=response) as mock_put:
        result = runner.invoke(
            put,
            ["model.pt", "s3://bucket/release", "-m", "publish release", "--yes"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code == 0, result.output
    assert (
        "Warning: defaulting to public visibility because registration.public_by_default=true"
        in result.output
    )
    request = mock_put.call_args.args[0]
    assert request.public is True


def test_put_cli_private_flag_overrides_public_default_from_config(tmp_path: Path) -> None:
    runner = CliRunner()
    config_set("registration.public_by_default", "true", start_dir=str(tmp_path))
    response = PutResponse(success=True, destination="s3://bucket/release")

    with patch("roar.cli.commands.put.put_artifacts", return_value=response) as mock_put:
        result = runner.invoke(
            put,
            ["model.pt", "s3://bucket/release", "-m", "publish release", "--private"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code == 0, result.output
    assert (
        "Warning: defaulting to public visibility because registration.public_by_default=true"
        not in result.output
    )
    request = mock_put.call_args.args[0]
    assert request.public is False


def test_put_cli_anonymous_forces_public_without_default_warning(tmp_path: Path) -> None:
    runner = CliRunner()
    config_set("registration.public_by_default", "false", start_dir=str(tmp_path))
    response = PutResponse(success=True, destination="s3://bucket/release")

    with patch("roar.cli.commands.put.put_artifacts", return_value=response) as mock_put:
        result = runner.invoke(
            put,
            ["model.pt", "s3://bucket/release", "-m", "publish release", "--anonymous", "--yes"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code == 0, result.output
    assert "Warning: defaulting to public visibility" not in result.output
    request = mock_put.call_args.args[0]
    assert request.public is True
    assert request.anonymous is True


def test_put_cli_prompts_before_anonymous_scope_publish(tmp_path: Path) -> None:
    runner = CliRunner()
    save_repo_scope("anonymous", start_dir=tmp_path)

    with patch("roar.cli.commands.put.put_artifacts") as mock_put:
        result = runner.invoke(
            put,
            ["model.pt", "s3://bucket/release", "-m", "publish release"],
            input="n\n",
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code != 0
    assert "Anonymous scope publishes publicly without account attribution." in result.output
    assert "Publish anonymously and publicly?" in result.output
    assert "Publication aborted." in result.output
    mock_put.assert_not_called()


def test_put_cli_anonymous_prompt_previews_publish_url(tmp_path: Path) -> None:
    """``roar put`` must also preview the GLaaS destination URL above the
    anonymous-publish confirmation, mirroring ``roar register``.
    """
    runner = CliRunner()
    save_repo_scope("anonymous", start_dir=tmp_path)

    with patch("roar.cli.commands.put.put_artifacts") as mock_put:
        result = runner.invoke(
            put,
            ["model.pt", "s3://bucket/release", "-m", "publish release"],
            input="n\n",
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code != 0
    assert "Will publish to: https://glaas.ai/dag/<session-hash>" in result.output
    preview_idx = result.output.index("Will publish to:")
    prompt_idx = result.output.index("Publish anonymously and publicly?")
    assert preview_idx < prompt_idx
    mock_put.assert_not_called()


def test_put_cli_rejects_anonymous_private(tmp_path: Path) -> None:
    runner = CliRunner()

    with patch("roar.cli.commands.put.put_artifacts") as mock_put:
        result = runner.invoke(
            put,
            [
                "model.pt",
                "s3://bucket/release",
                "-m",
                "publish release",
                "--anonymous",
                "--private",
            ],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code != 0
    assert "--anonymous requires public visibility" in result.output
    mock_put.assert_not_called()
