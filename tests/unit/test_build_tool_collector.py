"""
Unit tests for BuildToolCollectorService.
"""

from unittest.mock import MagicMock, patch

import pytest

from roar.services.execution.provenance.build_tool_collector import (
    BuildToolCollectorService,
    KNOWN_BUILD_TOOLS,
)


@pytest.fixture
def service():
    svc = BuildToolCollectorService()
    svc._logger = MagicMock()
    return svc


class TestBuildToolCollector:
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

    @patch("roar.services.execution.provenance.build_tool_collector.subprocess.run")
    @patch("roar.services.execution.provenance.build_tool_collector.shutil.which")
    def test_cmake_gcc_detected(self, mock_which, mock_run, service):
        processes = [
            {"command": ["/usr/bin/cmake", "..", "-DCMAKE_BUILD_TYPE=Release"]},
            {"command": ["/usr/bin/gcc", "-o", "foo.o", "-c", "foo.c"]},
            {"command": ["/usr/bin/python", "setup.py", "build"]},
        ]

        mock_which.side_effect = lambda t: f"/usr/bin/{t}" if t in ("cmake", "gcc") else None

        # dpkg -S response
        dpkg_s_result = MagicMock()
        dpkg_s_result.stdout = "cmake: /usr/bin/cmake\ngcc-12: /usr/bin/gcc\n"
        dpkg_s_result.returncode = 0

        # dpkg-query response
        dpkg_query_result = MagicMock()
        dpkg_query_result.stdout = "cmake\t3.25.1-1\ngcc-12\t12.2.0-14\n"
        dpkg_query_result.returncode = 0

        mock_run.side_effect = [dpkg_s_result, dpkg_query_result]

        result = service.collect(processes, sys_prefix="/some/venv")
        assert result == {"cmake": "3.25.1-1", "gcc-12": "12.2.0-14"}

    @patch("roar.services.execution.provenance.build_tool_collector.shutil.which")
    def test_pip_installed_tool_excluded(self, mock_which, service):
        """Tools under sys_prefix should be excluded."""
        processes = [
            {"command": ["/venv/bin/cmake", ".."]},
        ]
        mock_which.return_value = "/venv/bin/cmake"

        result = service.collect(processes, sys_prefix="/venv")
        assert result == {}

    @patch("roar.services.execution.provenance.build_tool_collector.shutil.which")
    def test_site_packages_tool_excluded(self, mock_which, service):
        """Tools in site-packages should be excluded."""
        processes = [
            {"command": ["cmake", ".."]},
        ]
        mock_which.return_value = "/venv/lib/python3.12/site-packages/cmake/bin/cmake"

        result = service.collect(processes, sys_prefix="/other")
        assert result == {}

    @patch("roar.services.execution.provenance.build_tool_collector.shutil.which")
    def test_unknown_tool_on_path_skipped(self, mock_which, service):
        """Tool not found on PATH is skipped gracefully."""
        processes = [
            {"command": ["cmake", ".."]},
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

    def test_known_build_tools_set(self):
        """Verify key tools are in the known set."""
        assert "cmake" in KNOWN_BUILD_TOOLS
        assert "gcc" in KNOWN_BUILD_TOOLS
        assert "ninja" in KNOWN_BUILD_TOOLS
        assert "rustc" in KNOWN_BUILD_TOOLS
        assert "python" not in KNOWN_BUILD_TOOLS
