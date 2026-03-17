"""Product-path coverage for the local `roar get` CLI."""

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


class _DownloadHandler(BaseHTTPRequestHandler):
    """Serve static download payloads for local `roar get` tests."""

    def _write_headers(self, status_code: int, content: bytes) -> None:
        self.send_response(status_code)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()

    def do_GET(self) -> None:
        content = self.server.payloads.get(self.path)  # type: ignore[attr-defined]
        if content is None:
            self._write_headers(404, b"")
            return

        self._write_headers(200, content)
        self.wfile.write(content)

    def do_HEAD(self) -> None:
        content = self.server.payloads.get(self.path)  # type: ignore[attr-defined]
        if content is None:
            self._write_headers(404, b"")
            return

        self._write_headers(200, content)

    def log_message(self, format: str, *args: object) -> None:
        """Suppress test-server stderr noise."""


class _DownloadServer:
    def __init__(self, payloads: dict[str, bytes]):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _DownloadHandler)
        self._server.payloads = payloads  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    def url_for(self, path: str) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}{path}"

    def __enter__(self) -> _DownloadServer:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


@pytest.fixture
def download_server() -> _DownloadServer:
    with _DownloadServer(
        {
            "/artifacts/model.bin": b"pretrained-weights",
            "/artifacts/weights.bin": b"weights-for-dry-run",
        }
    ) as server:
        yield server


def _query_rows(repo_path: Path, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    db_path = repo_path / ".roar" / "roar.db"
    assert db_path.exists(), ".roar/roar.db not found"
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


def test_get_downloads_default_filename_and_records_local_job(
    temp_git_repo: Path,
    roar_cli,
    download_server: _DownloadServer,
) -> None:
    source_url = download_server.url_for("/artifacts/model.bin")

    result = roar_cli("get", source_url, "-m", "download model")

    assert result.returncode == 0
    assert f"Downloaded 1 file(s) from {source_url}" in result.stdout
    assert "Job created: step" in result.stdout

    downloaded_path = temp_git_repo / "model.bin"
    assert downloaded_path.exists()
    assert downloaded_path.read_bytes() == b"pretrained-weights"

    jobs = _query_rows(
        temp_git_repo,
        "SELECT id, job_type, command, metadata FROM jobs ORDER BY id",
    )
    assert len(jobs) == 1
    assert jobs[0]["job_type"] == "get"
    assert source_url in jobs[0]["command"]

    metadata = json.loads(jobs[0]["metadata"])
    assert metadata["get"]["source"] == source_url
    assert metadata["get"]["message"] == "download model"


def test_get_dry_run_does_not_download_or_record_job(
    temp_git_repo: Path,
    roar_cli,
    download_server: _DownloadServer,
) -> None:
    source_url = download_server.url_for("/artifacts/weights.bin")

    result = roar_cli("get", source_url, "--dry-run")

    assert result.returncode == 0
    assert "Dry run - would download:" in result.stdout
    assert str(temp_git_repo / "weights.bin") in result.stdout
    assert not (temp_git_repo / "weights.bin").exists()

    jobs = _query_rows(temp_git_repo, "SELECT id FROM jobs ORDER BY id")
    assert jobs == []


def test_get_then_run_creates_local_lineage_chain(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    download_server: _DownloadServer,
) -> None:
    script = temp_git_repo / "consume_model.py"
    script.write_text(
        "from pathlib import Path\n"
        "data = Path('model.bin').read_bytes()\n"
        "Path('report.bin').write_bytes(data + b'::consumed')\n"
    )
    git_commit("Add consumer script")

    source_url = download_server.url_for("/artifacts/model.bin")
    get_result = roar_cli("get", source_url)
    assert get_result.returncode == 0
    assert (temp_git_repo / "model.bin").read_bytes() == b"pretrained-weights"

    git_commit("Commit downloaded artifact")

    run_result = roar_cli("run", python_exe, "consume_model.py")
    assert run_result.returncode == 0
    assert (temp_git_repo / "report.bin").read_bytes() == b"pretrained-weights::consumed"

    lineage_result = roar_cli("lineage", "report.bin")
    lineage = json.loads(lineage_result.stdout)

    commands = [job["command"] for job in lineage["jobs"]]
    assert any("roar get" in command and source_url in command for command in commands)
    assert any("consume_model.py" in command for command in commands)
