"""P0-14: roar's runtime injection must not let roar's own environment shadow the
workload's recorded packages on ``sys.path``.

roar makes itself importable in a traced child by putting entries on
``ROAR_RUNTIME_PYTHONPATH``; ``sitecustomize`` applies them. Previously *all* of
them were prepended, so on a cross-interpreter run roar's host ``dist-packages``
landed at ``sys.path[0]`` and shadowed the recorded pins (the run executed
against host packages, and could certify GREEN for the wrong reason). The fix:
prepend only roar's ABI-matched runtime **cache**; append everything else so the
workload's venv wins.

These run a roar-less child interpreter with a ``sitecustomize`` on
``PYTHONPATH`` and assert which copy of a shadowed module wins.
"""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

INJECT_DIR = Path(__file__).resolve().parents[2] / "roar" / "execution" / "runtime" / "inject"


def _roarless_python(tmp_path: Path) -> Path:
    """A Python that cannot already import roar (so the injection path runs)."""
    child = tmp_path / "child"
    venv.EnvBuilder(with_pip=False).create(str(child))
    return child / ("Scripts" if sys.platform == "win32" else "bin") / "python"


def _write_pkg(root: Path, mark: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "shadowpkg.py").write_text(f"MARK = {mark!r}\n", encoding="utf-8")


def _run(child_py: Path, env: dict) -> str:
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


def test_abi_matched_cache_still_beats_the_workload(tmp_path):
    """roar's ABI-matched runtime cache (~/.cache/roar/runtime/<tag>/...) is still
    PREPENDED — it must beat the child's wrong-ABI system copies. Here it wins over
    the workload copy, confirming the must-win branch is preserved."""
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

    assert _run(child_py, env) == "cache"
