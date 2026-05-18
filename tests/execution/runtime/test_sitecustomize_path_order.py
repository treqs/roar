"""sitecustomize prepends ROAR_RUNTIME_PYTHONPATH entries (in order).

Behavior under test: when the traced Python doesn't already have roar
importable (the cross-Python lazy-install scenario), ``sitecustomize.py``
must put the entries from ``ROAR_RUNTIME_PYTHONPATH`` at the *front* of
``sys.path``, preserving the declared order. Appending (the old behavior)
lets the system's stale site-packages win — which is the friction-journal
bug where lazy-installed ``typing_extensions`` 4.15.0 lost to the
system's 4.4.x.

Tested via subprocess so we exercise the real sitecustomize module-import
side effects without polluting the test process's ``sys.path``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[3]


def _run_python(
    code: str,
    *,
    roar_runtime_pythonpath: str | None = None,
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
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=SOURCE_ROOT,
        timeout=30,
    )


def test_runtime_pythonpath_entries_land_at_front_in_declared_order(tmp_path: Path) -> None:
    """When roar isn't already importable, ROAR_RUNTIME_PYTHONPATH wins."""
    fake_runtime = tmp_path / "fake-runtime"
    fake_runtime.mkdir()
    fake_other = tmp_path / "fake-other"
    fake_other.mkdir()

    # Force find_spec("roar") to return None by monkey-patching it before
    # sitecustomize runs. We mark roar's site-packages location empty for the
    # purposes of this subprocess by inserting a stub finder ahead of it that
    # claims "roar is missing" — that's what triggers the prepend codepath.
    code = textwrap.dedent(
        """
        import importlib.util
        import sys

        _real_find_spec = importlib.util.find_spec
        def _patched_find_spec(name, *args, **kwargs):
            if name == "roar":
                return None
            return _real_find_spec(name, *args, **kwargs)
        importlib.util.find_spec = _patched_find_spec

        import importlib
        importlib.import_module("roar.execution.runtime.inject.sitecustomize")
        # The prepend has run; assert the entries are at the front, in order.
        print(f"first={sys.path[0]}")
        print(f"second={sys.path[1]}")
        """
    )
    result = _run_python(
        code,
        roar_runtime_pythonpath=os.pathsep.join([str(fake_runtime), str(fake_other)]),
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert f"first={fake_runtime}" in out, out
    assert f"second={fake_other}" in out, out


def test_no_prepend_when_roar_already_importable(tmp_path: Path) -> None:
    """When roar is already importable, the function early-returns and leaves sys.path alone."""
    fake_runtime = tmp_path / "fake-runtime"
    fake_runtime.mkdir()
    code = textwrap.dedent(
        f"""
        import importlib
        import sys
        # Roar IS importable (PYTHONPATH points at source root). Prepend should no-op.
        importlib.import_module("roar.execution.runtime.inject.sitecustomize")
        target = {str(fake_runtime)!r}
        in_top_three = target in sys.path[:3]
        print("in_top_three=" + str(in_top_three))
        """
    )
    result = _run_python(code, roar_runtime_pythonpath=str(fake_runtime))
    assert result.returncode == 0, result.stderr
    assert "in_top_three=False" in result.stdout, result.stdout
