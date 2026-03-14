"""
Unit tests for the get transfer service.

These tests cover localized download mechanics only. Job/artifact persistence
is exercised at the application layer and through live/e2e product paths.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from roar.application.get.transfer import GetService
from roar.integrations.download.base import Source, parse_source
from roar.integrations.download.noop import NoOpDownloadBackend


def _make_source(url: str = "s3://my-bucket/models/model.pt") -> Source:
    return parse_source(url)


def test_get_single_file_returns_downloaded_file_facts(tmp_path: Path) -> None:
    backend = NoOpDownloadBackend(bucket="my-bucket")
    backend.seed("models/model.pt", b"model data here")
    service = GetService(backend=backend, source=_make_source(), repo_root=tmp_path)

    dest = tmp_path / "model.pt"
    result = service.get(destination=dest)

    assert result.success is True
    assert len(result.downloaded_files) == 1
    assert result.downloaded_files[0].local_path == str(dest)
    assert dest.exists()
    assert dest.read_bytes() == b"model data here"


def test_get_hash_verification_fails_and_cleans_tmp_file(tmp_path: Path) -> None:
    backend = NoOpDownloadBackend(bucket="my-bucket")
    backend.seed("model.pt", b"wrong content")
    service = GetService(backend=backend, source=_make_source("s3://my-bucket/model.pt"))

    result = service.get(
        destination=tmp_path / "model.pt",
        expected_hash="0000000000000000000000000000000000000000",
    )

    assert result.success is False
    assert "Hash mismatch" in result.error
    assert not (tmp_path / "model.pt.roar_tmp").exists()


def test_get_destination_into_directory_uses_original_filename(tmp_path: Path) -> None:
    backend = NoOpDownloadBackend(bucket="my-bucket")
    backend.seed("models/model.pt", b"data")
    service = GetService(backend=backend, source=_make_source(), repo_root=tmp_path)

    dest_dir = tmp_path / "output"
    dest_dir.mkdir()
    result = service.get(destination=dest_dir)

    expected_path = dest_dir / "model.pt"
    assert expected_path.exists()
    assert result.downloaded_files[0].local_path == str(expected_path)


def test_dry_run_does_not_download_files(tmp_path: Path) -> None:
    backend = NoOpDownloadBackend(bucket="my-bucket")
    service = GetService(backend=backend, source=_make_source(), repo_root=tmp_path)

    result = service.get(destination=tmp_path / "model.pt", dry_run=True)

    assert result.success is True
    assert result.dry_run is True
    assert len(result.would_download) == 1
    assert result.would_download[0].remote_url == "s3://my-bucket/models/model.pt"
    assert not (tmp_path / "model.pt").exists()


def test_raises_when_file_exists_without_force(tmp_path: Path) -> None:
    backend = NoOpDownloadBackend(bucket="my-bucket")
    backend.seed("model.pt", b"data")
    service = GetService(backend=backend, source=_make_source("s3://my-bucket/model.pt"))

    dest = tmp_path / "model.pt"
    dest.write_bytes(b"existing")

    with pytest.raises(FileExistsError, match="Use --force"):
        service.get(destination=dest)


def test_force_overwrites_existing_file(tmp_path: Path) -> None:
    backend = NoOpDownloadBackend(bucket="my-bucket")
    backend.seed("model.pt", b"new data")
    service = GetService(backend=backend, source=_make_source("s3://my-bucket/model.pt"))

    dest = tmp_path / "model.pt"
    dest.write_bytes(b"old data")

    result = service.get(destination=dest, force=True)

    assert result.success is True
    assert dest.read_bytes() == b"new data"


def test_prefix_download_multiple_files(tmp_path: Path) -> None:
    backend = NoOpDownloadBackend(bucket="my-bucket")
    backend.seed("checkpoints/epoch_1.pt", b"epoch 1")
    backend.seed("checkpoints/epoch_2.pt", b"epoch 2")
    source = Source(
        scheme="s3",
        bucket="my-bucket",
        key="checkpoints",
        original_url="s3://my-bucket/checkpoints/",
    )
    service = GetService(backend=backend, source=source, repo_root=tmp_path)

    dest = tmp_path / "local_checkpoints"
    result = service.get(destination=dest, is_prefix=True)

    assert result.success is True
    assert len(result.downloaded_files) == 2
    assert (dest / "epoch_1.pt").exists()
    assert (dest / "epoch_2.pt").exists()


def test_prefix_download_preserves_subdirs(tmp_path: Path) -> None:
    backend = NoOpDownloadBackend(bucket="my-bucket")
    backend.seed("data/train/images/img1.png", b"img1")
    backend.seed("data/train/labels/label1.txt", b"label1")
    source = Source(
        scheme="s3",
        bucket="my-bucket",
        key="data/train",
        original_url="s3://my-bucket/data/train/",
    )
    service = GetService(backend=backend, source=source, repo_root=tmp_path)

    dest = tmp_path / "training_data"
    result = service.get(destination=dest, is_prefix=True)

    assert result.success is True
    assert (dest / "images" / "img1.png").exists()
    assert (dest / "labels" / "label1.txt").exists()


def test_prefix_no_files_returns_error(tmp_path: Path) -> None:
    backend = NoOpDownloadBackend(bucket="my-bucket")
    source = Source(
        scheme="s3",
        bucket="my-bucket",
        key="nonexistent",
        original_url="s3://my-bucket/nonexistent/",
    )
    service = GetService(backend=backend, source=source, repo_root=tmp_path)

    result = service.get(destination=tmp_path / "output", is_prefix=True)

    assert result.success is False
    assert "No files found" in result.error


def test_prefix_hashes_all_files_in_single_batch_call(tmp_path: Path) -> None:
    import blake3

    backend = NoOpDownloadBackend(bucket="my-bucket")
    backend.seed("checkpoints/epoch_1.pt", b"epoch 1")
    backend.seed("checkpoints/epoch_2.pt", b"epoch 2")
    source = Source(
        scheme="s3",
        bucket="my-bucket",
        key="checkpoints",
        original_url="s3://my-bucket/checkpoints/",
    )
    service = GetService(backend=backend, source=source, repo_root=tmp_path)

    calls = {"count": 0}

    def fake_hashes(paths):
        calls["count"] += 1
        return {str(path): blake3.blake3(Path(path).read_bytes()).hexdigest() for path in paths}

    with patch("roar.application.get.transfer.hash_files_blake3", side_effect=fake_hashes):
        result = service.get(destination=tmp_path / "out", is_prefix=True)

    assert result.success is True
    assert calls["count"] == 1


def test_prefix_dry_run_lists_files_without_downloading(tmp_path: Path) -> None:
    backend = NoOpDownloadBackend(bucket="my-bucket")
    backend.seed("data/a.csv", b"a")
    backend.seed("data/b.csv", b"b")
    source = Source(
        scheme="s3",
        bucket="my-bucket",
        key="data",
        original_url="s3://my-bucket/data/",
    )
    service = GetService(backend=backend, source=source, repo_root=tmp_path)

    result = service.get(destination=tmp_path / "output", dry_run=True, is_prefix=True)

    assert result.success is True
    assert result.dry_run is True
    assert len(result.would_download) == 2


def test_relative_to_prefix_strips_prefix() -> None:
    assert GetService._relative_to_prefix("data/train.csv", "data") == "train.csv"
    assert GetService._relative_to_prefix("data/sub/file.csv", "data") == "sub/file.csv"
    assert GetService._relative_to_prefix("data/train.csv", "data/") == "train.csv"
    assert GetService._relative_to_prefix("other/file.csv", "data") == "other/file.csv"
