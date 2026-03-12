from __future__ import annotations

import types

from roar.execution.framework.contract import (
    DistributedExecutionBackend,
    DriverBootstrapAdapter,
    FragmentReconstitutionAdapter,
    SubmitCommandRewrite,
    WorkerBootstrapAdapter,
)
from roar.execution.framework.submit import maybe_rewrite_submit_command


def _backend(
    *,
    name: str,
    matches_command,
    rewrite_submit_command,
    with_reconstitution: bool = False,
) -> DistributedExecutionBackend:
    reconstitution = None
    if with_reconstitution:
        reconstitution = FragmentReconstitutionAdapter(
            create_reconstituter=lambda *_args, **_kwargs: types.SimpleNamespace(
                reconstitute=lambda: None
            )
        )
    return DistributedExecutionBackend(
        name=name,
        matches_submit_command=matches_command,
        rewrite_submit_command=rewrite_submit_command,
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
        fragment_reconstitution=reconstitution,
    )


def test_maybe_rewrite_submit_command_returns_first_matching_backend(monkeypatch) -> None:
    calls: list[str] = []

    def _never_match(_command: list[str]) -> bool:
        calls.append("never-match")
        return False

    def _noop(_command: list[str]) -> bool:
        calls.append("noop-match")
        return True

    def _rewrite(command: list[str]) -> SubmitCommandRewrite:
        calls.append("rewrite")
        return SubmitCommandRewrite(command=["rewritten", *command], session_id="session-123")

    monkeypatch.setattr(
        "roar.execution.framework.registry.iter_execution_backends",
        lambda: (
            _backend(
                name="never",
                matches_command=_never_match,
                rewrite_submit_command=lambda command: SubmitCommandRewrite(
                    command=["bad", *command]
                ),
            ),
            _backend(
                name="match",
                matches_command=_noop,
                rewrite_submit_command=_rewrite,
            ),
        ),
    )

    rewritten = maybe_rewrite_submit_command(["ray", "job", "submit"])

    assert calls == ["never-match", "noop-match", "rewrite"]
    assert rewritten.command == ["rewritten", "ray", "job", "submit"]
    assert rewritten.session_id == "session-123"


def test_maybe_rewrite_submit_command_returns_original_when_no_rewriter_matches(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "roar.execution.framework.registry.iter_execution_backends",
        lambda: (
            _backend(
                name="none",
                matches_command=lambda _command: False,
                rewrite_submit_command=lambda command: SubmitCommandRewrite(
                    command=["bad", *command]
                ),
            ),
        ),
    )

    rewritten = maybe_rewrite_submit_command(["python", "main.py"])

    assert rewritten.command == ["python", "main.py"]
    assert rewritten.session_id is None


def test_maybe_rewrite_submit_command_accepts_finalizer_without_command_change(
    monkeypatch,
) -> None:
    def finalizer(_ctx) -> None:
        return None

    monkeypatch.setattr(
        "roar.execution.framework.registry.iter_execution_backends",
        lambda: (
            _backend(
                name="finalizer",
                matches_command=lambda _command: True,
                rewrite_submit_command=lambda command: SubmitCommandRewrite(
                    command=command,
                    finalize_run=finalizer,
                ),
            ),
        ),
    )

    rewritten = maybe_rewrite_submit_command(["python", "main.py"])

    assert rewritten.command == ["python", "main.py"]
    assert rewritten.finalize_run is finalizer


def test_execution_backends_registers_ray_backend() -> None:
    from roar.execution.framework.registry import (
        iter_execution_backends,
        match_execution_backend_for_module,
    )

    backends = iter_execution_backends()

    assert any(backend.name == "ray" for backend in backends)
    assert match_execution_backend_for_module("ray") is not None
    assert match_execution_backend_for_module("ray.data").name == "ray"
    assert match_execution_backend_for_module("numpy") is None


def test_iter_execution_backends_loads_builtin_modules_once(monkeypatch) -> None:
    import roar.execution.framework.registry as module

    fake_backend = _backend(
        name="fake",
        matches_command=lambda _command: True,
        rewrite_submit_command=lambda command: SubmitCommandRewrite(command=["fake", *command]),
    )
    imports: list[str] = []

    monkeypatch.setattr(module, "_registered_execution_backends", [])
    monkeypatch.setattr(module, "_execution_backends_discovered", False)
    monkeypatch.setattr(
        module,
        "_BUILTIN_EXECUTION_BACKEND_MODULES",
        ("roar.backends.fake.plugin",),
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
        rewrite_submit_command=lambda command: SubmitCommandRewrite(command=["plugin", *command]),
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
