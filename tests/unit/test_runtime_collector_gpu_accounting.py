"""
Tests for RuntimeCollectorService._get_gpu_accounting_info.

The GPU accounting fingerprint is a per-run signal (peak memory / GPUs used by
the just-finished workload) read from NVIDIA accounting mode. It must be
auto-detected (captured only when accounting is Enabled), robust against
malformed / missing output, and never crash a run. See roar todo [86].
"""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def service():
    from roar.execution.provenance.runtime_collector import RuntimeCollectorService

    svc = RuntimeCollectorService()
    svc._logger = MagicMock()
    return svc


def _run_command_stub(mode_output, apps_output):
    """Build a _run_command replacement dispatching on the nvidia-smi query."""

    def _stub(args, timeout=5):
        joined = " ".join(args)
        if "accounting.mode" in joined:
            return mode_output
        if "query-accounted-apps" in joined:
            return apps_output
        return None

    return _stub


class TestGetGpuAccountingInfo:
    def test_enabled_with_usage_multi_gpu(self, service):
        """Accounting Enabled: distinct GPU UUIDs → count, peak = max mem."""
        apps = "GPU-aaaa, 1001, 2048, 90\nGPU-aaaa, 1002, 4096, 85\nGPU-bbbb, 1003, 3072, 70\n"
        service._run_command = _run_command_stub("Enabled\n", apps)

        result = service._get_gpu_accounting_info()

        assert result == {
            "enabled": True,
            "gpu_used": True,
            "gpu_count_used": 2,
            "gpu_peak_mem_mb": 4096,
        }

    def test_enabled_single_gpu(self, service):
        service._run_command = _run_command_stub("Enabled\n", "GPU-aaaa, 1001, 512, 10\n")

        result = service._get_gpu_accounting_info()

        assert result is not None
        assert result["gpu_used"] is True
        assert result["gpu_count_used"] == 1
        assert result["gpu_peak_mem_mb"] == 512

    def test_disabled_returns_none(self, service):
        """Accounting Disabled → capture nothing."""
        service._run_command = _run_command_stub("Disabled\n", "GPU-aaaa, 1001, 512, 10\n")

        assert service._get_gpu_accounting_info() is None

    def test_nvidia_smi_missing_returns_none(self, service):
        """nvidia-smi absent/errored → _run_command returns None → None."""
        service._run_command = _run_command_stub(None, None)

        assert service._get_gpu_accounting_info() is None

    def test_enabled_no_accounted_apps(self, service):
        """Accounting Enabled but no apps recorded → not used, no crash."""
        service._run_command = _run_command_stub("Enabled\n", "")

        result = service._get_gpu_accounting_info()

        assert result == {
            "enabled": True,
            "gpu_used": False,
            "gpu_count_used": 0,
            "gpu_peak_mem_mb": 0,
        }

    def test_zero_memory_apps_not_counted(self, service):
        """Apps with 0 max_memory_usage don't count as GPU usage."""
        service._run_command = _run_command_stub("Enabled\n", "GPU-aaaa, 1001, 0, 0\n")

        result = service._get_gpu_accounting_info()

        assert result is not None
        assert result["gpu_used"] is False
        assert result["gpu_count_used"] == 0
        assert result["gpu_peak_mem_mb"] == 0

    def test_malformed_rows_are_skipped(self, service):
        """Non-numeric / short rows are skipped without crashing."""
        apps = "GPU-aaaa, 1001, [N/A], [N/A]\ngarbage-line\nGPU-bbbb, 1002, 1024, 50\n"
        service._run_command = _run_command_stub("Enabled\n", apps)

        result = service._get_gpu_accounting_info()

        assert result is not None
        assert result["gpu_used"] is True
        assert result["gpu_count_used"] == 1
        assert result["gpu_peak_mem_mb"] == 1024

    def test_exception_returns_none(self, service):
        """Any unexpected failure is swallowed → None (never crash a run)."""

        def _boom(args, timeout=5):
            raise RuntimeError("boom")

        service._run_command = _boom

        assert service._get_gpu_accounting_info() is None

    def test_mode_case_insensitive(self, service):
        """'Enabled' detection is case-insensitive and tolerates whitespace."""
        service._run_command = _run_command_stub("  ENABLED \n", "GPU-aaaa, 1, 256, 5\n")

        result = service._get_gpu_accounting_info()

        assert result is not None
        assert result["enabled"] is True


class TestCollectDoesNotCacheAccounting:
    """gpu_accounting must be collected fresh, never served from the hardware cache."""

    def test_collect_always_calls_accounting_even_on_cache_hit(self, service, tmp_path):
        from roar.core.models.provenance import PythonInjectData, TracerData

        service._cache_dir = str(tmp_path)
        # Prime the hardware cache so cuda/gpu/cpu come from cache.
        fp = service._hardware_fingerprint()
        service._save_cache(fp, {"cuda": {"x": "y"}, "gpu": None, "cpu": None, "vm": None})

        called = {"n": 0}

        def _acct():
            called["n"] += 1
            return {"enabled": True, "gpu_used": True, "gpu_count_used": 1, "gpu_peak_mem_mb": 8}

        service._get_gpu_accounting_info = _acct

        result = service.collect(
            PythonInjectData(),
            TracerData(processes=[]),
            timing={},
        )

        assert called["n"] == 1  # collected fresh despite cache hit
        assert result.gpu_accounting == {
            "enabled": True,
            "gpu_used": True,
            "gpu_count_used": 1,
            "gpu_peak_mem_mb": 8,
        }
        # And the accounting fingerprint was NOT written into the hardware cache.
        cache = service._load_cache()
        assert "gpu_accounting" not in cache["data"]


def test_assembler_carries_gpu_accounting_into_stored_runtime():
    """Regression: the assembler that builds metadata['runtime'] must include
    gpu_accounting, or system_labels / the published DAG never see it — the
    RuntimeInfo field alone is dropped at _runtime_to_dict (real bug caught by
    on-hardware testing of [86])."""
    from roar.core.models.provenance import RuntimeInfo
    from roar.execution.provenance.assembler import ProvenanceAssemblerService

    fp = {"enabled": True, "gpu_used": True, "gpu_count_used": 2, "gpu_peak_mem_mb": 1264}
    result = ProvenanceAssemblerService()._runtime_to_dict(RuntimeInfo(gpu_accounting=fp))
    assert result["gpu_accounting"] == fp


def test_assembler_omits_gpu_accounting_when_absent():
    from roar.core.models.provenance import RuntimeInfo
    from roar.execution.provenance.assembler import ProvenanceAssemblerService

    result = ProvenanceAssemblerService()._runtime_to_dict(RuntimeInfo(gpu_accounting=None))
    assert "gpu_accounting" not in result
