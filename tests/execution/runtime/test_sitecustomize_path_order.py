"""sitecustomize places ROAR_RUNTIME_PYTHONPATH entries with the right precedence.

When the traced Python can't already import roar (the cross-Python /
lazy-install scenario), ``sitecustomize.py`` adds safe
``ROAR_RUNTIME_PYTHONPATH`` entries to ``sys.path`` with two precedences:

- a non-conflicting **ABI-matched runtime cache** is **prepended** so it can
  beat the child's system copies;
- a cache with any workload import-name collision is not activated;
- everything else (roar's host site-packages, added only so a cross-interpreter
  child can import roar) is **appended** — prepending it was P0-14: roar's host
  packages shadowed the workload's recorded pins and the run executed against
  host packages.

Tested via subprocess so we exercise the real sitecustomize module-import side
effects without polluting the test process's ``sys.path``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[3]

# Patches find_spec("roar") -> None so the add-path codepath runs, then imports
# sitecustomize. Callers append their own print statements.
_PATCH_AND_IMPORT = """
import importlib.util, importlib, sys
_real = importlib.util.find_spec
importlib.util.find_spec = lambda name, *a, **k: None if name == "roar" else _real(name, *a, **k)
_sitecustomize = importlib.import_module("roar.execution.runtime.inject.sitecustomize")
"""


def _run_python(
    code: str,
    *,
    roar_runtime_pythonpath: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess Python with sitecustomize loaded from this source tree."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    source_root = str(SOURCE_ROOT)
    env["PYTHONPATH"] = source_root if not existing else source_root + os.pathsep + existing
    if roar_runtime_pythonpath is not None:
        env["ROAR_RUNTIME_PYTHONPATH"] = roar_runtime_pythonpath
    else:
        env.pop("ROAR_RUNTIME_PYTHONPATH", None)
    env.pop("ROAR_WRAP", None)  # skip the backend-dispatch gate; we only care about path order
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=SOURCE_ROOT,
        timeout=30,
    )


def test_abi_matched_cache_is_prepended(tmp_path: Path) -> None:
    """roar's ABI-matched runtime cache must beat system copies -> prepended."""
    cache_home = tmp_path / "xdg"
    cache_dir = cache_home / "roar" / "runtime" / "cp999" / "site-packages"
    cache_dir.mkdir(parents=True)

    code = _PATCH_AND_IMPORT + "print(f'first={sys.path[0]}')\n"
    result = _run_python(
        code,
        roar_runtime_pythonpath=str(cache_dir),
        extra_env={"XDG_CACHE_HOME": str(cache_home)},
    )
    assert result.returncode == 0, result.stderr
    assert f"first={cache_dir}" in result.stdout, result.stdout


def test_abi_cache_with_workload_collision_is_not_activated(tmp_path: Path) -> None:
    cache_home = tmp_path / "xdg"
    cache_dir = cache_home / "roar" / "runtime" / "cp999" / "site-packages"
    workload_dir = tmp_path / "workload"
    cache_dir.mkdir(parents=True)
    workload_dir.mkdir()
    (cache_dir / "shadowpkg.py").write_text("MARK = 'cache'\n", encoding="utf-8")
    (workload_dir / "shadowpkg.py").write_text("MARK = 'workload'\n", encoding="utf-8")

    code = _PATCH_AND_IMPORT + textwrap.dedent(
        f"""
        import os, shadowpkg
        target = {str(cache_dir)!r}
        print("cache_present=" + str(target in sys.path))
        print("winner=" + shadowpkg.MARK)
        print("collisions=" + os.environ.get("ROAR_RUNTIME_CACHE_COLLISIONS", ""))
        """
    )
    result = _run_python(
        code,
        roar_runtime_pythonpath=str(cache_dir),
        extra_env={
            "XDG_CACHE_HOME": str(cache_home),
            "PYTHONPATH": os.pathsep.join([str(SOURCE_ROOT), str(workload_dir)]),
        },
    )
    assert result.returncode == 0, result.stderr
    assert "cache_present=False" in result.stdout, result.stdout
    assert "winner=workload" in result.stdout, result.stdout
    assert "collisions=shadowpkg" in result.stdout, result.stdout


def test_in_process_repair_degrades_instead_of_activating_a_colliding_cache(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache" / "site-packages"
    workload_dir = tmp_path / "workload"
    cache_dir.mkdir(parents=True)
    workload_dir.mkdir()
    (cache_dir / "shadowpkg.py").write_text("MARK = 'cache'\n", encoding="utf-8")
    (workload_dir / "shadowpkg.py").write_text("MARK = 'workload'\n", encoding="utf-8")

    code = _PATCH_AND_IMPORT + textwrap.dedent(
        f"""
        import os
        from pathlib import Path
        from roar.execution.runtime import lazy_install

        lazy_install.ensure_runtime = lambda **_kwargs: Path({str(cache_dir)!r})
        repaired = _sitecustomize._repair_runtime_in_process("cpython-999")
        print("repaired=" + str(repaired))
        print("cache_present=" + str({str(cache_dir)!r} in sys.path))
        print("collisions=" + os.environ.get("ROAR_RUNTIME_CACHE_COLLISIONS", ""))
        print(_sitecustomize._runtime_gate_degrade_message((3, 99)))
        """
    )
    result = _run_python(
        code,
        extra_env={
            "PYTHONPATH": os.pathsep.join([str(SOURCE_ROOT), str(workload_dir)]),
        },
    )
    assert result.returncode == 0, result.stderr
    assert "repaired=False" in result.stdout, result.stdout
    assert "cache_present=False" in result.stdout, result.stdout
    assert "collisions=shadowpkg" in result.stdout, result.stdout
    assert "Runtime cache disabled to preserve workload imports: shadowpkg." in result.stdout


def test_host_site_packages_are_appended_not_prepended(tmp_path: Path) -> None:
    """P0-14: a non-cache runtime entry is appended, so it can't shadow the
    workload — present on sys.path, but not at the front."""
    fake_host = tmp_path / "fake-host"
    fake_host.mkdir()

    code = _PATCH_AND_IMPORT + textwrap.dedent(
        f"""
        target = {str(fake_host)!r}
        print("present=" + str(target in sys.path))
        print("at_front=" + str(sys.path[0] == target))
        print("at_back=" + str(sys.path[-1] == target))
        """
    )
    result = _run_python(code, roar_runtime_pythonpath=str(fake_host))
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "present=True" in out, out
    assert "at_front=False" in out, out
    assert "at_back=True" in out, out


def test_no_change_when_roar_already_importable(tmp_path: Path) -> None:
    """When roar is already importable, the function early-returns and leaves
    sys.path alone."""
    fake_runtime = tmp_path / "fake-runtime"
    fake_runtime.mkdir()
    code = textwrap.dedent(
        f"""
        import importlib, sys
        # Roar IS importable (PYTHONPATH points at source root). Add-path should no-op.
        importlib.import_module("roar.execution.runtime.inject.sitecustomize")
        target = {str(fake_runtime)!r}
        print("present=" + str(target in sys.path))
        """
    )
    result = _run_python(code, roar_runtime_pythonpath=str(fake_runtime))
    assert result.returncode == 0, result.stderr
    assert "present=False" in result.stdout, result.stdout
