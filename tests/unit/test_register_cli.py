from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.application.publish.results import RegisterLineageResponse
from roar.cli.commands.register import register


def _fake_result():
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


def test_register_cli_accepts_s3_uri(tmp_path):
    runner = CliRunner()

    ctx = MagicMock()
    ctx.roar_dir = tmp_path / ".roar"
    ctx.roar_dir.mkdir()
    ctx.cwd = tmp_path
    ctx.is_initialized = True

    with patch("roar.cli.commands.register.register_lineage_target") as mock_register:
        mock_register.return_value = _fake_result()
        with patch("roar.cli.commands.register.config_get", return_value="https://glaas.local"):
            result = runner.invoke(
                register,
                ["s3://output-bucket/results/run123/final_report.json", "--yes"],
                obj=ctx,
            )

    assert result.exit_code == 0, result.output
    mock_register.assert_called_once()
    request = mock_register.call_args.args[0]
    assert request.target == "s3://output-bucket/results/run123/final_report.json"
