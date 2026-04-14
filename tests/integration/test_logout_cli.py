from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


class _FakeLogoutServer(ThreadingHTTPServer):
    last_authorization: str | None = None


class _FakeLogoutHandler(BaseHTTPRequestHandler):
    server: _FakeLogoutServer

    def do_POST(self) -> None:
        if self.path != "/api/v1/auth/logout":
            self._write_json(404, {"success": False, "error": {"message": "Not found"}})
            return

        self.server.last_authorization = self.headers.get("Authorization")
        self._write_json(200, {"success": True, "data": {}})

    def log_message(self, format: str, *args: object) -> None:
        return

    def _write_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_logout_server() -> _FakeLogoutServer:
    server = _FakeLogoutServer(("127.0.0.1", 0), _FakeLogoutHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _run_roar(
    *args: str, cwd: Path, env_overrides: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(env_overrides)
    env.setdefault("ROAR_ENABLE_EXPERIMENTAL_ACCOUNT_COMMANDS", "1")
    repo_root = str(Path(__file__).resolve().parents[2])
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{repo_root}{os.pathsep}{current_pythonpath}" if current_pythonpath else repo_root
    )
    return subprocess.run(
        [sys.executable, "-m", "roar", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )


def test_logout_revokes_remote_session_before_clearing_local_auth(
    temp_git_repo: Path,
    tmp_path: Path,
    fake_logout_server: _FakeLogoutServer,
) -> None:
    xdg_config_home = tmp_path / "xdg"
    auth_dir = xdg_config_home / "roar"
    auth_dir.mkdir(parents=True, exist_ok=True)
    auth_path = auth_dir / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "version": 1,
                "provider": "treqs-device",
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "expires_at": "2030-01-01T00:00:00Z",
                "user": {"sub": "user-123", "username": "trevor"},
            }
        ),
        encoding="utf-8",
    )

    result = _run_roar(
        "logout",
        cwd=temp_git_repo,
        env_overrides={
            "XDG_CONFIG_HOME": str(xdg_config_home),
            "GLAAS_API_URL": f"http://127.0.0.1:{fake_logout_server.server_address[1]}",
        },
    )

    assert result.returncode == 0, result.stderr
    assert fake_logout_server.last_authorization == "Bearer access-token"
    assert not auth_path.exists()
