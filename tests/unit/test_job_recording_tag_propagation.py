"""Integration test: JobRecordingService.record_job propagates tags end-to-end.

Exercises the real hook point in roar/db/services/job_recording.py against a
real SQLite-backed DatabaseContext, rather than mocking propagate_tags, so a
regression in the wiring (wrong repo, wrong artifact ids, wrong timing
relative to commit, wrong session_id/job_uid threading) would actually fail
this test.

All jobs here use `assign_to_session=False` (no real production caller does
this — it's test-only isolation), so every job's session_id is None. The
scope check treats "both sides unassigned" as in-scope (see
`_value_in_scope`'s docstring), so plain same-session-shaped propagation
still works without bootstrapping a session for every test.
"""

from __future__ import annotations

from pathlib import Path

from roar.core.label_origins import LABEL_ORIGIN_SYSTEM, LABEL_ORIGIN_USER
from roar.db.context import create_database_context


def _write(path: Path, content: bytes) -> str:
    path.write_bytes(content)
    return str(path)


def _tag_doc(**kinds: list[str]) -> dict:
    """A `{"tag": {...}}` doc simulating a prior `roar tag add` for each value —
    user-origin, job-less records, each with its own implicit bind event (the
    "one mechanism, no special cases" rule `TagService.add` implements)."""
    return {
        "tag": {
            **{
                kind: {"values": [{"value": v, "origin": LABEL_ORIGIN_USER} for v in values]}
                for kind, values in kinds.items()
            },
            "bind": {
                "events": [
                    {"action": "bind", "covers": {kind: values}} for kind, values in kinds.items()
                ]
            },
        }
    }


def _values(metadata: dict, kind: str) -> list[str]:
    return [record["value"] for record in metadata["tag"][kind]["values"]]


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
            _tag_doc(license=["MIT"]),
            artifact_id=dataset_artifact_id,
            write_origin=LABEL_ORIGIN_USER,
        )

    # Job 2: consumes the dataset, produces a model.
    with create_database_context(roar_dir) as db_ctx:
        job2_id, job2_uid = db_ctx.job_recording.record_job(
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
        assert _values(current["metadata"], "license") == ["MIT"]
        assert current["write_origin"] == LABEL_ORIGIN_SYSTEM
        # The propagated record is stamped with the job that derived it.
        assert current["metadata"]["tag"]["license"]["values"][0]["job"] == job2_uid


def test_block_tags_exempts_kind_from_propagation(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir(parents=True)

    dataset = _write(repo_root / "dataset.csv", b"a,b,c\n1,2,3\n")
    model = _write(repo_root / "model.bin", b"weights")

    with create_database_context(roar_dir) as db_ctx:
        db_ctx.job_recording.record_job(
            command="prepare_data.py",
            timestamp=1_700_000_000.0,
            output_files=[dataset],
            assign_to_session=False,
        )

    with create_database_context(roar_dir) as db_ctx:
        job1 = db_ctx.jobs.get_recent(1)[0]
        dataset_artifact_id = db_ctx.jobs.get_outputs(job1["id"])[0]["artifact_id"]
        db_ctx.labels.create_version(
            "artifact",
            _tag_doc(license=["GPL-3.0"], jurisdiction=["EU"]),
            artifact_id=dataset_artifact_id,
            write_origin=LABEL_ORIGIN_USER,
        )

    with create_database_context(roar_dir) as db_ctx:
        job2_id, _job2_uid = db_ctx.job_recording.record_job(
            command="relicense.py",
            timestamp=1_700_000_100.0,
            input_files=[dataset],
            output_files=[model],
            assign_to_session=False,
            block_tags=("license",),
        )
        model_artifact_id = db_ctx.jobs.get_outputs(job2_id)[0]["artifact_id"]

    with create_database_context(roar_dir) as db_ctx:
        current = db_ctx.labels.get_current("artifact", artifact_id=model_artifact_id)
        assert current is not None
        assert "license" not in current["metadata"]["tag"]
        assert _values(current["metadata"], "jurisdiction") == ["EU"]


def test_add_tags_stamps_output_with_user_origin(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir(parents=True)
    model = _write(repo_root / "model.bin", b"weights")

    with create_database_context(roar_dir) as db_ctx:
        job_id, job_uid = db_ctx.job_recording.record_job(
            command="train.py",
            timestamp=1_700_000_000.0,
            output_files=[model],
            assign_to_session=False,
            add_tags=("license=MIT", "jurisdiction=EU"),
        )
        artifact_id = db_ctx.jobs.get_outputs(job_id)[0]["artifact_id"]

    with create_database_context(roar_dir) as db_ctx:
        current = db_ctx.labels.get_current("artifact", artifact_id=artifact_id)
        assert current is not None
        assert _values(current["metadata"], "license") == ["MIT"]
        assert _values(current["metadata"], "jurisdiction") == ["EU"]
        assert current["write_origin"] == LABEL_ORIGIN_USER
        # --add-tag is user-origin but still job-stamped (session-scoped, not
        # auto-bound — the named-artifact rule: it quantifies over the job's
        # whole output set, not a specifically inspected artifact).
        assert current["metadata"]["tag"]["license"]["values"][0]["job"] == job_uid
        assert "bind" not in current["metadata"]["tag"]


def test_add_tags_and_propagation_combine(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir(parents=True)

    dataset = _write(repo_root / "dataset.csv", b"a,b,c\n1,2,3\n")
    model = _write(repo_root / "model.bin", b"weights")

    with create_database_context(roar_dir) as db_ctx:
        db_ctx.job_recording.record_job(
            command="prepare_data.py",
            timestamp=1_700_000_000.0,
            output_files=[dataset],
            assign_to_session=False,
        )

    with create_database_context(roar_dir) as db_ctx:
        job1 = db_ctx.jobs.get_recent(1)[0]
        dataset_artifact_id = db_ctx.jobs.get_outputs(job1["id"])[0]["artifact_id"]
        db_ctx.labels.create_version(
            "artifact",
            _tag_doc(license=["MIT"]),
            artifact_id=dataset_artifact_id,
            write_origin=LABEL_ORIGIN_USER,
        )

    with create_database_context(roar_dir) as db_ctx:
        job2_id, _job2_uid = db_ctx.job_recording.record_job(
            command="train.py",
            timestamp=1_700_000_100.0,
            input_files=[dataset],
            output_files=[model],
            assign_to_session=False,
            add_tags=("jurisdiction=US",),
        )
        model_artifact_id = db_ctx.jobs.get_outputs(job2_id)[0]["artifact_id"]

    with create_database_context(roar_dir) as db_ctx:
        current = db_ctx.labels.get_current("artifact", artifact_id=model_artifact_id)
        assert current is not None
        assert _values(current["metadata"], "license") == ["MIT"]
        assert _values(current["metadata"], "jurisdiction") == ["US"]


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


def test_cross_session_input_does_not_propagate_without_a_bind(tmp_path: Path) -> None:
    """The core scope-gating behavior, exercised through the real job-recording path.

    Job 1 runs under real session A; its output is tagged. Job 2 runs under a
    *different* real session B and reads that same artifact — without a bind,
    the tag must not cross the session boundary.
    """
    repo_root = tmp_path / "repo"
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir(parents=True)

    dataset = _write(repo_root / "dataset.csv", b"a,b,c\n1,2,3\n")
    model = _write(repo_root / "model.bin", b"weights")

    with create_database_context(roar_dir) as db_ctx:
        db_ctx.job_recording.record_job(
            command="prepare_data.py",
            timestamp=1_700_000_000.0,
            output_files=[dataset],
        )  # assign_to_session defaults True -> real session A

    with create_database_context(roar_dir) as db_ctx:
        job1 = db_ctx.jobs.get_recent(1)[0]
        dataset_artifact_id = db_ctx.jobs.get_outputs(job1["id"])[0]["artifact_id"]
        # System-origin, job-stamped — as if this came from an earlier propagation
        # in session A, not a manual (auto-bound) `tag add`.
        db_ctx.labels.create_version(
            "artifact",
            {
                "tag": {
                    "license": {
                        "values": [
                            {"value": "MIT", "origin": LABEL_ORIGIN_SYSTEM, "job": job1["job_uid"]}
                        ]
                    }
                }
            },
            artifact_id=dataset_artifact_id,
            write_origin=LABEL_ORIGIN_SYSTEM,
        )
        # Start a fresh session B (deactivates A) for the next record_job.
        db_ctx.sessions.create()

    with create_database_context(roar_dir) as db_ctx:
        job2_id, _job2_uid = db_ctx.job_recording.record_job(
            command="train.py",
            timestamp=1_700_000_100.0,
            input_files=[dataset],
            output_files=[model],
        )  # a new active session B
        model_artifact_id = db_ctx.jobs.get_outputs(job2_id)[0]["artifact_id"]

    with create_database_context(roar_dir) as db_ctx:
        current = db_ctx.labels.get_current("artifact", artifact_id=model_artifact_id)
        assert current is None
