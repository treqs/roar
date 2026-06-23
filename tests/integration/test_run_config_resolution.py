from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import _run_roar_cmd

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="ptrace tracer requires Linux"),
]


def _commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo,
        capture_output=True,
        check=True,
    )


def _latest_output_paths(roar_dir: Path) -> list[str]:
    conn = sqlite3.connect(roar_dir / "roar.db")
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT id FROM jobs ORDER BY id DESC LIMIT 1")
        job = cur.fetchone()
        assert job is not None
        cur.execute("SELECT path FROM job_outputs WHERE job_id = ?", (job["id"],))
        return [row["path"] for row in cur.fetchall()]
    finally:
        conn.close()


def test_run_from_subdir_uses_active_roar_config_and_root_roarconfig(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subdir = repo / "sub"
    subdir.mkdir(parents=True)

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / ".gitignore").write_text(".roar/\n", encoding="utf-8")
    (repo / ".roarconfig").write_text(
        '[filters]\nignore_paths = ["blocked-by-root.txt"]\n',
        encoding="utf-8",
    )
    _commit_all(repo, "initial project config")

    _run_roar_cmd("init", "-y", cwd=subdir)
    _run_roar_cmd("config", "set", "filters.ignore_tmp_files", "false", cwd=subdir)
    get_result = _run_roar_cmd("config", "get", "filters.ignore_tmp_files", cwd=subdir)
    assert "filters.ignore_tmp_files: False" in get_result.stdout

    result = _run_roar_cmd(
        "run",
        "--tracer",
        "ptrace",
        "--no-tracer-fallback",
        sys.executable,
        "-c",
        "open('out.txt', 'w').write('tracked'); open('blocked-by-root.txt', 'w').write('ignored')",
        cwd=subdir,
        check=False,
    )

    assert result.returncode == 0, (
        f"roar run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    output_paths = _latest_output_paths(subdir / ".roar")
    assert any(path.endswith("/out.txt") for path in output_paths), output_paths
    assert not any(path.endswith("/blocked-by-root.txt") for path in output_paths), output_paths
