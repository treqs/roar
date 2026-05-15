from __future__ import annotations

import pytest

from roar.execution.framework.contract import (
    ROAR_EXECUTION_BACKEND_ENV,
    DistributedRuntimeAdapter,
    DriverBootstrapAdapter,
    ExecutionBackend,
    ExecutionCommandPlan,
    HostExecutionAdapter,
    RuntimeImportAdapter,
    WorkerBootstrapAdapter,
)
from roar.execution.framework.runtime_imports import RuntimeImportController


def _fake_backend(calls: list[str]) -> ExecutionBackend:
    return ExecutionBackend(
        name="fake",
        priority=10,
        matches_command=lambda _command: False,
        plan_command=lambda command: ExecutionCommandPlan(
            backend_name="fake",
            command=list(command),
        ),
        host_execution=HostExecutionAdapter(execute=lambda _ctx: None),  # type: ignore[arg-type]
        distributed=DistributedRuntimeAdapter(
            driver_bootstrap=DriverBootstrapAdapter(
                build_proxy_fragment=lambda *_args, **_kwargs: None,
                local_merge=lambda *_args, **_kwargs: None,
            ),
            worker_bootstrap=WorkerBootstrapAdapter(
                py_executable="roar-worker",
                setup_hook="roar.execution.runtime.worker_bootstrap.startup",
                prepare_runtime_env=lambda runtime_env, _job_id, _environ: dict(runtime_env or {}),
                startup=lambda: None,
                run_entrypoint=lambda _argv: None,
            ),
            runtime_import=RuntimeImportAdapter(
                module_prefixes=("fake",),
                initialize_process=lambda: calls.append("initialize"),
                observe_import=lambda module_name, module: calls.append(f"observe:{module_name}"),
                patch_module=lambda module_name, module: calls.append(f"patch:{module_name}"),
            ),
        ),
    )


def test_handle_import_initializes_observes_and_patches_matched_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.execution.framework.runtime_imports as runtime_imports

    calls: list[str] = []
    fake_backend = _fake_backend(calls)
    controller = RuntimeImportController({})

    monkeypatch.setattr(
        runtime_imports,
        "match_execution_backend_for_module",
        lambda module_name: fake_backend if module_name == "json" else None,
    )
    monkeypatch.setattr(
        runtime_imports,
        "get_execution_backend",
        lambda backend_name: fake_backend if backend_name == "fake" else None,
    )

    module = __import__("json")
    matched_backend = controller.handle_import("json", module)

    assert matched_backend is fake_backend
    assert controller._environ[ROAR_EXECUTION_BACKEND_ENV] == "fake"
    assert calls == ["initialize", "observe:json", "patch:json"]


def test_handle_import_reuses_initialized_backend_for_unrelated_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.execution.framework.runtime_imports as runtime_imports

    calls: list[str] = []
    fake_backend = _fake_backend(calls)
    controller = RuntimeImportController({ROAR_EXECUTION_BACKEND_ENV: "fake"})

    monkeypatch.setattr(runtime_imports, "match_execution_backend_for_module", lambda _name: None)
    monkeypatch.setattr(
        runtime_imports,
        "get_execution_backend",
        lambda backend_name: fake_backend if backend_name == "fake" else None,
    )

    controller.handle_import("json", __import__("json"))
    controller.handle_import("os", __import__("os"))

    assert calls.count("initialize") == 1
    assert "observe:json" in calls
    assert calls[-1] == "observe:os"


def test_initialize_selected_backend_initializes_only_configured_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.execution.framework.runtime_imports as runtime_imports

    calls: list[str] = []
    fake_backend = _fake_backend(calls)
    controller = RuntimeImportController({ROAR_EXECUTION_BACKEND_ENV: "fake"})

    monkeypatch.setattr(
        runtime_imports,
        "get_execution_backend",
        lambda backend_name: fake_backend if backend_name == "fake" else None,
    )

    controller.initialize_selected_backend()
    controller.initialize_selected_backend()

    assert calls == ["initialize"]


def test_disable_backend_dispatch_short_circuits_initialize_selected_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.execution.framework.runtime_imports as runtime_imports

    calls: list[str] = []
    fake_backend = _fake_backend(calls)
    controller = RuntimeImportController({ROAR_EXECUTION_BACKEND_ENV: "fake"})

    monkeypatch.setattr(
        runtime_imports,
        "get_execution_backend",
        lambda backend_name: fake_backend if backend_name == "fake" else None,
    )

    controller.disable_backend_dispatch()
    controller.initialize_selected_backend()

    assert calls == []


def test_disable_backend_dispatch_short_circuits_handle_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import roar.execution.framework.runtime_imports as runtime_imports

    calls: list[str] = []
    fake_backend = _fake_backend(calls)
    controller = RuntimeImportController({})

    monkeypatch.setattr(
        runtime_imports,
        "match_execution_backend_for_module",
        lambda module_name: fake_backend if module_name == "json" else None,
    )
    monkeypatch.setattr(
        runtime_imports,
        "get_execution_backend",
        lambda backend_name: fake_backend if backend_name == "fake" else None,
    )

    controller.disable_backend_dispatch()
    result = controller.handle_import("json", __import__("json"))

    assert result is None
    assert calls == []
    assert ROAR_EXECUTION_BACKEND_ENV not in controller._environ
