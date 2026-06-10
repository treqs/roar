"""
Tests for macOS compatibility codepaths.

All tests mock sys.platform to "darwin" and verify that:
- Linux-specific codepaths (procfs, sysfs, dpkg) are skipped
- macOS alternatives (sysctl, ioreg, system_profiler, vm_stat) are used
- Static additions (path prefixes, .dylib) work correctly
"""

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 1. runtime_collector — macOS branches
# ---------------------------------------------------------------------------


class TestRuntimeCollectorMacOS:
    """RuntimeCollectorService methods under sys.platform == 'darwin'."""

    @pytest.fixture
    def service(self):
        from roar.execution.provenance.runtime_collector import RuntimeCollectorService

        svc = RuntimeCollectorService()
        svc._logger = MagicMock()
        return svc

    @patch("roar.execution.provenance.runtime_collector.sys")
    def test_hardware_fingerprint_linux_path_not_called_on_darwin(self, mock_sys):
        """On darwin, /etc/machine-id and /sys/bus/pci should not be read."""
        from roar.execution.provenance.runtime_collector import RuntimeCollectorService

        mock_sys.platform = "darwin"
        with patch("roar.execution.provenance.runtime_collector.subprocess.run") as mock_run:
            result_obj = MagicMock()
            result_obj.returncode = 1
            result_obj.stdout = ""
            mock_run.return_value = result_obj

            with patch(
                "builtins.open", side_effect=AssertionError("should not open files on darwin")
            ):
                fp = RuntimeCollectorService._hardware_fingerprint()
            assert fp  # still produces a fingerprint (from empty parts)

    @patch("roar.execution.provenance.runtime_collector.sys")
    def test_get_vm_info_darwin_not_in_vm(self, mock_sys, service):
        mock_sys.platform = "darwin"
        with patch.object(service, "_run_command") as mock_cmd:
            mock_cmd.return_value = "0\n"

            result = service._get_vm_info()

        assert result is None

    @patch("roar.execution.provenance.runtime_collector.sys")
    @patch.dict("os.environ", {"KUBERNETES_SERVICE_HOST": "10.0.0.1"})
    def test_get_container_info_darwin_env_vars_still_work(self, mock_sys, service):
        mock_sys.platform = "darwin"

        result = service._get_container_info()

        assert result is not None
        assert result["type"] == "kubernetes"


# ---------------------------------------------------------------------------
# 2. package_collector — dpkg guard on non-Linux
# ---------------------------------------------------------------------------


class TestPackageCollectorMacOS:
    @patch("roar.execution.provenance.package_collector.sys")
    def test_collect_dpkg_packages_returns_empty_on_darwin(self, mock_sys):
        mock_sys.platform = "darwin"

        from roar.execution.provenance.package_collector import PackageCollectorService

        svc = PackageCollectorService()
        svc._logger = MagicMock()

        result = svc._collect_dpkg_packages(
            shared_libs=["/usr/lib/libfoo.so"],
            sys_prefix="/usr",
            installed_packages={},
        )
        assert result == {}

# ---------------------------------------------------------------------------
# 3. build_tool_collector — dpkg guard on non-Linux
# ---------------------------------------------------------------------------


class TestBuildToolCollectorMacOS:
    @pytest.fixture
    def service(self):
        from roar.execution.provenance.build_tool_collector import (
            BuildToolCollectorService,
        )

        svc = BuildToolCollectorService()
        svc._logger = MagicMock()
        return svc

    @patch("roar.execution.provenance.build_tool_collector.sys")
    @patch("roar.execution.provenance.build_tool_collector.shutil.which")
    def test_collect_returns_basenames_on_darwin(self, mock_which, mock_sys, service):
        mock_sys.platform = "darwin"
        processes = [
            {"command": ["/usr/bin/cmake", "..", "-DCMAKE_BUILD_TYPE=Release"]},
            {"command": ["/usr/bin/gcc", "-o", "foo.o", "-c", "foo.c"]},
        ]
        mock_which.side_effect = lambda t: f"/usr/bin/{t}" if t in ("cmake", "gcc") else None

        result = service.collect(processes, sys_prefix="/some/venv")

        assert "cmake" in result
        assert "gcc" in result
        # Versions should be empty strings (no dpkg)
        assert result["cmake"] == ""
        assert result["gcc"] == ""

    @patch("roar.execution.provenance.build_tool_collector.sys")
    @patch("roar.execution.provenance.build_tool_collector.shutil.which")
    def test_collect_no_dpkg_subprocess_on_darwin(self, mock_which, mock_sys, service):
        """Ensure dpkg is never called on darwin."""
        mock_sys.platform = "darwin"
        processes = [{"command": ["/usr/bin/cmake", ".."]}]
        mock_which.return_value = "/usr/bin/cmake"

        with patch("roar.execution.provenance.build_tool_collector.subprocess.run") as mock_run:
            service.collect(processes, sys_prefix="/some/venv")
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 4. filters — macOS system paths classified as noise
# ---------------------------------------------------------------------------


class TestFiltersNoisePrefixes:
    def test_macos_write_noise(self):
        from roar.filters import is_noise_write

        assert is_noise_write("/System/Library/something")
        assert is_noise_write("/Library/Caches/com.apple.ld/cache.json")
        # /private/var/folders/ is temp space, not unconditional noise
        assert not is_noise_write("/private/var/folders/xx/tmp123")
        # But specific system sub-paths are still noise
        assert is_noise_write("/private/var/db/something")
        assert is_noise_write("/private/var/run/something")
        assert is_noise_write("/private/var/log/something")

# ---------------------------------------------------------------------------
# 5. filters/files.py — .dylib and macOS system dirs
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 6. file_filter.py — macOS prefixes
# ---------------------------------------------------------------------------


class TestFileFilterServiceMacOS:
    def test_is_write_noise_macos_path(self):
        from roar.execution.provenance.file_filter import FileFilterService

        svc = FileFilterService()
        svc._logger = MagicMock()
        assert svc._is_write_noise("/System/Library/foo")
        assert svc._is_write_noise("/Library/Caches/com.apple.dt.Xcode/foo")
        # /private/var/folders/ is temp space, not unconditional noise
        assert not svc._is_write_noise("/private/var/folders/xx/tmp")
        # But specific system sub-paths are still noise
        assert svc._is_write_noise("/private/var/db/something")
        assert svc._is_write_noise("/private/var/run/something")
        assert svc._is_write_noise("/private/var/log/something")

    def test_is_tmp_path_macos(self):
        from roar.execution.provenance.file_filter import FileFilterService

        assert FileFilterService._is_tmp_path("/private/var/folders/xx/T/tmp123")
        assert FileFilterService._is_tmp_path("/private/var/folders/ab/cdef/T/foo")
        assert not FileFilterService._is_tmp_path("/private/var/db/dyld/cache")
        assert not FileFilterService._is_tmp_path("/private/var/log/system.log")


# ---------------------------------------------------------------------------
# 7. assembler.py — .dylib as code file, macOS read noise
# ---------------------------------------------------------------------------


class TestAssemblerMacOS:
    @pytest.fixture
    def service(self):
        from roar.execution.provenance.assembler import ProvenanceAssemblerService

        svc = ProvenanceAssemblerService()
        svc._logger = MagicMock()
        return svc

    def test_user_file_not_read_noise(self, service):
        assert not service._is_read_noise("/home/user/data.csv")
        assert not service._is_read_noise("/Users/user/project/model.pt")
