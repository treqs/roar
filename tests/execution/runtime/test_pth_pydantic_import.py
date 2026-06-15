import os
import subprocess
import sys
import textwrap
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[3]
PTH_FILE = SOURCE_ROOT / "roar_inject.pth"


def _run_python(code: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    source_root = str(SOURCE_ROOT)
    env["PYTHONPATH"] = source_root if not existing else source_root + os.pathsep + existing
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=SOURCE_ROOT,
        timeout=30,
    )


def test_pth_import_does_not_require_pydantic() -> None:
    code = textwrap.dedent(
        """
        import importlib
        import importlib.abc
        import os
        import sys

        class _BlockPydantic(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.startswith("pydantic"):
                    raise ImportError(f"blocked import: {fullname}")
                return None

        sys.meta_path.insert(0, _BlockPydantic())
        os.environ["ROAR_WRAP"] = "1"
        importlib.import_module("roar.execution.runtime.inject.sitecustomize")
        """
    )

    result = _run_python(code)

    assert result.returncode == 0, result.stderr


def test_pth_import_chain_succeeds_when_pydantic_available() -> None:
    code = textwrap.dedent(
        """
        import importlib
        import os

        os.environ["ROAR_WRAP"] = "1"
        importlib.import_module("roar.execution.runtime.inject.sitecustomize")
        """
    )

    result = _run_python(code)

    assert result.returncode == 0, result.stderr
