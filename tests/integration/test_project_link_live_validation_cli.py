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


class _FakeGlaasServer(ThreadingHTTPServer):
    last_authorization: str | None = None


class _FakeGlaasHandler(BaseHTTPRequestHandler):
    server: _FakeGlaasServer

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
                        "sub": "cognito-sub-123",
                        "username": "trevor",
                        "email": "trevor@example.com",
                    },
                    "owners": [
                        {
                            "id": "user-123",
                            "type": "user",
                            "username": "trevor",
                            "display_name": "Trevor",
                            "role": "owner",
                        },
                        {
                            "id": "org-456",
                            "type": "organization",
                            "username": "acme",
                            "display_name": "Acme AI",
                            "role": "admin",
                        },
                    ],
                    "projects_by_owner": {
                        "org-456": [
                            {
                                "id": "proj-789",
                                "name": "foundation-models",
                                "visibility": "private",
                                "can_write": True,
                            },
                            {
                                "id": "proj-readonly",
                                "name": "readonly-project",
                                "visibility": "private",
                                "can_write": False,
                            },
                        ],
                        "user-123": [],
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
def fake_glaas_server() -> _FakeGlaasServer:
    server = _FakeGlaasServer(("127.0.0.1", 0), _FakeGlaasHandler)
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


def test_project_link_validates_against_access_context_server(
    temp_git_repo: Path,
    tmp_path: Path,
    fake_glaas_server: _FakeGlaasServer,
) -> None:
    xdg_config_home = tmp_path / "xdg"
    token_file = tmp_path / "token-file.json"
    _write_auth_token_file(token_file)

    glaas_api_url = f"http://127.0.0.1:{fake_glaas_server.server_address[1]}"
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
        "link",
        "proj-789",
        "--glaas-api-url",
        glaas_api_url,
        cwd=temp_git_repo,
        env_overrides={"XDG_CONFIG_HOME": str(xdg_config_home)},
    )

    assert result.returncode == 0, result.stderr
    assert "Resolved project proj-789 to organization org-456" in result.stdout
    assert fake_glaas_server.last_authorization == "Bearer access-token"

    config_text = (temp_git_repo / ".roar" / "config.toml").read_text(encoding="utf-8")
    assert 'owner_id = "org-456"' in config_text
    assert 'owner_type = "organization"' in config_text
    assert 'project_id = "proj-789"' in config_text


def test_projects_unlink_removes_repo_project_binding(
    temp_git_repo: Path,
    tmp_path: Path,
    fake_glaas_server: _FakeGlaasServer,
) -> None:
    xdg_config_home = tmp_path / "xdg"
    token_file = tmp_path / "token-file.json"
    _write_auth_token_file(token_file)

    glaas_api_url = f"http://127.0.0.1:{fake_glaas_server.server_address[1]}"
    login_result = _run_roar(
        "login",
        "--token-file",
        str(token_file),
        cwd=temp_git_repo,
        env_overrides={"XDG_CONFIG_HOME": str(xdg_config_home), "GLAAS_API_URL": glaas_api_url},
    )
    assert login_result.returncode == 0, login_result.stderr

    link_result = _run_roar(
        "projects",
        "link",
        "proj-789",
        "--glaas-api-url",
        glaas_api_url,
        cwd=temp_git_repo,
        env_overrides={"XDG_CONFIG_HOME": str(xdg_config_home)},
    )
    assert link_result.returncode == 0, link_result.stderr
    assert 'project_id = "proj-789"' in (temp_git_repo / ".roar" / "config.toml").read_text(
        encoding="utf-8"
    )

    unlink_result = _run_roar(
        "projects",
        "unlink",
        cwd=temp_git_repo,
        env_overrides={"XDG_CONFIG_HOME": str(xdg_config_home)},
    )
    assert unlink_result.returncode == 0, unlink_result.stderr
    assert "Unlinked repo project binding." in unlink_result.stdout

    config_text = (temp_git_repo / ".roar" / "config.toml").read_text(encoding="utf-8")
    assert "[treqs]" not in config_text
    assert 'project_id = "proj-789"' not in config_text


def test_project_link_rejects_readonly_project_from_access_context(
    temp_git_repo: Path,
    tmp_path: Path,
    fake_glaas_server: _FakeGlaasServer,
) -> None:
    xdg_config_home = tmp_path / "xdg"
    token_file = tmp_path / "token-file.json"
    _write_auth_token_file(token_file)

    glaas_api_url = f"http://127.0.0.1:{fake_glaas_server.server_address[1]}"
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
        "link",
        "proj-readonly",
        "--glaas-api-url",
        glaas_api_url,
        cwd=temp_git_repo,
        env_overrides={"XDG_CONFIG_HOME": str(xdg_config_home)},
    )

    combined_output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert (
        "Project is visible but not writable in GLaaS auth access context: proj-readonly"
        in combined_output
    )


def test_project_link_rejects_unknown_project_from_access_context(
    temp_git_repo: Path,
    tmp_path: Path,
    fake_glaas_server: _FakeGlaasServer,
) -> None:
    xdg_config_home = tmp_path / "xdg"
    token_file = tmp_path / "token-file.json"
    _write_auth_token_file(token_file)

    glaas_api_url = f"http://127.0.0.1:{fake_glaas_server.server_address[1]}"
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
        "link",
        "proj-missing",
        "--glaas-api-url",
        glaas_api_url,
        cwd=temp_git_repo,
        env_overrides={"XDG_CONFIG_HOME": str(xdg_config_home)},
    )

    combined_output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "Project not available in GLaaS auth access context: proj-missing" in combined_output
