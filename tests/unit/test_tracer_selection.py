"""Tests for tracer backend selection and fallback behavior."""

from unittest.mock import MagicMock, patch

from roar.core.exceptions import TracerPreflightError
from roar.execution.runtime import tracer_backends
from roar.execution.runtime.tracer import TracerService


def _make_signal_handler():
    handler = MagicMock()
    handler.is_interrupted.return_value = False
    return handler


def _make_service(tmp_path):
    return TracerService(package_path=tmp_path / "roar", logger=MagicMock())


def _preflight_result(
    backend: str, ok: bool, summary: str
) -> tracer_backends.TracerPreflightResult:
    return tracer_backends.TracerPreflightResult(
        backend=backend,
        ok=ok,
        summary=summary,
    )


class TestTracerSelection:
    # Note: we intentionally do not auto-build tracer binaries during pip/CLI execution.

    def test_auto_prefers_preload_when_ebpf_unready(self, tmp_path):
        svc = _make_service(tmp_path)
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
        svc = _make_service(tmp_path)
        with (
            patch.object(svc, "_get_tracer_mode", return_value="auto"),
            patch.object(svc, "_get_fallback_enabled", return_value=True),
            patch.object(svc, "_find_ebpf_tracer", return_value="/bin/roar-tracer-ebpf"),
            patch.object(svc, "_ebpf_is_ready", return_value=(False, "missing capabilities")),
            patch.object(svc, "_find_ptrace_tracer", return_value="/bin/roar-tracer"),
        ):
            assert svc.find_tracer() == "/bin/roar-tracer"

    def test_forced_ebpf_ignores_auto_readiness_gate(self, tmp_path):
        svc = _make_service(tmp_path)
        with (
            patch.object(svc, "_get_tracer_mode", return_value="ebpf"),
            patch.object(svc, "_get_fallback_enabled", return_value=True),
            patch.object(svc, "_find_ebpf_tracer", return_value="/bin/roar-tracer-ebpf"),
        ):
            assert svc.find_tracer() == "/bin/roar-tracer-ebpf"

    def test_forced_preload_selects_preload_launcher(self, tmp_path):
        svc = _make_service(tmp_path)
        with (
            patch.object(svc, "_get_tracer_mode", return_value="preload"),
            patch.object(svc, "_get_fallback_enabled", return_value=True),
            patch.object(svc, "_find_preload_tracer", return_value="/bin/roar-tracer-preload"),
        ):
            assert svc.find_tracer() == "/bin/roar-tracer-preload"

    def test_resolve_execution_candidates_skips_failed_preflight_in_auto_mode(self, tmp_path):
        svc = _make_service(tmp_path)
        with (
            patch.object(svc, "_get_tracer_mode", return_value="auto"),
            patch.object(svc, "_get_fallback_enabled", return_value=True),
            patch.object(
                svc,
                "_get_tracer_candidates",
                return_value=[("ebpf", "/bin/roar-tracer-ebpf"), ("ptrace", "/bin/roar-tracer")],
            ),
            patch.object(
                svc,
                "_preflight_candidate",
                side_effect=[
                    _preflight_result("ebpf", False, "attach failed"),
                    _preflight_result("ptrace", True, "ptrace preflight succeeded"),
                ],
            ),
        ):
            candidates = svc.resolve_execution_candidates(["python", "train.py"])

        assert candidates == [("ptrace", "/bin/roar-tracer")]

    def test_resolve_execution_candidates_raises_on_forced_backend_preflight_failure(
        self, tmp_path
    ):
        svc = _make_service(tmp_path)
        with (
            patch.object(svc, "_get_tracer_mode", return_value="ebpf"),
            patch.object(svc, "_get_fallback_enabled", return_value=True),
            patch.object(
                svc,
                "_get_tracer_candidates",
                return_value=[("ebpf", "/bin/roar-tracer-ebpf")],
            ),
            patch.object(
                svc,
                "_preflight_candidate",
                return_value=tracer_backends.TracerPreflightResult(
                    "ebpf",
                    False,
                    "attach failed",
                    checks=(
                        tracer_backends.PreflightCheck(
                            "load_and_attach", False, "missing tracepoint"
                        ),
                    ),
                ),
            ),
        ):
            try:
                svc.resolve_execution_candidates(["python", "train.py"])
            except TracerPreflightError as exc:
                assert "attach failed" in exc.message
                assert "Next steps:" in exc.message
                assert "roar tracer check --backend preload" in exc.message
                assert "failures=" not in exc.message
            else:
                raise AssertionError("expected TracerPreflightError")

    def test_execute_raises_preflight_error_before_spawning_process(self, tmp_path):
        svc = _make_service(tmp_path)
        roar_dir = tmp_path / ".roar"
        roar_dir.mkdir()

        with (
            patch.object(
                svc,
                "resolve_execution_candidates",
                side_effect=TracerPreflightError("preflight failed"),
            ),
            patch("roar.execution.runtime.tracer.subprocess") as mock_subprocess,
        ):
            try:
                svc.execute(
                    command=["python", "train.py"],
                    roar_dir=roar_dir,
                    signal_handler=_make_signal_handler(),
                )
            except TracerPreflightError as exc:
                assert "preflight failed" in str(exc)
            else:
                raise AssertionError("expected TracerPreflightError")

        mock_subprocess.Popen.assert_not_called()

    def test_execute_falls_back_when_first_backend_fails_without_report(self, tmp_path):
        svc = _make_service(tmp_path)
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
                "resolve_execution_candidates",
                return_value=[("ebpf", "/bin/roar-tracer-ebpf"), ("ptrace", "/bin/roar-tracer")],
            ),
            patch("roar.execution.runtime.tracer.subprocess") as mock_subprocess,
            patch("os.path.exists", return_value=False),
            patch("roar.integrations.config.load_config", return_value={}),
        ):
            mock_subprocess.Popen.side_effect = [proc1, proc2]
            svc.execute(
                command=["python", "train.py"],
                roar_dir=roar_dir,
                signal_handler=_make_signal_handler(),
            )

        assert mock_subprocess.Popen.call_count == 2

    def test_execute_does_not_fallback_when_first_backend_exits_zero_without_report(self, tmp_path):
        svc = _make_service(tmp_path)
        roar_dir = tmp_path / ".roar"
        roar_dir.mkdir()

        proc = MagicMock()
        proc.wait.return_value = 0
        proc.pid = 101

        with (
            patch.object(
                svc,
                "resolve_execution_candidates",
                return_value=[("ebpf", "/bin/roar-tracer-ebpf"), ("ptrace", "/bin/roar-tracer")],
            ),
            patch("roar.execution.runtime.tracer.subprocess") as mock_subprocess,
            patch("os.path.exists", return_value=False),
            patch("roar.integrations.config.load_config", return_value={}),
        ):
            mock_subprocess.Popen.return_value = proc
            svc.execute(
                command=["python", "train.py"],
                roar_dir=roar_dir,
                signal_handler=_make_signal_handler(),
            )

        assert mock_subprocess.Popen.call_count == 1
