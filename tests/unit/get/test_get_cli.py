"""Unit tests for the thin get CLI wrapper."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.application.get.requests import GetResponse, GetResult
from roar.cli.commands.get import get


def _mock_context(tmp_path: Path) -> MagicMock:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    ctx = MagicMock()
    ctx.roar_dir = roar_dir
    ctx.repo_root = tmp_path
    ctx.cwd = tmp_path
    ctx.is_initialized = True
    return ctx


def test_source_required(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(get, [], obj=_mock_context(tmp_path))

    assert result.exit_code != 0
    assert "Missing argument" in result.output or "Error" in result.output


def test_cli_surfaces_application_errors(tmp_path: Path) -> None:
    runner = CliRunner()

    with patch("roar.cli.commands.get.get_artifacts", side_effect=ValueError("Unsupported")):
        result = runner.invoke(get, ["ftp://server/file.pt"], obj=_mock_context(tmp_path))

    assert result.exit_code != 0
    assert "Unsupported" in result.output


def test_cli_prints_dry_run_plan(tmp_path: Path) -> None:
    runner = CliRunner()
    response = GetResponse(
        result=GetResult(
            success=True,
            dry_run=True,
            would_download=[
                {
                    "remote_url": "s3://bucket/model.pt",
                    "local_path": str(tmp_path / "model.pt"),
                }
            ],
        )
    )

    with patch("roar.cli.commands.get.get_artifacts", return_value=response):
        result = runner.invoke(
            get,
            ["s3://bucket/model.pt", "--dry-run"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code == 0
    assert "Dry run - would download:" in result.output
    assert "s3://bucket/model.pt" in result.output


def test_cli_prints_success_tag_and_warnings(tmp_path: Path) -> None:
    runner = CliRunner()
    response = GetResponse(
        result=GetResult(
            success=True,
            job_id=7,
            downloaded_files=[
                {
                    "remote_url": "s3://bucket/model.pt",
                    "local_path": str(tmp_path / "model.pt"),
                }
            ],
        ),
        git_tag="roar/deadbeef",
        warnings=["Could not create git tag: warning"],
    )

    with patch("roar.cli.commands.get.get_artifacts", return_value=response):
        result = runner.invoke(
            get,
            ["s3://bucket/model.pt", str(tmp_path / "model.pt"), "--tag"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code == 0
    assert "Created git tag: roar/deadbeef" in result.output
    assert "Downloaded 1 file(s) from s3://bucket/model.pt" in result.output
    assert "Job created: step 7" in result.output
    assert "Warning: Could not create git tag: warning" in result.stderr
