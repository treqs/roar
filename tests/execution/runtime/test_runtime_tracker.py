from __future__ import annotations

import json
import sys

from roar.execution.runtime.inject.tracker import (
    RuntimeInjectionTracker,
)


def test_runtime_tracker_writes_expected_log_payload(tmp_path) -> None:
    log_path = tmp_path / "inject-log.json"
    environ = {"ROAR_WRAP": "1", "ROAR_LOG_FILE": str(log_path), "VIRTUAL_ENV": "/tmp/venv"}

    class _FakeController:
        def handle_import(self, module_name: str, module) -> None:
            return None

    tracker = RuntimeInjectionTracker(
        environ,
        _FakeController(),
        log_file=str(log_path),
        inject_dir=str(tmp_path / "inject"),
    )

    data_path = tmp_path / "data.txt"
    data_path.write_text("hello", encoding="utf-8")

    with tracker.tracking_open(str(data_path), "r", encoding="utf-8") as handle:
        assert handle.read() == "hello"
    assert tracker.patched_environ_get("VIRTUAL_ENV") == "/tmp/venv"
    tracker.write_log()

    payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert str(data_path.resolve()) in payload["opened_files"]
    assert payload["env_reads"]["VIRTUAL_ENV"] == "/tmp/venv"
    assert payload["virtual_env"] == "/tmp/venv"
    # Python identity from the *traced* process — consumed by runtime_collector
    # to populate job metadata. Critical when roar's host Python and the traced
    # Python differ (cross-Python `roar run`).
    assert payload["python_version"].count(".") >= 2  # e.g. "3.12.3"
    assert payload["python_implementation"]  # e.g. "CPython"


def test_runtime_tracker_excludes_roar_runtime_pythonpath_modules(tmp_path) -> None:
    log_path = tmp_path / "inject-log.json"
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    runtime_module = runtime_root / "runtime_only.py"
    runtime_module.write_text("value = 1\n", encoding="utf-8")

    environ = {
        "ROAR_LOG_FILE": str(log_path),
        "ROAR_RUNTIME_PYTHONPATH_ACTIVE": str(runtime_root),
    }

    class _FakeController:
        def handle_import(self, module_name: str, module) -> None:
            return None

    tracker = RuntimeInjectionTracker(
        environ,
        _FakeController(),
        log_file=str(log_path),
        inject_dir=str(tmp_path / "inject"),
    )

    sys.path.append(str(runtime_root))
    try:
        import runtime_only  # type: ignore[import-not-found]

        assert runtime_only.value == 1
        tracker.write_log()
    finally:
        sys.path.remove(str(runtime_root))
        sys.modules.pop("runtime_only", None)

    payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert str(runtime_module) not in payload["modules_files"]


def test_runtime_tracker_excludes_roar_internal_env_reads(tmp_path) -> None:
    """roar's own injected vars must not leak into captured env_reads (issue #164)."""
    log_path = tmp_path / "inject-log.json"
    environ = {
        "ROAR_WRAP": "1",
        "ROAR_EXECUTION_BACKEND": "local",
        "ROAR_RUNTIME_PYTHONPATH_ACTIVE": "/cache/site-packages",
        "ROAR_LOG_FILE": str(log_path),
        "HOME": "/home/ubuntu",
    }

    class _FakeController:
        def handle_import(self, module_name: str, module) -> None:
            return None

    tracker = RuntimeInjectionTracker(
        environ,
        _FakeController(),
        log_file=str(log_path),
        inject_dir=str(tmp_path / "inject"),
    )

    # Read both a roar-internal var and a user var through the patched getter.
    assert tracker.patched_environ_get("ROAR_WRAP") == "1"
    assert tracker.patched_environ_get("ROAR_EXECUTION_BACKEND") == "local"
    assert tracker.patched_environ_get("HOME") == "/home/ubuntu"
    tracker.write_log()

    payload = json.loads(log_path.read_text(encoding="utf-8"))
    env_reads = payload["env_reads"]
    # User-facing reads are kept; roar's reserved namespace is dropped.
    assert env_reads == {"HOME": "/home/ubuntu"}
    assert not any(key.startswith("ROAR_") for key in env_reads)
