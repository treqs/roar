from __future__ import annotations

import json
import sys

from roar.execution.runtime.inject.tracker import (
    RuntimeInjectionTracker,
    get_installed_packages,
    get_used_packages,
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


def test_get_used_packages_returns_dict_for_known_module() -> None:
    import json as json_module

    json_file = json_module.__file__
    modules_files = [json_file] if json_file else []

    installed = get_installed_packages()
    used = get_used_packages(modules_files, installed)

    assert isinstance(used, dict)
