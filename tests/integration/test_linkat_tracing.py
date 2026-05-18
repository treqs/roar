"""Regression coverage for hard-link publication tracing."""

import platform
import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest

import tests.conftest as test_conftest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(platform.system() != "Linux", reason="native tracers require Linux here"),
]


def _ensure_preload_tracer() -> None:
    test_conftest._ensure_repo_local_preload_tracer()


@pytest.mark.parametrize("tracer", ["ptrace", "preload"])
def test_hardlink_publication_records_destination_output(
    temp_git_repo: Path,
    roar_cli,
    git_commit,
    tracer: str,
) -> None:
    """Lance-style temp hard-link publication should record the final path as output."""
    if tracer == "preload":
        _ensure_preload_tracer()

    script = temp_git_repo / "linkat_fixture.py"
    script.write_text(
        textwrap.dedent(
            """\
            import os
            from pathlib import Path

            root = Path("out")
            root.mkdir(exist_ok=True)
            tmp = root / "artifact.txt#1"
            final = root / "artifact.txt"

            if final.exists():
                final.unlink()
            if tmp.exists():
                tmp.unlink()

            tmp.write_text("payload", encoding="utf-8")
            os.link(tmp, final)
            tmp.unlink()
            """
        ),
        encoding="utf-8",
    )
    git_commit("add hard-link publication fixture")

    result = roar_cli(
        "run",
        "--tracer",
        tracer,
        "--no-tracer-fallback",
        sys.executable,
        "linkat_fixture.py",
        check=False,
    )
    assert result.returncode == 0, (
        f"roar run failed for {tracer}:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    assert (temp_git_repo / "out" / "artifact.txt").read_text(encoding="utf-8") == "payload"
    assert not (temp_git_repo / "out" / "artifact.txt#1").exists()

    db_path = temp_git_repo / ".roar" / "roar.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        job_id = conn.execute("SELECT id FROM jobs ORDER BY id DESC LIMIT 1").fetchone()["id"]
        output_paths = [
            row["path"]
            for row in conn.execute("SELECT path FROM job_outputs WHERE job_id = ?", (job_id,))
        ]
    finally:
        conn.close()

    assert any(path.endswith("out/artifact.txt") for path in output_paths), output_paths
