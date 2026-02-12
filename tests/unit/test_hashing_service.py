"""Unit tests for default hashing service behavior."""

from __future__ import annotations

import os
from pathlib import Path

from roar.db.services.hashing import DefaultHashingService


class FakeHashCache:
    """In-memory hash cache repository for tests."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], tuple[str, int, float]] = {}

    def get_cached_hash(self, path: str, algorithm: str = "blake3") -> str | None:
        row = self._rows.get((path, algorithm))
        if row is None:
            return None
        digest, size, mtime = row
        try:
            stat = os.stat(path)
        except OSError:
            return None
        if stat.st_size == size and abs(stat.st_mtime - mtime) < 0.001:
            return digest
        return None

    def get_cached_hashes(self, path: str) -> dict[str, str]:
        try:
            stat = os.stat(path)
        except OSError:
            return {}
        output: dict[str, str] = {}
        for (cache_path, algorithm), (digest, size, mtime) in self._rows.items():
            if cache_path != path:
                continue
            if stat.st_size == size and abs(stat.st_mtime - mtime) < 0.001:
                output[algorithm] = digest
        return output

    def cache_hash(self, path: str, algorithm: str, digest: str, size: int, mtime: float) -> None:
        self._rows[(path, algorithm)] = (digest, size, mtime)

    def cache_hashes(self, path: str, hashes: dict[str, str], size: int, mtime: float) -> None:
        for algorithm, digest in hashes.items():
            self.cache_hash(path, algorithm, digest, size, mtime)

    def invalidate(self, path: str, algorithm: str | None = None) -> None:
        keys = [
            key
            for key in self._rows
            if key[0] == path and (algorithm is None or key[1] == algorithm)
        ]
        for key in keys:
            del self._rows[key]

    def clean_stale(self, max_age_days: int = 30) -> None:
        del max_age_days


def test_compute_hashes_uses_cache_after_first_call(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"test artifact data")

    calls = {"count": 0}

    def fake_backend(paths: list[str], algorithms: list[str]) -> dict[str, dict[str, str]]:
        calls["count"] += 1
        return {
            path_value: {algorithm: f"{algorithm}-digest" for algorithm in algorithms}
            for path_value in paths
        }

    monkeypatch.setattr("roar.db.services.hashing.compute_hashes_batch_backend", fake_backend)
    service = DefaultHashingService(FakeHashCache())

    first = service.compute_hashes(str(path), ["blake3", "sha256"])
    second = service.compute_hashes(str(path), ["blake3", "sha256"])

    assert first == {"blake3": "blake3-digest", "sha256": "sha256-digest"}
    assert second == first
    assert calls["count"] == 1


def test_compute_hash_reads_from_cache_when_available(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"another artifact")

    calls = {"count": 0}

    def fake_backend(paths: list[str], algorithms: list[str]) -> dict[str, dict[str, str]]:
        calls["count"] += 1
        return {
            path_value: {algorithm: f"{algorithm}-digest" for algorithm in algorithms}
            for path_value in paths
        }

    monkeypatch.setattr("roar.db.services.hashing.compute_hashes_batch_backend", fake_backend)
    service = DefaultHashingService(FakeHashCache())

    assert service.compute_hash(str(path), "blake3") == "blake3-digest"
    assert service.compute_hash(str(path), "blake3") == "blake3-digest"
    assert calls["count"] == 1


def test_compute_hash_missing_file_returns_none(tmp_path: Path) -> None:
    service = DefaultHashingService(FakeHashCache())
    assert service.compute_hash(str(tmp_path / "missing.bin"), "blake3") is None
