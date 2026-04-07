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


class _FakeDeviceAuthServer(ThreadingHTTPServer):
    poll_count: int = 0
    last_authorization: str | None = None


class _FakeDeviceAuthHandler(BaseHTTPRequestHandler):
    server: _FakeDeviceAuthServer

    def do_POST(self) -> None:
        if self.path == "/api/v1/auth/device/start":
            self._write_json(
                200,
                {
                    "success": True,
                    "data": {
                        "device_code": "device-123",
                        "user_code": "ABCD-EFGH",
                        "verification_uri": "https://glaas.ai/login/device",
                        "verification_uri_complete": "https://glaas.ai/login/device?user_code=ABCD-EFGH",
                        "expires_in": 30,
                        "interval": 0,
                    },
                },
            )
            return

        if self.path == "/api/v1/auth/device/token":
            self.server.poll_count += 1
            if self.server.poll_count == 1:
                self._write_json(
                    200,
                    {"success": True, "data": {"status": "authorization_pending", "interval": 0}},
                )
                return

            self._write_json(
                200,
                {
                    "success": True,
                    "data": {
                        "status": "approved",
                        "token": "device-access-token",
                        "refresh_token": "refresh-token",
                        "id_token": "id-token",
                        "expires_at": "2030-01-01T01:00:00Z",
                        "provider": "github",
                        "issuer": "https://api.treqs.ai",
                        "client_id": "roar-cli",
                    },
                },
            )
            return

        self._write_json(404, {"success": False, "error": {"message": "Not found"}})

    def do_GET(self) -> None:
        if self.path != "/api/v1/auth/access-context":
            self._write_json(404, {"success": False, "error": {"message": "Not found"}})
            return

        self.server.last_authorization = self.headers.get("Authorization")
        self._write_json(
            200,
            {
                "success": True,
                "data": {
                    "user": {
                        "id": "user-123",
                        "sub": "sub-123",
                        "username": "trevor",
                        "email": "trevor@example.com",
                    },
                    "owners": [],
                    "projects_by_owner": {},
                },
            },
        )

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
def fake_device_auth_server() -> _FakeDeviceAuthServer:
    server = _FakeDeviceAuthServer(("127.0.0.1", 0), _FakeDeviceAuthHandler)
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


def test_login_device_flow_creates_global_auth_state(
    temp_git_repo: Path,
    tmp_path: Path,
    fake_device_auth_server: _FakeDeviceAuthServer,
) -> None:
    xdg_config_home = tmp_path / "xdg"
    glaas_api_url = f"http://127.0.0.1:{fake_device_auth_server.server_address[1]}"

    result = _run_roar(
        "login",
        cwd=temp_git_repo,
        env_overrides={
            "XDG_CONFIG_HOME": str(xdg_config_home),
            "GLAAS_API_URL": glaas_api_url,
            "ROAR_DISABLE_BROWSER_OPEN": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "Starting GLaaS device login" in result.stdout
    assert "ABCD-EFGH" in result.stdout
    assert "Waiting for approval" in result.stdout
    assert "Stored auth state for trevor <trevor@example.com>" in result.stdout
    assert fake_device_auth_server.last_authorization == "Bearer device-access-token"

    auth_path = xdg_config_home / "roar" / "auth.json"
    stored_auth = json.loads(auth_path.read_text(encoding="utf-8"))
    assert stored_auth["provider"] == "github"
    assert stored_auth["access_token"] == "device-access-token"
    assert stored_auth["refresh_token"] == "refresh-token"
    assert stored_auth["user"]["db_user_id"] == "user-123"
    assert stored_auth["user"]["username"] == "trevor"
