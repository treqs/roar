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


class _FakeGlaasProjectsServer(ThreadingHTTPServer):
    last_authorization: str | None = None


class _FakeGlaasProjectsHandler(BaseHTTPRequestHandler):
    server: _FakeGlaasProjectsServer

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
                    "owners": [
                        {
                            "id": "owner-123",
                            "type": "organization",
                            "username": "acme",
                            "display_name": "Acme AI",
                            "role": "admin",
                        }
                    ],
                    "projects_by_owner": {
                        "owner-123": [
                            {
                                "id": "proj-123",
                                "name": "alpha",
                                "description": "First project",
                                "visibility": "private",
                                "can_write": True,
                            },
                            {
                                "id": "proj-456",
                                "name": "beta",
                                "description": None,
                                "visibility": "public",
                                "can_write": True,
                            },
                        ]
                    },
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
def fake_glaas_projects_server() -> _FakeGlaasProjectsServer:
    server = _FakeGlaasProjectsServer(("127.0.0.1", 0), _FakeGlaasProjectsHandler)
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


def _write_auth_token_file(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "provider": "treqs-cognito",
                "issuer": "https://cognito-idp.us-east-2.amazonaws.com/us-east-2_test",
                "client_id": "test-client-id",
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "expires_at": "2030-04-01T00:00:00Z",
                "user": {
                    "sub": "cognito-sub-123",
                    "db_user_id": "user-123",
                    "email": "trevor@example.com",
                    "username": "trevor",
                },
            }
        ),
        encoding="utf-8",
    )


def test_projects_list_displays_project_summary(
    temp_git_repo: Path,
    tmp_path: Path,
    fake_glaas_projects_server: _FakeGlaasProjectsServer,
) -> None:
    xdg_config_home = tmp_path / "xdg"
    token_file = tmp_path / "token-file.json"
    _write_auth_token_file(token_file)

    glaas_api_url = f"http://127.0.0.1:{fake_glaas_projects_server.server_address[1]}"
    login_result = _run_roar(
        "login",
        "--token-file",
        str(token_file),
        cwd=temp_git_repo,
        env_overrides={"XDG_CONFIG_HOME": str(xdg_config_home), "GLAAS_API_URL": glaas_api_url},
    )
    assert login_result.returncode == 0, login_result.stderr

    result = _run_roar(
        "projects",
        "list",
        "--glaas-api-url",
        glaas_api_url,
        cwd=temp_git_repo,
        env_overrides={"XDG_CONFIG_HOME": str(xdg_config_home)},
    )

    assert result.returncode == 0, result.stderr
    assert "ID" in result.stdout
    assert "NAME" in result.stdout
    assert "DESCRIPTION" in result.stdout
    assert "VISIBILITY" in result.stdout
    assert "proj-123" in result.stdout
    assert "alpha" in result.stdout
    assert "First project" in result.stdout
    assert "private" in result.stdout
    assert "proj-456" in result.stdout
    assert "beta" in result.stdout
    assert "public" in result.stdout
    assert fake_glaas_projects_server.last_authorization == "Bearer access-token"
