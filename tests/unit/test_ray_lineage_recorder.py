"""Unit tests for distributed Ray lineage ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from roar.core.models.run import RunContext
from roar.db.context import create_database_context
from roar.services.execution.ray_lineage import RayLineageRecorder


@dataclass(frozen=True)
class _Result:
    exit_code: int
    duration: float


def _make_ctx(tmp_path: Path) -> RunContext:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()
    return RunContext(
        roar_dir=roar_dir,
        repo_root=str(tmp_path),
        command=["python", "workflow.py"],
        execution_backend="ray",
        hash_algorithms=["blake3"],
    )


def _blake3_digest(item: dict) -> str | None:
    for hash_item in item.get("hashes", []):
        if hash_item.get("algorithm") == "blake3":
            return hash_item.get("digest")
    return None


def test_record_persists_task_dag_with_ref_edges(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    recorder = RayLineageRecorder()

    source = tmp_path / "input.txt"
    source.write_text("distributed-input")
    derived = tmp_path / "features.txt"
    derived.write_text("feature-bytes")

    events = [
        {
            "task_name": "pipeline.load_data",
            "start_time": 10.0,
            "end_time": 11.0,
            "exit_code": 0,
            "inputs": [{"path": str(source)}],
            "outputs": [{"path": str(derived)}],
            "input_refs": [],
            "output_refs": ["ref:features"],
        },
        {
            "task_name": "pipeline.train",
            "start_time": 12.0,
            "end_time": 13.0,
            "exit_code": 0,
            "inputs": [{"path": str(derived)}],
            "outputs": [],
            "input_refs": ["ref:features"],
            "output_refs": ["ref:model"],
        },
    ]

    job_id, _job_uid, inputs, outputs, *_ = recorder.record(
        ctx=ctx,
        events=events,
        execution_result=_Result(exit_code=0, duration=3.0),
        start_time=9.0,
        run_id="run-abc",
    )

    assert job_id > 0
    assert any(item["path"] == "ray://object/ref:features" for item in inputs)
    assert any(item["path"] == "ray://object/ref:model" for item in outputs)

    with create_database_context(ctx.roar_dir) as db_ctx:
        jobs = db_ctx.jobs.get_recent(10)
        commands = [job["command"] for job in jobs]
        assert "ray::pipeline.load_data" in commands
        assert "ray::pipeline.train" in commands

        model_artifact = db_ctx.artifacts.get_by_path("ray://object/ref:model")
        assert model_artifact is not None
        model_hash = _blake3_digest(model_artifact)
        assert model_hash is not None

        lineage_jobs = db_ctx.lineage.get_lineage_jobs([model_hash])
        lineage_commands = {job["command"] for job in lineage_jobs}
        assert "ray::pipeline.load_data" in lineage_commands
        assert "ray::pipeline.train" in lineage_commands


def test_record_without_events_creates_fallback_job(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    recorder = RayLineageRecorder()

    job_id, _job_uid, inputs, outputs, *_ = recorder.record(
        ctx=ctx,
        events=[],
        execution_result=_Result(exit_code=0, duration=1.5),
        start_time=5.0,
        run_id="run-empty",
    )

    assert job_id > 0
    assert inputs == []
    assert outputs == []

    with create_database_context(ctx.roar_dir) as db_ctx:
        job = db_ctx.jobs.get(job_id)
        assert job is not None
        assert job["command"] == "python workflow.py"
