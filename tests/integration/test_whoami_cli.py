from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


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


def test_whoami_reports_missing_login(temp_git_repo: Path, tmp_path: Path) -> None:
    result = _run_roar(
        "whoami",
        cwd=temp_git_repo,
        env_overrides={"XDG_CONFIG_HOME": str(tmp_path / "xdg")},
    )

    combined_output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0
    assert "Not logged in" in combined_output
    assert "roar login" in combined_output


def test_whoami_prints_global_auth_identity_and_repo_binding(
    temp_git_repo: Path, tmp_path: Path
) -> None:
    xdg_config_home = tmp_path / "xdg"
    auth_dir = xdg_config_home / "roar"
    auth_dir.mkdir(parents=True, exist_ok=True)
    auth_path = auth_dir / "auth.json"
    auth_path.write_text(
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

    config_path = temp_git_repo / ".roar" / "config.toml"
    with config_path.open("a", encoding="utf-8") as f:
        f.write("\n[treqs]\n")
        f.write('owner_id = "org-456"\n')
        f.write('owner_type = "organization"\n')
        f.write('project_id = "proj-789"\n')

    result = _run_roar(
        "whoami",
        cwd=temp_git_repo,
        env_overrides={"XDG_CONFIG_HOME": str(xdg_config_home)},
    )

    assert result.returncode == 0, result.stderr
    assert "Authenticated as trevor <trevor@example.com>" in result.stdout
    assert "TReqs user id: user-123" in result.stdout
    assert "Token expires at: 2030-04-01T00:00:00Z" in result.stdout
    assert "Session status: valid (refresh token available)" in result.stdout
    assert "Repo binding: organization org-456 / project proj-789" in result.stdout


def test_whoami_flags_expired_session(temp_git_repo: Path, tmp_path: Path) -> None:
    xdg_config_home = tmp_path / "xdg"
    auth_dir = xdg_config_home / "roar"
    auth_dir.mkdir(parents=True, exist_ok=True)
    auth_path = auth_dir / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "version": 1,
                "provider": "treqs-device",
                "access_token": "expired-token",
                "refresh_token": None,
                "expires_at": "2020-01-01T00:00:00Z",
                "user": {
                    "sub": "user-123",
                    "email": "expired@example.com",
                    "username": "expired-user",
                },
            }
        ),
        encoding="utf-8",
    )

    result = _run_roar(
        "whoami",
        cwd=temp_git_repo,
        env_overrides={"XDG_CONFIG_HOME": str(xdg_config_home)},
    )

    assert result.returncode == 0, result.stderr
    assert "Session status: expired (run `roar login`)" in result.stdout


def test_whoami_flags_expiring_session_with_refresh_token(
    temp_git_repo: Path, tmp_path: Path
) -> None:
    xdg_config_home = tmp_path / "xdg"
    auth_dir = xdg_config_home / "roar"
    auth_dir.mkdir(parents=True, exist_ok=True)
    auth_path = auth_dir / "auth.json"
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=3)).replace(microsecond=0)
    auth_path.write_text(
        json.dumps(
            {
                "version": 1,
                "provider": "treqs-device",
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
                "user": {
                    "sub": "user-123",
                    "email": "soon@example.com",
                    "username": "soon-user",
                },
            }
        ),
        encoding="utf-8",
    )

    result = _run_roar(
        "whoami",
        cwd=temp_git_repo,
        env_overrides={"XDG_CONFIG_HOME": str(xdg_config_home)},
    )

    assert result.returncode == 0, result.stderr
    assert "Session status: expiring soon" in result.stdout
    assert "refresh token available" in result.stdout
