from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from roar.db.context import create_database_context


def test_recording_skips_duplicate_output_for_existing_job_path(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()

    output_path = repo_root / "out.txt"
    output_path.write_text("hello")

    with create_database_context(roar_dir) as db_ctx:
        job_id, _job_uid = db_ctx.jobs.create(
            command="python train.py",
            timestamp=1.0,
            job_type="run",
        )
        db_ctx.session.execute(
            text("UPDATE jobs SET status = 'completed' WHERE id = :job_id"),
            {"job_id": job_id},
        )

        ray_artifact_id, _created = db_ctx.artifacts.register(
            hashes={"etag": "ray-worker-etag-1"},
            size=5,
            path=str(output_path),
        )
        db_ctx.jobs.add_output(job_id, ray_artifact_id, str(output_path))

        before_artifacts = db_ctx.session.execute(text("SELECT COUNT(*) FROM artifacts")).scalar_one()

        db_ctx.job_recording._register_artifacts(
            job_id=job_id,
            file_paths=[str(output_path)],
            hashes_by_path={str(output_path): {"blake3": "b" * 64}},
            hash_algorithms=["blake3"],
            is_input=False,
        )

        output_count = db_ctx.session.execute(
            text("SELECT COUNT(*) FROM job_outputs WHERE job_id = :job_id AND path = :path"),
            {"job_id": job_id, "path": str(output_path)},
        ).scalar_one()
        after_artifacts = db_ctx.session.execute(text("SELECT COUNT(*) FROM artifacts")).scalar_one()
        status = db_ctx.session.execute(
            text("SELECT status FROM jobs WHERE id = :job_id"),
            {"job_id": job_id},
        ).scalar_one()

    assert output_count == 1
    assert after_artifacts == before_artifacts
    assert status == "completed"


def test_job_create_uses_explicit_job_uid_when_present(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()

    with create_database_context(roar_dir) as db_ctx:
        _job_id, job_uid = db_ctx.jobs.create(
            command="python train.py",
            timestamp=1.0,
            job_uid="driver-job-uid-1234",
            job_type="run",
        )

    assert job_uid == "driver-job-uid-1234"
