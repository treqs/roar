"""
Integration test for build tool detection via ``roar build``.

Verifies that running a build step which invokes cmake causes the
build_dpkg package collector to record cmake in the job metadata.
"""

import json
import platform
import shutil
import sqlite3

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(platform.system() != "Linux", reason="dpkg-based detection is Linux-only"),
]


@pytest.mark.skipif(shutil.which("cmake") is None, reason="cmake not on PATH")
def test_build_detects_cmake(temp_git_repo, roar_cli, git_commit):
    """``roar build cmake --version`` should record cmake in build_dpkg."""
    # -- Run cmake directly so the tracer captures it -----------------------
    result = roar_cli("build", "cmake", "--version", check=False)
    assert result.returncode == 0, (
        f"roar build cmake --version failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    git_commit("After cmake build")

    # -- Verify: read build_dpkg from the job metadata ----------------------
    db_path = temp_git_repo / ".roar" / "roar.db"
    assert db_path.exists(), ".roar/roar.db not found"

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("SELECT metadata FROM jobs ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        assert row is not None, "No job found in the database"

        metadata = json.loads(row[0]) if row[0] else {}
        packages = metadata.get("packages", {})
        build_dpkg = packages.get("build_dpkg", {})

        assert "cmake" in build_dpkg, (
            f"cmake not found in build_dpkg; got keys: {list(build_dpkg.keys())}"
        )
    finally:
        conn.close()


@pytest.mark.timeout(600)
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
@pytest.mark.skipif(shutil.which("make") is None, reason="make not on PATH")
def test_build_detects_cmake_from_uv_sync(temp_git_repo, roar_cli, git_commit):
    """``roar build uv sync`` with source builds should record make in build_dpkg."""
    # -- Set up a minimal pyproject.toml that forces source builds -----------
    pyproject = temp_git_repo / "pyproject.toml"
    pyproject.write_text(
        "[project]\n"
        'name = "test-proj"\n'
        'version = "0.0.1"\n'
        'requires-python = ">=3.10"\n'
        'dependencies = ["pynacl"]\n'
        "\n"
        "[tool.uv]\n"
        "no-binary = true\n"
    )

    git_commit("Add pyproject.toml for uv sync")

    # -- Run uv sync via roar build so the tracer captures it ---------------
    result = roar_cli("build", "uv", "sync", "--no-cache", check=False)
    assert result.returncode == 0, (
        f"roar build uv sync failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    git_commit("After uv sync build")

    # -- Verify: read build_dpkg from the job metadata ----------------------
    db_path = temp_git_repo / ".roar" / "roar.db"
    assert db_path.exists(), ".roar/roar.db not found"

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("SELECT metadata FROM jobs ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        assert row is not None, "No job found in the database"

        metadata = json.loads(row[0]) if row[0] else {}
        packages = metadata.get("packages", {})
        build_dpkg = packages.get("build_dpkg", {})

        assert "make" in build_dpkg, (
            f"make not found in build_dpkg; got keys: {list(build_dpkg.keys())}"
        )
    finally:
        conn.close()
