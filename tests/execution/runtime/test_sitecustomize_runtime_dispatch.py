from __future__ import annotations

import builtins

import pytest

from roar.execution.runtime.inject import sitecustomize
from roar.execution.runtime.inject.support import SuppressTracking


@pytest.fixture(autouse=True)
def _restore_builtins() -> None:
    real_open = builtins.open
    real_import = builtins.__import__
    try:
        yield
    finally:
        builtins.open = real_open
        builtins.__import__ = real_import


def test_tracking_open_skips_recording_when_suppressed(tmp_path) -> None:
    file_path = tmp_path / "suppressed-read.txt"
    file_path.write_text("hello", encoding="utf-8")
    abs_path = str(file_path.resolve())
    sitecustomize.opened_files.discard(abs_path)

    with (
        SuppressTracking(),
        sitecustomize.tracking_open(str(file_path), "r", encoding="utf-8") as handle,
    ):
        assert handle.read() == "hello"

    assert abs_path not in sitecustomize.opened_files


def test_tracking_import_delegates_to_runtime_import_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class _FakeController:
        def handle_import(self, module_name: str, module) -> None:
            calls.append((module_name, module.__name__))

    monkeypatch.setenv("ROAR_WRAP", "1")
    monkeypatch.setattr(sitecustomize._runtime_tracker, "_runtime_import_controller", _FakeController())

    module = sitecustomize.tracking_import("json")

    assert module.__name__ == "json"
    assert calls == [("json", "json")]
