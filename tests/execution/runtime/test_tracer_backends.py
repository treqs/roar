"""Tests for shared tracer backend discovery/readiness helpers."""

import json
import subprocess
from pathlib import Path
from unittest.mock import mock_open, patch

from roar.execution.runtime import tracer_backends


def test_preload_is_ready_requires_library(tmp_path: Path) -> None:
    package_path = tmp_path / "roar"
    package_path.mkdir()

    launcher = tmp_path / "launcher"
    launcher.write_text("")

    with patch.object(tracer_backends, "find_preload_library", return_value=None):
        ok, reason = tracer_backends.preload_is_ready(package_path, str(launcher))

    assert not ok
    assert reason == "preload library not found"


def test_preload_is_ready_probes_launcher_execution(tmp_path: Path) -> None:
    package_path = tmp_path / "roar"
    package_path.mkdir()

    launcher = tmp_path / "roar-tracer-preload"
    launcher.write_text("")
    library = tmp_path / "libroar_tracer_preload.so"
    library.write_text("")

    def _run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        Path(command[1]).write_text("{}")
        return subprocess.CompletedProcess(command, 0, "", "")

    with (
        patch.object(tracer_backends, "find_preload_library", return_value=str(library)),
        patch.object(tracer_backends.subprocess, "run", side_effect=_run),
    ):
        ok, reason = tracer_backends.preload_is_ready(package_path, str(launcher))

    assert ok
    assert reason is None


def test_ptrace_is_ready_runs_preflight_and_passes_when_ok(tmp_path: Path) -> None:
    """Happy path: binary's `--preflight --json` returns ok=True."""
    binary = tmp_path / "roar-tracer"
    binary.write_text("")

    payload = json.dumps({"backend": "ptrace", "ok": True, "summary": "ptrace preflight ok"})
    tracer_backends._probe_ptrace_binary.cache_clear()
    with patch.object(
        tracer_backends.subprocess,
        "run",
        return_value=subprocess.CompletedProcess([str(binary)], 0, payload, ""),
    ):
        ok, reason = tracer_backends.ptrace_is_ready(str(binary))

    assert ok
    assert reason is None


def test_ptrace_is_ready_catches_wrong_arch_enoexec(tmp_path: Path) -> None:
    """The headline P0-3 case: a wrong-arch ELF in the wheel raises
    OSError(ENOEXEC) when subprocess.run tries to exec it. The
    existence-only check used to silently pass; ptrace_readiness now
    surfaces it."""
    binary = tmp_path / "roar-tracer"
    binary.write_text("not an elf\n")

    tracer_backends._probe_ptrace_binary.cache_clear()
    with patch.object(
        tracer_backends.subprocess,
        "run",
        side_effect=OSError(8, "Exec format error"),
    ):
        ok, reason = tracer_backends.ptrace_is_ready(str(binary))

    assert not ok
    assert reason is not None
    assert "Exec format error" in reason


def test_ptrace_is_ready_reports_preflight_failure_summary(tmp_path: Path) -> None:
    """When the binary execs but its preflight reports a problem (e.g.
    ptrace tracer detecting an unsupported host arch), surface the
    binary's own summary instead of a generic 'failed' message."""
    binary = tmp_path / "roar-tracer"
    binary.write_text("")

    payload = json.dumps(
        {
            "backend": "ptrace",
            "ok": False,
            "summary": "ptrace tracer supports x86_64 and aarch64 (got riscv64)",
        }
    )
    tracer_backends._probe_ptrace_binary.cache_clear()
    with patch.object(
        tracer_backends.subprocess,
        "run",
        return_value=subprocess.CompletedProcess([str(binary)], 1, payload, ""),
    ):
        ok, reason = tracer_backends.ptrace_is_ready(str(binary))

    assert not ok
    assert reason == "ptrace tracer supports x86_64 and aarch64 (got riscv64)"


def test_backend_ready_auto_skips_ptrace_when_binary_unexecable(tmp_path: Path) -> None:
    """If the ptrace binary exists but can't exec, auto must NOT pick
    it. Pre-fix, `find_ptrace_tracer` returning a path was treated as
    sufficient and ptrace was selected even when ENOEXEC awaited."""
    package_path = tmp_path / "roar"
    package_path.mkdir()

    with (
        patch.object(tracer_backends, "find_ebpf_tracer", return_value=None),
        patch.object(tracer_backends, "find_preload_tracer", return_value=None),
        patch.object(tracer_backends, "find_ptrace_tracer", return_value="/bin/roar-tracer"),
        patch.object(
            tracer_backends,
            "ptrace_is_ready",
            return_value=(False, "ptrace tracer failed to exec: Exec format error"),
        ),
    ):
        ok, detail = tracer_backends.backend_ready(package_path, "auto")

    assert not ok
    assert "no usable tracer" in detail


def test_preload_is_ready_reports_probe_failure(tmp_path: Path) -> None:
    package_path = tmp_path / "roar"
    package_path.mkdir()

    launcher = tmp_path / "roar-tracer-preload"
    launcher.write_text("")
    library = tmp_path / "libroar_tracer_preload.so"
    library.write_text("")

    with (
        patch.object(tracer_backends, "find_preload_library", return_value=str(library)),
        patch.object(
            tracer_backends.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [str(launcher)],
                1,
                "",
                "roar-tracer-preload: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.39' not found",
            ),
        ),
    ):
        ok, reason = tracer_backends.preload_is_ready(package_path, str(launcher))

    assert not ok
    assert reason == (
        "preload launcher probe failed: "
        "roar-tracer-preload: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.39' not found"
    )


def test_backend_ready_auto_prefers_preload_when_ebpf_not_ready(tmp_path: Path) -> None:
    package_path = tmp_path / "roar"
    package_path.mkdir()

    with (
        patch.object(tracer_backends, "find_ebpf_tracer", return_value="/bin/roar-tracer-ebpf"),
        patch.object(
            tracer_backends, "ebpf_is_ready", return_value=(False, "missing capabilities")
        ),
        patch.object(
            tracer_backends, "find_preload_tracer", return_value="/bin/roar-tracer-preload"
        ),
        patch.object(tracer_backends, "preload_is_ready", return_value=(True, None)),
        patch.object(tracer_backends, "find_ptrace_tracer", return_value="/bin/roar-tracer"),
    ):
        ok, detail = tracer_backends.backend_ready(package_path, "auto")

    assert ok
    assert detail == "preload ready"


def test_ebpf_readiness_root_with_caps_is_ready() -> None:
    """Root with full capabilities should be ready."""
    # CapEff with CAP_BPF (39) + CAP_PERFMON (38) set
    cap_eff = (1 << 39) | (1 << 38)
    proc_status = f"Name:\tpython\nCapEff:\t{cap_eff:016x}\n"
    with (
        patch("os.geteuid", return_value=0),
        patch("builtins.open", mock_open(read_data=proc_status)),
    ):
        result = tracer_backends.ebpf_readiness("/bin/roar-tracer-ebpf")
    assert result.ok


def test_ebpf_readiness_root_without_caps_not_ready() -> None:
    """Root in a container without BPF capabilities should not be ready."""
    # CapEff with none of the BPF-related caps
    cap_eff = 0x00000000A80425FB  # typical runpod container caps
    proc_status = f"Name:\tpython\nCapEff:\t{cap_eff:016x}\n"
    with (
        patch("os.geteuid", return_value=0),
        patch("builtins.open", mock_open(read_data=proc_status)),
    ):
        result = tracer_backends.ebpf_readiness("/bin/roar-tracer-ebpf")
    assert not result.ok
    assert "container" in result.reason


def test_ebpf_readiness_root_with_sys_admin_is_ready() -> None:
    """Root with CAP_SYS_ADMIN (legacy) should be ready."""
    cap_eff = 1 << 21  # CAP_SYS_ADMIN only
    proc_status = f"Name:\tpython\nCapEff:\t{cap_eff:016x}\n"
    with (
        patch("os.geteuid", return_value=0),
        patch("builtins.open", mock_open(read_data=proc_status)),
    ):
        result = tracer_backends.ebpf_readiness("/bin/roar-tracer-ebpf")
    assert result.ok


def test_find_preload_library_supports_dylib_on_macos(tmp_path: Path) -> None:
    package_path = tmp_path / "roar"
    package_path.mkdir()
    release_dir = tmp_path / "rust" / "target" / "release"
    release_dir.mkdir(parents=True)

    dylib = release_dir / "libroar_tracer_preload.dylib"
    dylib.write_text("")

    with patch.object(tracer_backends.sys, "platform", "darwin"):
        resolved = tracer_backends.find_preload_library(package_path)

    assert resolved == str(dylib.resolve())


def test_find_preload_library_supports_so_on_linux(tmp_path: Path) -> None:
    package_path = tmp_path / "roar"
    package_path.mkdir()
    release_dir = tmp_path / "rust" / "target" / "release"
    release_dir.mkdir(parents=True)

    so = release_dir / "libroar_tracer_preload.so"
    so.write_text("")

    with patch.object(tracer_backends.sys, "platform", "linux"):
        resolved = tracer_backends.find_preload_library(package_path)

    assert resolved == str(so.resolve())


def test_find_preload_library_ignores_dylib_on_linux(tmp_path: Path) -> None:
    package_path = tmp_path / "roar"
    package_path.mkdir()
    release_dir = tmp_path / "rust" / "target" / "release"
    release_dir.mkdir(parents=True)

    (release_dir / "libroar_tracer_preload.dylib").write_text("")

    with patch.object(tracer_backends.sys, "platform", "linux"):
        resolved = tracer_backends.find_preload_library(package_path)

    assert resolved is None


def test_preflight_backend_ebpf_parses_json_result(tmp_path: Path) -> None:
    package_path = tmp_path / "roar"
    package_path.mkdir()
    payload = {
        "backend": "ebpf",
        "ok": True,
        "summary": "eBPF preflight succeeded",
        "command_checked": None,
        "warnings": [],
        "checks": [{"name": "load_and_attach", "ok": True, "detail": "ok"}],
    }

    with (
        patch.object(tracer_backends, "find_ebpf_tracer", return_value="/bin/roar-tracer-ebpf"),
        patch.object(
            tracer_backends,
            "ebpf_readiness",
            return_value=tracer_backends.TracerReadiness(True, None),
        ),
        patch.object(
            tracer_backends.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["/bin/roar-tracer-ebpf", "--preflight", "--json"],
                0,
                json.dumps(payload),
                "",
            ),
        ),
    ):
        result = tracer_backends.preflight_backend(package_path, "ebpf")

    assert result.ok is True
    assert result.summary == "eBPF preflight succeeded"
    assert any(
        check.name == "binary" and check.detail == "/bin/roar-tracer-ebpf"
        for check in result.checks
    )
    assert any(check.name == "load_and_attach" and check.ok for check in result.checks)


def test_preflight_backend_preload_uses_native_binary_preflight(tmp_path: Path) -> None:
    package_path = tmp_path / "roar"
    package_path.mkdir()
    payload = {
        "backend": "preload",
        "ok": False,
        "summary": "macOS protected binary blocks preload injection",
        "command_checked": "/usr/bin/python3",
        "warnings": [],
        "checks": [
            {
                "name": "command_compatibility",
                "ok": False,
                "detail": "macOS protected binary blocks preload injection",
            }
        ],
    }

    with (
        patch.object(
            tracer_backends, "find_preload_tracer", return_value="/bin/roar-tracer-preload"
        ),
        patch.object(
            tracer_backends.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["/bin/roar-tracer-preload", "--preflight", "--json", "--command", "python"],
                1,
                json.dumps(payload),
                "",
            ),
        ) as mock_run,
    ):
        result = tracer_backends.preflight_backend(package_path, "preload", command=["python"])

    assert result.ok is False
    assert result.command_checked == "/usr/bin/python3"
    assert "protected binary" in result.summary
    assert mock_run.call_args.args[0] == [
        "/bin/roar-tracer-preload",
        "--preflight",
        "--json",
        "--command",
        "python",
    ]


def test_preflight_backend_ptrace_uses_native_binary_preflight(tmp_path: Path) -> None:
    package_path = tmp_path / "roar"
    package_path.mkdir()
    payload = {
        "backend": "ptrace",
        "ok": False,
        "summary": "ptrace tracer only supports x86_64 today (got aarch64)",
        "command_checked": None,
        "warnings": [],
        "checks": [
            {
                "name": "architecture",
                "ok": False,
                "detail": "aarch64",
            }
        ],
    }

    with (
        patch.object(tracer_backends, "find_ptrace_tracer", return_value="/bin/roar-tracer"),
        patch.object(
            tracer_backends.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["/bin/roar-tracer", "--preflight", "--json"],
                1,
                json.dumps(payload),
                "",
            ),
        ) as mock_run,
    ):
        result = tracer_backends.preflight_backend(package_path, "ptrace")

    assert result.ok is False
    assert "x86_64" in result.summary
    assert mock_run.call_args.args[0] == ["/bin/roar-tracer", "--preflight", "--json"]


def test_preflight_auto_backend_returns_first_passing_backend(tmp_path: Path) -> None:
    package_path = tmp_path / "roar"
    package_path.mkdir()

    ebpf = tracer_backends.TracerPreflightResult("ebpf", False, "attach failed")
    preload = tracer_backends.TracerPreflightResult("preload", True, "preload preflight succeeded")

    with patch.object(tracer_backends, "preflight_backend", side_effect=[ebpf, preload, preload]):
        result = tracer_backends.preflight_auto_backend(package_path)

    assert result.ok is True
    assert result.selected_backend == "preload"
    assert [item.backend for item in result.results] == ["ebpf", "preload"]


def _ptrace_command_not_found(command: str) -> tracer_backends.TracerPreflightResult:
    """Shape of a ptrace preflight that found+launched the tracer fine but the
    user's target command does not resolve on PATH."""
    return tracer_backends.TracerPreflightResult(
        backend="ptrace",
        ok=False,
        summary=f"command not found: {command}",
        command_checked=command,
        checks=(
            tracer_backends.PreflightCheck("binary", True, "/path/to/roar-tracer"),
            tracer_backends.PreflightCheck("command", False, f"command not found: {command}"),
        ),
    )


def test_suggestions_command_not_found_does_not_suggest_building_tracer() -> None:
    """Regression: a missing *target command* must not produce 'Build the ptrace
    tracer' / 'Check kernel ptrace policy' hints — the tracer is fine, the user
    mistyped the command they wanted to run."""
    result = _ptrace_command_not_found("gobbledegook")
    suggestions = tracer_backends.suggestions_for_preflight_result(result)

    joined = " ".join(suggestions).lower()
    assert "gobbledegook" in joined
    assert "path" in joined
    # the misleading tracer-setup hints must be absent
    assert not any("build the ptrace tracer" in s.lower() for s in suggestions)
    assert not any("kernel ptrace policy" in s.lower() for s in suggestions)


def test_suggestions_tracer_binary_missing_still_suggests_building() -> None:
    """Contrast: when the *tracer binary* is genuinely missing, the build hint
    is the right advice and must still appear."""
    result = tracer_backends.TracerPreflightResult(
        backend="ptrace",
        ok=False,
        summary="ptrace tracer not found",
        command_checked="python",
        checks=(tracer_backends.PreflightCheck("binary", False, "roar-tracer not found"),),
    )
    suggestions = tracer_backends.suggestions_for_preflight_result(result)
    assert any("build the ptrace tracer" in s.lower() for s in suggestions)


def test_suggestions_auto_command_not_found_is_command_oriented() -> None:
    """Auto selection with a missing command surfaces the command hint, not a
    pile of per-backend tracer-setup suggestions."""
    auto = tracer_backends.AutoPreflightResult(
        ok=False,
        selected_backend=None,
        summary="no usable tracer found",
        command_checked="gobbledegook",
        results=(_ptrace_command_not_found("gobbledegook"),),
    )
    suggestions = tracer_backends.suggestions_for_preflight_result(auto)
    joined = " ".join(suggestions).lower()
    assert "gobbledegook" in joined
    assert not any("build the" in s.lower() for s in suggestions)
