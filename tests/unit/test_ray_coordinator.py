"""Unit tests for RunCoordinator Ray backend orchestration."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from roar.services.execution.coordinator import RunCoordinator


def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.command = ["python", "workflow.py"]
    ctx.job_type = None
    ctx.repo_root = "/tmp/repo"
    ctx.roar_dir = Path("/tmp/repo/.roar")
    ctx.hash_algorithms = ["blake3"]
    ctx.tracer_mode = None
    ctx.tracer_fallback = None
    ctx.execution_backend = "ray"
    ctx.ray_address = "127.0.0.1:6379"
    ctx.ray_namespace = "test-ray"
    ctx.step_name = None
    ctx.git_repo = None
    ctx.git_commit = None
    ctx.git_branch = None
    return ctx


def test_ray_backend_uses_distributed_path_and_recorder() -> None:
    ctx = _make_ctx()
    tracer = MagicMock()
    ray_recorder = MagicMock()
    ray_recorder.record.return_value = (9, "abcd1234", [], [], [], [])
    coordinator = RunCoordinator(
        tracer_service=tracer,
        proxy_service=None,
        ray_lineage_recorder=ray_recorder,
    )

    with (
        patch("roar.ray.is_ray_available", return_value=True),
        patch("roar.ray.create_lineage_actor", return_value=(True, None)),
        patch("roar.ray.fetch_lineage_events", return_value=([{"task_name": "x"}], None)),
        patch("roar.ray.destroy_lineage_actor", return_value=(True, None)),
        patch.object(
            coordinator,
            "_execute_direct_command",
            return_value=SimpleNamespace(exit_code=0, duration=1.2, interrupted=False),
        ) as mock_direct,
        patch.object(coordinator, "_backup_previous_outputs"),
    ):
        result = coordinator.execute(ctx)

    assert result.exit_code == 0
    assert result.job_id == 9
    tracer.execute.assert_not_called()
    ray_recorder.record.assert_called_once()

    extra_env = mock_direct.call_args.kwargs["extra_env"]
    assert extra_env["ROAR_DISTRIBUTED_BACKEND"] == "ray"
    assert extra_env["ROAR_RAY_LINEAGE_ACTOR"].startswith("roar-lineage-")
    assert extra_env["ROAR_RAY_NAMESPACE"] == "test-ray"
    assert extra_env["ROAR_RAY_ADDRESS"] == "127.0.0.1:6379"
    assert extra_env["ROAR_RAY_RUN_ID"]


def test_ray_backend_requires_ray_dependency() -> None:
    ctx = _make_ctx()
    ray_recorder = MagicMock()
    coordinator = RunCoordinator(proxy_service=None, ray_lineage_recorder=ray_recorder)

    with (
        patch("roar.ray.is_ray_available", return_value=False),
        patch.object(coordinator, "_backup_previous_outputs"),
    ):
        result = coordinator.execute(ctx)

    assert result.exit_code == 1
    assert result.job_id == 0
    ray_recorder.record.assert_not_called()
