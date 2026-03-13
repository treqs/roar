from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from roar.execution.fragments import reconstitution as submit_finalize
from roar.execution.framework.contract import (
    DistributedRuntimeAdapter,
    DriverBootstrapAdapter,
    ExecutionBackend,
    ExecutionCommandPlan,
    FragmentReconstitutionAdapter,
    HostExecutionAdapter,
    WorkerBootstrapAdapter,
)


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(roar_dir=Path("/tmp/repo/.roar"))


def test_build_ray_submit_finalizer_reconstitutes_lineage(monkeypatch) -> None:
    finalizer = submit_finalize.build_submit_finalizer("ray", "session-123")
    echo = MagicMock()
    reconstituter = MagicMock()
    reconstituter.reconstitute.return_value = SimpleNamespace(jobs_merged=2, artifacts_merged=3)

    monkeypatch.setattr(submit_finalize, "get_glaas_url", lambda: "http://localhost:3001")
    monkeypatch.setattr(
        submit_finalize,
        "load_fragment_session",
        lambda *_args: {"token": "ab" * 32},
    )
    monkeypatch.setattr(
        submit_finalize,
        "get_execution_backend",
        lambda _name: ExecutionBackend(
            name="ray",
            matches_command=lambda _command: False,
            plan_command=lambda command: ExecutionCommandPlan(
                backend_name="ray",
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
                    prepare_runtime_env=lambda runtime_env, _job_id, _env: dict(runtime_env or {}),
                    startup=lambda: None,
                    run_entrypoint=lambda _argv: None,
                ),
                fragment_reconstitution=FragmentReconstitutionAdapter(
                    create_reconstituter=MagicMock(return_value=reconstituter)
                ),
            ),
        ),
    )
    monkeypatch.setattr(submit_finalize.click, "echo", echo)

    finalizer(_ctx())

    reconstituter.reconstitute.assert_called_once_with()
    echo.assert_called_once_with("[roar] lineage reconstituted: 2 jobs, 3 artifacts")


def test_build_ray_submit_finalizer_warns_when_key_loading_fails(monkeypatch) -> None:
    finalizer = submit_finalize.build_submit_finalizer("ray", "session-123")
    echo = MagicMock()

    monkeypatch.setattr(submit_finalize, "get_glaas_url", lambda: "http://localhost:3001")
    monkeypatch.setattr(
        submit_finalize,
        "load_fragment_session",
        lambda *_args: (_ for _ in ()).throw(ValueError("missing key")),
    )
    monkeypatch.setattr(submit_finalize.click, "echo", echo)

    finalizer(_ctx())

    echo.assert_called_once()
    assert (
        "failed to load fragment session for session session-123: missing key"
        in echo.call_args.args[0]
    )
    assert echo.call_args.kwargs["err"] is True
