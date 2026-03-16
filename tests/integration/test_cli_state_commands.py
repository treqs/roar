"""Product-path coverage for local stateful CLI commands."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _query_rows(repo_path: Path, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    db_path = repo_path / ".roar" / "roar.db"
    assert db_path.exists(), ".roar/roar.db not found"
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


def _active_session(repo_path: Path) -> sqlite3.Row:
    rows = _query_rows(
        repo_path,
        "SELECT id, is_active, current_step FROM sessions WHERE is_active = 1 ORDER BY id DESC LIMIT 1",
    )
    assert rows, "Expected an active session"
    return rows[0]


def test_config_set_get_and_list_round_trip(temp_git_repo: Path, roar_cli) -> None:
    set_result = roar_cli("config", "set", "output.quiet", "true")
    assert set_result.returncode == 0
    assert "Set output.quiet = True" in set_result.stdout

    get_result = roar_cli("config", "get", "output.quiet")
    assert get_result.returncode == 0
    assert "output.quiet: True" in get_result.stdout

    list_result = roar_cli("config", "list")
    assert list_result.returncode == 0
    assert "output.quiet" in list_result.stdout


def test_env_set_get_list_and_unset_round_trip(temp_git_repo: Path, roar_cli) -> None:
    set_result = roar_cli("env", "set", "FOO", "bar")
    assert set_result.returncode == 0
    assert "Set FOO=bar" in set_result.stdout

    get_result = roar_cli("env", "get", "FOO")
    assert get_result.returncode == 0
    assert get_result.stdout.strip() == "bar"

    list_result = roar_cli("env", "list")
    assert list_result.returncode == 0
    assert "FOO=bar" in list_result.stdout

    unset_result = roar_cli("env", "unset", "FOO")
    assert unset_result.returncode == 0
    assert "Unset FOO" in unset_result.stdout

    list_after_unset = roar_cli("env", "list")
    assert list_after_unset.returncode == 0
    assert "No environment variables set." in list_after_unset.stdout


def test_reset_creates_and_rotates_active_session(temp_git_repo: Path, roar_cli) -> None:
    sessions_before_reset = _query_rows(
        temp_git_repo,
        "SELECT id, is_active, current_step FROM sessions ORDER BY id",
    )
    assert sessions_before_reset == []

    first_reset_result = roar_cli("reset", "-y")
    assert first_reset_result.returncode == 0
    assert "Created new session" in first_reset_result.stdout

    first_active_session = _active_session(temp_git_repo)
    assert first_active_session["current_step"] == 1

    second_reset_result = roar_cli("reset", "-y")
    assert second_reset_result.returncode == 0
    assert "Current session has 0 step(s)." in second_reset_result.stdout
    assert f"Deactivated session {first_active_session['id']}." in second_reset_result.stdout
    assert "Created new session" in second_reset_result.stdout

    sessions_after_reset = _query_rows(
        temp_git_repo,
        "SELECT id, is_active, current_step FROM sessions ORDER BY id",
    )
    active_sessions = [row for row in sessions_after_reset if row["is_active"] == 1]
    assert len(active_sessions) == 1
    assert active_sessions[0]["id"] != first_active_session["id"]
    assert active_sessions[0]["current_step"] == 1

    previous_session = next(
        row for row in sessions_after_reset if row["id"] == first_active_session["id"]
    )
    assert previous_session["is_active"] == 0

    log_result = roar_cli("log")
    assert log_result.returncode == 0
    assert "No log entries found." in log_result.stdout


def test_reset_rotates_active_session_after_recorded_job(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
) -> None:
    script = temp_git_repo / "simple.py"
    script.write_text("with open('result.txt', 'w') as f: f.write('ok')\n")
    git_commit("Add reset fixture")

    run_result = roar_cli("run", python_exe, "simple.py")
    assert run_result.returncode == 0
    assert (temp_git_repo / "result.txt").exists()

    previous_active = _active_session(temp_git_repo)
    jobs_before_reset = _query_rows(
        temp_git_repo,
        "SELECT id FROM jobs WHERE session_id = ? ORDER BY id",
        (previous_active["id"],),
    )
    assert len(jobs_before_reset) == 1

    reset_result = roar_cli("reset", "-y")
    assert reset_result.returncode == 0
    assert "Current session has 1 step(s)." in reset_result.stdout
    assert f"Deactivated session {previous_active['id']}." in reset_result.stdout
    assert "Created new session" in reset_result.stdout

    active_after_reset = _active_session(temp_git_repo)
    assert active_after_reset["id"] != previous_active["id"]
    assert active_after_reset["current_step"] == 1

    log_result = roar_cli("log")
    assert log_result.returncode == 0
    assert "No log entries found." in log_result.stdout


def test_pop_reports_when_no_active_session(temp_git_repo: Path, roar_cli) -> None:
    pop_result = roar_cli("pop", "-y")
    assert pop_result.returncode == 0
    assert "No active session." in pop_result.stdout


def test_pop_reports_when_active_session_has_no_jobs(temp_git_repo: Path, roar_cli) -> None:
    reset_result = roar_cli("reset", "-y")
    assert reset_result.returncode == 0

    pop_result = roar_cli("pop", "-y")
    assert pop_result.returncode == 0
    assert "No jobs in the active session." in pop_result.stdout


def test_pop_removes_latest_job_and_output_file(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    python_exe: str,
) -> None:
    output_path = temp_git_repo / "result.txt"
    script = temp_git_repo / "simple.py"
    script.write_text("with open('result.txt', 'w') as f: f.write('ok')\n")
    git_commit("Add pop fixture")

    run_result = roar_cli("run", python_exe, "simple.py")
    assert run_result.returncode == 0
    assert output_path.exists()

    active_session = _active_session(temp_git_repo)
    jobs_before_pop = _query_rows(
        temp_git_repo,
        "SELECT id FROM jobs WHERE session_id = ? ORDER BY id",
        (active_session["id"],),
    )
    assert len(jobs_before_pop) == 1

    pop_result = roar_cli("pop", "-y")
    assert pop_result.returncode == 0
    assert "Removed job" in pop_result.stdout
    assert "Deleted 1 output file(s)." in pop_result.stdout
    assert not output_path.exists()

    jobs_after_pop = _query_rows(
        temp_git_repo,
        "SELECT id FROM jobs WHERE session_id = ? ORDER BY id",
        (active_session["id"],),
    )
    assert jobs_after_pop == []

    pop_again_result = roar_cli("pop", "-y")
    assert pop_again_result.returncode == 0
    assert "No jobs in the active session." in pop_again_result.stdout
