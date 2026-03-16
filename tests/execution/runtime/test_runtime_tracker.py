from __future__ import annotations

import json

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


def test_get_used_packages_returns_dict_for_known_module() -> None:
    import json as json_module

    json_file = json_module.__file__
    modules_files = [json_file] if json_file else []

    installed = get_installed_packages()
    used = get_used_packages(modules_files, installed)

    assert isinstance(used, dict)
