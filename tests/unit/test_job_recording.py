from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from roar.db.context import create_database_context as _create_database_context
from roar.execution.cluster.proxy import S3LogEntry
from roar.execution.recording import (
    ExecutionJobRecorder,
    LocalJobRecorder,
    LocalRecordedArtifact,
)


def test_record_registers_proxy_outputs_before_get_outputs(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()

    call_order: list[str] = []

    @contextmanager
    def instrumented_create_database_context(path: Path):
        with _create_database_context(path) as db_ctx:
            original_add_output = db_ctx.jobs.add_output
            original_get_outputs = db_ctx.jobs.get_outputs

            def add_output(*args, **kwargs):
                call_order.append("add_output")
                return original_add_output(*args, **kwargs)

            def get_outputs(*args, **kwargs):
                call_order.append("get_outputs")
                return original_get_outputs(*args, **kwargs)

            db_ctx.jobs.add_output = add_output  # type: ignore[method-assign]
            db_ctx.jobs.get_outputs = get_outputs  # type: ignore[method-assign]
            yield db_ctx

    recorder = ExecutionJobRecorder(telemetry_collector=lambda *_args, **_kwargs: None)
    ctx = SimpleNamespace(
        repo_root=str(repo_root),
        roar_dir=roar_dir,
        command=["python", "train.py"],
        execution_backend="local",
        execution_role="host",
        step_name="train",
        job_type="run",
        hash_algorithms=["blake3"],
        block_tags=[],
        add_tags=[],
        wandb_to_trackio=False,
    )
    prov = {
        "data": {"read_files": [], "written_files": []},
        "executables": {"code": {"git": {}}},
    }
    tracer_result = SimpleNamespace(duration=0.1, exit_code=0)
    s3_entries = [
        S3LogEntry(
            operation="PutObject",
            bucket="my-bucket",
            key="outputs/model.bin",
            etag="abc123",
            size_bytes=128,
        )
    ]

    with patch("roar.db.context.create_database_context", instrumented_create_database_context):
        (
            _job_id,
            _job_uid,
            _read_file_info,
            written_file_info,
            _stale_upstream,
            _stale_downstream,
            _dag_stats,
        ) = recorder.record(
            ctx=ctx,
            prov=prov,
            tracer_result=tracer_result,
            start_time=1700000000.0,
            is_build=False,
            s3_entries=s3_entries,
        )

    assert "add_output" in call_order
    assert "get_outputs" in call_order
    assert call_order.index("add_output") < call_order.index("get_outputs")
    assert any(item["path"] == "s3://my-bucket/outputs/model.bin" for item in written_file_info)


def test_local_job_recorder_records_precomputed_output_artifacts(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()
    local_file = repo_root / "downloaded.bin"
    local_file.write_bytes(b"payload")

    with _create_database_context(roar_dir) as db_ctx:
        session_id = db_ctx.sessions.create(make_active=True)
        recorder = LocalJobRecorder()
        job_id, job_uid = recorder.record(
            db_ctx,
            command="roar get s3://bucket/downloaded.bin",
            timestamp=1700000000.0,
            metadata='{"get":{"source":"s3://bucket/downloaded.bin"}}',
            execution_backend="local",
            execution_role="host",
            job_type="get",
            session_id=session_id,
            output_artifacts=[
                LocalRecordedArtifact(
                    path=str(local_file),
                    hashes={"blake3": "abc123"},
                    size=local_file.stat().st_size,
                )
            ],
            exit_code=0,
        )

        job = db_ctx.jobs.get(job_id)
        outputs = db_ctx.jobs.get_outputs(job_id)

    assert job is not None
    assert job["job_uid"] == job_uid
    assert job["execution_backend"] == "local"
    assert job["execution_role"] == "host"
    assert job["job_type"] == "get"
    assert len(outputs) == 1
    assert outputs[0]["path"] == str(local_file)


def test_local_job_recorder_creates_active_session_when_missing(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()
    local_file = repo_root / "downloaded.bin"
    local_file.write_bytes(b"payload")

    with _create_database_context(roar_dir) as db_ctx:
        recorder = LocalJobRecorder()
        job_id, _job_uid = recorder.record(
            db_ctx,
            command="roar get https://example.com/downloaded.bin",
            timestamp=1700000000.0,
            metadata='{"get":{"source":"https://example.com/downloaded.bin"}}',
            execution_backend="local",
            execution_role="host",
            job_type="get",
            output_artifacts=[
                LocalRecordedArtifact(
                    path=str(local_file),
                    hashes={"blake3": "abc123"},
                    size=local_file.stat().st_size,
                )
            ],
            exit_code=0,
        )

        job = db_ctx.jobs.get(job_id)
        session = db_ctx.sessions.get_active()

    assert job is not None
    assert session is not None
    assert job["session_id"] == session["id"]
    assert session["current_step"] == 1
