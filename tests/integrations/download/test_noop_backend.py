"""
Unit tests for the NoOp download backend.
"""

from pathlib import Path

from roar.integrations.download.noop import NoOpDownloadBackend, should_skip_download


class TestNoOpDownloadBackend:
    """Tests for no-op download backend."""

    def test_download_seeded_content(self, tmp_path: Path):
        """Download writes seeded content."""
        backend = NoOpDownloadBackend(bucket="test-bucket")
        backend.seed("model.pt", b"fake model")

        local_path = tmp_path / "model.pt"
        backend.download("model.pt", local_path)

        assert local_path.read_bytes() == b"fake model"

    def test_download_unseeded_writes_placeholder(self, tmp_path: Path):
        """Download writes placeholder for unseeded keys."""
        backend = NoOpDownloadBackend(bucket="test-bucket")

        local_path = tmp_path / "model.pt"
        backend.download("model.pt", local_path)

        assert local_path.exists()
        assert b"noop-content-for-model.pt" in local_path.read_bytes()

    def test_exists_true_for_seeded(self):
        """exists returns True for seeded keys."""
        backend = NoOpDownloadBackend(bucket="test-bucket")
        backend.seed("model.pt", b"data")

        assert backend.exists("model.pt") is True

    def test_exists_false_for_unseeded(self):
        """exists returns False for unseeded keys."""
        backend = NoOpDownloadBackend(bucket="test-bucket")

        assert backend.exists("missing.pt") is False

    def test_list_keys_with_prefix(self):
        """list_keys returns seeded keys matching prefix."""
        backend = NoOpDownloadBackend(bucket="test-bucket")
        backend.seed("data/train.csv", b"train")
        backend.seed("data/test.csv", b"test")
        backend.seed("models/model.pt", b"model")

        keys = backend.list_keys("data")
        assert set(keys) == {"data/train.csv", "data/test.csv"}

    def test_download_creates_parent_dirs(self, tmp_path: Path):
        """Download creates parent directories."""
        backend = NoOpDownloadBackend(bucket="test-bucket")
        backend.seed("nested/file.pt", b"data")

        local_path = tmp_path / "a" / "b" / "file.pt"
        backend.download("nested/file.pt", local_path)

        assert local_path.exists()


class TestShouldSkipDownload:
    """Tests for should_skip_download."""

    def test_returns_false_by_default(self, monkeypatch):
        """Returns False when env var not set."""
        monkeypatch.delenv("ROAR_GET_SKIP_DOWNLOAD", raising=False)
        assert should_skip_download() is False

    def test_returns_true_when_set(self, monkeypatch):
        """Returns True when env var is 1."""
        monkeypatch.setenv("ROAR_GET_SKIP_DOWNLOAD", "1")
        assert should_skip_download() is True

    def test_returns_true_for_true_string(self, monkeypatch):
        """Returns True when env var is 'true'."""
        monkeypatch.setenv("ROAR_GET_SKIP_DOWNLOAD", "true")
        assert should_skip_download() is True
