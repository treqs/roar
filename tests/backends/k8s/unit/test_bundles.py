from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from roar.backends.k8s.bundles import (
    K8sBundleError,
    bundle_filename_for_pod,
    discover_fragment_bundles,
    ingest_fragment_bundles,
    write_fragment_bundle,
)


def _fragment(pod: str, index: str) -> dict:
    return {
        "job_uid": f"{pod}-job",
        "parent_job_uid": "cafe0123",
        "task_id": f"{pod}-uid:trainer:{index}:0",
        "worker_id": "",
        "node_id": "",
        "actor_id": None,
        "task_name": "bundle-train/trainer",
        "started_at": 1000.0,
        "ended_at": 1010.0,
        "exit_code": 0,
        "backend": "k8s",
        "reads": [],
        "writes": [
            {
                "path": f"/work/out-{index}.bin",
                "hash": f"digest-{index}",
                "hash_algorithm": "blake3",
                "size": 10,
                "capture_method": "python",
            }
        ],
        "backend_metadata": {"execution_role": "task"},
    }


def test_bundle_filename_sanitizes_pod_names() -> None:
    assert bundle_filename_for_pod("train-0/pod x") == "roar-fragments-train-0-pod-x.json"
    assert bundle_filename_for_pod("") == "roar-fragments-pod.json"


def test_write_and_discover_bundles(tmp_path: Path) -> None:
    first = write_fragment_bundle(tmp_path / "bundles", "pod-0", [_fragment("pod-0", "0")])
    second = write_fragment_bundle(tmp_path / "bundles", "pod-1", [_fragment("pod-1", "1")])
    assert discover_fragment_bundles(tmp_path / "bundles") == [first, second]


def test_ingest_merges_bundles_into_db(tmp_path: Path) -> None:
    from roar.db.context import create_database_context

    project_dir = tmp_path / "project"
    roar_dir = project_dir / ".roar"
    roar_dir.mkdir(parents=True)
    with create_database_context(roar_dir):
        pass  # initialize schema

    bundle_dir = tmp_path / "bundles"
    ranged_fragment = _fragment("pod-0", "0")
    ranged_fragment["reads"] = [
        {
            "path": "s3://data/shard.bin",
            "hash": "etag-1",
            "hash_algorithm": "etag",
            "size": 4096,
            "capture_method": "python",
            "byte_ranges": [[0, 1023], [2048, 4095]],
        }
    ]
    write_fragment_bundle(bundle_dir, "pod-0", [ranged_fragment])
    write_fragment_bundle(bundle_dir, "pod-1", [_fragment("pod-1", "1")])

    result = ingest_fragment_bundles(roar_dir=roar_dir, directory=bundle_dir)
    assert result.bundles_ingested == 2
    assert result.fragments_merged == 2

    conn = sqlite3.connect(roar_dir / "roar.db")
    conn.row_factory = sqlite3.Row
    try:
        count = conn.execute("SELECT COUNT(*) FROM jobs WHERE job_type = 'k8s_task'").fetchone()
        assert count[0] == 2

        ranged = conn.execute(
            "SELECT byte_ranges FROM job_inputs WHERE path = 's3://data/shard.bin'"
        ).fetchone()
        assert ranged is not None
        assert ranged["byte_ranges"] == "[[0,1023],[2048,4095]]"
    finally:
        conn.close()


def test_ingest_empty_directory_fails_actionably(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(K8sBundleError, match="no roar-fragments-"):
        ingest_fragment_bundles(roar_dir=tmp_path / ".roar", directory=empty)
