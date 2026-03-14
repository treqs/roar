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
    @patch("roar.execution.provenance.runtime_collector.subprocess.run")
    def test_hardware_fingerprint_uses_ioreg(self, mock_run, mock_sys):
        from roar.execution.provenance.runtime_collector import RuntimeCollectorService

        mock_sys.platform = "darwin"
        ioreg_output = '  | {\n  |   "IOPlatformUUID" = "ABCD-1234-EFGH-5678"\n  | }\n'
        result_obj = MagicMock()
        result_obj.returncode = 0
        result_obj.stdout = ioreg_output
        mock_run.return_value = result_obj

        fp = RuntimeCollectorService._hardware_fingerprint()
        assert fp  # returns a hex digest
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "ioreg"

    @patch("roar.execution.provenance.runtime_collector.sys")
    def test_hardware_fingerprint_linux_path_not_called_on_darwin(self, mock_sys):
        """On darwin, /etc/machine-id and /sys/bus/pci should not be read."""
        from roar.execution.provenance.runtime_collector import RuntimeCollectorService

        mock_sys.platform = "darwin"
        with patch(
            "roar.execution.provenance.runtime_collector.subprocess.run"
        ) as mock_run:
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
    def test_get_cpu_info_darwin(self, mock_sys, service):
        mock_sys.platform = "darwin"
        with patch.object(service, "_run_command") as mock_cmd:
            mock_cmd.side_effect = lambda args, **kw: {
                ("sysctl", "-n", "machdep.cpu.brand_string"): "Apple M2 Pro\n",
                ("sysctl", "-n", "hw.physicalcpu"): "10\n",
                ("sysctl", "-n", "hw.logicalcpu"): "12\n",
            }.get(tuple(args))

            result = service._get_cpu_info()

        assert result is not None
        assert result["model"] == "Apple M2 Pro"
        assert result["cores_per_socket"] == 10
        assert result["count"] == 12
        assert "architecture" in result

    @patch("roar.execution.provenance.runtime_collector.sys")
    def test_get_memory_info_darwin(self, mock_sys, service):
        mock_sys.platform = "darwin"

        vm_stat_output = (
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            "Pages free:                              100000.\n"
            "Pages active:                            200000.\n"
            "Pages inactive:                           50000.\n"
            "Pages speculative:                        10000.\n"
        )

        with patch.object(service, "_run_command") as mock_cmd:
            mock_cmd.side_effect = lambda args, **kw: {
                ("sysctl", "-n", "hw.memsize"): "34359738368\n",  # 32 GB
                ("vm_stat",): vm_stat_output,
            }.get(tuple(args))

            result = service._get_memory_info()

        assert result is not None
        assert result["total_mb"] == 32768  # 32 GB
        assert "available_mb" in result
        # (100000 + 50000) * 16384 / 1024 / 1024 = 2343 MB (approx)
        assert result["available_mb"] > 0

    @patch("roar.execution.provenance.runtime_collector.sys")
    def test_get_gpu_info_darwin_system_profiler(self, mock_sys, service):
        mock_sys.platform = "darwin"

        profiler_output = (
            "Graphics/Displays:\n"
            "\n"
            "    Apple M2 Pro:\n"
            "\n"
            "      Chipset Model: Apple M2 Pro\n"
            "      Type: GPU\n"
            "      Bus: Built-In\n"
            "      Total Number of Cores: 19\n"
            "      Vendor: Apple (0x106b)\n"
            "      Metal Support: Metal 3\n"
        )

        with patch.object(service, "_run_command") as mock_cmd:

            def side_effect(args, **kw):
                if args[0] == "nvidia-smi":
                    return None
                if args[0] == "system_profiler":
                    return profiler_output
                return None

            mock_cmd.side_effect = side_effect

            result = service._get_gpu_info()

        assert result is not None
        assert len(result) == 1
        assert result[0]["name"] == "Apple M2 Pro"

    @patch("roar.execution.provenance.runtime_collector.sys")
    def test_get_gpu_info_darwin_with_vram(self, mock_sys, service):
        mock_sys.platform = "darwin"

        profiler_output = (
            "Graphics/Displays:\n"
            "\n"
            "    AMD Radeon Pro 5500M:\n"
            "\n"
            "      Chipset Model: AMD Radeon Pro 5500M\n"
            "      VRAM (Total): 4 GB\n"
        )

        with patch.object(service, "_run_command") as mock_cmd:
            mock_cmd.side_effect = lambda args, **kw: {
                ("system_profiler", "SPDisplaysDataType"): profiler_output,
            }.get(tuple(args))

            result = service._get_gpu_info()

        assert result is not None
        assert result[0]["name"] == "AMD Radeon Pro 5500M"
        assert result[0]["memory_mb"] == 4096

    @patch("roar.execution.provenance.runtime_collector.sys")
    def test_get_vm_info_darwin_in_vm(self, mock_sys, service):
        mock_sys.platform = "darwin"
        with patch.object(service, "_run_command") as mock_cmd:
            mock_cmd.return_value = "1\n"

            result = service._get_vm_info()

        assert result is not None
        assert result["hypervisor"] == "unknown"

    @patch("roar.execution.provenance.runtime_collector.sys")
    def test_get_vm_info_darwin_not_in_vm(self, mock_sys, service):
        mock_sys.platform = "darwin"
        with patch.object(service, "_run_command") as mock_cmd:
            mock_cmd.return_value = "0\n"

            result = service._get_vm_info()

        assert result is None

    @patch("roar.execution.provenance.runtime_collector.sys")
    @patch.dict("os.environ", {}, clear=True)
    def test_get_container_info_darwin_skips_proc(self, mock_sys, service):
        mock_sys.platform = "darwin"

        result = service._get_container_info()

        # No /proc/self/cgroup on macOS, no env vars set → None
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

    @patch("roar.execution.provenance.package_collector.sys")
    def test_collect_dpkg_packages_runs_on_linux(self, mock_sys):
        """Ensure the guard only activates on non-Linux."""
        mock_sys.platform = "linux"

        from roar.execution.provenance.package_collector import PackageCollectorService

        svc = PackageCollectorService()
        svc._logger = MagicMock()

        # Should not early-return; will attempt dpkg resolution (which will
        # find nothing since we don't mock subprocess)
        with patch.object(svc, "_get_shared_libs_info", return_value=[]):
            result = svc._collect_dpkg_packages(
                shared_libs=[],
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

        with patch(
            "roar.execution.provenance.build_tool_collector.subprocess.run"
        ) as mock_run:
            service.collect(processes, sys_prefix="/some/venv")
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 4. filters — macOS system paths classified as noise
# ---------------------------------------------------------------------------


class TestFiltersNoisePrefixes:
    def test_macos_system_path_is_noise_read(self):
        from roar.filters import is_noise_read

        assert is_noise_read(
            "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"
        )
        assert is_noise_read("/Library/Developer/CommandLineTools/usr/lib/libclang.dylib")
        assert is_noise_read("/Applications/Xcode.app/Contents/MacOS/Xcode")
        assert is_noise_read("/private/var/db/dyld/dyld_shared_cache_arm64e")
        assert is_noise_read("/private/var/run/something")
        assert is_noise_read("/private/var/log/system.log")

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

    def test_linux_paths_still_work(self):
        from roar.filters import is_noise_read, is_noise_write

        assert is_noise_read("/usr/lib/libm.so.6")
        assert is_noise_read("/proc/self/maps")
        assert is_noise_write("/dev/null")
        assert is_noise_write("/tmp/foo.txt")

    def test_user_files_not_noise(self):
        from roar.filters import is_noise_read, is_noise_write

        assert not is_noise_read("/home/user/project/data.csv")
        assert not is_noise_write("/home/user/project/output.json")


# ---------------------------------------------------------------------------
# 5. filters/files.py — .dylib and macOS system dirs
# ---------------------------------------------------------------------------


class TestFileClassifierMacOS:
    def test_dylib_in_system_lib_is_system_shared_lib(self):
        from roar.filters.files import FileClassifier

        fc = FileClassifier.__new__(FileClassifier)
        assert fc._is_system_shared_lib("/usr/lib/libSystem.B.dylib")
        assert fc._is_system_shared_lib(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation.dylib"
        )

    def test_dylib_is_system_file(self):
        from roar.filters.files import FileClassifier

        fc = FileClassifier.__new__(FileClassifier)
        assert fc._is_system_file(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        assert fc._is_system_file(
            "/Library/Frameworks/Python.framework/Versions/3.12/lib/libpython3.12.dylib"
        )

    def test_so_still_detected(self):
        from roar.filters.files import FileClassifier

        fc = FileClassifier.__new__(FileClassifier)
        assert fc._is_system_shared_lib("/usr/lib/libm.so.6")
        assert fc._is_system_file("/usr/lib/x86_64-linux-gnu/libstdc++.so.6")


# ---------------------------------------------------------------------------
# 6. file_filter.py — macOS prefixes
# ---------------------------------------------------------------------------


class TestFileFilterServiceMacOS:
    def test_system_read_prefixes_include_macos(self):
        from roar.execution.provenance.file_filter import FileFilterService

        prefixes = FileFilterService.SYSTEM_READ_PREFIXES
        assert "/System/" in prefixes
        assert "/Library/" in prefixes
        assert "/Applications/" in prefixes
        assert "/private/var/db/" in prefixes
        assert "/private/var/run/" in prefixes
        assert "/private/var/log/" in prefixes

    def test_write_noise_prefixes_include_macos(self):
        from roar.execution.provenance.file_filter import FileFilterService

        prefixes = FileFilterService.WRITE_NOISE_PREFIXES
        assert "/System/" in prefixes
        assert "/Library/Caches/" in prefixes
        assert "/private/var/db/" in prefixes
        assert "/private/var/run/" in prefixes
        assert "/private/var/log/" in prefixes

    def test_is_system_read_macos_path(self):
        from roar.execution.provenance.file_filter import FileFilterService

        svc = FileFilterService()
        assert svc._is_system_read("/System/Library/Frameworks/Security.framework/Security")
        assert svc._is_system_read("/Library/Developer/CommandLineTools/usr/lib/libclang.dylib")
        assert svc._is_system_read("/Applications/Xcode.app/Contents/MacOS/Xcode")

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

    def test_is_tmp_path_linux(self):
        from roar.execution.provenance.file_filter import FileFilterService

        assert FileFilterService._is_tmp_path("/tmp/foo.txt")
        assert FileFilterService._is_tmp_path("/tmp/subdir/bar")
        assert not FileFilterService._is_tmp_path("/home/user/tmp/foo")

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

    def test_dylib_is_code_file(self, service):
        assert service._is_code_file("/usr/lib/libfoo.dylib")
        assert service._is_code_file(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation.dylib"
        )

    def test_dylib_in_code_extensions(self):
        from roar.execution.provenance.assembler import ProvenanceAssemblerService

        assert ".dylib" in ProvenanceAssemblerService.CODE_EXTENSIONS

    def test_so_still_code_file(self, service):
        assert service._is_code_file("/usr/lib/libm.so.6")
        assert service._is_code_file("/usr/lib/libm.so")

    def test_macos_system_lib_is_read_noise(self, service):
        assert service._is_read_noise(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        assert service._is_read_noise("/Library/Frameworks/Python.framework/lib/libpython3.dylib")

    def test_linux_system_lib_still_read_noise(self, service):
        assert service._is_read_noise("/usr/lib/libm.so.6")
        assert service._is_read_noise("/lib64/ld-linux-x86-64.so.2")

    def test_user_file_not_read_noise(self, service):
        assert not service._is_read_noise("/home/user/data.csv")
        assert not service._is_read_noise("/Users/user/project/model.pt")
