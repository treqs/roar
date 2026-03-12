from __future__ import annotations

import builtins
from types import SimpleNamespace

import pytest

from roar.execution.framework.contract import ROAR_EXECUTION_BACKEND_ENV
from roar.services.execution.inject import sitecustomize
from roar.services.execution.inject.support import SuppressTracking


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


def test_tracking_import_initializes_observes_and_patches_matched_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    fake_backend = SimpleNamespace(
        name="fake",
        runtime_import=SimpleNamespace(
            initialize_process=lambda: calls.append("initialize"),
            observe_import=lambda module_name, module: calls.append(f"observe:{module_name}"),
            patch_module=lambda module_name, module: calls.append(f"patch:{module_name}"),
        ),
    )

    monkeypatch.setenv("ROAR_WRAP", "1")
    monkeypatch.delenv(ROAR_EXECUTION_BACKEND_ENV, raising=False)
    monkeypatch.setattr(
        sitecustomize,
        "match_execution_backend_for_module",
        lambda module_name: fake_backend if module_name == "json" else None,
    )
    monkeypatch.setattr(
        sitecustomize,
        "_resolve_runtime_backend",
        lambda: fake_backend
        if sitecustomize.os.environ.get(ROAR_EXECUTION_BACKEND_ENV) == "fake"
        else None,
    )

    module = sitecustomize.tracking_import("json")

    assert module.__name__ == "json"
    assert sitecustomize.os.environ[ROAR_EXECUTION_BACKEND_ENV] == "fake"
    assert calls == ["initialize", "observe:json", "patch:json"]


def test_tracking_import_reuses_initialized_backend_for_unrelated_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    fake_backend = SimpleNamespace(
        name="fake",
        runtime_import=SimpleNamespace(
            initialize_process=lambda: calls.append("initialize"),
            observe_import=lambda module_name, module: calls.append(f"observe:{module_name}"),
            patch_module=lambda module_name, module: calls.append(f"patch:{module_name}"),
        ),
    )

    monkeypatch.setenv("ROAR_WRAP", "1")
    monkeypatch.setenv(ROAR_EXECUTION_BACKEND_ENV, "fake")
    monkeypatch.setattr(sitecustomize, "_initialized_runtime_backends", set())
    monkeypatch.setattr(sitecustomize, "_resolve_runtime_backend", lambda: fake_backend)
    monkeypatch.setattr(sitecustomize, "match_execution_backend_for_module", lambda _module_name: None)

    sitecustomize.tracking_import("json")
    sitecustomize.tracking_import("os")

    assert calls.count("initialize") == 1
    assert "observe:json" in calls
    assert calls[-1] == "observe:os"
