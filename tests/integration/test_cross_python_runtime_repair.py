"""Integration coverage for the cross-Python in-process runtime repair.

Reproduces the real failure mode the unit tests can only approximate: a
NON-python launcher (a shell script, standing in for ``torchrun`` / ``uv run``)
spawns several workers on a *different* CPython than the one roar's bundled deps
were built for. Each worker's ``sitecustomize`` gate must repair its own runtime
in-process (so wrapper launches work at all), and N concurrent workers must
collapse to a single install (the per-ABI lock), not a thundering herd.

Skipped unless ``uv`` and a second CPython minor are available — real coverage
where the environment supports it (dev boxes, the integration CI lane), silent
elsewhere. The unit test ``test_apply_runtime_gate_*`` guards the gate ordering
in every CI job regardless.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pydantic_core
import pytest

import roar

pytestmark = pytest.mark.integration

_INJECT_DIR = str(Path(roar.__file__).resolve().parent / "execution" / "runtime" / "inject")
# Roar's importable root + the site-packages carrying the *current* (and so,
# for a different-ABI worker, wrong-ABI) pydantic_core — mirrors what the tracer
# puts on ROAR_RUNTIME_PYTHONPATH for a cross-Python child.
_SOURCE_ROOT = str(Path(roar.__file__).resolve().parent.parent)
_CURRENT_SITE_PACKAGES = str(Path(pydantic_core.__file__).resolve().parent.parent)

_WORKER_PAYLOAD = textwrap.dedent(
    """
    import json, sys
    try:
        import pydantic_core
        print(json.dumps({"ok": True, "file": pydantic_core.__file__,
                          "tag": sys.implementation.cache_tag}))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "err": repr(exc),
                          "tag": sys.implementation.cache_tag}))
    """
)


def _find_other_interpreter() -> str | None:
    """An installed CPython whose minor differs from the test interpreter's."""
    uv = shutil.which("uv")
    if not uv:
        return None
    current = f"{sys.version_info.major}.{sys.version_info.minor}"
    for minor in ("3.10", "3.11", "3.12", "3.13"):
        if minor == current:
            continue
        proc = subprocess.run(
            [uv, "python", "find", minor], capture_output=True, text=True, check=False
        )
        path = proc.stdout.strip()
        if proc.returncode == 0 and path and Path(path).exists():
            return path
    return None


def _cache_tag(interpreter: str) -> str:
    proc = subprocess.run(
        [interpreter, "-c", "import sys; print(sys.implementation.cache_tag)"],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def test_wrapper_launch_repairs_runtime_in_process_and_installs_once(tmp_path: Path) -> None:
    other = _find_other_interpreter()
    if other is None:
        pytest.skip("needs uv and a second installed CPython minor")

    worker_count = 4
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    launcher = tmp_path / "launcher.sh"
    # argv[0] is `bash` (the launcher), NOT python — so the launch-time ABI
    # probe bails and repair must happen in-process inside each worker.
    launcher.write_text(
        "#!/usr/bin/env bash\nset -u\n"
        'PY="$1"; OUT="$2"; N="$3"\n'
        "pids=()\n"
        'for i in $(seq 1 "$N"); do\n'
        '  "$PY" -c "$PAYLOAD" >"$OUT/w$i.json" 2>"$OUT/w$i.err" &\n'
        "  pids+=($!)\n"
        "done\n"
        'for p in "${pids[@]}"; do wait "$p"; done\n'
    )

    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([_INJECT_DIR, _SOURCE_ROOT, _CURRENT_SITE_PACKAGES]),
        "ROAR_WRAP": "1",
        "XDG_CACHE_HOME": str(tmp_path / "xdg"),
        "PAYLOAD": _WORKER_PAYLOAD,
    }

    subprocess.run(
        ["bash", str(launcher), other, str(out_dir), str(worker_count)],
        env=env,
        check=True,
        timeout=300,
    )

    results = [json.loads(p.read_text() or "{}") for p in sorted(out_dir.glob("w*.json"))]
    errs = "\n".join(p.read_text() for p in sorted(out_dir.glob("w*.err")))

    # If the install couldn't run (offline box), don't fail the suite for it.
    if any(
        "install failed" in (out_dir / f"w{i + 1}.err").read_text() for i in range(worker_count)
    ):
        pytest.skip(f"runtime install unavailable in this environment:\n{errs}")

    assert len(results) == worker_count
    # Every worker imported an ABI-matched pydantic_core (not roar's wrong-ABI
    # bundled copy) — i.e. the in-process repair fired despite the wrapper.
    other_tag = _cache_tag(other)
    for result in results:
        assert result.get("ok") is True, f"worker crashed: {result}\n{errs}"
        assert f"/roar/runtime/{other_tag}/" in result["file"], result

    # The per-ABI lock collapsed N concurrent installs into exactly one.
    install_lines = errs.count("installing roar runtime")
    assert install_lines == 1, f"expected a single install across workers, got {install_lines}"
