from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


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


def test_login_dev_email_requires_explicit_feature_flag(
    temp_git_repo: Path, tmp_path: Path
) -> None:
    xdg_config_home = tmp_path / "xdg"

    blocked_result = _run_roar(
        "login",
        "--dev-email",
        "treqs-user@treqs.ai",
        cwd=temp_git_repo,
        env_overrides={"XDG_CONFIG_HOME": str(xdg_config_home)},
    )

    combined_output = f"{blocked_result.stdout}\n{blocked_result.stderr}"
    assert blocked_result.returncode != 0
    assert "ROAR_ENABLE_DEV_EMAIL_LOGIN=1" in combined_output
    assert not (xdg_config_home / "roar" / "auth.json").exists()

    enabled_result = _run_roar(
        "login",
        "--dev-email",
        "treqs-user@treqs.ai",
        cwd=temp_git_repo,
        env_overrides={
            "XDG_CONFIG_HOME": str(xdg_config_home),
            "ROAR_ENABLE_DEV_EMAIL_LOGIN": "1",
        },
    )

    assert enabled_result.returncode == 0, enabled_result.stderr
    assert "Stored auth state for treqs-user@treqs.ai" in enabled_result.stdout

    auth_path = xdg_config_home / "roar" / "auth.json"
    stored_auth = json.loads(auth_path.read_text(encoding="utf-8"))
    assert stored_auth["provider"] == "treqs-dev"
    assert stored_auth["access_token"] == "dev-email:treqs-user@treqs.ai"
    assert stored_auth["user"]["email"] == "treqs-user@treqs.ai"
    assert stored_auth["user"]["username"] == "treqs-user@treqs.ai"
