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
