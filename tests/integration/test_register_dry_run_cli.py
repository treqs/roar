"""Product-path coverage for local `roar register --dry-run` targets."""

from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from .fake_glaas import FakeGlaasServer

pytestmark = pytest.mark.integration


@pytest.fixture
def fake_glaas_publish_server() -> FakeGlaasServer:
    with FakeGlaasServer() as server:
        yield server


def _query_rows(repo_path: Path, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    db_path = repo_path / ".roar" / "roar.db"
    assert db_path.exists(), ".roar/roar.db not found"
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


def _parse_dry_run_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in ("Jobs", "Artifacts", "Links"):
        match = re.search(rf"{key}: (\d+)", output)
        assert match is not None, f"Missing {key} count in output: {output}"
        counts[key.lower()] = int(match.group(1))
    return counts


def _parse_session_hash(output: str) -> str:
    match = re.search(r"Session:\s+https://glaas\.ai/dag/([0-9a-f]+)", output)
    assert match is not None, f"Missing session URL in output: {output}"
    return match.group(1)


def _configure_register_repo(repo: Path, roar_cli, fake_glaas_url: str) -> dict[str, str]:
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    xdg_config_home = repo / '.xdg'
    token_file = repo / 'token-file.json'
    token_file.write_text(
        '{"version":1,"provider":"treqs-cognito","access_token":"test-access-token","user":{"sub":"treqs-user-123","db_user_id":"user-123","email":"trevor@example.com","username":"trevor"}}',
        encoding='utf-8',
    )
    env = {'XDG_CONFIG_HOME': str(xdg_config_home)}
    roar_cli('login', '--token-file', str(token_file), env_overrides=env)
    roar_cli('project', 'link', '--owner-id', 'owner-test', '--owner-type', 'organization', '--project-id', 'proj-test', env_overrides=env)
    roar_cli('config', 'set', 'glaas.url', fake_glaas_url, env_overrides=env)
    roar_cli('config', 'set', 'glaas.web_url', fake_glaas_url, env_overrides=env)
    return env


def test_register_dry_run_resolves_artifact_step_and_session_targets(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
) -> None:
    script = temp_git_repo / "generate_report.py"
    script.write_text("from pathlib import Path\nPath('report.txt').write_text('register me')\n")
    git_commit("Add register fixture")

    run_result = roar_cli("run", python_exe, "generate_report.py", env_overrides=env)
    assert run_result.returncode == 0
    assert (temp_git_repo / "report.txt").read_text() == "register me"

    active_sessions = _query_rows(
        temp_git_repo,
        "SELECT hash FROM sessions WHERE is_active = 1 ORDER BY id DESC LIMIT 1",
    )
    assert active_sessions
    session_hash = active_sessions[0]["hash"]
    assert isinstance(session_hash, str) and len(session_hash) >= 12

    artifact_result = roar_cli("register", "--dry-run", "report.txt")
    step_result = roar_cli("register", "--dry-run", "@1")
    session_result = roar_cli("register", "--dry-run", session_hash)

    for result in (artifact_result, step_result, session_result):
        assert result.returncode == 0
        assert "Dry run: would register lineage for:" in result.stdout
        assert "GLaaS:" in result.stdout

    published_session_hashes = {
        _parse_session_hash(artifact_result.stdout),
        _parse_session_hash(step_result.stdout),
        _parse_session_hash(session_result.stdout),
    }
    assert len(published_session_hashes) == 1

    artifact_counts = _parse_dry_run_counts(artifact_result.stdout)
    step_counts = _parse_dry_run_counts(step_result.stdout)
    session_counts = _parse_dry_run_counts(session_result.stdout)

    assert artifact_counts == step_counts == session_counts
    assert artifact_counts["jobs"] == 1
    assert artifact_counts["artifacts"] >= 1
    assert artifact_counts["links"] >= 1


def test_register_publishes_local_lineage_with_fake_glaas(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    fake_glaas_publish_server: FakeGlaasServer,
) -> None:
    env = _configure_register_repo(temp_git_repo, roar_cli, fake_glaas_publish_server.base_url)

    input_path = temp_git_repo / "input.txt"
    input_path.write_text("register me\n")
    script = temp_git_repo / "generate_report.py"
    script.write_text(
        "from pathlib import Path\n"
        "content = Path('input.txt').read_text()\n"
        "Path('report.txt').write_text(content.upper())\n"
    )
    git_commit("Add register publish fixture")

    run_result = roar_cli("run", python_exe, "generate_report.py", env_overrides=env)
    assert run_result.returncode == 0
    assert (temp_git_repo / "report.txt").read_text() == "REGISTER ME\n"

    git_commit("Commit tracked report")

    result = roar_cli("register", "report.txt", "--yes", env_overrides=env)

    assert result.returncode == 0
    assert "Registered lineage for: report.txt" in result.stdout
    assert "Jobs: 1" in result.stdout
    assert "Artifacts:" in result.stdout
    assert "Links:" in result.stdout
    assert "Next:" in result.stdout
    assert "roar reproduce " in result.stdout
    assert "GLaaS:" in result.stdout

    assert fake_glaas_publish_server.health_checks >= 1
    assert len(fake_glaas_publish_server.session_registrations) == 1
    assert fake_glaas_publish_server.session_registrations[0]["scope_request"] == {
        "owner_id": "owner-test",
        "owner_type": "organization",
        "project_id": "proj-test",
        "visibility": "private",
    }
    assert any(
        entry.get("authorization") == "Bearer test-access-token"
        for entry in fake_glaas_publish_server.auth_headers
    )
    assert len(fake_glaas_publish_server.job_batches) == 1
    assert len(fake_glaas_publish_server.job_creates) == 0
    assert len(fake_glaas_publish_server.artifact_batches) >= 1
    assert fake_glaas_publish_server.input_links
    assert fake_glaas_publish_server.output_links

    registered_jobs = fake_glaas_publish_server.job_batches[0]["jobs"]
    assert len(registered_jobs) == 1
    assert registered_jobs[0]["job_type"] == "run"


def test_register_honors_logging_config_for_console_and_file(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    fake_glaas_publish_server: FakeGlaasServer,
    monkeypatch,
) -> None:
    env = _configure_register_repo(temp_git_repo, roar_cli, fake_glaas_publish_server.base_url)

    input_path = temp_git_repo / "input.txt"
    input_path.write_text("register me\n")
    script = temp_git_repo / "generate_report.py"
    script.write_text(
        "from pathlib import Path\n"
        "content = Path('input.txt').read_text()\n"
        "Path('report.txt').write_text(content.upper())\n"
    )
    git_commit("Add register logging fixture")

    run_result = roar_cli("run", python_exe, "generate_report.py", env_overrides=env)
    assert run_result.returncode == 0
    git_commit("Commit tracked report for logging test")

    home_dir = temp_git_repo / ".test-home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))

    config_path = temp_git_repo / ".roar" / "config.toml"
    config_text = config_path.read_text(encoding="utf-8")
    if "[logging]" in config_text:
        config_text = config_text.replace('level = "warning"', 'level = "debug"')
        config_text = config_text.replace("console = false", "console = true")
        config_text = config_text.replace("file = false", "file = true")
    else:
        config_text = (
            f'{config_text.rstrip()}\n\n[logging]\nlevel = "debug"\nconsole = true\nfile = true\n'
        )
    config_path.write_text(config_text, encoding="utf-8")

    log_path = home_dir / ".roar" / "roar.log"
    if log_path.exists():
        log_path.unlink()

    result = roar_cli("register", "report.txt", "--yes", env_overrides=env)

    assert result.returncode == 0
    assert "Running GLaaS health check" in result.stderr
    assert log_path.is_file()
    log_output = log_path.read_text(encoding="utf-8")
    assert "Running GLaaS health check" in log_output
