from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _run_roar(*args: str, cwd: Path, env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(env_overrides)
    repo_root = str(Path(__file__).resolve().parents[2])
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{current_pythonpath}" if current_pythonpath else repo_root
    return subprocess.run(
        [sys.executable, "-m", "roar", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )


def test_login_imports_auth_state_from_token_file(temp_git_repo: Path, tmp_path: Path) -> None:
    xdg_config_home = tmp_path / "xdg"
    token_file = tmp_path / "token-file.json"
    token_file.write_text(
        json.dumps(
            {
                "version": 1,
                "provider": "treqs-cognito",
                "issuer": "https://cognito-idp.us-east-2.amazonaws.com/us-east-2_test",
                "client_id": "test-client-id",
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "expires_at": "2026-04-01T00:00:00Z",
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

    result = _run_roar(
        "login",
        "--token-file",
        str(token_file),
        cwd=temp_git_repo,
        env_overrides={"XDG_CONFIG_HOME": str(xdg_config_home)},
    )

    assert result.returncode == 0, result.stderr
    assert "Stored auth state for trevor <trevor@example.com>" in result.stdout

    stored_auth = json.loads((xdg_config_home / "roar" / "auth.json").read_text(encoding="utf-8"))
    assert stored_auth["access_token"] == "access-token"
    assert stored_auth["user"]["db_user_id"] == "user-123"


def test_logout_removes_global_auth_state(temp_git_repo: Path, tmp_path: Path) -> None:
    xdg_config_home = tmp_path / "xdg"
    auth_dir = xdg_config_home / "roar"
    auth_dir.mkdir(parents=True, exist_ok=True)
    auth_path = auth_dir / "auth.json"
    auth_path.write_text('{"version":1,"provider":"treqs-cognito","access_token": "***","user":{"sub":"sub"}}')

    result = _run_roar(
        "logout",
        cwd=temp_git_repo,
        env_overrides={"XDG_CONFIG_HOME": str(xdg_config_home)},
    )

    assert result.returncode == 0, result.stderr
    assert "Logged out" in result.stdout
    assert not auth_path.exists()


def test_login_rejects_token_file_without_access_token(temp_git_repo: Path, tmp_path: Path) -> None:
    xdg_config_home = tmp_path / "xdg"
    token_file = tmp_path / "token-file.json"
    token_file.write_text(
        json.dumps(
            {
                "version": 1,
                "provider": "treqs-cognito",
                "access_token": None,
                "user": {"sub": "cognito-sub-123"},
            }
        ),
        encoding="utf-8",
    )

    result = _run_roar(
        "login",
        "--token-file",
        str(token_file),
        cwd=temp_git_repo,
        env_overrides={"XDG_CONFIG_HOME": str(xdg_config_home)},
    )

    assert result.returncode != 0
    assert "Auth state is missing access_token" in result.stderr
    assert not (xdg_config_home / "roar" / "auth.json").exists()


def test_project_link_writes_repo_local_binding(temp_git_repo: Path, tmp_path: Path) -> None:
    result = _run_roar(
        "project",
        "link",
        "--owner-id",
        "org-456",
        "--owner-type",
        "organization",
        "--project-id",
        "proj-789",
        cwd=temp_git_repo,
        env_overrides={"XDG_CONFIG_HOME": str(tmp_path / "xdg")},
    )

    assert result.returncode == 0, result.stderr
    assert "Linked repo to organization org-456 / project proj-789" in result.stdout

    config_text = (temp_git_repo / ".roar" / "config.toml").read_text(encoding="utf-8")
    assert '[treqs]' in config_text
    assert 'owner_id = "org-456"' in config_text
    assert 'owner_type = "organization"' in config_text
    assert 'project_id = "proj-789"' in config_text
