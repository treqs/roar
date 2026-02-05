"""
Unit tests for the HTTP download backend.
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

from roar.services.get.backends.http import HTTPBackend


class MockHTTPHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler for testing."""

    # Class-level storage for responses
    responses: dict = {}  # noqa: RUF012

    def do_GET(self):
        if self.path in self.responses:
            resp = self.responses[self.path]
            self.send_response(resp.get("status", 200))
            for key, val in resp.get("headers", {}).items():
                self.send_header(key, val)
            self.end_headers()
            self.wfile.write(resp.get("body", b""))
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def do_HEAD(self):
        if self.path in self.responses:
            resp = self.responses[self.path]
            self.send_response(resp.get("status", 200))
            for key, val in resp.get("headers", {}).items():
                self.send_header(key, val)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress server logs during tests."""
        pass


@pytest.fixture
def http_server():
    """Start a local HTTP server for testing."""
    # Reset responses
    MockHTTPHandler.responses = {}
    server = HTTPServer(("127.0.0.1", 0), MockHTTPHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", MockHTTPHandler
    server.shutdown()


class TestHTTPBackendDownload:
    """Tests for HTTP download functionality."""

    def test_download_simple_file(self, http_server, tmp_path: Path):
        """Download a simple file from HTTP."""
        base_url, handler = http_server
        handler.responses["/model.pt"] = {
            "body": b"fake model weights",
            "headers": {"Content-Length": "18"},
        }

        backend = HTTPBackend(f"{base_url}/model.pt")
        local_path = tmp_path / "model.pt"
        backend.download("model.pt", local_path)

        assert local_path.exists()
        assert local_path.read_bytes() == b"fake model weights"

    def test_download_creates_parent_dirs(self, http_server, tmp_path: Path):
        """Download creates parent directories if needed."""
        base_url, handler = http_server
        handler.responses["/data.csv"] = {"body": b"a,b,c\n1,2,3"}

        backend = HTTPBackend(f"{base_url}/data.csv")
        local_path = tmp_path / "nested" / "dir" / "data.csv"
        backend.download("data.csv", local_path)

        assert local_path.exists()
        assert local_path.read_bytes() == b"a,b,c\n1,2,3"

    def test_download_404_raises_filenotfound(self, http_server, tmp_path: Path):
        """Download of non-existent URL raises FileNotFoundError."""
        base_url, _ = http_server
        backend = HTTPBackend(f"{base_url}/missing.pt")

        with pytest.raises(FileNotFoundError, match="not found"):
            backend.download("missing.pt", tmp_path / "missing.pt")

    def test_download_large_content(self, http_server, tmp_path: Path):
        """Download handles content larger than chunk size."""
        base_url, handler = http_server
        large_content = b"x" * 100_000  # Larger than 8192 chunk size
        handler.responses["/large.bin"] = {"body": large_content}

        backend = HTTPBackend(f"{base_url}/large.bin")
        local_path = tmp_path / "large.bin"
        backend.download("large.bin", local_path)

        assert local_path.exists()
        assert local_path.read_bytes() == large_content


class TestHTTPBackendExists:
    """Tests for HTTP existence check."""

    def test_exists_returns_true_for_200(self, http_server):
        """exists returns True for a valid URL."""
        base_url, handler = http_server
        handler.responses["/model.pt"] = {"body": b"data"}

        backend = HTTPBackend(f"{base_url}/model.pt")
        assert backend.exists("model.pt") is True

    def test_exists_returns_false_for_404(self, http_server):
        """exists returns False for a missing URL."""
        base_url, _ = http_server
        backend = HTTPBackend(f"{base_url}/missing.pt")
        assert backend.exists("missing.pt") is False


class TestHTTPBackendListKeys:
    """Tests for HTTP list_keys."""

    def test_list_keys_returns_url_path(self, http_server):
        """list_keys returns the URL path as a single key."""
        base_url, _ = http_server
        backend = HTTPBackend(f"{base_url}/models/model.pt")

        keys = backend.list_keys("models")
        assert keys == ["models/model.pt"]


class TestHTTPBackendFilename:
    """Tests for Content-Disposition filename extraction."""

    def test_get_filename_from_content_disposition(self, http_server):
        """Extract filename from Content-Disposition header."""
        base_url, handler = http_server
        handler.responses["/download"] = {
            "body": b"data",
            "headers": {
                "Content-Disposition": 'attachment; filename="model_v2.pt"',
            },
        }

        backend = HTTPBackend(f"{base_url}/download")
        filename = backend.get_filename_from_headers()
        assert filename == "model_v2.pt"

    def test_get_filename_returns_none_without_header(self, http_server):
        """Returns None when no Content-Disposition header."""
        base_url, handler = http_server
        handler.responses["/model.pt"] = {"body": b"data"}

        backend = HTTPBackend(f"{base_url}/model.pt")
        filename = backend.get_filename_from_headers()
        assert filename is None
