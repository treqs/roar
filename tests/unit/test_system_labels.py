from __future__ import annotations

import json
from pathlib import Path

from roar.application.system_labels import (
    build_job_system_labels,
    omit_display_system_labels,
    refresh_job_system_labels,
)
from roar.db.context import create_database_context
from roar.execution.recording import LocalJobRecorder, LocalRecordedArtifact


def test_build_job_system_labels_derives_common_runtime_tracker_and_dataset_fields() -> None:
    labels = build_job_system_labels(
        {
            "job_type": "run",
            "metadata": {
                "git": {
                    "commit": "deadbeef",
                    "branch": "main",
                    "remote_url": "https://github.com/treqs/roar.git",
                    "clean": True,
                    "commit_timestamp": "2026-04-16T00:00:00+00:00",
                },
                "cwd": "experiments/demo",
                "runtime": {
                    "hostname": "host-1",
                    "os": {"system": "Linux", "machine": "x86_64"},
                    "python": {"version": "3.12.2", "implementation": "CPython"},
                    "gpu": [{"name": "NVIDIA A100", "memory_mb": 81920, "compute_cap": "8.0"}],
                    "cpu": {"count": 32, "model": "AMD EPYC"},
                    "memory": {"total_mb": 262144},
                },
                "packages": {"pip": {"numpy": "1.26.4"}, "dpkg": {"libc6": "2.35"}},
                "analysis": {
                    "experiment_tracking": {
                        "trackers_detected": ["wandb"],
                        "runs": [
                            {
                                "tracker": "wandb",
                                "entity": "acme",
                                "project": "forecast",
                                "run_id": "abc123",
                                "url": "https://wandb.ai/acme/forecast/runs/abc123",
                            }
                        ],
                    }
                },
                "dataset_identifiers": [
                    {
                        "dataset_id": "file:///data/train",
                        "dataset_fingerprint": "a1b2c3d4",
                        "dataset_fingerprint_algorithm": "blake3",
                        "confidence": 0.91,
                        "observed_paths": 3,
                        "split": "train",
                        "version_hint": "v2",
                        "evidence": ["explicit_root_hint", "partition_collapse"],
                    }
                ],
                "composites": [
                    {
                        "hash": "f" * 64,
                        "root_path": "/repo/outputs/dataset",
                        "component_count_total": 2,
                        "component_count_stored": 2,
                    }
                ],
            },
        }
    )

    roar = labels["roar"]
    assert roar["schema_version"] == 1
    assert roar["operation"]["kind"] == "run"
    assert roar["git"]["commit"] == "deadbeef"
    assert roar["cwd"] == "experiments/demo"
    assert roar["runtime"]["python"]["version"] == "3.12.2"
    assert roar["runtime"]["gpu"]["count"] == 1
    assert roar["runtime"]["gpu"]["0"]["name"] == "NVIDIA A100"
    assert roar["packages"]["pip"]["numpy"] == "1.26.4"
    assert roar["tracker"]["0"]["url"] == "https://wandb.ai/acme/forecast/runs/abc123"
    assert roar["tracker"]["by_name"]["wandb"]["url"] == (
        "https://wandb.ai/acme/forecast/runs/abc123"
    )
    assert roar["datasets"]["0"]["id"] == "file:///data/train"
    assert roar["composites"]["0"]["root_path"] == "/repo/outputs/dataset"


def test_build_job_system_labels_projects_frozen_roar_version() -> None:
    labels = build_job_system_labels(
        {"job_type": "run", "metadata": {"roar_version": "0.3.4rc1", "git": {"commit": "abc"}}}
    )
    # roar.version is the producing version, projected from the frozen job-metadata field.
    assert labels["roar"]["version"] == "0.3.4rc1"


def test_build_job_system_labels_omits_roar_version_when_not_captured() -> None:
    # Jobs recorded by an older roar have no captured version -> no roar.version label.
    labels = build_job_system_labels({"job_type": "run", "metadata": {"git": {"commit": "abc"}}})
    assert "version" not in labels["roar"]


def test_omit_display_system_labels_removes_roar_root_only() -> None:
    metadata = {
        "name": "preprocess",
        "phase": "train",
        "roar": {"operation": {"kind": "run"}},
        "dataset": {"type": "dataset"},
    }

    filtered = omit_display_system_labels(metadata)

    assert filtered == {
        "name": "preprocess",
        "phase": "train",
        "dataset": {"type": "dataset"},
    }


def test_build_job_system_labels_uses_job_row_parent_for_task_labels() -> None:
    labels = build_job_system_labels(
        {
            "job_type": "ray_task",
            "execution_backend": "ray",
            "parent_job_uid": "driver-main",
            "metadata": {
                "task_identity": "identity-1",
                "ray_task_id": "task-1",
                "ray_worker_id": "worker-1",
                "ray_node_id": "node-1",
            },
        }
    )

    roar = labels["roar"]
    assert roar["operation"]["kind"] == "ray_task"
    assert roar["task"]["backend"] == "ray"
    assert roar["task"]["id"] == "task-1"
    assert roar["task"]["parent_job_uid"] == "driver-main"


def test_refresh_job_system_labels_preserves_user_labels_and_replaces_reserved_prefix(
    tmp_path: Path,
) -> None:
    roar_dir = tmp_path / ".roar"
    roar_dir.mkdir()

    with create_database_context(roar_dir) as db_ctx:
        session_id = db_ctx.sessions.create(make_active=True)
        job_id, _job_uid = db_ctx.jobs.create(
            command="roar get s3://bucket/model.pt",
            timestamp=1700000000.0,
            session_id=session_id,
            step_number=1,
            metadata=json.dumps(
                {
                    "get": {
                        "source": "s3://bucket/model.pt",
                        "source_type": "s3",
                        "artifacts": {"model.pt": "s3://bucket/model.pt"},
                        "git_commit": "deadbeef",
                        "git_tag": None,
                        "timestamp": 123.0,
                    }
                }
            ),
            execution_backend="local",
            execution_role="host",
            job_type="get",
            exit_code=0,
        )
        db_ctx.labels.create_version(
            "job",
            {
                "phase": "download",
                "roar": {"operation": {"kind": "stale"}, "get": {"source_type": "old"}},
            },
            job_id=job_id,
            write_origin="user",
        )

        db_ctx.jobs.update_metadata(
            job_id,
            json.dumps(
                {
                    "put": {
                        "destination": "s3://bucket/out",
                        "destination_type": "s3",
                        "artifacts": {"artifact-1": "s3://bucket/out"},
                        "composites": [
                            {"hash": "a" * 64, "root_path": "/repo/out", "registered": True}
                        ],
                        "lineage_composites": [],
                        "dataset_identifiers": [],
                        "git_commit": "feedface",
                        "git_tag": "put/feedface",
                        "timestamp": 456.0,
                    }
                }
            ),
        )
        refresh_job_system_labels(db_ctx, job_id=job_id)

        current = db_ctx.labels.get_current("job", job_id=job_id)

    assert current is not None
    metadata = current["metadata"]
    assert metadata["phase"] == "download"
    assert metadata["roar"]["operation"]["kind"] == "put"
    assert metadata["roar"]["put"]["destination_type"] == "s3"
    assert metadata["roar"]["put"]["artifact_count"] == 1
    assert metadata["roar"]["git"]["commit"] == "feedface"
    assert metadata["roar"]["git"]["tag"] == "put/feedface"
    assert metadata["roar"]["put"]["composites"]["registered_count"] == 1
    assert metadata["roar"]["put"]["composites"]["0"]["root_path"] == "/repo/out"


def test_local_job_recorder_records_get_system_labels(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()
    downloaded_file = repo_root / "downloaded.bin"
    downloaded_file.write_bytes(b"payload")

    with create_database_context(roar_dir) as db_ctx:
        session_id = db_ctx.sessions.create(make_active=True)
        recorder = LocalJobRecorder()
        job_id, _job_uid = recorder.record(
            db_ctx,
            command="roar get s3://bucket/downloaded.bin",
            timestamp=1700000000.0,
            metadata=json.dumps(
                {
                    "get": {
                        "source": "s3://bucket/downloaded.bin",
                        "source_type": "s3",
                        "message": "download",
                        "artifacts": {str(downloaded_file): "s3://bucket/downloaded.bin"},
                        "git_commit": "deadbeef",
                        "git_tag": None,
                        "timestamp": 123.0,
                    }
                }
            ),
            execution_backend="local",
            execution_role="host",
            job_type="get",
            session_id=session_id,
            output_artifacts=[
                LocalRecordedArtifact(
                    path=str(downloaded_file),
                    hashes={"blake3": "abc123"},
                    size=downloaded_file.stat().st_size,
                )
            ],
            exit_code=0,
        )

        current = db_ctx.labels.get_current("job", job_id=job_id)

    assert current is not None
    metadata = current["metadata"]
    assert metadata["roar"]["operation"]["kind"] == "get"
    assert metadata["roar"]["get"]["source_type"] == "s3"
    assert metadata["roar"]["get"]["artifact_count"] == 1
    assert metadata["roar"]["git"]["commit"] == "deadbeef"
