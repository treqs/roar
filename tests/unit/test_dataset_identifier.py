import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from roar.core.models.dataset_identifier import DatasetIdentifier
from roar.db.context import create_database_context
from roar.execution.recording import DatasetIdentifierInferer, ExecutionJobRecorder


def _raw_session_paths() -> list[str]:
    paths: list[str] = []
    for i in range(10):
        session = f"session_{i:04d}_42"
        base = f"/home/trevor/dev/lance_demo/data/raw/{session}"
        paths.append(f"{base}/manifest.json")
        paths.append(f"{base}/{session}.mcap")
    return paths


def _extracted_internal_paths() -> list[str]:
    paths: list[str] = []
    for i in range(10):
        session = f"session_{i:04d}_42.lance"
        paths.append(
            "/home/trevor/dev/lance_demo/data/extracted/"
            f"{session}/_versions/18446744073709551614.manifest"
        )
    return paths


def test_infers_raw_dataset_with_high_confidence():
    inferer = DatasetIdentifierInferer()

    results = inferer.infer(_raw_session_paths())

    assert len(results) == 1
    candidate = results[0]
    assert candidate["dataset_id"] == "file:///home/trevor/dev/lance_demo/data/raw"
    assert candidate["confidence"] >= 0.8
    assert "manifest_anchor" in candidate["evidence"]
    assert "session_cluster" in candidate["evidence"]


def test_infers_extracted_dataset_from_lance_internal_paths():
    inferer = DatasetIdentifierInferer()
    paths = [
        *_extracted_internal_paths(),
        "/home/trevor/dev/lance_demo/data/training/manifest.json",
        "/home/trevor/dev/lance_demo/data/eval/metrics.json",
    ]

    results = inferer.infer(paths)
    ids = {str(item["dataset_id"]) for item in results}

    assert "file:///home/trevor/dev/lance_demo/data/extracted" in ids
    assert "file:///home/trevor/dev/lance_demo/data/training" not in ids
    assert "file:///home/trevor/dev/lance_demo/data/eval" not in ids

    extracted = next(
        item
        for item in results
        if item["dataset_id"] == "file:///home/trevor/dev/lance_demo/data/extracted"
    )
    assert extracted["confidence"] >= 0.7
    assert "container_internal_collapse" in extracted["evidence"]
    assert "split" not in extracted


def test_execution_metadata_includes_dataset_identifiers():
    recorder = ExecutionJobRecorder()
    ctx = SimpleNamespace(repo_root="/home/trevor/dev/lance_demo")
    prov = {"executables": {"code": {"git": {}}}}

    metadata_json = recorder._build_metadata_json(
        ctx=ctx,
        prov=prov,
        git_info={},
        read_files=_raw_session_paths(),
        written_files=["/home/trevor/dev/lance_demo/data/training/manifest.json"],
    )

    assert metadata_json is not None
    metadata = json.loads(metadata_json)
    assert "dataset_identifiers" in metadata
    assert metadata["dataset_identifiers"][0]["dataset_id"] == (
        "file:///home/trevor/dev/lance_demo/data/raw"
    )


def test_record_materializes_local_composite_outputs(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()

    dataset_dir = repo_root / "outputs" / "dataset"
    dataset_dir.mkdir(parents=True)
    shard_a = dataset_dir / "part-00000.parquet"
    shard_b = dataset_dir / "part-00001.parquet"
    shard_a.write_text("a")
    shard_b.write_text("b")

    recorder = ExecutionJobRecorder(telemetry_collector=lambda *_args, **_kwargs: None)
    ctx = SimpleNamespace(
        repo_root=str(repo_root),
        roar_dir=roar_dir,
        command=["python", "train.py", "--output-dir", str(dataset_dir)],
        execution_backend="local",
        execution_role="host",
        step_name="train",
        job_type="run",
        hash_algorithms=["blake3"],
    )
    prov = {
        "data": {"read_files": [], "written_files": [str(shard_a), str(shard_b)]},
        "executables": {"code": {"git": {}}},
    }
    tracer_result = SimpleNamespace(duration=0.2, exit_code=0)

    job_id, _job_uid, _inputs, outputs, _stale_upstream, _stale_downstream = recorder.record(
        ctx=ctx,
        prov=prov,
        tracer_result=tracer_result,
        start_time=1700000000.0,
        is_build=False,
    )

    outputs_by_path = {item["path"]: item for item in outputs}
    assert str(dataset_dir) in outputs_by_path
    composite_output = outputs_by_path[str(dataset_dir)]
    assert composite_output["kind"] == "composite"
    assert composite_output["component_count"] == 2

    with create_database_context(roar_dir) as db_ctx:
        composite_artifact = db_ctx.artifacts.get_by_path(str(dataset_dir))
        assert composite_artifact is not None
        assert composite_artifact["kind"] == "composite"

        summary = db_ctx.composites.get(composite_artifact["id"])
        assert summary is not None
        assert summary["component_count"] == 2

        components = db_ctx.composites.get_components(composite_artifact["id"])
        assert len(components) == 2

        job = db_ctx.jobs.get(job_id)
        assert job is not None
        metadata = json.loads(job["metadata"])
        assert "composites" in metadata
        assert len(metadata["composites"]) == 1
        assert metadata["composites"][0]["root_path"] == str(dataset_dir)
        assert metadata["composites"][0]["component_count_total"] == 2


def test_record_materializes_composite_even_when_run_hashes_exclude_blake3(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()

    dataset_dir = repo_root / "outputs" / "dataset"
    dataset_dir.mkdir(parents=True)
    shard_a = dataset_dir / "part-00000.parquet"
    shard_b = dataset_dir / "part-00001.parquet"
    shard_a.write_text("a")
    shard_b.write_text("b")

    recorder = ExecutionJobRecorder(telemetry_collector=lambda *_args, **_kwargs: None)
    ctx = SimpleNamespace(
        repo_root=str(repo_root),
        roar_dir=roar_dir,
        command=["python", "train.py", "--output-dir", str(dataset_dir)],
        execution_backend="local",
        execution_role="host",
        step_name="train",
        job_type="run",
        hash_algorithms=["sha256"],
    )
    prov = {
        "data": {"read_files": [], "written_files": [str(shard_a), str(shard_b)]},
        "executables": {"code": {"git": {}}},
    }
    tracer_result = SimpleNamespace(duration=0.2, exit_code=0)

    _job_id, _job_uid, _inputs, outputs, _stale_upstream, _stale_downstream = recorder.record(
        ctx=ctx,
        prov=prov,
        tracer_result=tracer_result,
        start_time=1700000000.0,
        is_build=False,
    )

    outputs_by_path = {item["path"]: item for item in outputs}
    assert str(dataset_dir) in outputs_by_path
    assert outputs_by_path[str(dataset_dir)]["kind"] == "composite"


def test_record_skips_composite_materialization_when_disabled_in_config(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roar_dir = repo_root / ".roar"
    roar_dir.mkdir()
    (roar_dir / "config.toml").write_text(
        """
[composites.run]
enabled = false
"""
    )

    dataset_dir = repo_root / "outputs" / "dataset"
    dataset_dir.mkdir(parents=True)
    shard_a = dataset_dir / "part-00000.parquet"
    shard_b = dataset_dir / "part-00001.parquet"
    shard_a.write_text("a")
    shard_b.write_text("b")

    recorder = ExecutionJobRecorder(telemetry_collector=lambda *_args, **_kwargs: None)
    ctx = SimpleNamespace(
        repo_root=str(repo_root),
        roar_dir=roar_dir,
        command=["python", "train.py", "--output-dir", str(dataset_dir)],
        execution_backend="local",
        execution_role="host",
        step_name="train",
        job_type="run",
        hash_algorithms=["blake3"],
    )
    prov = {
        "data": {"read_files": [], "written_files": [str(shard_a), str(shard_b)]},
        "executables": {"code": {"git": {}}},
    }
    tracer_result = SimpleNamespace(duration=0.2, exit_code=0)

    _job_id, _job_uid, _inputs, outputs, _stale_upstream, _stale_downstream = recorder.record(
        ctx=ctx,
        prov=prov,
        tracer_result=tracer_result,
        start_time=1700000000.0,
        is_build=False,
    )

    outputs_by_path = {item["path"]: item for item in outputs}
    assert str(dataset_dir) not in outputs_by_path


def test_extract_dataset_hint_paths_parses_common_flags():
    recorder = ExecutionJobRecorder()
    command = [
        "python",
        "./src/extract_to_lance.py",
        "--input-dir",
        "data/raw",
        "--output-dir=data/extracted",
        "--seed",
        "42",
    ]

    hints = recorder._extract_dataset_hint_paths(command, "/home/trevor/dev/lance_demo")

    assert "/home/trevor/dev/lance_demo/data/raw" in hints
    assert "/home/trevor/dev/lance_demo/data/extracted" in hints


def test_metadata_uses_dataset_hint_paths_when_observation_is_sparse():
    recorder = ExecutionJobRecorder()
    ctx = SimpleNamespace(repo_root="/home/trevor/dev/lance_demo")
    prov = {"executables": {"code": {"git": {}}}}

    metadata_json = recorder._build_metadata_json(
        ctx=ctx,
        prov=prov,
        git_info={},
        read_files=[
            "/home/trevor/dev/lance_demo/data/extracted/"
            "session_0000_42.lance/_versions/18446744073709551614.manifest"
        ],
        written_files=["/home/trevor/dev/lance_demo/data/training/manifest.json"],
        dataset_hint_paths=["/home/trevor/dev/lance_demo/data/extracted"],
    )

    assert metadata_json is not None
    metadata = json.loads(metadata_json)
    ids = {item["dataset_id"] for item in metadata["dataset_identifiers"]}
    assert "file:///home/trevor/dev/lance_demo/data/extracted" in ids


def test_collect_session_window_paths_includes_recent_steps():
    recorder = ExecutionJobRecorder()

    class _Sessions:
        def get_active(self):
            return {"id": 1}

        def get_steps(self, _session_id):
            return [
                {"id": 10, "step_number": 1},
                {"id": 11, "step_number": 2},
                {"id": 12, "step_number": 3},
            ]

    class _Jobs:
        def get_inputs(self, job_id):
            mapping = {
                10: [{"path": "/repo/data/raw/session_0000_42/manifest.json"}],
                11: [{"path": "/repo/data/extracted/session_0000_42.lance/_versions/x.manifest"}],
                12: [{"path": "/repo/data/extracted/session_0001_42.lance/_versions/y.manifest"}],
            }
            return mapping.get(job_id, [])

        def get_outputs(self, job_id):
            mapping = {
                10: [{"path": "/repo/data/raw/session_0000_42/session_0000_42.mcap"}],
                11: [{"path": "/repo/data/extracted/extraction_manifest.json"}],
                12: [{"path": "/repo/data/training/manifest.json"}],
            }
            return mapping.get(job_id, [])

    db_ctx = SimpleNamespace(sessions=_Sessions(), jobs=_Jobs())
    paths = recorder._collect_session_window_paths(db_ctx, max_steps=1)

    # max_steps=1 keeps step numbers 2 and 3
    assert "/repo/data/extracted/extraction_manifest.json" in paths
    assert "/repo/data/training/manifest.json" in paths
    assert "/repo/data/raw/session_0000_42/manifest.json" not in paths


def test_infers_partitioned_object_store_root():
    inferer = DatasetIdentifierInferer()
    paths = [
        "s3://ml-bucket/features/dt=2026-02-18/part-00000.snappy.parquet",
        "s3://ml-bucket/features/dt=2026-02-18/part-00001.snappy.parquet",
        "s3://ml-bucket/features/dt=2026-02-19/part-00000.snappy.parquet",
        "s3://ml-bucket/features/dt=2026-02-19/part-00001.snappy.parquet",
        "s3://ml-bucket/features/dt=2026-02-20/part-00000.snappy.parquet",
        "s3://ml-bucket/features/dt=2026-02-20/part-00001.snappy.parquet",
        "s3://ml-bucket/features/dt=2026-02-21/part-00000.snappy.parquet",
        "s3://ml-bucket/features/dt=2026-02-21/part-00001.snappy.parquet",
    ]

    results = inferer.infer(paths)
    ids = {item["dataset_id"] for item in results}

    assert "s3://ml-bucket/features" in ids
    candidate = next(item for item in results if item["dataset_id"] == "s3://ml-bucket/features")
    assert candidate["confidence"] >= 0.55
    assert "partition_collapse" in candidate["evidence"]


def test_infers_image_dataset_root_from_split_class_layout():
    inferer = DatasetIdentifierInferer()
    paths = [
        "/datasets/cifar10/train/cat/image_0001.jpg",
        "/datasets/cifar10/train/cat/image_0002.jpg",
        "/datasets/cifar10/train/cat/image_0003.jpg",
        "/datasets/cifar10/train/dog/image_0001.jpg",
        "/datasets/cifar10/train/dog/image_0002.jpg",
        "/datasets/cifar10/train/dog/image_0003.jpg",
        "/datasets/cifar10/train/bird/image_0001.jpg",
        "/datasets/cifar10/train/bird/image_0002.jpg",
    ]

    results = inferer.infer(paths)
    ids = {item["dataset_id"] for item in results}
    assert "file:///datasets/cifar10" in ids

    candidate = next(item for item in results if item["dataset_id"] == "file:///datasets/cifar10")
    assert candidate["confidence"] >= 0.55
    assert "class_folder_collapse" in candidate["evidence"]


def test_normalizes_training_split_to_supported_literal():
    inferer = DatasetIdentifierInferer()
    paths = [
        "/datasets/cifar10/training/cat/image_0001.jpg",
        "/datasets/cifar10/training/cat/image_0002.jpg",
        "/datasets/cifar10/training/dog/image_0001.jpg",
        "/datasets/cifar10/training/dog/image_0002.jpg",
        "/datasets/cifar10/training/bird/image_0001.jpg",
        "/datasets/cifar10/training/bird/image_0002.jpg",
        "/datasets/cifar10/training/bird/image_0003.jpg",
        "/datasets/cifar10/training/bird/image_0004.jpg",
    ]

    results = inferer.infer(paths)
    candidate = next(item for item in results if item["dataset_id"] == "file:///datasets/cifar10")
    assert candidate["split"] == "train"


def test_suppresses_volatile_checkpoint_tree():
    inferer = DatasetIdentifierInferer()
    paths = [
        "/workspace/runs/2026-02-18/checkpoints/epoch_01/model.ckpt",
        "/workspace/runs/2026-02-18/checkpoints/epoch_02/model.ckpt",
        "/workspace/runs/2026-02-18/checkpoints/epoch_03/model.ckpt",
        "/workspace/runs/2026-02-18/checkpoints/epoch_04/model.ckpt",
        "/workspace/runs/2026-02-18/checkpoints/epoch_05/model.ckpt",
        "/workspace/runs/2026-02-18/checkpoints/epoch_06/model.ckpt",
        "/workspace/runs/2026-02-18/checkpoints/epoch_07/model.ckpt",
        "/workspace/runs/2026-02-18/checkpoints/epoch_08/model.ckpt",
    ]

    results = inferer.infer(paths)
    assert results == []


def test_inferred_dataset_identifiers_conform_contract():
    inferer = DatasetIdentifierInferer()
    results = inferer.infer(_raw_session_paths())

    assert results
    for item in results:
        validated = DatasetIdentifier.model_validate(item)
        assert 0.0 <= validated.confidence <= 1.0
        assert validated.dataset_id


def test_dataset_identifier_contract_rejects_relative_file_uri():
    with pytest.raises(ValueError):
        DatasetIdentifier.model_validate(
            {
                "dataset_id": "file://relative/path",
                "dataset_fingerprint": "abcd1234abcd1234",
                "dataset_fingerprint_algorithm": "sha256",
                "confidence": 0.7,
                "evidence": ["explicit_root_hint"],
                "observed_paths": 3,
            }
        )
