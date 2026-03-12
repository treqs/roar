from __future__ import annotations

import types

from roar.execution.framework.contract import (
    DistributedRuntimeAdapter,
    DriverBootstrapAdapter,
    ExecutionBackend,
    ExecutionCommandPlan,
    ExecutionPolicyAdapter,
    FragmentReconstitutionAdapter,
    HostExecutionAdapter,
    WorkerBootstrapAdapter,
)
from roar.execution.framework.planning import plan_execution_command


def _backend(
    *,
    name: str,
    priority: int = 0,
    matches_command,
    rewrite_command,
    with_reconstitution: bool = False,
    distributed: DistributedRuntimeAdapter | None = None,
    policy: ExecutionPolicyAdapter | None = None,
) -> ExecutionBackend:
    runtime = distributed
    if with_reconstitution:
        runtime = DistributedRuntimeAdapter(
            driver_bootstrap=DriverBootstrapAdapter(
                build_proxy_fragment=lambda *_args, **_kwargs: None,
                local_merge=lambda *_args, **_kwargs: None,
            ),
            worker_bootstrap=WorkerBootstrapAdapter(
                py_executable="roar-worker",
                setup_hook="roar.services.execution.worker_bootstrap.startup",
                prepare_runtime_env=lambda runtime_env, _job_id, _environ: dict(runtime_env or {}),
                startup=lambda: None,
                run_entrypoint=lambda _argv: None,
            ),
            fragment_reconstitution=FragmentReconstitutionAdapter(
                create_reconstituter=lambda *_args, **_kwargs: types.SimpleNamespace(
                    reconstitute=lambda: None
                )
            ),
        )
    return ExecutionBackend(
        name=name,
        priority=priority,
        matches_command=matches_command,
        rewrite_command=rewrite_command,
        host_execution=HostExecutionAdapter(execute=lambda _ctx: None),  # type: ignore[arg-type]
        distributed=runtime,
        policy=policy,
    )


def test_plan_execution_command_returns_first_highest_priority_match(monkeypatch) -> None:
    calls: list[str] = []

    def _fallback_match(_command: list[str]) -> bool:
        calls.append("local-match")
        return True

    def _specific_match(_command: list[str]) -> bool:
        calls.append("ray-match")
        return True

    monkeypatch.setattr(
        "roar.execution.framework.registry.iter_execution_backends",
        lambda: (
            _backend(
                name="ray",
                priority=100,
                matches_command=_specific_match,
                rewrite_command=lambda command: ExecutionCommandPlan(
                    backend_name="ray",
                    command=["rewritten", *command],
                    session_id="session-123",
                ),
                with_reconstitution=True,
            ),
            _backend(
                name="local",
                priority=-100,
                matches_command=_fallback_match,
                rewrite_command=lambda command: ExecutionCommandPlan(
                    backend_name="local",
                    command=list(command),
                ),
            ),
        ),
    )

    planned = plan_execution_command(["ray", "job", "submit"])

    assert calls == ["ray-match"]
    assert planned.backend_name == "ray"
    assert planned.command == ["rewritten", "ray", "job", "submit"]
    assert planned.session_id == "session-123"
    assert planned.finalize_run is not None


def test_plan_execution_command_returns_local_backend_for_plain_commands(monkeypatch) -> None:
    monkeypatch.setattr(
        "roar.execution.framework.registry.iter_execution_backends",
        lambda: (
            _backend(
                name="ray",
                priority=100,
                matches_command=lambda _command: False,
                rewrite_command=lambda command: ExecutionCommandPlan(
                    backend_name="ray",
                    command=["bad", *command],
                ),
            ),
            _backend(
                name="local",
                priority=-100,
                matches_command=lambda _command: True,
                rewrite_command=lambda command: ExecutionCommandPlan(
                    backend_name="local",
                    command=list(command),
                ),
            ),
        ),
    )

    planned = plan_execution_command(["python", "main.py"])

    assert planned.backend_name == "local"
    assert planned.command == ["python", "main.py"]
    assert planned.session_id is None


def test_plan_execution_command_accepts_finalizer_without_command_change(monkeypatch) -> None:
    def finalizer(_ctx) -> None:
        return None

    monkeypatch.setattr(
        "roar.execution.framework.registry.iter_execution_backends",
        lambda: (
            _backend(
                name="local",
                priority=-100,
                matches_command=lambda _command: True,
                rewrite_command=lambda command: ExecutionCommandPlan(
                    backend_name="local",
                    command=command,
                    finalize_run=finalizer,
                ),
            ),
        ),
    )

    planned = plan_execution_command(["python", "main.py"])

    assert planned.backend_name == "local"
    assert planned.command == ["python", "main.py"]
    assert planned.finalize_run is finalizer


def test_execution_backends_register_local_and_ray_backends() -> None:
    from roar.execution.framework.registry import (
        is_execution_backend_job_environment,
        is_execution_noise_command,
        is_execution_submit_command,
        is_execution_task_command,
        iter_execution_backends,
        match_execution_backend_for_module,
    )

    backends = iter_execution_backends()
    backend_names = [backend.name for backend in backends]

    assert "local" in backend_names
    assert "ray" in backend_names
    assert backend_names.index("ray") < backend_names.index("local")
    assert match_execution_backend_for_module("ray") is not None
    assert match_execution_backend_for_module("ray.data").name == "ray"
    assert match_execution_backend_for_module("numpy") is None
    assert is_execution_backend_job_environment({"RAY_JOB_ID": "job-123"})
    assert is_execution_submit_command("ray job submit -- python main.py")
    assert not is_execution_submit_command("python main.py")
    assert is_execution_noise_command("ray_task:s3_proxy")
    assert is_execution_task_command("ray_task:my_task")
    assert not is_execution_noise_command("python train.py")


def test_execution_policy_helpers_use_registered_backend_policy(monkeypatch) -> None:
    import roar.execution.framework.registry as module

    fake_backend = _backend(
        name="fake",
        matches_command=lambda command: command[:2] == ["fake", "submit"],
        rewrite_command=lambda command: ExecutionCommandPlan(
            backend_name="fake",
            command=["fake", *command],
        ),
        distributed=DistributedRuntimeAdapter(
            driver_bootstrap=DriverBootstrapAdapter(
                build_proxy_fragment=lambda *_args, **_kwargs: None,
                local_merge=lambda *_args, **_kwargs: None,
            ),
            worker_bootstrap=WorkerBootstrapAdapter(
                py_executable="fake-worker",
                setup_hook="fake.worker.startup",
                prepare_runtime_env=lambda runtime_env, _job_id, _environ: dict(runtime_env or {}),
                startup=lambda: None,
                run_entrypoint=lambda _argv: None,
            ),
        ),
        policy=ExecutionPolicyAdapter(
            job_environment_markers=("FAKE_JOB_ID",),
            task_command_prefixes=("fake_task:",),
        ),
    )

    monkeypatch.setattr(module, "_registered_execution_backends", [fake_backend])
    monkeypatch.setattr(module, "_execution_backends_discovered", True)

    assert module.is_execution_backend_job_environment({"FAKE_JOB_ID": "job-123"})
    assert module.is_execution_submit_command(["fake", "submit", "--", "python", "main.py"])
    assert module.is_execution_task_command("fake_task:train")


def test_iter_execution_backends_loads_builtin_modules_once(monkeypatch) -> None:
    import roar.execution.framework.registry as module

    fake_backend = _backend(
        name="fake",
        matches_command=lambda _command: True,
        rewrite_command=lambda command: ExecutionCommandPlan(
            backend_name="fake",
            command=["fake", *command],
        ),
    )
    imports: list[str] = []

    monkeypatch.setattr(module, "_registered_execution_backends", [])
    monkeypatch.setattr(module, "_execution_backends_discovered", False)
    monkeypatch.setattr(
        module,
        "_iter_builtin_execution_backend_modules",
        lambda: ("roar.backends.fake.plugin",),
    )
    monkeypatch.setattr(module, "_iter_execution_backend_entrypoints", lambda: ())

    def _fake_import_module(name: str):
        imports.append(name)
        module.register_execution_backend(fake_backend)
        return types.SimpleNamespace()

    monkeypatch.setattr(module.importlib, "import_module", _fake_import_module)

    backends_first = module.iter_execution_backends()
    backends_second = module.iter_execution_backends()

    assert imports == ["roar.backends.fake.plugin"]
    assert backends_first == (fake_backend,)
    assert backends_second == (fake_backend,)


def test_iter_execution_backends_loads_entrypoint_callable_once(monkeypatch) -> None:
    import roar.execution.framework.registry as module

    fake_backend = _backend(
        name="plugin",
        matches_command=lambda _command: True,
        rewrite_command=lambda command: ExecutionCommandPlan(
            backend_name="plugin",
            command=["plugin", *command],
        ),
    )
    loads: list[str] = []

    monkeypatch.setattr(module, "_registered_execution_backends", [])
    monkeypatch.setattr(module, "_execution_backends_discovered", False)
    monkeypatch.setattr(module, "_iter_builtin_execution_backend_modules", lambda: ())

    class _FakeEntryPoint:
        name = "plugin"

        def load(self):
            loads.append("load")

            def _register():
                return fake_backend

            return _register

    monkeypatch.setattr(module, "_iter_execution_backend_entrypoints", lambda: (_FakeEntryPoint(),))

    backends_first = module.iter_execution_backends()
    backends_second = module.iter_execution_backends()

    assert loads == ["load"]
    assert backends_first == (fake_backend,)
    assert backends_second == (fake_backend,)
