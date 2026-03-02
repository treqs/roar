"""Tests for shared tracer backend discovery/readiness helpers."""

from pathlib import Path
from unittest.mock import patch

from roar.services.execution import tracer_backends


def test_preload_is_ready_requires_library(tmp_path: Path) -> None:
    package_path = tmp_path / "roar"
    package_path.mkdir()

    launcher = tmp_path / "launcher"
    launcher.write_text("")

    with patch.object(tracer_backends, "find_preload_library", return_value=None):
        ok, reason = tracer_backends.preload_is_ready(package_path, str(launcher))

    assert not ok
    assert reason == "preload library not found"


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
