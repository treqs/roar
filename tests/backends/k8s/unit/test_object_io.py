from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from roar.backends.k8s.object_io import (
    OBJECT_IO_FILE_ENV,
    load_object_io_refs,
    patch_imported_module,
)


class _FakeBaseClient:
    def __init__(self) -> None:
        self.meta = SimpleNamespace(service_model=SimpleNamespace(service_name="s3"))

    def _make_api_call(self, operation_name: str, api_params: dict[str, Any]) -> dict[str, Any]:
        if operation_name == "GetObject":
            return {"ETag": '"abc123"', "ContentLength": 42}
        return {"ETag": '"def456"'}


def _patched_client_module() -> Any:
    module = SimpleNamespace(BaseClient=type("BaseClient", (_FakeBaseClient,), {}))
    patch_imported_module("botocore.client", module)
    return module


def test_hooks_record_s3_reads_and_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events_file = tmp_path / "events.jsonl"
    monkeypatch.setenv(OBJECT_IO_FILE_ENV, str(events_file))

    module = _patched_client_module()
    client = module.BaseClient()
    client._make_api_call("GetObject", {"Bucket": "data", "Key": "train/input.csv"})
    client._make_api_call("PutObject", {"Bucket": "models", "Key": "run/model.pt", "Body": b"xy"})
    client._make_api_call("ListObjectsV2", {"Bucket": "data"})  # control op, ignored

    events = [json.loads(line) for line in events_file.read_text().splitlines()]
    assert len(events) == 2
    read, write = events
    assert read == {
        "mode": "read",
        "path": "s3://data/train/input.csv",
        "operation": "GetObject",
        "etag": "abc123",
        "size": 42,
    }
    assert write["mode"] == "write"
    assert write["path"] == "s3://models/run/model.pt"
    assert write["etag"] == "def456"
    assert write["size"] == 2


def test_hooks_noop_without_events_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OBJECT_IO_FILE_ENV, raising=False)
    module = _patched_client_module()
    result = module.BaseClient()._make_api_call("GetObject", {"Bucket": "b", "Key": "k"})
    assert result["ContentLength"] == 42  # call passes through untouched


def test_patch_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events_file = tmp_path / "events.jsonl"
    monkeypatch.setenv(OBJECT_IO_FILE_ENV, str(events_file))

    module = _patched_client_module()
    patch_imported_module("botocore.client", module)  # second patch must not double-wrap
    module.BaseClient()._make_api_call("GetObject", {"Bucket": "b", "Key": "k"})

    events = events_file.read_text().splitlines()
    assert len(events) == 1


def test_non_s3_services_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events_file = tmp_path / "events.jsonl"
    monkeypatch.setenv(OBJECT_IO_FILE_ENV, str(events_file))

    module = _patched_client_module()
    client = module.BaseClient()
    client.meta = SimpleNamespace(service_model=SimpleNamespace(service_name="dynamodb"))
    client._make_api_call("GetObject", {"Bucket": "b", "Key": "k"})
    assert not events_file.exists()


def test_load_object_io_refs_dedupes_last_wins(tmp_path: Path) -> None:
    events_file = tmp_path / "events.jsonl"
    events_file.write_text(
        "\n".join(
            [
                json.dumps({"mode": "read", "path": "s3://d/a", "etag": "old", "size": 1}),
                json.dumps({"mode": "read", "path": "s3://d/a", "etag": "new", "size": 2}),
                json.dumps({"mode": "write", "path": "s3://m/out", "etag": None, "size": 9}),
                "not json",
                json.dumps({"mode": "bogus", "path": "s3://x/y"}),
            ]
        ),
        encoding="utf-8",
    )

    reads, writes = load_object_io_refs(events_file)
    assert reads == [
        {
            "path": "s3://d/a",
            "hash": "new",
            "hash_algorithm": "etag",
            "size": 2,
            "capture_method": "python",
        }
    ]
    assert writes == [
        {
            "path": "s3://m/out",
            "hash": None,
            "hash_algorithm": "",
            "size": 9,
            "capture_method": "python",
        }
    ]


def test_load_object_io_refs_missing_file(tmp_path: Path) -> None:
    reads, writes = load_object_io_refs(tmp_path / "absent.jsonl")
    assert reads == [] and writes == []


def test_ranged_reads_record_and_accumulate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events_file = tmp_path / "events.jsonl"
    monkeypatch.setenv(OBJECT_IO_FILE_ENV, str(events_file))

    module = _patched_client_module()
    client = module.BaseClient()
    client._make_api_call("GetObject", {"Bucket": "d", "Key": "shard.bin", "Range": "bytes=0-1023"})
    client._make_api_call(
        "GetObject", {"Bucket": "d", "Key": "shard.bin", "Range": "bytes=2048-4095"}
    )
    client._make_api_call("GetObject", {"Bucket": "d", "Key": "shard.bin", "Range": "bytes=0-1023"})

    reads, _writes = load_object_io_refs(events_file)
    assert len(reads) == 1
    assert reads[0]["path"] == "s3://d/shard.bin"
    # Ranges accumulate (deduped); etag/size come from the last event.
    assert reads[0]["byte_ranges"] == [[0, 1023], [2048, 4095]]


def test_range_header_parsing_edge_cases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from roar.backends.k8s.object_io import _parse_range_header

    assert _parse_range_header("bytes=0-99") == [[0, 99]]
    assert _parse_range_header("bytes=0-99,200-299") == [[0, 99], [200, 299]]
    assert _parse_range_header("bytes=500-") == []  # open-ended skipped
    assert _parse_range_header("bytes=-500") == []  # suffix skipped
    assert _parse_range_header("items=0-9") == []
    assert _parse_range_header(None) == []
    assert _parse_range_header("bytes=99-0") == []  # inverted skipped
