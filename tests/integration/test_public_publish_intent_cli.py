"""Product-path coverage for explicit public publication intent."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from .fake_glaas import FakeGlaasServer

pytestmark = pytest.mark.integration


@pytest.fixture
def fake_glaas_publish_server() -> FakeGlaasServer:
    with FakeGlaasServer() as server:
        yield server


@pytest.fixture
def ssh_keypair(tmp_path: Path) -> Path:
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen is required for SSH-auth publish integration tests")

    key_path = tmp_path / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path), "-C", "roar-test"],
        check=True,
        capture_output=True,
    )
    return key_path


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
    env = {
        "XDG_CONFIG_HOME": str(xdg_config_home),
        "GLAAS_API_URL": fake_glaas_url,
        "ROAR_ENABLE_EXPERIMENTAL_ACCOUNT_COMMANDS": "1",
    }
    roar_cli("login", "--token-file", str(token_file), env_overrides=env)
    roar_cli("config", "set", "glaas.url", fake_glaas_url, env_overrides=env)
    roar_cli("config", "set", "glaas.web_url", fake_glaas_url, env_overrides=env)
    return env


def _configure_unbound_repo_for_ssh_only(
    repo: Path,
    roar_cli,
    fake_glaas_url: str,
    ssh_keypair: Path,
) -> dict[str, str]:
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    xdg_config_home = repo / ".xdg"
    home_dir = repo / ".home"
    home_dir.mkdir(exist_ok=True)
    env = {
        "HOME": str(home_dir),
        "XDG_CONFIG_HOME": str(xdg_config_home),
        "GLAAS_API_URL": fake_glaas_url,
        "ROAR_SSH_KEY": str(ssh_keypair),
    }
    roar_cli("config", "set", "glaas.url", fake_glaas_url, env_overrides=env)
    roar_cli("config", "set", "glaas.web_url", fake_glaas_url, env_overrides=env)
    return env


def _create_register_fixture(
    repo: Path, roar_cli, git_commit, python_exe: str, env: dict[str, str]
) -> None:
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


def _create_put_fixture(
    repo: Path, roar_cli, git_commit, python_exe: str, env: dict[str, str]
) -> None:
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


def _parse_session_hash(output: str) -> str:
    match = re.search(r"/dag/([0-9a-f]{64})", output)
    assert match is not None, f"Missing session URL in output: {output}"
    return match.group(1)


def _write_repo_binding(repo: Path) -> None:
    config_path = repo / ".roar" / "config.toml"
    config_text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        f'{config_text.rstrip()}\n\n[treqs]\nowner_id = "owner-test"\nowner_type = "organization"\nproject_id = "proj-test"\n',
        encoding="utf-8",
    )


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
    assert "Error: No GLaaS repo binding found" in combined
    assert "--public" in combined
    assert "Traceback (most recent call last)" not in combined
    assert "RuntimeError:" not in combined
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
    assert fake_glaas_publish_server.session_registrations == []
    assert len(fake_glaas_publish_server.registration_session_creations) == 1
    assert len(fake_glaas_publish_server.registration_session_finalizations) == 1
    assert "scope_request" not in fake_glaas_publish_server.registration_session_finalizations[0]


def test_register_public_with_valid_ssh_uses_authenticated_creator_identity_for_hash_and_registration(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    fake_glaas_publish_server: FakeGlaasServer,
    ssh_keypair: Path,
) -> None:
    ssh_env = _configure_unbound_repo_for_ssh_only(
        temp_git_repo,
        roar_cli,
        fake_glaas_publish_server.base_url,
        ssh_keypair,
    )
    _create_register_fixture(temp_git_repo, roar_cli, git_commit, python_exe, ssh_env)

    anonymous_env = {k: v for k, v in ssh_env.items() if k != "ROAR_SSH_KEY"}
    anonymous_preview = roar_cli(
        "register",
        "report.txt",
        "--dry-run",
        "--yes",
        "--public",
        env_overrides=anonymous_env,
    )
    anonymous_hash = _parse_session_hash(anonymous_preview.stdout)

    ssh_preview = roar_cli(
        "register",
        "report.txt",
        "--dry-run",
        "--yes",
        "--public",
        env_overrides=ssh_env,
    )
    ssh_hash = _parse_session_hash(ssh_preview.stdout)

    assert ssh_hash != anonymous_hash

    result = roar_cli("register", "report.txt", "--yes", "--public", env_overrides=ssh_env)

    assert result.returncode == 0
    assert fake_glaas_publish_server.session_registrations == []
    assert len(fake_glaas_publish_server.registration_session_creations) == 1
    assert len(fake_glaas_publish_server.registration_session_finalizations) == 1
    assert "scope_request" not in fake_glaas_publish_server.registration_session_finalizations[0]
    assert any(
        entry["path"] == "/api/v1/auth/me"
        and str(entry.get("authorization") or "").startswith("Signature ")
        for entry in fake_glaas_publish_server.auth_headers
    )


def test_register_public_with_valid_ssh_ignores_existing_repo_binding(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    fake_glaas_publish_server: FakeGlaasServer,
    ssh_keypair: Path,
) -> None:
    env = _configure_unbound_repo_for_ssh_only(
        temp_git_repo,
        roar_cli,
        fake_glaas_publish_server.base_url,
        ssh_keypair,
    )
    _write_repo_binding(temp_git_repo)
    _create_register_fixture(temp_git_repo, roar_cli, git_commit, python_exe, env)

    result = roar_cli(
        "register",
        "report.txt",
        "--yes",
        "--public",
        env_overrides=env,
    )

    assert result.returncode == 0
    assert fake_glaas_publish_server.session_registrations == []
    assert len(fake_glaas_publish_server.registration_session_creations) == 1
    assert len(fake_glaas_publish_server.registration_session_finalizations) == 1
    assert "scope_request" not in fake_glaas_publish_server.registration_session_finalizations[0]
    assert any(
        entry["path"] == "/api/v1/auth/me"
        and str(entry.get("authorization") or "").startswith("Signature ")
        for entry in fake_glaas_publish_server.auth_headers
    )


def test_register_public_uses_registration_sessions_with_ssh_only_auth(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    ssh_keypair: Path,
    fake_glaas_publish_server: FakeGlaasServer,
) -> None:
    env = _configure_unbound_repo_for_ssh_only(
        temp_git_repo,
        roar_cli,
        fake_glaas_publish_server.base_url,
        ssh_keypair,
    )
    _create_register_fixture(temp_git_repo, roar_cli, git_commit, python_exe, env)

    result = roar_cli("register", "report.txt", "--yes", "--public", env_overrides=env)

    assert result.returncode == 0
    assert fake_glaas_publish_server.session_registrations == []
    assert len(fake_glaas_publish_server.registration_session_creations) == 1
    assert len(fake_glaas_publish_server.registration_session_finalizations) == 1
    assert "scope_request" not in fake_glaas_publish_server.registration_session_finalizations[0]
    assert any(
        isinstance(entry.get("authorization"), str)
        and entry["authorization"].startswith("Signature ")
        for entry in fake_glaas_publish_server.auth_headers
    )


def test_register_scoped_ssh_only_publish_uses_registration_sessions(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    ssh_keypair: Path,
    fake_glaas_publish_server: FakeGlaasServer,
) -> None:
    env = _configure_unbound_repo_for_ssh_only(
        temp_git_repo,
        roar_cli,
        fake_glaas_publish_server.base_url,
        ssh_keypair,
    )
    _write_repo_binding(temp_git_repo)
    _create_register_fixture(temp_git_repo, roar_cli, git_commit, python_exe, env)

    result = roar_cli("register", "report.txt", "--yes", env_overrides=env)

    assert result.returncode == 0
    assert fake_glaas_publish_server.session_registrations == []
    assert len(fake_glaas_publish_server.registration_session_creations) == 1
    assert len(fake_glaas_publish_server.registration_session_finalizations) == 1
    assert fake_glaas_publish_server.registration_session_finalizations[0]["scope_request"] == {
        "owner_id": "owner-test",
        "owner_type": "organization",
        "project_id": "proj-test",
        "visibility": "private",
    }
    assert any(
        isinstance(entry.get("authorization"), str)
        and entry["authorization"].startswith("Signature ")
        for entry in fake_glaas_publish_server.auth_headers
    )


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
    assert fake_glaas_publish_server.session_registrations == []
    assert len(fake_glaas_publish_server.registration_session_creations) == 1
    assert len(fake_glaas_publish_server.registration_session_finalizations) == 1
    assert "scope_request" not in fake_glaas_publish_server.registration_session_finalizations[0]


def test_put_public_uses_registration_sessions_with_ssh_only_auth(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    monkeypatch,
    ssh_keypair: Path,
    fake_glaas_publish_server: FakeGlaasServer,
) -> None:
    env = _configure_unbound_repo_for_ssh_only(
        temp_git_repo,
        roar_cli,
        fake_glaas_publish_server.base_url,
        ssh_keypair,
    )
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
    assert fake_glaas_publish_server.session_registrations == []
    assert len(fake_glaas_publish_server.registration_session_creations) == 1
    assert len(fake_glaas_publish_server.registration_session_finalizations) == 1
    assert "scope_request" not in fake_glaas_publish_server.registration_session_finalizations[0]
    assert any(
        isinstance(entry.get("authorization"), str)
        and entry["authorization"].startswith("Signature ")
        for entry in fake_glaas_publish_server.auth_headers
    )


def test_put_scoped_ssh_only_publish_uses_registration_sessions(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    monkeypatch,
    ssh_keypair: Path,
    fake_glaas_publish_server: FakeGlaasServer,
) -> None:
    env = _configure_unbound_repo_for_ssh_only(
        temp_git_repo,
        roar_cli,
        fake_glaas_publish_server.base_url,
        ssh_keypair,
    )
    _write_repo_binding(temp_git_repo)
    _create_put_fixture(temp_git_repo, roar_cli, git_commit, python_exe, env)
    monkeypatch.setenv("ROAR_PUT_SKIP_UPLOAD", "1")

    result = roar_cli(
        "put",
        "model.pt",
        "s3://test-bucket/models",
        "-m",
        "publish model",
        env_overrides=env,
    )

    assert result.returncode == 0
    assert fake_glaas_publish_server.session_registrations == []
    assert len(fake_glaas_publish_server.registration_session_creations) == 1
    assert len(fake_glaas_publish_server.registration_session_finalizations) == 1
    assert fake_glaas_publish_server.registration_session_finalizations[0]["scope_request"] == {
        "owner_id": "owner-test",
        "owner_type": "organization",
        "project_id": "proj-test",
        "visibility": "private",
    }
    assert fake_glaas_publish_server.label_syncs
    assert any(
        isinstance(entry.get("authorization"), str)
        and entry["authorization"].startswith("Signature ")
        for entry in fake_glaas_publish_server.auth_headers
    )
