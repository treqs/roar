"""
Tests for RuntimeCollectorService._get_cpu_info on Linux.

Focus: the CPU model is resolved correctly on architectures where
/proc/cpuinfo does not expose a "model name" field (e.g. aarch64),
falling back to lscpu's "Model name:" field. See issue #165.
"""

from unittest.mock import MagicMock, mock_open, patch

import pytest

# Representative lscpu output (trimmed) on an aarch64 host. Note there is a
# "Model name:" line but /proc/cpuinfo carries no "model name" field.
LSCPU_AARCH64 = """\
Architecture:                            aarch64
CPU(s):                                  4
Vendor ID:                               ARM
Model name:                              Neoverse-N1
Thread(s) per core:                      1
Core(s) per socket:                      4
Socket(s):                               1
"""

CPUINFO_AARCH64 = """\
processor	: 0
BogoMIPS	: 243.75
CPU implementer	: 0x41
CPU part	: 0xd0c
"""

LSCPU_X86 = """\
Architecture:                            x86_64
CPU(s):                                  8
Model name:                              Intel(R) Xeon(R) Platinum 8259CL
Thread(s) per core:                      2
Core(s) per socket:                      4
Socket(s):                               1
"""

CPUINFO_X86 = """\
processor	: 0
model name	: Intel(R) Xeon(R) Platinum 8259CL CPU @ 2.50GHz
"""


class TestGetCpuInfoLinux:
    @pytest.fixture
    def service(self):
        from roar.execution.provenance.runtime_collector import RuntimeCollectorService

        svc = RuntimeCollectorService()
        svc._logger = MagicMock()
        return svc

    @patch("roar.execution.provenance.runtime_collector.sys")
    def test_aarch64_model_from_lscpu(self, mock_sys, service):
        """aarch64: no 'model name' in /proc/cpuinfo, so lscpu supplies it."""
        mock_sys.platform = "linux"
        with (
            patch("builtins.open", mock_open(read_data=CPUINFO_AARCH64)),
            patch.object(service, "_run_command", return_value=LSCPU_AARCH64),
        ):
            result = service._get_cpu_info()

        assert result is not None
        assert result["model"] == "Neoverse-N1"
        assert result["architecture"] == "aarch64"
        assert result["count"] == 4

    @patch("roar.execution.provenance.runtime_collector.sys")
    def test_x86_model_from_cpuinfo_not_overridden(self, mock_sys, service):
        """x86: /proc/cpuinfo 'model name' wins; lscpu does not override it."""
        mock_sys.platform = "linux"
        with (
            patch("builtins.open", mock_open(read_data=CPUINFO_X86)),
            patch.object(service, "_run_command", return_value=LSCPU_X86),
        ):
            result = service._get_cpu_info()

        assert result is not None
        # /proc/cpuinfo value (with clock speed) is preferred over lscpu's.
        assert result["model"] == "Intel(R) Xeon(R) Platinum 8259CL CPU @ 2.50GHz"
        assert result["architecture"] == "x86_64"


class TestRuntimeCacheSchemaVersion:
    """The hardware cache is invalidated generically when its schema version
    changes, so improvements to *any* collected field reach existing projects
    instead of serving stale values. See issue #165 (CPU model on aarch64)."""

    def _make_inputs(self):
        from roar.core.models.provenance import PythonInjectData, TracerData

        tracer = TracerData(
            opened_files=[],
            read_files=[],
            written_files=[],
            processes=[],
            start_time=0.0,
            end_time=1.0,
        )
        return PythonInjectData(modules_files=[]), tracer

    def _service(self, tmp_path):
        from roar.execution.provenance.runtime_collector import RuntimeCollectorService

        svc = RuntimeCollectorService(cache_dir=str(tmp_path))
        svc._logger = MagicMock()
        return svc

    def test_save_cache_records_schema_version(self, tmp_path):
        import json

        from roar.execution.provenance import runtime_collector as rc

        svc = self._service(tmp_path)
        svc._save_cache("fp", {"cpu": {"model": "x"}})

        written = json.loads((tmp_path / "runtime_cache.json").read_text())
        assert written["schema_version"] == rc._CACHE_SCHEMA_VERSION

    def test_stale_schema_version_forces_recollection(self, tmp_path):
        """A cache with the right fingerprint but an old/missing schema version
        is treated as a miss and re-collected — not served."""
        import json

        from roar.execution.provenance import runtime_collector as rc

        svc = self._service(tmp_path)
        python_data, tracer_data = self._make_inputs()

        # Pre-seed a stale cache: matching fingerprint, but no schema_version
        # and a CPU dict missing the model (the pre-fix shape).
        (tmp_path / "runtime_cache.json").write_text(
            json.dumps(
                {
                    "fingerprint": "FIXED",
                    "data": {"cpu": {"count": 4}, "gpu": None, "cuda": None, "vm": None},
                }
            )
        )

        fresh_cpu = {"count": 4, "model": "Neoverse-N1"}
        with (
            patch.object(svc, "_hardware_fingerprint", return_value="FIXED"),
            patch.object(svc, "_get_cpu_info", return_value=fresh_cpu) as cpu_mock,
            patch.object(svc, "_get_gpu_info", return_value=None),
            patch.object(svc, "_get_cuda_info", return_value=None),
            patch.object(svc, "_get_vm_info", return_value=None),
        ):
            result = svc.collect(python_data, tracer_data, {})

        # Re-collected despite the matching fingerprint...
        cpu_mock.assert_called_once()
        assert result.cpu == fresh_cpu
        # ...and the cache was rewritten with the current schema version.
        rewritten = json.loads((tmp_path / "runtime_cache.json").read_text())
        assert rewritten["schema_version"] == rc._CACHE_SCHEMA_VERSION
        assert rewritten["data"]["cpu"] == fresh_cpu

    def test_matching_schema_version_is_a_cache_hit(self, tmp_path):
        """A cache with matching fingerprint *and* schema version is served
        without re-collecting."""
        import json

        from roar.execution.provenance import runtime_collector as rc

        svc = self._service(tmp_path)
        python_data, tracer_data = self._make_inputs()

        cached_cpu = {"count": 4, "model": "Cached-CPU"}
        (tmp_path / "runtime_cache.json").write_text(
            json.dumps(
                {
                    "schema_version": rc._CACHE_SCHEMA_VERSION,
                    "fingerprint": "FIXED",
                    "data": {"cpu": cached_cpu, "gpu": None, "cuda": None, "vm": None},
                }
            )
        )

        with (
            patch.object(svc, "_hardware_fingerprint", return_value="FIXED"),
            patch.object(svc, "_get_cpu_info") as cpu_mock,
        ):
            result = svc.collect(python_data, tracer_data, {})

        cpu_mock.assert_not_called()
        assert result.cpu == cached_cpu
