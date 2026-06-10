"""
Unit tests for PipelineExecutor.

Tests the roar executable handling for command wrapping.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from roar.execution.reproduction.pipeline_executor import PipelineExecutor


class TestPipelineExecutorRoarExecutable:
    """Test roar executable handling in PipelineExecutor."""

    @pytest.fixture
    def mock_environment(self):
        """Create a mock EnvironmentInfo."""
        env = MagicMock()
        env.venv_dir = Path("/tmp/test-repo/.venv")
        env.repo_dir = Path("/tmp/test-repo")
        return env

    def test_wrap_with_roar_uses_external_executable(self, mock_environment):
        """_wrap_with_roar should use external roar path, not python -m roar."""
        roar_exe = "/home/user/.venv/bin/roar"
        executor = PipelineExecutor(roar_executable=roar_exe)

        wrapped = executor._wrap_with_roar(
            "pip install -r requirements.txt",
            "build",
            mock_environment,
        )

        # Should use the external roar executable
        assert roar_exe in wrapped
        # Should NOT use python -m roar pattern
        assert "-m roar" not in wrapped

    def test_wrapped_command_format(self, mock_environment):
        """Wrapped command should have format: {roar_exe} {roar_cmd} {original_command}."""
        roar_exe = "/home/user/.venv/bin/roar"
        executor = PipelineExecutor(roar_executable=roar_exe)

        # Test build command
        wrapped = executor._wrap_with_roar(
            "pip install -r requirements.txt",
            "build",
            mock_environment,
        )
        assert wrapped == f"{roar_exe} build pip install -r requirements.txt"

        # Test run command
        wrapped = executor._wrap_with_roar(
            "python script.py",
            "run",
            mock_environment,
        )
        assert wrapped == f"{roar_exe} run python script.py"

class TestPipelineExecutorDetectRoarExecutable:
    """Test roar executable auto-detection."""

    def test_detect_roar_executable_prefers_which_roar(self):
        """If 'roar' is on PATH, _detect_roar_executable should use it."""
        from unittest.mock import patch

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/local/bin/roar"

            executor = PipelineExecutor()

            # The executor should have used the which result
            assert executor._roar_executable == "/usr/local/bin/roar"
            mock_which.assert_called_with("roar")

    def test_detect_roar_executable_fallback_to_python_module(self):
        """If 'roar' is not on PATH, fallback to sys.executable -m roar."""
        import sys
        from unittest.mock import patch

        with patch("shutil.which") as mock_which:
            mock_which.return_value = None

            executor = PipelineExecutor()

            # Should fall back to python -m roar
            expected = f"{sys.executable} -m roar"
            assert executor._roar_executable == expected
