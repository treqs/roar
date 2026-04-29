"""Unit tests for the export-registration-package CLI surface."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from roar.application.publish.registration_package import ExportRegistrationPackageResponse
from roar.cli.commands.export_registration_package import export_registration_package


def _mock_context(tmp_path: Path) -> MagicMock:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir(exist_ok=True)
    ctx = MagicMock()
    ctx.roar_dir = roar_dir
    ctx.cwd = tmp_path
    ctx.is_initialized = True
    return ctx


def test_export_registration_package_cli_emits_json_stdout(tmp_path: Path) -> None:
    runner = CliRunner()
    output = tmp_path / "lineage.roarreg"
    response = ExportRegistrationPackageResponse(
        success=True,
        package_path=str(output),
        package_sha256="a" * 64,
        job_count=3,
        artifact_count=12,
        link_count=18,
    )

    with patch(
        "roar.cli.commands.export_registration_package.export_registration_package_app",
        return_value=response,
    ) as mock_export:
        result = runner.invoke(
            export_registration_package,
            ["--output", str(output), "--json"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "success": True,
        "package_path": str(output),
        "package_sha256": "a" * 64,
        "job_count": 3,
        "artifact_count": 12,
        "link_count": 18,
    }
    request = mock_export.call_args.args[0]
    assert request.output == output
    assert request.roar_dir == tmp_path / ".roar"
    assert request.cwd == tmp_path


def test_export_registration_package_cli_emits_json_failure(tmp_path: Path) -> None:
    runner = CliRunner()
    response = ExportRegistrationPackageResponse(
        success=False,
        error="No active session. Run 'roar run' to create a session first.",
    )

    with patch(
        "roar.cli.commands.export_registration_package.export_registration_package_app",
        return_value=response,
    ):
        result = runner.invoke(
            export_registration_package,
            ["--output", str(tmp_path / "lineage.roarreg"), "--json"],
            obj=_mock_context(tmp_path),
        )

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "success": False,
        "error": "No active session. Run 'roar run' to create a session first.",
    }
