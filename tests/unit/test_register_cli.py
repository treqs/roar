from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.cli.commands.register import register


def test_register_cli_accepts_s3_uri(tmp_path):
    runner = CliRunner()

    ctx = MagicMock()
    ctx.roar_dir = tmp_path / ".roar"
    ctx.roar_dir.mkdir()
    ctx.cwd = tmp_path
    ctx.is_initialized = True

    fake_result = MagicMock(
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

    with patch("roar.cli.commands.register.RegisterService") as mock_service_cls:
        service = MagicMock()
        service.register_artifact_lineage.return_value = fake_result
        mock_service_cls.return_value = service

        with patch("roar.cli.commands.register.config_get", return_value="https://glaas.local"):
            result = runner.invoke(
                register,
                ["s3://output-bucket/results/run123/final_report.json", "--yes"],
                obj=ctx,
            )

    assert result.exit_code == 0, result.output
    service.register_artifact_lineage.assert_called_once()
    called_artifact_path = service.register_artifact_lineage.call_args.kwargs["artifact_path"]
    assert called_artifact_path == "s3://output-bucket/results/run123/final_report.json"
