"""Unit tests for unified hashing backend."""

from __future__ import annotations

import hashlib
from pathlib import Path

import blake3
import pytest

from roar.db.hashing import backend


def test_normalize_algorithms_deduplicates_in_order() -> None:
    normalized = backend.normalize_algorithms(["sha256", "blake3", "sha256", "md5"])
    assert normalized == ["sha256", "blake3", "md5"]


def test_normalize_algorithms_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown hash algorithm"):
        backend.normalize_algorithms(["sha1"])


def test_compute_hashes_python_fallback_matches_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend, "_hash_native", None)

    path = tmp_path / "artifact.bin"
    content = b"hello native hashing"
    path.write_bytes(content)

    hashes = backend.compute_hashes(path, ["blake3", "sha256", "sha512", "md5"])
    assert hashes is not None

    assert hashes["blake3"] == blake3.blake3(content).hexdigest()
    assert hashes["sha256"] == hashlib.sha256(content).hexdigest()
    assert hashes["sha512"] == hashlib.sha512(content).hexdigest()
    assert hashes["md5"] == hashlib.md5(content).hexdigest()


def test_compute_hashes_missing_file_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend, "_hash_native", None)
    missing = tmp_path / "missing.bin"
    assert backend.compute_hashes(missing, ["blake3"]) is None


def test_compute_hashes_batch_python_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend, "_hash_native", None)

    path1 = tmp_path / "a.bin"
    path2 = tmp_path / "b.bin"
    path1.write_bytes(b"a")
    path2.write_bytes(b"b")

    result = backend.compute_hashes_batch([path1, path2], ["sha256"])
    assert set(result) == {str(path1), str(path2)}
    assert result[str(path1)]["sha256"] == hashlib.sha256(b"a").hexdigest()
    assert result[str(path2)]["sha256"] == hashlib.sha256(b"b").hexdigest()


class _FakeNative:
    def __init__(self) -> None:
        self.last_workers: int | None = None
        self.last_paths: list[str] | None = None

    def compute_hashes(self, path: str, algorithms: list[str]) -> dict[str, str]:
        return {algo: f"{path}:{algo}" for algo in algorithms}

    def compute_hashes_batch(
        self, paths: list[str], algorithms: list[str], workers: int | None
    ) -> dict[str, dict[str, str]]:
        self.last_workers = workers
        self.last_paths = list(paths)
        return {path: {algo: f"{path}:{algo}" for algo in algorithms} for path in paths}


def test_compute_hashes_batch_native_preserves_explicit_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_native = _FakeNative()
    monkeypatch.setattr(backend, "_hash_native", fake_native)
    monkeypatch.setattr(backend.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(backend.os.path, "isfile", lambda _path: True)

    paths = [f"/tmp/f{i}" for i in range(32)]
    result = backend.compute_hashes_batch(paths, ["sha256"], workers=3)

    assert set(result) == set(paths)
    assert fake_native.last_workers == 3


def test_compute_hashes_batch_native_auto_workers_for_large_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_native = _FakeNative()
    monkeypatch.setattr(backend, "_hash_native", fake_native)
    monkeypatch.setattr(backend.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(backend.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(backend.os.path, "getsize", lambda _path: 1024)

    paths = [f"/tmp/f{i}" for i in range(80)]
    backend.compute_hashes_batch(paths, ["sha256"])

    assert fake_native.last_workers == 8


def test_compute_hashes_batch_native_auto_workers_disabled_for_tiny_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_native = _FakeNative()
    monkeypatch.setattr(backend, "_hash_native", fake_native)
    monkeypatch.setattr(backend.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(backend.os.path, "getsize", lambda _path: 1024)

    paths = ["/tmp/a", "/tmp/b"]
    backend.compute_hashes_batch(paths, ["sha256"])

    assert fake_native.last_workers is None


def test_compute_hashes_batch_native_skips_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_native = _FakeNative()
    monkeypatch.setattr(backend, "_hash_native", fake_native)

    file_path = tmp_path / "artifact.bin"
    file_path.write_bytes(b"artifact")
    dir_path = tmp_path / "dataset"
    dir_path.mkdir()

    result = backend.compute_hashes_batch([dir_path, file_path], ["sha256"])

    assert fake_native.last_paths == [str(file_path)]
    assert set(result) == {str(file_path)}
