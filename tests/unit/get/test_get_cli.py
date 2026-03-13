"""
Unit tests for the get CLI command.

Tests Click command parsing, option handling, and backend selection.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from roar.cli.commands.get import get
from roar.integrations.download import resolve_download_backend


@pytest.fixture
def runner():
    """Create a Click test runner."""
    return CliRunner()


@pytest.fixture
def mock_roar_context(tmp_path: Path):
    """Create a mock RoarContext."""
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    ctx = MagicMock()
    ctx.roar_dir = roar_dir
    ctx.repo_root = tmp_path
    ctx.cwd = tmp_path
    ctx.is_initialized = True
    return ctx


class TestGetCLIParsing:
    """Tests for CLI argument/option parsing."""

    def test_source_required(self, runner, mock_roar_context):
        """Source argument is required."""
        result = runner.invoke(get, [], obj=mock_roar_context)
        assert result.exit_code != 0
        assert "Missing argument" in result.output or "Error" in result.output

    def test_unsupported_scheme_error(self, runner, mock_roar_context):
        """Unsupported scheme shows error."""
        import importlib

        get_module = importlib.import_module("roar.cli.commands.get")

        with patch.object(get_module, "bootstrap"):
            result = runner.invoke(
                get,
                ["ftp://server/file.pt"],
                obj=mock_roar_context,
            )

            assert result.exit_code != 0
            assert "Unsupported" in result.output


class TestGetBackendSelection:
    """Tests for backend selection logic."""

    def test_http_uses_http_backend(self):
        """HTTP URLs use HTTPBackend."""
        with patch("roar.integrations.download.get.should_skip_download", return_value=False):
            backend = resolve_download_backend("https://example.com/model.pt")
            from roar.integrations.download.http import HTTPBackend

            assert isinstance(backend, HTTPBackend)

    def test_skip_download_uses_noop(self):
        """ROAR_GET_SKIP_DOWNLOAD=1 uses NoOpDownloadBackend."""
        with patch("roar.integrations.download.get.should_skip_download", return_value=True):
            backend = resolve_download_backend("s3://bucket/model.pt")
            from roar.integrations.download.noop import NoOpDownloadBackend

            assert isinstance(backend, NoOpDownloadBackend)

    def test_unsupported_scheme_raises_valueerror(self):
        """Unsupported scheme in parse_source raises ValueError (CLI catches it)."""
        with (
            patch("roar.integrations.download.get.should_skip_download", return_value=False),
            pytest.raises(ValueError, match="Unsupported"),
        ):
            resolve_download_backend("wandb://project/artifact")
