"""Product-path coverage for the `roar auth` CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


class _FakeGlaasAuthServer(ThreadingHTTPServer):
    auth_status_code = 200
    last_authorization: str | None = None


class _FakeGlaasAuthHandler(BaseHTTPRequestHandler):
    server: _FakeGlaasAuthServer

    def do_GET(self) -> None:
        if self.path == "/api/v1/health":
            self._write_json(200, {"status": "ok"})
            return

        if self.path == "/api/v1/sessions?limit=1":
            self.server.last_authorization = self.headers.get("Authorization")
            if self.server.auth_status_code == 200 and self.server.last_authorization:
                self._write_json(200, {"success": True, "data": {"items": [], "pagination": {}}})
                return

            self._write_json(
                401,
                {"success": False, "error": {"message": "Unknown key", "code": "UNAUTHORIZED"}},
            )
            return

        self._write_json(404, {"detail": "Not found"})

    def log_message(self, format: str, *args: object) -> None:
        return

    def _write_json(self, status: int, payload: dict[str, str]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _run_roar_auth(
    *args: str, cwd: Path, env_overrides: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "roar", "auth", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def ssh_keypair(tmp_path: Path) -> Path:
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen is required for auth CLI tests")

    key_path = tmp_path / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path), "-C", "roar-test"],
        check=True,
        capture_output=True,
    )
    return key_path


@pytest.fixture
def fake_glaas_auth_server() -> _FakeGlaasAuthServer:
    server = _FakeGlaasAuthServer(("127.0.0.1", 0), _FakeGlaasAuthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_auth_register_prints_public_key(temp_git_repo: Path, ssh_keypair: Path) -> None:
    pubkey_path = Path(f"{ssh_keypair}.pub")
    pubkey_content = pubkey_path.read_text().strip()

    result = _run_roar_auth(
        "register",
        cwd=temp_git_repo,
        env_overrides={"ROAR_SSH_KEY": str(ssh_keypair)},
    )

    assert result.returncode == 0, result.stderr
    assert "Your SSH public key:" in result.stdout
    assert pubkey_content in result.stdout
    assert f"Path: {pubkey_path}" in result.stdout


def test_auth_key_copy_recommends_login_and_avoids_signup_verb(
    temp_git_repo: Path, ssh_keypair: Path
) -> None:
    # The website is GitHub sign-in, not SSH-key "sign up"; and browser
    # login is the recommended default. Guard the corrected copy.
    result = _run_roar_auth(
        "key",
        cwd=temp_git_repo,
        env_overrides={"ROAR_SSH_KEY": str(ssh_keypair)},
    )

    assert result.returncode == 0, result.stderr
    assert "sign up" not in result.stdout.lower()
    # Points at the deep-linkable key page and recommends browser login.
    assert "/settings/ssh-key" in result.stdout
    assert "roar login" in result.stdout


def test_auth_key_url_follows_dev_api(temp_git_repo: Path, ssh_keypair: Path) -> None:
    # Pointed at a dev API, the key-registration link must follow to dev
    # (derived web URL), not hardcode prod glaas.ai.
    result = _run_roar_auth(
        "key",
        cwd=temp_git_repo,
        env_overrides={
            "ROAR_SSH_KEY": str(ssh_keypair),
            "ROAR_GLAAS__URL": "https://api.dev.glaas.ai",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "https://dev.glaas.ai/settings/ssh-key" in result.stdout
    assert "https://glaas.ai/settings/ssh-key" not in result.stdout


def test_auth_status_prefers_env_glaas_url(
    temp_git_repo: Path,
    ssh_keypair: Path,
    fake_glaas_auth_server: _FakeGlaasAuthServer,
) -> None:
    fake_glaas_url = f"http://127.0.0.1:{fake_glaas_auth_server.server_address[1]}"
    result = _run_roar_auth(
        "status",
        cwd=temp_git_repo,
        env_overrides={
            "ROAR_SSH_KEY": str(ssh_keypair),
            "GLAAS_URL": fake_glaas_url,
        },
    )

    assert result.returncode == 0, result.stderr
    assert f"Server URL: {fake_glaas_url}" in result.stdout
    assert f"SSH key: {ssh_keypair}.pub" in result.stdout
    assert "Fingerprint: SHA256:" in result.stdout
    assert "https://api.glaas.ai" not in result.stdout


def test_auth_test_succeeds_against_local_server(
    temp_git_repo: Path,
    ssh_keypair: Path,
    fake_glaas_auth_server: _FakeGlaasAuthServer,
) -> None:
    fake_glaas_url = f"http://127.0.0.1:{fake_glaas_auth_server.server_address[1]}"
    result = _run_roar_auth(
        "test",
        cwd=temp_git_repo,
        env_overrides={
            "ROAR_SSH_KEY": str(ssh_keypair),
            "GLAAS_URL": fake_glaas_url,
        },
    )

    assert result.returncode == 0, result.stderr
    assert f"Testing connection to {fake_glaas_url}..." in result.stdout
    assert "Server is reachable." in result.stdout
    assert "Testing authentication..." in result.stdout
    assert "Authentication successful!" in result.stdout
    assert fake_glaas_auth_server.last_authorization is not None
    assert "Signature keyid=" in fake_glaas_auth_server.last_authorization


def test_auth_test_surfaces_unauthorized_response(
    temp_git_repo: Path,
    ssh_keypair: Path,
    fake_glaas_auth_server: _FakeGlaasAuthServer,
) -> None:
    fake_glaas_auth_server.auth_status_code = 401
    fake_glaas_url = f"http://127.0.0.1:{fake_glaas_auth_server.server_address[1]}"
    result = _run_roar_auth(
        "test",
        cwd=temp_git_repo,
        env_overrides={
            "ROAR_SSH_KEY": str(ssh_keypair),
            "GLAAS_URL": fake_glaas_url,
        },
    )

    combined_output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "Authentication failed: Unknown key" in combined_output
    # Error must direct the user to the deep-linkable key page and mention the
    # browser-based alternative. The old copy pointed at https://glaas.ai
    # (homepage) which doesn't accept key paste.
    assert "https://glaas.ai/settings/ssh-key" in combined_output
    assert "roar login" in combined_output
    assert fake_glaas_auth_server.last_authorization is not None
