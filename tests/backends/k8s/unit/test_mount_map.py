from __future__ import annotations

from typing import Any

from roar.backends.k8s.mount_map import (
    build_container_mount_map,
    dump_mount_map,
    parse_mount_map,
    rewrite_fragment_paths,
)

POD_SPEC = {
    "volumes": [
        {
            "name": "gcs-data",
            "csi": {
                "driver": "gcsfuse.csi.storage.gke.io",
                "volumeAttributes": {"bucketName": "training-data"},
            },
        },
        {
            "name": "s3-data",
            "csi": {
                "driver": "s3.csi.aws.com",
                "volumeAttributes": {"bucketName": "shards"},
            },
        },
        {"name": "ckpt", "persistentVolumeClaim": {"claimName": "shared-ckpt"}},
        {"name": "scratch", "emptyDir": {}},
    ]
}

CONTAINER = {
    "name": "trainer",
    "volumeMounts": [
        {"name": "gcs-data", "mountPath": "/data"},
        {"name": "s3-data", "mountPath": "/shards", "subPath": "run-42"},
        {"name": "ckpt", "mountPath": "/ckpt"},
        {"name": "scratch", "mountPath": "/scratch"},
    ],
}


def test_derives_inline_csi_and_pvc_entries() -> None:
    entries = build_container_mount_map(POD_SPEC, CONTAINER)
    assert entries == [
        {"mount_path": "/data", "uri": "gs://training-data"},
        {"mount_path": "/shards", "uri": "s3://shards/run-42"},
        {"mount_path": "/ckpt", "volume": "pvc://shared-ckpt"},
    ]


def test_config_entries_win_over_derived() -> None:
    entries = build_container_mount_map(
        POD_SPEC,
        CONTAINER,
        {"/data": "gs://training-data/curated/", "/extra": "s3://other"},
    )
    by_path = {entry["mount_path"]: entry for entry in entries}
    assert by_path["/data"] == {"mount_path": "/data", "uri": "gs://training-data/curated"}
    assert by_path["/extra"] == {"mount_path": "/extra", "uri": "s3://other"}


def test_mount_map_round_trips_through_env() -> None:
    entries = build_container_mount_map(POD_SPEC, CONTAINER)
    assert parse_mount_map(dump_mount_map(entries)) == entries
    assert parse_mount_map(None) == []
    assert parse_mount_map("not json") == []


def _fragment_with_paths(read_path: str, write_path: str) -> dict[str, Any]:
    return {
        "backend_metadata": {
            "k8s_mount_map": [
                {"mount_path": "/data", "uri": "gs://training-data"},
                {"mount_path": "/data/nested", "uri": "s3://nested-bucket"},
                {"mount_path": "/ckpt", "volume": "pvc://shared-ckpt"},
            ]
        },
        "reads": [{"path": read_path}],
        "writes": [{"path": write_path}],
    }


def test_rewrite_uses_longest_prefix_and_skips_volume_tags() -> None:
    fragment = _fragment_with_paths("/data/nested/shard-0.bin", "/ckpt/step-100.pt")
    rewrite_fragment_paths(fragment)
    assert fragment["reads"][0]["path"] == "s3://nested-bucket/shard-0.bin"
    # PVC identity tags never rewrite: local path + content hash stay canonical.
    assert fragment["writes"][0]["path"] == "/ckpt/step-100.pt"


def test_rewrite_exact_mount_path_and_untouched_paths() -> None:
    fragment = _fragment_with_paths("/data", "/work/model.bin")
    rewrite_fragment_paths(fragment)
    assert fragment["reads"][0]["path"] == "gs://training-data"
    assert fragment["writes"][0]["path"] == "/work/model.bin"


def test_rewrite_noops_without_mount_map() -> None:
    fragment = {"backend_metadata": {}, "reads": [{"path": "/data/x"}], "writes": []}
    rewrite_fragment_paths(fragment)
    assert fragment["reads"][0]["path"] == "/data/x"
