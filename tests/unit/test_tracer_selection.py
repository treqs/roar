"""Tests for tracer backend selection and fallback behavior."""

from unittest.mock import MagicMock, patch

from roar.services.execution.tracer import TracerService


def _make_signal_handler():
    handler = MagicMock()
    handler.is_interrupted.return_value = False
    return handler


class TestTracerSelection:
    # Note: we intentionally do not auto-build tracer binaries during pip/CLI execution.

    def test_auto_prefers_preload_when_ebpf_unready(self, tmp_path):
        svc = TracerService(package_path=tmp_path / "roar")
        with (
            patch.object(svc, "_get_tracer_mode", return_value="auto"),
            patch.object(svc, "_get_fallback_enabled", return_value=True),
            patch.object(svc, "_find_ebpf_tracer", return_value="/bin/roar-tracer-ebpf"),
            patch.object(svc, "_ebpf_is_ready", return_value=(False, "missing capabilities")),
            patch.object(svc, "_find_preload_tracer", return_value="/bin/roar-tracer-preload"),
            patch.object(svc, "_preload_is_ready", return_value=(True, None)),
            patch.object(svc, "_find_ptrace_tracer", return_value="/bin/roar-tracer"),
        ):
            assert svc.find_tracer() == "/bin/roar-tracer-preload"

    def test_auto_skips_unready_ebpf_and_selects_ptrace(self, tmp_path):
        svc = TracerService(package_path=tmp_path / "roar")
        with (
            patch.object(svc, "_get_tracer_mode", return_value="auto"),
            patch.object(svc, "_get_fallback_enabled", return_value=True),
            patch.object(svc, "_find_ebpf_tracer", return_value="/bin/roar-tracer-ebpf"),
            patch.object(svc, "_ebpf_is_ready", return_value=(False, "missing capabilities")),
            patch.object(svc, "_find_ptrace_tracer", return_value="/bin/roar-tracer"),
        ):
            assert svc.find_tracer() == "/bin/roar-tracer"

    def test_forced_ebpf_ignores_auto_readiness_gate(self, tmp_path):
        svc = TracerService(package_path=tmp_path / "roar")
        with (
            patch.object(svc, "_get_tracer_mode", return_value="ebpf"),
            patch.object(svc, "_get_fallback_enabled", return_value=True),
            patch.object(svc, "_find_ebpf_tracer", return_value="/bin/roar-tracer-ebpf"),
        ):
            assert svc.find_tracer() == "/bin/roar-tracer-ebpf"

    def test_forced_preload_selects_preload_launcher(self, tmp_path):
        svc = TracerService(package_path=tmp_path / "roar")
        with (
            patch.object(svc, "_get_tracer_mode", return_value="preload"),
            patch.object(svc, "_get_fallback_enabled", return_value=True),
            patch.object(svc, "_find_preload_tracer", return_value="/bin/roar-tracer-preload"),
        ):
            assert svc.find_tracer() == "/bin/roar-tracer-preload"

    def test_execute_falls_back_when_first_backend_fails_without_report(self, tmp_path):
        svc = TracerService(package_path=tmp_path / "roar")
        roar_dir = tmp_path / ".roar"
        roar_dir.mkdir()

        proc1 = MagicMock()
        proc1.wait.return_value = 1
        proc1.pid = 101
        proc2 = MagicMock()
        proc2.wait.return_value = 1
        proc2.pid = 102

        with (
            patch.object(
                svc,
                "_get_tracer_candidates",
                return_value=[("ebpf", "/bin/roar-tracer-ebpf"), ("ptrace", "/bin/roar-tracer")],
            ),
            patch("subprocess.Popen", side_effect=[proc1, proc2]) as mock_popen,
            patch("os.path.exists", return_value=False),
            patch("roar.config.load_config", return_value={}),
        ):
            svc.execute(
                command=["python", "train.py"],
                roar_dir=roar_dir,
                signal_handler=_make_signal_handler(),
            )

        assert mock_popen.call_count == 2

    def test_execute_does_not_fallback_when_first_backend_exits_zero_without_report(self, tmp_path):
        svc = TracerService(package_path=tmp_path / "roar")
        roar_dir = tmp_path / ".roar"
        roar_dir.mkdir()

        proc = MagicMock()
        proc.wait.return_value = 0
        proc.pid = 101

        with (
            patch.object(
                svc,
                "_get_tracer_candidates",
                return_value=[("ebpf", "/bin/roar-tracer-ebpf"), ("ptrace", "/bin/roar-tracer")],
            ),
            patch("subprocess.Popen", return_value=proc) as mock_popen,
            patch("os.path.exists", return_value=False),
            patch("roar.config.load_config", return_value={}),
        ):
            svc.execute(
                command=["python", "train.py"],
                roar_dir=roar_dir,
                signal_handler=_make_signal_handler(),
            )

        assert mock_popen.call_count == 1
