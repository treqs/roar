"""
Minimal S3-compatible HTTP server for integration testing.

Provides PUT, GET, HEAD operations with proper ETag headers
so the roar proxy can intercept and record S3 traffic.
"""

import hashlib
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class _S3Handler(BaseHTTPRequestHandler):
    """Request handler for the fake S3 server."""

    def _parse_bucket_key(self):
        """Extract (bucket, key) from URL path like /bucket/rest/of/key."""
        path = self.path.lstrip("/")
        if "/" not in path:
            return path, ""
        bucket, key = path.split("/", 1)
        return bucket, key

    def _etag(self, content: bytes) -> str:
        """Compute S3-style ETag: quoted md5 hex digest."""
        return f'"{hashlib.md5(content).hexdigest()}"'

    def do_PUT(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""
        bucket, key = self._parse_bucket_key()

        self.server._objects[(bucket, key)] = body
        self.server._requests.append(("PUT", self.path, time.time()))

        etag = self._etag(body)
        self.send_response(200)
        self.send_header("ETag", etag)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        bucket, key = self._parse_bucket_key()
        self.server._requests.append(("GET", self.path, time.time()))

        content = self.server._objects.get((bucket, key))
        if content is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        etag = self._etag(content)
        self.send_response(200)
        self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_HEAD(self):
        bucket, key = self._parse_bucket_key()
        self.server._requests.append(("HEAD", self.path, time.time()))

        content = self.server._objects.get((bucket, key))
        if content is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        etag = self._etag(content)
        self.send_response(200)
        self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()

    def log_message(self, format, *args):
        """Suppress default stderr logging."""
        pass


class FakeS3Server:
    """Minimal S3-compatible HTTP server for integration testing.

    Usage::

        with FakeS3Server() as s3:
            s3.seed("my-bucket", "data.csv", b"col1,col2\\n")
            # ... make requests to s3.url ...
            assert any(m == "PUT" for m, _, _ in s3.requests)
    """

    def __init__(self):
        self._server = HTTPServer(("127.0.0.1", 0), _S3Handler)
        # Attach storage to the server so the handler can access it
        self._server._objects = {}
        self._server._requests = []
        self._thread = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def requests(self) -> list[tuple[str, str, float]]:
        """List of (method, path, timestamp) for all received requests."""
        return self._server._requests

    def seed(self, bucket: str, key: str, content: bytes) -> None:
        """Pre-populate an object in the fake S3 store."""
        self._server._objects[(bucket, key)] = content

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
