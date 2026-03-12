from __future__ import annotations

from pathlib import Path

from roar.core.interfaces.run import RunResult
from roar.services.execution.execution_service import (
    ExecutionRequest,
    ExecutionService,
    GitValidationResult,
)


def test_execution_service_dispatches_through_backend_host_execution(monkeypatch) -> None:
    service = ExecutionService()
    request = ExecutionRequest(
        roar_dir=Path("/tmp/repo/.roar"),
        command=["python", "main.py"],
        execution_backend="local",
    )
    git_info = GitValidationResult(
        is_valid=True,
        repo_root="/tmp/repo",
        commit="abc123",
        branch="main",
        remote_url="git@example.com:repo.git",
    )
    captured: list[object] = []

    class _FakeBackend:
        class host_execution:
            @staticmethod
            def execute(run_ctx):
                captured.append(run_ctx)
                return RunResult(
                    exit_code=0,
                    job_id=1,
                    job_uid="job-123",
                    duration=0.1,
                    inputs=[],
                    outputs=[],
                )

    monkeypatch.setattr(
        "roar.services.execution.execution_service.get_execution_backend",
        lambda name: _FakeBackend() if name == "local" else None,
    )

    result = service.execute(request, git_info=git_info)

    assert result.exit_code == 0
    assert len(captured) == 1
    run_ctx = captured[0]
    assert run_ctx.execution_backend == "local"
    assert run_ctx.repo_root == "/tmp/repo"
