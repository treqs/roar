"""Product-path coverage for explicit public publication intent."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from .fake_glaas import FakeGlaasServer

pytestmark = pytest.mark.integration


@pytest.fixture
def fake_glaas_publish_server() -> FakeGlaasServer:
    with FakeGlaasServer() as server:
        yield server


def _configure_unbound_repo(repo: Path, roar_cli, fake_glaas_url: str) -> dict[str, str]:
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    xdg_config_home = repo / ".xdg"
    token_file = repo / "token-file.json"
    token_file.write_text(
        json.dumps(
            {
                "version": 1,
                "provider": "treqs-cognito",
                "access_token": "test-access-token",
                "user": {
                    "sub": "treqs-user-123",
                    "db_user_id": "user-123",
                    "email": "trevor@example.com",
                    "username": "trevor",
                },
            }
        ),
        encoding="utf-8",
    )
    env = {"XDG_CONFIG_HOME": str(xdg_config_home), "GLAAS_API_URL": fake_glaas_url}
    roar_cli("login", "--token-file", str(token_file), env_overrides=env)
    roar_cli("config", "set", "glaas.url", fake_glaas_url, env_overrides=env)
    roar_cli("config", "set", "glaas.web_url", fake_glaas_url, env_overrides=env)
    return env


def _create_register_fixture(repo: Path, roar_cli, git_commit, python_exe: str, env: dict[str, str]) -> None:
    input_path = repo / "input.txt"
    input_path.write_text("register me\n")
    script = repo / "generate_report.py"
    script.write_text(
        "from pathlib import Path\n"
        "content = Path('input.txt').read_text()\n"
        "Path('report.txt').write_text(content.upper())\n"
    )
    git_commit("Add register public fixture")
    run_result = roar_cli("run", python_exe, "generate_report.py", env_overrides=env)
    assert run_result.returncode == 0
    git_commit("Commit register public outputs")


def _create_put_fixture(repo: Path, roar_cli, git_commit, python_exe: str, env: dict[str, str]) -> None:
    script = repo / "train.py"
    script.write_text(
        "from pathlib import Path\n"
        "Path('model.pt').write_bytes(b'fake model weights' * 10)\n"
        "Path('metrics.json').write_text('{\"accuracy\": 0.95}')\n"
    )
    git_commit("Add put public fixture")
    run_result = roar_cli("run", python_exe, "train.py", env_overrides=env)
    assert run_result.returncode == 0
    git_commit("Commit put public outputs")


def test_register_requires_explicit_public_flag_when_repo_has_no_binding(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    fake_glaas_publish_server: FakeGlaasServer,
) -> None:
    env = _configure_unbound_repo(temp_git_repo, roar_cli, fake_glaas_publish_server.base_url)
    _create_register_fixture(temp_git_repo, roar_cli, git_commit, python_exe, env)

    result = roar_cli("register", "report.txt", "--yes", env_overrides=env, check=False)

    assert result.returncode != 0
    combined = f"{result.stdout}\n{result.stderr}"
    assert "No GLaaS repo binding found" in combined
    assert "--public" in combined
    assert fake_glaas_publish_server.session_registrations == []


def test_register_public_succeeds_without_repo_binding_when_public_flag_is_set(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    fake_glaas_publish_server: FakeGlaasServer,
) -> None:
    env = _configure_unbound_repo(temp_git_repo, roar_cli, fake_glaas_publish_server.base_url)
    _create_register_fixture(temp_git_repo, roar_cli, git_commit, python_exe, env)

    result = roar_cli("register", "report.txt", "--yes", "--public", env_overrides=env)

    assert result.returncode == 0
    assert len(fake_glaas_publish_server.session_registrations) == 1
    assert "scope_request" not in fake_glaas_publish_server.session_registrations[0]


def test_put_requires_explicit_public_flag_when_repo_has_no_binding(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    monkeypatch,
    fake_glaas_publish_server: FakeGlaasServer,
) -> None:
    env = _configure_unbound_repo(temp_git_repo, roar_cli, fake_glaas_publish_server.base_url)
    _create_put_fixture(temp_git_repo, roar_cli, git_commit, python_exe, env)
    monkeypatch.setenv("ROAR_PUT_SKIP_UPLOAD", "1")

    result = roar_cli(
        "put",
        "model.pt",
        "s3://test-bucket/models",
        "-m",
        "publish model",
        env_overrides=env,
        check=False,
    )

    assert result.returncode != 0
    combined = f"{result.stdout}\n{result.stderr}"
    assert "No GLaaS repo binding found" in combined
    assert "--public" in combined
    assert fake_glaas_publish_server.session_registrations == []


def test_put_public_succeeds_without_repo_binding_when_public_flag_is_set(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    monkeypatch,
    fake_glaas_publish_server: FakeGlaasServer,
) -> None:
    env = _configure_unbound_repo(temp_git_repo, roar_cli, fake_glaas_publish_server.base_url)
    _create_put_fixture(temp_git_repo, roar_cli, git_commit, python_exe, env)
    monkeypatch.setenv("ROAR_PUT_SKIP_UPLOAD", "1")

    result = roar_cli(
        "put",
        "model.pt",
        "s3://test-bucket/models",
        "-m",
        "publish model",
        "--public",
        env_overrides=env,
    )

    assert result.returncode == 0
    assert len(fake_glaas_publish_server.session_registrations) == 1
    assert "scope_request" not in fake_glaas_publish_server.session_registrations[0]
