from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from roar.db.context import create_database_context
from roar.db.query_context import create_query_database_context

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_query_database_context_imports_without_sqlalchemy() -> None:
    env = os.environ.copy()
    pythonpath_entries = [str(REPO_ROOT)]
    existing = env.get("PYTHONPATH")
    if existing:
        pythonpath_entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from pathlib import Path

from roar.db.query_context import create_query_database_context

assert "sqlalchemy" not in sys.modules
_ctx = create_query_database_context(Path(".roar"))
assert "sqlalchemy" not in sys.modules
print("ok")
""",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_query_database_context_reads_hot_query_data(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()

    input_path = tmp_path / "input.txt"
    output_path = tmp_path / "output.txt"
    input_path.write_text("hello\n", encoding="utf-8")
    output_path.write_text("world\n", encoding="utf-8")

    with create_database_context(roar_dir) as db_ctx:
        session_id = db_ctx.sessions.create(
            git_repo="/repo",
            git_commit="abc123",
            make_active=True,
        )
        input_artifact_id, _ = db_ctx.artifacts.register(
            {"blake3": "1" * 64},
            size=input_path.stat().st_size,
            path=str(input_path),
        )
        output_artifact_id, _ = db_ctx.artifacts.register(
            {"blake3": "a" * 64, "sha256": "b" * 64},
            size=output_path.stat().st_size,
            path=str(output_path),
            metadata='{"dataset":{"dataset_id":"demo"}}',
        )
        job_id, job_uid = db_ctx.jobs.create(
            "python train.py",
            1700000000.0,
            session_id=session_id,
            step_number=1,
            duration_seconds=1.5,
            exit_code=0,
            metadata='{"epoch":1}',
            telemetry='{"backend":"local"}',
        )
        db_ctx.jobs.add_input(job_id, input_artifact_id, str(input_path))
        db_ctx.jobs.add_output(job_id, output_artifact_id, str(output_path))
        db_ctx.labels.create_version("dag", {"owner": "alice"}, session_id=session_id)
        db_ctx.labels.create_version("job", {"stage": "train"}, job_id=job_id)
        db_ctx.labels.create_version("artifact", {"split": "train"}, artifact_id=output_artifact_id)

    with create_query_database_context(roar_dir) as query_db:
        active_session = query_db.sessions.get_active()
        assert active_session is not None
        assert active_session["id"] == session_id
        assert active_session["git_commit_start"] == "abc123"

        session_steps = query_db.sessions.get_steps(session_id)
        assert [job["job_uid"] for job in session_steps] == [job_uid]

        jobs = query_db.jobs.get_by_session(session_id, limit=10)
        assert [job["job_uid"] for job in jobs] == [job_uid]

        resolved_job = query_db.jobs.get_by_uid(job_uid[:6])
        assert resolved_job is not None
        assert resolved_job["id"] == job_id
        assert resolved_job["metadata"] == '{"epoch":1}'
        assert resolved_job["telemetry"] == '{"backend":"local"}'

        inputs = query_db.jobs.get_inputs(job_id)
        assert len(inputs) == 1
        assert inputs[0]["path"] == str(input_path)
        assert inputs[0]["artifact_hash"] == "1" * 64

        outputs = query_db.jobs.get_outputs(job_id)
        assert len(outputs) == 1
        assert outputs[0]["path"] == str(output_path)
        assert outputs[0]["artifact_hash"] == "a" * 64

        distinct_outputs = query_db.jobs.get_distinct_outputs_by_session(session_id)
        assert len(distinct_outputs) == 1
        assert distinct_outputs[0]["artifact_id"] == output_artifact_id

        artifact = query_db.artifacts.get_by_hash("a" * 8)
        assert artifact is not None
        assert artifact["id"] == output_artifact_id
        assert artifact["metadata"] == '{"dataset":{"dataset_id":"demo"}}'

        by_path = query_db.artifacts.get_by_path(str(output_path))
        assert by_path is not None
        assert by_path["id"] == output_artifact_id

        locations = query_db.artifacts.get_locations(output_artifact_id)
        assert locations == [{"path": str(output_path)}]

        related_jobs = query_db.artifacts.get_jobs(output_artifact_id)
        assert [job["job_uid"] for job in related_jobs["produced_by"]] == [job_uid]
        assert related_jobs["consumed_by"] == []

        dag_label = query_db.labels.get_current("dag", session_id=session_id)
        assert dag_label is not None
        assert dag_label["metadata"] == {"owner": "alice"}

        job_label = query_db.labels.get_current("job", job_id=job_id)
        assert job_label is not None
        assert job_label["metadata"] == {"stage": "train"}

        artifact_label = query_db.labels.get_current("artifact", artifact_id=output_artifact_id)
        assert artifact_label is not None
        assert artifact_label["metadata"] == {"split": "train"}


def test_step_lookup_prioritizes_host_over_task_phase_and_noise(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()

    with create_database_context(roar_dir) as db_ctx:
        session_id = db_ctx.sessions.create(make_active=True)
        _noise_id, _ = db_ctx.jobs.create(
            "ray_task:shutdown",
            1.0,
            session_id=session_id,
            step_number=7,
            execution_backend="ray",
            execution_role="noise",
            job_type="ray_task",
        )
        _task_id, _ = db_ctx.jobs.create(
            "ray_task:train",
            2.0,
            session_id=session_id,
            step_number=7,
            execution_backend="ray",
            execution_role="task",
            job_type="ray_task",
        )
        _phase_id, _ = db_ctx.jobs.create(
            "ray_task:phase_train",
            3.0,
            session_id=session_id,
            step_number=7,
            execution_backend="ray",
            execution_role="phase",
            job_type="ray_task",
        )
        host_id, host_uid = db_ctx.jobs.create(
            "python train.py",
            4.0,
            session_id=session_id,
            step_number=7,
            execution_backend="local",
            execution_role="host",
            job_type=None,
        )
        host_step = db_ctx.sessions.get_step_by_number(session_id, 7)

    assert host_step is not None
    assert host_step["id"] == host_id
    assert host_step["job_uid"] == host_uid

    with create_query_database_context(roar_dir) as query_db:
        host_step_query = query_db.sessions.get_step_by_number(session_id, 7)

    assert host_step_query is not None
    assert host_step_query["id"] == host_id
    assert host_step_query["job_uid"] == host_uid


def test_step_lookup_uses_legacy_ray_command_fallback_when_role_missing(tmp_path: Path) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()

    with create_database_context(roar_dir) as db_ctx:
        session_id = db_ctx.sessions.create(make_active=True)
        _task_id, _ = db_ctx.jobs.create(
            "ray_task:process",
            1.0,
            session_id=session_id,
            step_number=9,
            execution_backend="ray",
            execution_role=None,
            job_type="ray_task",
        )
        _noise_id, _ = db_ctx.jobs.create(
            "ray_task:shutdown",
            2.0,
            session_id=session_id,
            step_number=9,
            execution_backend="ray",
            execution_role=None,
            job_type="ray_task",
        )
        driver_id, driver_uid = db_ctx.jobs.create(
            "python driver.py",
            3.0,
            session_id=session_id,
            step_number=9,
            execution_backend=None,
            execution_role=None,
            job_type=None,
        )
        driver_step = db_ctx.sessions.get_step_by_number(session_id, 9)

    assert driver_step is not None
    assert driver_step["id"] == driver_id
    assert driver_step["job_uid"] == driver_uid

    with create_query_database_context(roar_dir) as query_db:
        driver_step_query = query_db.sessions.get_step_by_number(session_id, 9)

    assert driver_step_query is not None
    assert driver_step_query["id"] == driver_id
    assert driver_step_query["job_uid"] == driver_uid
