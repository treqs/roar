"""The lazy-install runtime probe + install must target the SAME interpreter.

A bare ``command[0]`` like "python" is resolved against PATH for the ABI probe, but
``uv pip install --python python`` uses uv's own discovery and can pick a different
interpreter (e.g. a 3.13 venv), landing wrong-ABI wheels in the runtime tree. The tracer
resolves to an absolute path up front so both agree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from roar.execution.runtime import abi_probe, lazy_install
from roar.execution.runtime.tracer import TracerService


def test_bare_python_is_resolved_to_absolute_path_for_probe_and_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    abs_python = "/opt/pythons/3.12/bin/python"
    captured: dict[str, str] = {}

    monkeypatch.setattr("shutil.which", lambda name: abs_python if name == "python" else None)

    def _probe(target: str) -> str:
        captured["probe"] = target
        return "cpython-000"  # never equals the test interpreter's bundled abi

    def _ensure(*, target_python: str, **_kwargs: object) -> None:
        captured["ensure"] = target_python
        return None

    monkeypatch.setattr(abi_probe, "probe_python_abi", _probe)
    monkeypatch.setattr(lazy_install, "ensure_runtime", _ensure)

    TracerService()._lazy_install_runtime_entries(["python", "train.py"], tmp_path)

    # Both the ABI probe and the uv --python install target the resolved absolute path,
    # not the bare "python" name.
    assert captured["probe"] == abs_python
    assert captured["ensure"] == abs_python
