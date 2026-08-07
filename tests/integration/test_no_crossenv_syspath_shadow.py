"""P0-14: roar's runtime injection must not let roar's own environment shadow the
workload's recorded packages on ``sys.path`` or in captured provenance.

roar makes itself importable in a traced child by putting entries on
``ROAR_RUNTIME_PYTHONPATH``; ``sitecustomize`` applies them. Previously *all* of
them were prepended, so on a cross-interpreter run roar's host ``dist-packages``
landed at ``sys.path[0]`` and shadowed the recorded pins (the run executed
against host packages, and could certify GREEN for the wrong reason). The fix:
append roar's host environment, and activate the ABI-matched runtime **cache**
only when none of its import names collide with the workload.

These run a roar-less child interpreter with a ``sitecustomize`` on
``PYTHONPATH`` and assert which copy of a shadowed module wins.
"""

from __future__ import annotations

import json
import os
import platform
import sqlite3
import subprocess
import sys
import venv
from collections.abc import Callable
from pathlib import Path

import pytest

import tests.conftest as test_conftest

INJECT_DIR = Path(__file__).resolve().parents[2] / "roar" / "execution" / "runtime" / "inject"
SOURCE_ROOT = Path(__file__).resolve().parents[2]


def _roarless_python(tmp_path: Path) -> Path:
    """A Python that cannot already import roar (so the injection path runs)."""
    child = tmp_path / "child"
    venv.EnvBuilder(with_pip=False).create(str(child))
    return child / ("Scripts" if sys.platform == "win32" else "bin") / "python"


def _write_pkg(root: Path, mark: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "shadowpkg.py").write_text(f"MARK = {mark!r}\n", encoding="utf-8")


def _write_distribution(root: Path, version: str) -> None:
    _write_pkg(root, version)
    metadata = root / f"shadowpkg-{version}.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: shadowpkg\nVersion: {version}\n",
        encoding="utf-8",
    )
    (metadata / "top_level.txt").write_text("shadowpkg\n", encoding="utf-8")


def _site_packages(python: Path) -> Path:
    result = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


def _run(child_py: Path, env: dict[str, str]) -> str:
    r = subprocess.run(
        [str(child_py), "-c", "import shadowpkg; print('WINNER=' + shadowpkg.MARK)"],
        env=env,
        capture_output=True,
        text=True,
    )
    for line in r.stdout.splitlines():
        if line.startswith("WINNER="):
            return line[len("WINNER=") :]
    raise AssertionError(f"no winner line.\nstdout={r.stdout!r}\nstderr={r.stderr!r}")


def test_host_site_packages_do_not_shadow_the_workload(tmp_path):
    """A non-cache runtime entry (roar's host site-packages) is APPENDED, so the
    workload's own copy (on PYTHONPATH) wins. Before the fix it was prepended and
    'host' won — a silent execution against host packages."""
    child_py = _roarless_python(tmp_path)
    _write_pkg(tmp_path / "workload", "workload")
    _write_pkg(tmp_path / "host", "host")

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(INJECT_DIR), str(tmp_path / "workload")])
    env["ROAR_RUNTIME_PYTHONPATH"] = str(tmp_path / "host")
    env.pop("ROAR_WRAP", None)

    assert _run(child_py, env) == "workload"


def test_abi_matched_cache_does_not_shadow_the_workload(tmp_path):
    """A cache entry that overlaps the workload is not activated."""
    child_py = _roarless_python(tmp_path)
    cache_home = tmp_path / "xdg"
    cache_pkg = cache_home / "roar" / "runtime" / "cp999" / "site-packages"
    _write_pkg(cache_pkg, "cache")
    _write_pkg(tmp_path / "workload", "workload")

    env = dict(os.environ)
    env["XDG_CACHE_HOME"] = str(cache_home)
    env["PYTHONPATH"] = os.pathsep.join([str(INJECT_DIR), str(tmp_path / "workload")])
    env["ROAR_RUNTIME_PYTHONPATH"] = str(cache_pkg)
    env.pop("ROAR_WRAP", None)

    assert _run(child_py, env) == "workload"


@pytest.mark.skipif(platform.system() != "Linux", reason="product path uses Linux tracer")
def test_roar_run_records_the_distribution_that_the_workload_imported(
    temp_git_repo: Path,
    git_commit: Callable[[str], None],
) -> None:
    """The child imports and records its own pin even when Roar's host has another.

    The workload is intentionally Roar-unaware. A separate parent venv imports this
    worktree through a .pth file, while a child venv owns the package under test.
    """
    test_conftest._ensure_repo_local_ptrace_tracer()
    env_root = temp_git_repo.parent / f"{temp_git_repo.name}-crossenv"
    parent_root = env_root / "parent"
    child_root = env_root / "child"
    venv.EnvBuilder(with_pip=False).create(str(parent_root))
    venv.EnvBuilder(with_pip=False).create(str(child_root))
    scripts_dir = "Scripts" if sys.platform == "win32" else "bin"
    parent_python = parent_root / scripts_dir / "python"
    child_python = child_root / scripts_dir / "python"
    parent_site = _site_packages(parent_python)
    child_site = _site_packages(child_python)
    current_site = _site_packages(Path(sys.executable))

    (parent_site / "roar-worktree.pth").write_text(
        f"{SOURCE_ROOT}\n{current_site}\n",
        encoding="utf-8",
    )
    _write_distribution(parent_site, "9.0")
    _write_distribution(child_site, "1.0")

    script = temp_git_repo / "workload.py"
    script.write_text(
        "import json, shadowpkg\n"
        "with open('observed.json', 'w', encoding='utf-8') as handle:\n"
        "    json.dump({'version': shadowpkg.MARK, 'file': shadowpkg.__file__}, handle)\n",
        encoding="utf-8",
    )
    git_commit("add cross-environment workload")

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            str(parent_python),
            "-m",
            "roar",
            "run",
            "--tracer",
            "ptrace",
            "--no-tracer-fallback",
            str(child_python),
            script.name,
        ],
        cwd=temp_git_repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r}\nstderr={result.stderr!r}"

    observed = json.loads((temp_git_repo / "observed.json").read_text(encoding="utf-8"))
    assert observed["version"] == "1.0"
    assert Path(observed["file"]).is_relative_to(child_site)

    connection = sqlite3.connect(temp_git_repo / ".roar" / "roar.db")
    try:
        row = connection.execute("SELECT metadata FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        connection.close()
    assert row is not None and row[0]
    metadata = json.loads(row[0])
    assert metadata["packages"]["pip"]["shadowpkg"] == "1.0"
