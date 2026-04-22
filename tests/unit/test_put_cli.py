"""Unit tests for the put CLI output surface."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.application.publish.results import PutDryRunItem, PutResponse, PutUploadedFile
from roar.cli.commands.put import put
from roar.integrations.config import config_set


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
            ["model.pt", "s3://bucket/release", "-m", "publish release"],
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


def test_put_cli_dry_run_does_not_load_glaas_web_url(tmp_path: Path) -> None:
    runner = CliRunner()
    response = PutResponse(
        success=True,
        destination="s3://bucket/release",
        dry_run=True,
        would_upload=[PutDryRunItem(path=str(tmp_path / "model.pt"), exists=True)],
    )

    with (
        patch("roar.cli.commands.put.put_artifacts", return_value=response),
        patch("roar.cli.commands.put._resolve_glaas_web_url") as resolve_web_url,
    ):
        result = runner.invoke(
            put,
            ["model.pt", "s3://bucket/release", "-m", "publish release", "--dry-run"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code == 0, result.output
    resolve_web_url.assert_not_called()


def test_put_cli_uses_public_default_from_config(tmp_path: Path) -> None:
    runner = CliRunner()
    config_set("registration.public_by_default", "true", start_dir=str(tmp_path))
    response = PutResponse(success=True, destination="s3://bucket/release")

    with patch("roar.cli.commands.put.put_artifacts", return_value=response) as mock_put:
        result = runner.invoke(
            put,
            ["model.pt", "s3://bucket/release", "-m", "publish release"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code == 0, result.output
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
    request = mock_put.call_args.args[0]
    assert request.public is False
