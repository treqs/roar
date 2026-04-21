"""Product-path coverage for explicit remote label push flows."""

from __future__ import annotations

import json
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


def _configure_label_push_repo(repo: Path, roar_cli, fake_glaas_url: str) -> dict[str, str]:
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
    roar_cli("projects", "link", "proj-test", env_overrides=env)
    roar_cli("config", "set", "glaas.url", fake_glaas_url, env_overrides=env)
    roar_cli("config", "set", "glaas.web_url", fake_glaas_url, env_overrides=env)
    return env


def _create_tracked_output(
    repo: Path,
    *,
    roar_cli,
    git_commit,
    python_exe: str,
    env_overrides: dict[str, str] | None = None,
) -> None:
    (repo / "input.csv").write_text("id,value\n1,foo\n2,bar\n", encoding="utf-8")
    (repo / "preprocess.py").write_text(
        "import sys\n"
        "from pathlib import Path\n\n"
        "src = Path(sys.argv[1]).read_text(encoding='utf-8').upper()\n"
        "Path(sys.argv[2]).write_text(src, encoding='utf-8')\n",
        encoding="utf-8",
    )
    git_commit("Add preprocess script")

    run_result = roar_cli(
        "run",
        python_exe,
        "preprocess.py",
        "input.csv",
        "processed.csv",
        env_overrides=env_overrides,
    )
    assert run_result.returncode == 0
    git_commit("Track processed output")


def _artifact_hash_for(repo: Path, roar_cli, target: str) -> str:
    payload = json.loads(roar_cli("lineage", target).stdout)
    return str(payload["artifact"]["hash"])


def _job_uid_for(repo: Path, roar_cli, target: str) -> str:
    payload = json.loads(roar_cli("lineage", target).stdout)
    return str(payload["jobs"][0]["job_uid"])


def _active_local_session_hash(repo: Path) -> str:
    db_path = repo / ".roar" / "roar.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT hash FROM sessions WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None and isinstance(row[0], str) and row[0]
    return str(row[0])


def test_label_push_artifact_patches_existing_remote_labels(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    fake_glaas_publish_server: FakeGlaasServer,
) -> None:
    env = _configure_label_push_repo(temp_git_repo, roar_cli, fake_glaas_publish_server.base_url)
    _create_tracked_output(
        temp_git_repo,
        roar_cli=roar_cli,
        git_commit=git_commit,
        python_exe=python_exe,
        env_overrides=env,
    )

    roar_cli(
        "label",
        "set",
        "artifact",
        "processed.csv",
        "owner=ml",
        "stage=raw",
        env_overrides=env,
    )
    roar_cli("register", "processed.csv", "--yes", env_overrides=env)
    roar_cli(
        "label",
        "set",
        "artifact",
        "processed.csv",
        "stage=gold",
        env_overrides=env,
    )

    artifact_hash = _artifact_hash_for(temp_git_repo, roar_cli, "processed.csv")
    result = roar_cli("label", "push", "artifact", "processed.csv", env_overrides=env)

    assert result.returncode == 0
    assert "Pushed remote labels (version 2):" in result.stdout
    assert "owner=ml" in result.stdout
    assert "stage=gold" in result.stdout
    assert fake_glaas_publish_server.label_mutations == [
        {
            "entity_type": "artifact",
            "artifact_hash": artifact_hash,
            "metadata": {"owner": "ml", "stage": "gold"},
        }
    ]


def test_label_push_job_omits_system_labels_and_targets_job_session_hash(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    fake_glaas_publish_server: FakeGlaasServer,
) -> None:
    env = _configure_label_push_repo(temp_git_repo, roar_cli, fake_glaas_publish_server.base_url)
    _create_tracked_output(
        temp_git_repo,
        roar_cli=roar_cli,
        git_commit=git_commit,
        python_exe=python_exe,
        env_overrides=env,
    )

    roar_cli("label", "set", "job", "@1", "phase=preprocess", env_overrides=env)
    session_ref = _active_local_session_hash(temp_git_repo)
    roar_cli("register", session_ref, "--yes", env_overrides=env)
    published_session_hash = fake_glaas_publish_server.registration_session_finalizations[0]["hash"]
    roar_cli("label", "set", "job", "@1", "phase=train", env_overrides=env)

    staged_jobs = fake_glaas_publish_server.registration_session_job_batches[0]["jobs"]
    remote_job_uid = next(
        str(job["job_uid"]) for job in staged_jobs if int(job.get("step_number") or 0) == 1
    )
    synced_job_labels = [
        label
        for batch in fake_glaas_publish_server.label_syncs
        for label in batch
        if label.get("entity_type") == "job" and label.get("job_uid") == remote_job_uid
    ]
    assert len(synced_job_labels) == 1
    assert synced_job_labels[0]["session_hash"] == published_session_hash
    result = roar_cli("label", "push", "job", "@1", check=False, env_overrides=env)

    assert len(fake_glaas_publish_server.label_mutation_attempts) == 1
    attempted = fake_glaas_publish_server.label_mutation_attempts[0]
    assert attempted["entity_type"] == "job"
    assert attempted["job_uid"] == remote_job_uid
    assert attempted["metadata"] == {"phase": "train"}
    assert re.fullmatch(r"[0-9a-f]{64}", attempted["session_hash"])
    assert result.returncode == 0
    assert "Pushed remote labels (version 2):" in result.stdout
    assert "phase=train" in result.stdout
    assert fake_glaas_publish_server.label_mutations == [
        {
            "entity_type": "job",
            "session_hash": attempted["session_hash"],
            "job_uid": remote_job_uid,
            "metadata": {"phase": "train"},
        }
    ]
    assert any(
        entry.get("path") == "/api/v1/labels/current"
        and entry.get("authorization") == "Bearer test-access-token"
        for entry in fake_glaas_publish_server.auth_headers
    )


def test_label_push_requires_existing_remote_current_labels(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    fake_glaas_publish_server: FakeGlaasServer,
) -> None:
    env = _configure_label_push_repo(temp_git_repo, roar_cli, fake_glaas_publish_server.base_url)
    _create_tracked_output(
        temp_git_repo,
        roar_cli=roar_cli,
        git_commit=git_commit,
        python_exe=python_exe,
        env_overrides=env,
    )

    roar_cli(
        "label",
        "set",
        "artifact",
        "processed.csv",
        "owner=ml",
        "stage=gold",
        env_overrides=env,
    )
    result = roar_cli("label", "push", "artifact", "processed.csv", check=False, env_overrides=env)

    assert result.returncode != 0
    assert "Remote label push failed: HTTP 404: Label not found" in result.stderr
    assert fake_glaas_publish_server.label_mutations == []
