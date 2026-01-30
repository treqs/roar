"""
Unit tests for the 'roar reproduce' CLI command.

Tests the CLI behavior with mocked dependencies:
- Default shows preview with copy-paste command
- --run flag triggers full reproduction
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from roar.cli.commands.reproduce import reproduce


class TestReproduceCLI:
    """Test reproduce CLI command behavior."""

    @pytest.fixture
    def runner(self):
        """Create a Click CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def mock_glaas_client(self):
        """Create a mock GLaaS client."""
        client = MagicMock()
        client.is_configured.return_value = True
        return client

    @pytest.fixture
    def mock_pipeline_info(self):
        """Create a mock PipelineInfo for preview."""
        pipeline = MagicMock()
        pipeline.artifact_hash = "abc123def456789"
        pipeline.git_repo = "https://github.com/test/repo"
        pipeline.git_commit = "abc123def456"
        pipeline.build_steps = [{"type": "build", "command": "pip install -r requirements.txt"}]
        pipeline.run_steps = [{"type": "run", "command": "python train.py"}]
        return pipeline

    def test_reproduce_default_shows_preview_with_copypaste_command(
        self, runner, mock_glaas_client, mock_pipeline_info
    ):
        """Bare 'roar reproduce <hash>' shows preview and suggests --run command."""
        hash_prefix = "abc123def456"

        with (
            patch("roar.cli.commands.reproduce.load_config") as mock_config,
            patch("roar.glaas_client.GlaasClient") as mock_glaas_cls,
            patch("roar.cli.commands.reproduce.ReproductionService") as mock_service_cls,
            patch("roar.services.reproduction.PipelineExecutor") as mock_executor_cls,
        ):
            mock_config.return_value = {"glaas": {"url": "http://localhost:3001"}}
            mock_glaas_cls.return_value = mock_glaas_client

            mock_service = MagicMock()
            mock_service._lookup_pipeline.return_value = (mock_pipeline_info, None)
            mock_service_cls.return_value = mock_service

            mock_executor = MagicMock()
            mock_executor_cls.return_value = mock_executor

            ctx = MagicMock()
            ctx.roar_dir = Path("/tmp/.roar")
            ctx.cwd = Path("/tmp")

            result = runner.invoke(
                reproduce,
                [hash_prefix],
                obj=ctx,
            )

        # Command should succeed
        assert result.exit_code == 0, f"Exit code was {result.exit_code}: {result.output}"

        # Should show artifact info
        assert "Artifact:" in result.output or "abc123" in result.output

        # Should show git repo and commit
        assert "git" in result.output.lower() or "repo" in result.output.lower()

        # Should show copy-paste command for --run
        assert "--run" in result.output
        assert "roar reproduce" in result.output

        # Should NOT call service.reproduce (no actual setup)
        mock_service.reproduce.assert_not_called()

    def test_reproduce_run_does_full_reproduction(self, runner, mock_glaas_client):
        """'roar reproduce <hash> --run' clones, sets up venv, and runs pipeline."""
        hash_prefix = "abc123def456"

        with (
            patch("roar.cli.commands.reproduce.load_config") as mock_config,
            patch("roar.glaas_client.GlaasClient") as mock_glaas_cls,
            patch("roar.cli.commands.reproduce.ReproductionService") as mock_service_cls,
        ):
            mock_config.return_value = {"glaas": {"url": "http://localhost:3001"}}
            mock_glaas_cls.return_value = mock_glaas_client

            mock_service = MagicMock()

            # Mock successful reproduction result
            from roar.core.interfaces.reproduction import ReproductionResult

            mock_result = ReproductionResult(
                success=True,
                repo_dir=Path("/tmp/reproduce/test-repo"),
                steps_run=2,
                steps_total=2,
                warnings=[],
            )
            mock_service.reproduce.return_value = mock_result
            mock_service_cls.return_value = mock_service

            ctx = MagicMock()
            ctx.roar_dir = Path("/tmp/.roar")
            ctx.cwd = Path("/tmp")

            result = runner.invoke(
                reproduce,
                [hash_prefix, "--run"],
                obj=ctx,
            )

        # Command should succeed
        assert result.exit_code == 0, f"Exit code was {result.exit_code}: {result.output}"

        # Should call service.reproduce with run_pipeline=True
        mock_service.reproduce.assert_called_once()
        call_kwargs = mock_service.reproduce.call_args.kwargs
        assert call_kwargs["run_pipeline"] is True

        # Should show completion message
        assert "Reproduction Complete" in result.output

    def test_reproduce_run_with_autoconfirm(self, runner, mock_glaas_client):
        """'roar reproduce <hash> --run -y' runs with auto-confirmation."""
        hash_prefix = "abc123def456"

        with (
            patch("roar.cli.commands.reproduce.load_config") as mock_config,
            patch("roar.glaas_client.GlaasClient") as mock_glaas_cls,
            patch("roar.cli.commands.reproduce.ReproductionService") as mock_service_cls,
        ):
            mock_config.return_value = {"glaas": {"url": "http://localhost:3001"}}
            mock_glaas_cls.return_value = mock_glaas_client

            mock_service = MagicMock()

            from roar.core.interfaces.reproduction import ReproductionResult

            mock_result = ReproductionResult(
                success=True,
                repo_dir=Path("/tmp/reproduce/test-repo"),
                steps_run=2,
                steps_total=2,
                warnings=[],
            )
            mock_service.reproduce.return_value = mock_result
            mock_service_cls.return_value = mock_service

            ctx = MagicMock()
            ctx.roar_dir = Path("/tmp/.roar")
            ctx.cwd = Path("/tmp")

            result = runner.invoke(
                reproduce,
                [hash_prefix, "--run", "-y"],
                obj=ctx,
            )

        # Command should succeed
        assert result.exit_code == 0

        # Should call service.reproduce with auto_confirm=True
        mock_service.reproduce.assert_called_once()
        call_kwargs = mock_service.reproduce.call_args.kwargs
        assert call_kwargs["auto_confirm"] is True

    def test_reproduce_preview_flag_not_recognized(self, runner):
        """The --preview flag should not be recognized (removed from CLI)."""
        hash_prefix = "abc123def456"

        ctx = MagicMock()
        ctx.roar_dir = Path("/tmp/.roar")
        ctx.cwd = Path("/tmp")

        result = runner.invoke(
            reproduce,
            [hash_prefix, "--preview"],
            obj=ctx,
        )

        # Should fail with unknown option error
        assert result.exit_code != 0
        assert "preview" in result.output.lower() or "no such option" in result.output.lower()

    def test_reproduce_default_does_not_create_directories(
        self, runner, mock_glaas_client, mock_pipeline_info, tmp_path
    ):
        """Default preview mode should not create any directories."""
        hash_prefix = "abc123def456"
        reproduce_dir = tmp_path / "reproduce"

        with (
            patch("roar.cli.commands.reproduce.load_config") as mock_config,
            patch("roar.glaas_client.GlaasClient") as mock_glaas_cls,
            patch("roar.cli.commands.reproduce.ReproductionService") as mock_service_cls,
            patch("roar.services.reproduction.PipelineExecutor") as mock_executor_cls,
        ):
            mock_config.return_value = {"glaas": {"url": "http://localhost:3001"}}
            mock_glaas_cls.return_value = mock_glaas_client

            mock_service = MagicMock()
            mock_service._lookup_pipeline.return_value = (mock_pipeline_info, None)
            mock_service_cls.return_value = mock_service

            mock_executor = MagicMock()
            mock_executor_cls.return_value = mock_executor

            ctx = MagicMock()
            ctx.roar_dir = tmp_path / ".roar"
            ctx.cwd = tmp_path

            result = runner.invoke(
                reproduce,
                [hash_prefix],
                obj=ctx,
            )

        assert result.exit_code == 0

        # Verify no reproduce directory was created
        assert not reproduce_dir.exists(), "Preview mode should not create directories"

        # Service.reproduce should NOT be called
        mock_service.reproduce.assert_not_called()

    def test_reproduce_shows_disclaimer_about_run_actions(
        self, runner, mock_glaas_client, mock_pipeline_info
    ):
        """Preview should show disclaimer about what --run will do."""
        hash_prefix = "abc123def456"

        with (
            patch("roar.cli.commands.reproduce.load_config") as mock_config,
            patch("roar.glaas_client.GlaasClient") as mock_glaas_cls,
            patch("roar.cli.commands.reproduce.ReproductionService") as mock_service_cls,
            patch("roar.services.reproduction.PipelineExecutor") as mock_executor_cls,
        ):
            mock_config.return_value = {"glaas": {"url": "http://localhost:3001"}}
            mock_glaas_cls.return_value = mock_glaas_client

            mock_service = MagicMock()
            mock_service._lookup_pipeline.return_value = (mock_pipeline_info, None)
            mock_service_cls.return_value = mock_service

            mock_executor = MagicMock()
            mock_executor_cls.return_value = mock_executor

            ctx = MagicMock()
            ctx.roar_dir = Path("/tmp/.roar")
            ctx.cwd = Path("/tmp")

            result = runner.invoke(
                reproduce,
                [hash_prefix],
                obj=ctx,
            )

        assert result.exit_code == 0

        # Should explain what --run will do (clone, venv, install, run)
        output_lower = result.output.lower()
        assert any(
            keyword in output_lower for keyword in ["clone", "venv", "install", "reproduce"]
        ), f"Output should describe what --run does. Got: {result.output}"
