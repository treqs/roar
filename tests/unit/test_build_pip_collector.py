"""
Unit tests for BuildPipCollectorService.
"""

from unittest.mock import MagicMock, patch

import pytest

from roar.services.execution.provenance.build_pip_collector import (
    KNOWN_PYTHON_BUILD_TOOLS,
    BuildPipCollectorService,
)


@pytest.fixture
def service():
    svc = BuildPipCollectorService()
    svc._logger = MagicMock()
    return svc


class TestBuildPipCollector:
    def test_empty_process_tree(self, service):
        result = service.collect([], sys_prefix="/some/venv")
        assert result == {}

    def test_no_build_tools_in_processes(self, service):
        processes = [
            {"command": ["/usr/bin/python", "train.py"]},
            {"command": ["/usr/bin/bash", "-c", "echo hello"]},
        ]
        result = service.collect(processes, sys_prefix="/some/venv")
        assert result == {}

    @patch(
        "roar.services.execution.provenance.build_pip_collector.BuildPipCollectorService._get_package_version"
    )
    @patch("roar.services.execution.provenance.build_pip_collector.shutil.which")
    def test_uv_detected(self, mock_which, mock_version, service):
        processes = [
            {"command": ["/some/venv/bin/uv", "pip", "install", "numpy"]},
            {"command": ["/usr/bin/python", "train.py"]},
        ]

        mock_which.return_value = "/some/venv/bin/uv"
        mock_version.return_value = "0.1.40"

        result = service.collect(processes, sys_prefix="/some/venv")
        assert result == {"uv": "0.1.40"}

    @patch("roar.services.execution.provenance.build_pip_collector.shutil.which")
    def test_system_tool_excluded(self, mock_which, service):
        """Tools NOT under sys_prefix and NOT in site-packages should be excluded."""
        processes = [
            {"command": ["/usr/bin/pip", "install", "numpy"]},
        ]
        mock_which.return_value = "/usr/bin/pip"

        result = service.collect(processes, sys_prefix="/some/venv")
        assert result == {}

    @patch(
        "roar.services.execution.provenance.build_pip_collector.BuildPipCollectorService._get_package_version"
    )
    @patch("roar.services.execution.provenance.build_pip_collector.shutil.which")
    def test_pip_installed_tool_kept(self, mock_which, mock_version, service):
        """Tools under sys_prefix should be kept (inverse of build_dpkg)."""
        processes = [
            {"command": ["/venv/bin/maturin", "build"]},
        ]
        mock_which.return_value = "/venv/bin/maturin"
        mock_version.return_value = "1.4.0"

        result = service.collect(processes, sys_prefix="/venv")
        assert result == {"maturin": "1.4.0"}

    @patch(
        "roar.services.execution.provenance.build_pip_collector.BuildPipCollectorService._get_package_version"
    )
    @patch("roar.services.execution.provenance.build_pip_collector.shutil.which")
    def test_site_packages_tool_kept(self, mock_which, mock_version, service):
        """Tools in site-packages should be kept."""
        processes = [
            {"command": ["hatch", "build"]},
        ]
        mock_which.return_value = "/other/lib/python3.12/site-packages/hatch/cli.py"
        mock_version.return_value = "1.9.0"

        result = service.collect(processes, sys_prefix="/venv")
        assert result == {"hatch": "1.9.0"}

    @patch("roar.services.execution.provenance.build_pip_collector.shutil.which")
    def test_tool_not_on_path(self, mock_which, service):
        processes = [
            {"command": ["maturin", "build"]},
        ]
        mock_which.return_value = None

        result = service.collect(processes, sys_prefix="/venv")
        assert result == {}

    def test_process_with_empty_command(self, service):
        processes = [
            {"command": []},
            {},
        ]
        result = service.collect(processes, sys_prefix="/venv")
        assert result == {}

    @patch(
        "roar.services.execution.provenance.build_pip_collector.BuildPipCollectorService._get_package_version"
    )
    @patch("roar.services.execution.provenance.build_pip_collector.shutil.which")
    def test_pip3_normalized_to_pip(self, mock_which, mock_version, service):
        """pip3 should be normalized to pip package name."""
        processes = [
            {"command": ["/venv/bin/pip3", "install", "foo"]},
        ]
        mock_which.return_value = "/venv/bin/pip3"
        mock_version.return_value = "23.3.1"

        result = service.collect(processes, sys_prefix="/venv")
        assert result == {"pip": "23.3.1"}

    def test_known_python_build_tools_set(self):
        """Verify key tools are in the known set."""
        assert "uv" in KNOWN_PYTHON_BUILD_TOOLS
        assert "pip" in KNOWN_PYTHON_BUILD_TOOLS
        assert "maturin" in KNOWN_PYTHON_BUILD_TOOLS
        assert "poetry" in KNOWN_PYTHON_BUILD_TOOLS
        assert "python" not in KNOWN_PYTHON_BUILD_TOOLS
        assert "cmake" not in KNOWN_PYTHON_BUILD_TOOLS
