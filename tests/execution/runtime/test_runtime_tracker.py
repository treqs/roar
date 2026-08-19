from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from roar.execution.runtime.inject.tracker import (
    RuntimeInjectionTracker,
    get_active_runtime_pythonpath,
    get_installed_packages,
    get_used_packages,
    merge_inject_logs,
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

    merge_inject_logs(str(log_path))  # write_log writes a per-PID shard; merge -> canonical
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

    merge_inject_logs(str(log_path))  # write_log writes a per-PID shard; merge -> canonical
    payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert str(runtime_module) not in payload["modules_files"]


def test_package_version_comes_from_imported_workload_distribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workload = tmp_path / "workload" / "site-packages"
    runtime = tmp_path / "runtime" / "site-packages"

    def write_distribution(root: Path, version: str) -> Path:
        root.mkdir(parents=True)
        module = root / "shadowpkg.py"
        module.write_text("VALUE = 1\n", encoding="utf-8")
        metadata = root / f"shadowpkg-{version}.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: shadowpkg\nVersion: {version}\n",
            encoding="utf-8",
        )
        (metadata / "top_level.txt").write_text("shadowpkg\n", encoding="utf-8")
        return module

    workload_module = write_distribution(workload, "1.0")
    write_distribution(runtime, "9.0")
    monkeypatch.setattr(sys, "path", [str(workload), str(runtime), *sys.path])
    runtime_paths = get_active_runtime_pythonpath({"ROAR_RUNTIME_PYTHONPATH_ACTIVE": str(runtime)})

    installed = get_installed_packages(excluded_paths=runtime_paths)
    used = get_used_packages([str(workload_module)], installed)

    assert installed["shadowpkg"] == "1.0"
    assert used["shadowpkg"] == "1.0"


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

    merge_inject_logs(str(log_path))  # write_log writes a per-PID shard; merge -> canonical
    payload = json.loads(log_path.read_text(encoding="utf-8"))
    env_reads = payload["env_reads"]
    # User-facing reads are kept; roar's reserved namespace is dropped.
    assert env_reads == {"HOME": "/home/ubuntu"}
    assert not any(key.startswith("ROAR_") for key in env_reads)


def test_workload_boundary_drops_bootstrap_modules_and_shared_libs(tmp_path, monkeypatch) -> None:
    from roar.execution.runtime.inject import tracker as tracker_module

    log_path = tmp_path / "inject-log.json"

    class _FakeController:
        def handle_import(self, module_name: str, module) -> None:
            return None

    tracker = RuntimeInjectionTracker({}, _FakeController(), log_file=str(log_path))
    bootstrap_module = tmp_path / "site-packages" / "click" / "__init__.py"
    workload_module = tmp_path / "site-packages" / "numpy" / "__init__.py"
    bootstrap = type(sys)("click")
    bootstrap.__file__ = str(bootstrap_module)
    workload = type(sys)("numpy")
    workload.__file__ = str(workload_module)
    monkeypatch.setitem(sys.modules, "click", bootstrap)

    bootstrap_lib = "/tool/site-packages/cryptography/_rust.abi3.so"
    libs = [[bootstrap_lib], [bootstrap_lib, "/venv/numpy.libs/libopenblas.so"]]
    monkeypatch.setattr(tracker_module, "get_loaded_shared_libs", lambda _open: libs.pop(0))
    monkeypatch.setattr(
        tracker_module,
        "get_installed_packages",
        lambda **_: {"click": "8.4.2", "numpy": "2.5.1"},
    )
    tracker.mark_workload_boundary()
    monkeypatch.setitem(sys.modules, "numpy", workload)
    tracker.write_log()

    merge_inject_logs(str(log_path))
    payload = json.loads(log_path.read_text())
    assert str(bootstrap_module) not in payload["modules_files"]
    assert str(workload_module) in payload["modules_files"]
    assert payload["shared_libs"] == ["/venv/numpy.libs/libopenblas.so"]


class _NullController:
    def handle_import(self, module_name: str, module) -> None:
        return None


def _origin_tracker(tmp_path):
    return RuntimeInjectionTracker(
        {"ROAR_LOG_FILE": str(tmp_path / "inject-log.json")},
        _NullController(),
        log_file=str(tmp_path / "inject-log.json"),
        inject_dir=str(tmp_path / "inject"),
    )


class TestWorkloadImportOrigin:
    """The origin heuristic decides what counts as workload code, and it reads
    the importing module's globals. The real ``__import__`` ignores ``globals``
    entirely at ``level == 0``, so anything it accepts today must keep working:
    injected code that raises into a user's ``import`` is the cardinal failure
    for this module."""

    @pytest.mark.parametrize(
        "origin_file",
        [
            pytest.param(Path("/work/train.py"), id="path-object"),
            pytest.param(b"/work/train.py", id="bytes"),
            pytest.param(5, id="int"),
            pytest.param(None, id="none"),
        ],
    )
    def test_a_non_str_origin_file_does_not_escape_into_the_import(
        self, tmp_path, origin_file
    ) -> None:
        tracker = _origin_tracker(tmp_path)

        # A non-stdlib name: the heuristic short-circuits on stdlib before it
        # ever reads __file__, so "json" here would test nothing.
        module = tracker.tracking_import(
            "pytest", {"__name__": "m", "__file__": origin_file}, None, (), 0
        )

        assert module is not None  # the import itself still succeeded

    def test_a_non_dict_globals_does_not_escape_into_the_import(self, tmp_path) -> None:
        assert tracker_import_survives(_origin_tracker(tmp_path), object())

    def test_loose_workload_code_is_recorded(self, tmp_path) -> None:
        tracker = _origin_tracker(tmp_path)

        tracker.tracking_import(
            "json", {"__name__": "__main__", "__file__": "/work/train.py"}, None, (), 0
        )

        assert "json" not in tracker.workload_import_names  # stdlib is excluded

        tracker.tracking_import(
            "pytest", {"__name__": "__main__", "__file__": "/work/train.py"}, None, (), 0
        )
        assert "pytest" in tracker.workload_import_names

    def test_an_installed_packages_own_import_is_not_workload_code(self, tmp_path) -> None:
        """A dependency-of-a-dependency is the package's business, not the
        workload's -- that distinction is the whole point of the signal."""
        tracker = _origin_tracker(tmp_path)

        tracker.tracking_import(
            "pytest",
            {
                "__name__": "requests.adapters",
                "__file__": "/v/lib/site-packages/requests/adapters.py",
            },
            None,
            (),
            0,
        )

        assert "pytest" not in tracker.workload_import_names

    def test_roars_own_imports_are_never_workload_code(self, tmp_path) -> None:
        tracker = _origin_tracker(tmp_path)

        tracker.tracking_import(
            "pytest",
            {"__name__": "roar.execution.thing", "__file__": "/anywhere/x.py"},
            None,
            (),
            0,
        )

        assert "pytest" not in tracker.workload_import_names


def tracker_import_survives(tracker, globals_obj) -> bool:
    tracker.tracking_import("pytest", globals_obj, None, (), 0)
    return True


def test_the_boundary_survives_a_module_with_a_non_str_file(tmp_path) -> None:
    """``mark_workload_boundary`` walks live interpreter state, where a module's
    ``__file__`` need not be a ``str``. Losing the snapshot is survivable;
    raising here costs the whole inject log, since it runs during
    ``sitecustomize`` where ``site`` swallows the exception."""
    import types

    tracker = _origin_tracker(tmp_path)
    poisoned = types.ModuleType("poisoned_module")
    poisoned.__file__ = 5
    sys.modules["poisoned_module"] = poisoned
    try:
        tracker.mark_workload_boundary()
    finally:
        del sys.modules["poisoned_module"]

    assert tracker._baseline_module_files  # real modules were still snapshotted
