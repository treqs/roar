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
