"""
Unit tests for no-op storage backend.

Tests the skip-upload functionality for testing.
"""

from pathlib import Path

from roar.integrations.storage import NoOpBackend, should_skip_upload


class TestShouldSkipUpload:
    """Tests for skip upload detection."""

    def test_skip_upload_when_env_set_1(self, monkeypatch):
        """Returns True when ROAR_PUT_SKIP_UPLOAD=1."""
        monkeypatch.setenv("ROAR_PUT_SKIP_UPLOAD", "1")
        assert should_skip_upload() is True

    def test_skip_upload_when_env_set_true(self, monkeypatch):
        """Returns True when ROAR_PUT_SKIP_UPLOAD=true."""
        monkeypatch.setenv("ROAR_PUT_SKIP_UPLOAD", "true")
        assert should_skip_upload() is True

    def test_skip_upload_when_env_set_yes(self, monkeypatch):
        """Returns True when ROAR_PUT_SKIP_UPLOAD=yes."""
        monkeypatch.setenv("ROAR_PUT_SKIP_UPLOAD", "yes")
        assert should_skip_upload() is True

    def test_skip_upload_when_env_not_set(self, monkeypatch):
        """Returns False when ROAR_PUT_SKIP_UPLOAD not set."""
        monkeypatch.delenv("ROAR_PUT_SKIP_UPLOAD", raising=False)
        assert should_skip_upload() is False

    def test_skip_upload_when_env_set_0(self, monkeypatch):
        """Returns False when ROAR_PUT_SKIP_UPLOAD=0."""
        monkeypatch.setenv("ROAR_PUT_SKIP_UPLOAD", "0")
        assert should_skip_upload() is False


class TestNoOpBackend:
    """Tests for NoOpBackend."""

    def test_upload_returns_valid_url(self, tmp_path: Path):
        """Upload returns a valid-looking URL."""
        test_file = tmp_path / "model.pt"
        test_file.write_bytes(b"model data")

        backend = NoOpBackend(bucket="my-bucket", prefix="models", scheme="s3")

        url = backend.upload(test_file, "model.pt")

        assert url == "s3://my-bucket/models/model.pt"

    def test_upload_tracks_uploaded_files(self, tmp_path: Path):
        """Upload tracks which files were 'uploaded'."""
        test_file = tmp_path / "model.pt"
        test_file.write_bytes(b"model data")

        backend = NoOpBackend(bucket="bucket", prefix="")

        backend.upload(test_file, "model.pt")

        assert backend.exists("model.pt") is True
        assert backend.exists("other.pt") is False

    def test_upload_tracks_file_size(self, tmp_path: Path):
        """Upload tracks the size of uploaded files."""
        test_file = tmp_path / "model.pt"
        test_file.write_bytes(b"x" * 1000)

        backend = NoOpBackend(bucket="bucket", prefix="")

        backend.upload(test_file, "model.pt")

        assert backend.get_uploaded_size("model.pt") == 1000

    def test_get_uploaded_keys(self, tmp_path: Path):
        """Get all uploaded keys."""
        file1 = tmp_path / "a.pt"
        file2 = tmp_path / "b.pt"
        file1.write_bytes(b"a")
        file2.write_bytes(b"b")

        backend = NoOpBackend(bucket="bucket", prefix="")

        backend.upload(file1, "a.pt")
        backend.upload(file2, "b.pt")

        keys = backend.get_uploaded_keys()
        assert set(keys) == {"a.pt", "b.pt"}

    def test_url_with_different_scheme(self, tmp_path: Path):
        """Backend uses the specified scheme in URLs."""
        test_file = tmp_path / "model.pt"
        test_file.write_bytes(b"data")

        backend = NoOpBackend(bucket="bucket", prefix="path", scheme="gs")

        url = backend.upload(test_file, "model.pt")

        assert url == "gs://bucket/path/model.pt"
