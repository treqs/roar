"""Product-path coverage for remote label editing without a local project.

`roar label set/unset/show/history --remote` must work from a directory with
no `.roar` project against remote identifiers (session hash, job uid,
artifact/composite hash), using only the global auth store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ..conftest import _run_roar_cmd
from .fake_glaas import FakeGlaasServer
from .test_label_sync_cli_integration import (
    _active_local_session_hash,
    _artifact_hash_for,
    _configure_label_sync_repo,
    _create_tracked_output,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def fake_glaas_publish_server() -> FakeGlaasServer:
    with FakeGlaasServer() as server:
        yield server


def _publish_lineage(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    server: FakeGlaasServer,
) -> tuple[dict[str, str], str, str, str]:
    """Register a lineage; return (env, session_hash, artifact_hash, job_uid)."""
    server.force_authoritative_finalize_hash = True
    env = _configure_label_sync_repo(temp_git_repo, roar_cli, server.base_url)
    _create_tracked_output(
        temp_git_repo,
        roar_cli=roar_cli,
        git_commit=git_commit,
        python_exe=python_exe,
        env_overrides=env,
    )
    session_ref = _active_local_session_hash(temp_git_repo)
    roar_cli("register", session_ref, "--yes", env_overrides=env)

    session_hash = server.registration_session_finalizations[0]["hash"]
    artifact_hash = _artifact_hash_for(roar_cli, "processed.csv")
    staged_jobs = server.registration_session_job_batches[0]["jobs"]
    job_uid = next(
        str(job["job_uid"]) for job in staged_jobs if int(job.get("step_number") or 0) == 1
    )
    return env, session_hash, artifact_hash, job_uid


def _remote_env(env: dict[str, str], server: FakeGlaasServer) -> dict[str, str]:
    """Environment for CLI calls outside any repo: global auth + GLAAS_URL."""
    return {**env, "GLAAS_URL": server.base_url}


def test_remote_label_edits_from_bare_directory(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
    fake_glaas_publish_server: FakeGlaasServer,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    server = fake_glaas_publish_server
    env, session_hash, artifact_hash, job_uid = _publish_lineage(
        temp_git_repo, roar_cli, git_commit, python_exe, server
    )
    bare_dir = tmp_path_factory.mktemp("no-roar-project")
    assert not (bare_dir / ".roar").exists()
    remote_env = _remote_env(env, server)

    # dag: create labels on a lineage identified purely by its remote hash.
    result = _run_roar_cmd(
        "label",
        "set",
        "--remote",
        "dag",
        session_hash,
        "team=nlp",
        "priority=2",
        cwd=bare_dir,
        env_overrides=remote_env,
    )
    assert result.returncode == 0
    assert "Updated remote labels: processed=1 created=1" in result.stdout
    request = server.label_reconciles[-1]
    assert request["labels"] == [
        {
            "entity_type": "dag",
            "session_hash": session_hash,
            "metadata": {"priority": 2, "team": "nlp"},
            "base_version": 0,
        }
    ]

    # show: server-authoritative doc with editability verdict.
    shown = _run_roar_cmd(
        "label",
        "show",
        "--remote",
        "dag",
        session_hash,
        cwd=bare_dir,
        env_overrides=remote_env,
    )
    assert shown.returncode == 0
    assert "Remote labels (version 1, editable):" in shown.stdout
    assert "team=nlp" in shown.stdout

    # job: addressed by remote job uid + --session.
    job_result = _run_roar_cmd(
        "label",
        "set",
        "--remote",
        "job",
        job_uid,
        "--session",
        session_hash,
        "phase=train",
        cwd=bare_dir,
        env_overrides=remote_env,
    )
    assert job_result.returncode == 0
    job_request = server.label_reconciles[-1]
    assert job_request["labels"][0] == {
        "entity_type": "job",
        "session_hash": session_hash,
        "job_uid": job_uid,
        "metadata": {"phase": "train"},
        # register-time sync already recorded the job's system labels as v1.
        "base_version": 1,
    }

    # composite: alias for artifact targets, addressed by content hash.
    composite_result = _run_roar_cmd(
        "label",
        "set",
        "--remote",
        "composite",
        artifact_hash,
        "--session",
        session_hash,
        "stage=gold",
        "dataset-name=demo",
        cwd=bare_dir,
        env_overrides=remote_env,
    )
    assert composite_result.returncode == 0
    composite_request = server.label_reconciles[-1]
    assert composite_request["labels"][0] == {
        "entity_type": "artifact",
        "session_hash": session_hash,
        "artifact_hash": artifact_hash,
        "metadata": {"dataset-name": "demo", "stage": "gold"},
        "base_version": 0,
    }

    # artifact unset by hash prefix, session resolved from the remote label doc.
    unset_result = _run_roar_cmd(
        "label",
        "unset",
        "--remote",
        "artifact",
        artifact_hash[:16],
        "stage",
        cwd=bare_dir,
        env_overrides=remote_env,
    )
    assert unset_result.returncode == 0
    assert "deleted_keys=1" in unset_result.stdout
    assert "(deleted: stage)" in unset_result.stdout
    unset_request = server.label_reconciles[-1]
    assert unset_request["labels"][0] == {
        "entity_type": "artifact",
        "session_hash": session_hash,
        "artifact_hash": artifact_hash,
        "metadata": {},
        "deleted_keys": ["stage"],
        "base_version": 1,
    }
    remote_artifact_label = next(
        label
        for label in server.current_labels_by_target.values()
        if label.get("artifactHash") == artifact_hash
    )
    assert remote_artifact_label["metadata"] == {"dataset-name": "demo"}

    # history: versioned remote trail including the deletion.
    history_result = _run_roar_cmd(
        "label",
        "history",
        "--remote",
        "artifact",
        artifact_hash,
        "--session",
        session_hash,
        cwd=bare_dir,
        env_overrides=remote_env,
    )
    assert history_result.returncode == 0
    assert "Version 1:" in history_result.stdout
    assert "Version 2:" in history_result.stdout


def test_remote_label_edit_rejects_invalid_targets_and_flags(
    temp_git_repo: Path,
    roar_cli,
    fake_glaas_publish_server: FakeGlaasServer,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    server = fake_glaas_publish_server
    env = _configure_label_sync_repo(temp_git_repo, roar_cli, server.base_url)
    bare_dir = tmp_path_factory.mktemp("no-roar-validation")
    remote_env = _remote_env(env, server)

    bad_dag = _run_roar_cmd(
        "label",
        "set",
        "--remote",
        "dag",
        "not-a-hash",
        "team=nlp",
        cwd=bare_dir,
        env_overrides=remote_env,
        check=False,
    )
    assert bad_dag.returncode != 0
    assert "64-character" in bad_dag.stderr

    missing_session = _run_roar_cmd(
        "label",
        "set",
        "--remote",
        "job",
        "step-1",
        "phase=train",
        cwd=bare_dir,
        env_overrides=remote_env,
        check=False,
    )
    assert missing_session.returncode != 0
    assert "--session" in missing_session.stderr

    session_without_remote = _run_roar_cmd(
        "label",
        "set",
        "dag",
        "a" * 64,
        "team=nlp",
        "--session",
        "a" * 64,
        cwd=bare_dir,
        env_overrides=remote_env,
        check=False,
    )
    assert session_without_remote.returncode != 0
    assert "--session is only valid together with --remote" in session_without_remote.stderr

    # Local mode without a project still gives the classic init guidance.
    local_without_project = _run_roar_cmd(
        "label",
        "set",
        "dag",
        "current",
        "team=nlp",
        cwd=bare_dir,
        env_overrides=remote_env,
        check=False,
    )
    assert local_without_project.returncode != 0
    assert "roar is not initialized" in local_without_project.stdout


def test_remote_label_edit_reports_missing_get_routes_on_old_servers(
    temp_git_repo: Path,
    roar_cli,
    fake_glaas_publish_server: FakeGlaasServer,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Task C: an old, not-yet-upgraded glaas-api doesn't have the GET
    /api/v1/labels/current or /api/v1/labels/history routes at all — a
    genuine "route not found" 404, distinct from the app-level "no labels
    for this target yet" 404. Both `roar label set --remote` (which reads
    the current doc first to resolve base_version / create-vs-update) and
    `roar label history --remote` must surface this as a clear, actionable
    error rather than silently treating the target as unlabeled.
    """
    server = fake_glaas_publish_server
    env = _configure_label_sync_repo(temp_git_repo, roar_cli, server.base_url)
    bare_dir = tmp_path_factory.mktemp("no-roar-old-server")
    remote_env = _remote_env(env, server)
    session_hash = "a" * 64

    server.supports_get_label_routes = False

    set_result = _run_roar_cmd(
        "label",
        "set",
        "--remote",
        "dag",
        session_hash,
        "team=nlp",
        cwd=bare_dir,
        env_overrides=remote_env,
        check=False,
    )
    assert set_result.returncode != 0
    assert "GET /api/v1/labels/current" in set_result.stderr
    assert "may not be upgraded yet" in set_result.stderr
    assert not server.label_reconciles

    history_result = _run_roar_cmd(
        "label",
        "history",
        "--remote",
        "dag",
        session_hash,
        cwd=bare_dir,
        env_overrides=remote_env,
        check=False,
    )
    assert history_result.returncode != 0
    assert "GET /api/v1/labels/history" in history_result.stderr
    assert "may not be upgraded yet" in history_result.stderr

    # Once the server is "upgraded", the same commands work normally again
    # (and a target that genuinely has no labels yet still behaves as before).
    server.supports_get_label_routes = True
    recovered = _run_roar_cmd(
        "label",
        "set",
        "--remote",
        "dag",
        session_hash,
        "team=nlp",
        cwd=bare_dir,
        env_overrides=remote_env,
        check=False,
    )
    assert recovered.returncode == 0
    assert "Updated remote labels: processed=1 created=1" in recovered.stdout
