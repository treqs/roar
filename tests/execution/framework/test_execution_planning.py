from __future__ import annotations

import importlib
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
    plan_command,
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
                setup_hook="roar.execution.runtime.worker_bootstrap.startup",
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
        plan_command=plan_command,
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
                plan_command=lambda command: ExecutionCommandPlan(
                    backend_name="ray",
                    command=["rewritten", *command],
                    execution_role="submit",
                    session_id="session-123",
                ),
                with_reconstitution=True,
            ),
            _backend(
                name="local",
                priority=-100,
                matches_command=_fallback_match,
                plan_command=lambda command: ExecutionCommandPlan(
                    backend_name="local",
                    command=list(command),
                    execution_role="host",
                ),
            ),
        ),
    )

    planned = plan_execution_command(["ray", "job", "submit"])

    assert calls == ["ray-match"]
    assert planned.backend_name == "ray"
    assert planned.command == ["rewritten", "ray", "job", "submit"]
    assert planned.execution_role == "submit"
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
                plan_command=lambda command: ExecutionCommandPlan(
                    backend_name="ray",
                    command=["bad", *command],
                    execution_role="submit",
                ),
            ),
            _backend(
                name="local",
                priority=-100,
                matches_command=lambda _command: True,
                plan_command=lambda command: ExecutionCommandPlan(
                    backend_name="local",
                    command=list(command),
                    execution_role="host",
                ),
            ),
        ),
    )

    planned = plan_execution_command(["python", "main.py"])

    assert planned.backend_name == "local"
    assert planned.command == ["python", "main.py"]
    assert planned.execution_role == "host"
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
                plan_command=lambda command: ExecutionCommandPlan(
                    backend_name="local",
                    command=command,
                    execution_role="host",
                    finalize_run=finalizer,
                ),
            ),
        ),
    )

    planned = plan_execution_command(["python", "main.py"])

    assert planned.backend_name == "local"
    assert planned.command == ["python", "main.py"]
    assert planned.execution_role == "host"
    assert planned.finalize_run is finalizer


def test_execution_backends_register_local_ray_and_osmo_backends() -> None:
    from roar.execution.framework.registry import (
        is_distributed_submission_command,
        is_execution_backend_job_environment,
        is_execution_noise_command,
        is_execution_noise_job,
        is_execution_phase_job,
        is_execution_submit_job,
        is_execution_task_command,
        is_execution_task_job,
        iter_execution_backends,
        match_execution_backend_for_module,
    )

    backends = iter_execution_backends()
    backend_names = [backend.name for backend in backends]

    assert "local" in backend_names
    assert "ray" in backend_names
    assert "osmo" in backend_names
    assert backend_names.index("ray") < backend_names.index("local")
    assert backend_names.index("osmo") < backend_names.index("local")
    assert match_execution_backend_for_module("ray") is not None
    assert match_execution_backend_for_module("ray.data").name == "ray"
    assert match_execution_backend_for_module("numpy") is None
    assert is_execution_backend_job_environment({"RAY_JOB_ID": "job-123"})
    assert is_distributed_submission_command("ray job submit -- python main.py")
    assert not is_distributed_submission_command("python main.py")
    assert is_execution_noise_command("ray_task:s3_proxy")
    assert is_execution_task_command("ray_task:my_task")
    assert not is_execution_noise_command("python train.py")
    assert is_execution_submit_job({"execution_backend": "ray", "execution_role": "submit"})
    assert is_execution_task_job({"execution_backend": "ray", "execution_role": "task"})
    assert is_execution_phase_job({"execution_backend": "ray", "execution_role": "phase"})
    assert is_execution_noise_job({"execution_backend": "ray", "execution_role": "noise"})


def test_execution_policy_helpers_use_registered_backend_policy(monkeypatch) -> None:
    import roar.execution.framework.registry as module

    fake_backend = _backend(
        name="fake",
        matches_command=lambda command: command[:2] == ["fake", "submit"],
        plan_command=lambda command: ExecutionCommandPlan(
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
            submit_roles=("submit",),
            task_roles=("task",),
            noise_roles=("noise",),
        ),
    )

    monkeypatch.setattr(module, "_registered_execution_backends", [fake_backend])
    monkeypatch.setattr(module, "_execution_backends_discovered", True)

    assert module.is_execution_backend_job_environment({"FAKE_JOB_ID": "job-123"})
    assert module.is_distributed_submission_command(["fake", "submit", "--", "python", "main.py"])
    assert module.is_execution_task_command("fake_task:train")
    assert module.is_execution_submit_job({"execution_backend": "fake", "execution_role": "submit"})
    assert module.is_execution_task_job({"execution_backend": "fake", "execution_role": "task"})
    assert module.is_execution_noise_job({"execution_backend": "fake", "execution_role": "noise"})


def test_job_environment_detection_does_not_trigger_backend_discovery(monkeypatch) -> None:
    """``is_execution_backend_job_environment`` runs on every roar
    invocation (called from telemetry suppression). It must answer from
    the static built-in marker set without importing any backend plugin
    module — otherwise we pay ~300ms per invocation to enumerate
    env-var names that are stable and few.
    """
    import roar.execution.framework.registry as module

    monkeypatch.setattr(module, "_registered_execution_backends", [])
    monkeypatch.setattr(module, "_execution_backends_discovered", False)
    imports: list[str] = []

    real_import = module.importlib.import_module

    def _tracking_import_module(name: str):
        imports.append(name)
        return real_import(name)

    monkeypatch.setattr(module.importlib, "import_module", _tracking_import_module)

    # Negative case (no markers set) — must not discover backends.
    assert module.is_execution_backend_job_environment({}) is False
    # Positive case via a built-in static marker — must not discover either.
    assert module.is_execution_backend_job_environment({"RAY_JOB_ID": "job-1"}) is True
    # Positive case via the explicit override env var.
    assert module.is_execution_backend_job_environment({"ROAR_JOB_INSTRUMENTED": "1"}) is True

    assert imports == [], f"backend discovery should not run, got imports: {imports}"
    assert module._execution_backends_discovered is False


def test_builtin_job_environment_markers_match_backend_policies() -> None:
    """Single-source-of-truth invariant: the static marker set used by
    the fast path must equal the union of ``job_environment_markers``
    declared by every built-in backend. If a developer adds a marker to
    a built-in backend's policy without updating the static set, this
    test fails before the slow path silently kicks in for that marker.
    """
    from roar.execution.framework.registry import (
        _BUILTIN_EXECUTION_BACKEND_MODULES,
        _BUILTIN_JOB_ENVIRONMENT_MARKERS,
    )

    policy_markers: set[str] = set()
    for module_name in _BUILTIN_EXECUTION_BACKEND_MODULES:
        mod = importlib.import_module(module_name)
        for attr in vars(mod).values():
            policy = getattr(attr, "policy", None)
            markers = getattr(policy, "job_environment_markers", None)
            if markers:
                policy_markers.update(str(m) for m in markers if m)

    assert policy_markers == set(_BUILTIN_JOB_ENVIRONMENT_MARKERS), (
        f"builtin markers ({sorted(_BUILTIN_JOB_ENVIRONMENT_MARKERS)}) must match the "
        f"union of policy.job_environment_markers across builtin backends "
        f"({sorted(policy_markers)}). Update _BUILTIN_JOB_ENVIRONMENT_MARKERS in "
        f"roar/execution/framework/registry.py."
    )


def test_builtin_backend_config_sections_match_backend_adapters() -> None:
    """Same invariant for ``BackendConfigAdapter.section_name``. The
    static set drives ``config_get``'s fast path: keys outside both
    typed sections and these names short-circuit before triggering
    backend discovery.
    """
    from roar.execution.framework.registry import (
        _BUILTIN_BACKEND_CONFIG_SECTIONS,
        _BUILTIN_EXECUTION_BACKEND_MODULES,
    )

    adapter_sections: set[str] = set()
    for module_name in _BUILTIN_EXECUTION_BACKEND_MODULES:
        mod = importlib.import_module(module_name)
        for attr in vars(mod).values():
            config_adapter = getattr(attr, "config", None)
            section = getattr(config_adapter, "section_name", None)
            if section:
                adapter_sections.add(str(section))

    assert adapter_sections == set(_BUILTIN_BACKEND_CONFIG_SECTIONS), (
        f"builtin section names ({sorted(_BUILTIN_BACKEND_CONFIG_SECTIONS)}) must match "
        f"the union of config.section_name across builtin backends "
        f"({sorted(adapter_sections)}). Update _BUILTIN_BACKEND_CONFIG_SECTIONS in "
        f"roar/execution/framework/registry.py."
    )


def test_iter_execution_backends_loads_builtin_modules_once(monkeypatch) -> None:
    import roar.execution.framework.registry as module

    fake_backend = _backend(
        name="fake",
        matches_command=lambda _command: True,
        plan_command=lambda command: ExecutionCommandPlan(
            backend_name="fake",
            command=["fake", *command],
            execution_role="host",
        ),
    )
    imports: list[str] = []

    monkeypatch.setattr(module, "_registered_execution_backends", [])
    monkeypatch.setattr(module, "_execution_backends_discovered", False)
    monkeypatch.setattr(
        module, "_BUILTIN_EXECUTION_BACKEND_MODULES", ("roar.backends.fake.plugin",)
    )
    monkeypatch.setattr(module, "_iter_execution_backend_entrypoints", lambda: ())

    def _fake_import_module(name: str):
        imports.append(name)
        return types.SimpleNamespace(register=lambda: fake_backend)

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
        plan_command=lambda command: ExecutionCommandPlan(
            backend_name="plugin",
            command=["plugin", *command],
            execution_role="host",
        ),
    )
    loads: list[str] = []

    monkeypatch.setattr(module, "_registered_execution_backends", [])
    monkeypatch.setattr(module, "_execution_backends_discovered", False)
    monkeypatch.setattr(module, "_BUILTIN_EXECUTION_BACKEND_MODULES", ())

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
