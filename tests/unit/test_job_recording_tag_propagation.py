"""Integration test: JobRecordingService.record_job propagates tags end-to-end.

Exercises the real hook point in roar/db/services/job_recording.py against a
real SQLite-backed DatabaseContext, rather than mocking propagate_tags, so a
regression in the wiring (wrong repo, wrong artifact ids, wrong timing
relative to commit) would actually fail this test.
"""

from __future__ import annotations

from pathlib import Path

from roar.core.label_origins import LABEL_ORIGIN_SYSTEM, LABEL_ORIGIN_USER
from roar.db.context import create_database_context


def _write(path: Path, content: bytes) -> str:
    path.write_bytes(content)
    return str(path)


def test_output_inherits_tags_from_input(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir(parents=True)

    dataset = _write(repo_root / "dataset.csv", b"a,b,c\n1,2,3\n")
    model = _write(repo_root / "model.bin", b"weights")

    with create_database_context(roar_dir) as db_ctx:
        # Job 1: produces the dataset (no inputs).
        db_ctx.job_recording.record_job(
            command="prepare_data.py",
            timestamp=1_700_000_000.0,
            output_files=[dataset],
            assign_to_session=False,
        )

    # Tag the dataset artifact directly (simulating a prior `roar tag add`).
    with create_database_context(roar_dir) as db_ctx:
        job1 = db_ctx.jobs.get_recent(1)[0]
        dataset_artifact_id = db_ctx.jobs.get_outputs(job1["id"])[0]["artifact_id"]
        db_ctx.labels.create_version(
            "artifact",
            {"tag": {"license": ["MIT"]}},
            artifact_id=dataset_artifact_id,
            write_origin=LABEL_ORIGIN_USER,
        )

    # Job 2: consumes the dataset, produces a model.
    with create_database_context(roar_dir) as db_ctx:
        job2_id, _job2_uid = db_ctx.job_recording.record_job(
            command="train.py",
            timestamp=1_700_000_100.0,
            input_files=[dataset],
            output_files=[model],
            assign_to_session=False,
        )
        model_artifact_id = db_ctx.jobs.get_outputs(job2_id)[0]["artifact_id"]

    with create_database_context(roar_dir) as db_ctx:
        current = db_ctx.labels.get_current("artifact", artifact_id=model_artifact_id)
        assert current is not None
        assert current["metadata"]["tag"] == {"license": ["MIT"]}
        assert current["write_origin"] == LABEL_ORIGIN_SYSTEM


def test_no_inputs_means_no_propagation(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir(parents=True)
    model = _write(repo_root / "model.bin", b"weights")

    with create_database_context(roar_dir) as db_ctx:
        job_id, _job_uid = db_ctx.job_recording.record_job(
            command="train.py",
            timestamp=1_700_000_000.0,
            output_files=[model],
            assign_to_session=False,
        )
        artifact_id = db_ctx.jobs.get_outputs(job_id)[0]["artifact_id"]

    with create_database_context(roar_dir) as db_ctx:
        current = db_ctx.labels.get_current("artifact", artifact_id=artifact_id)
        assert current is None
