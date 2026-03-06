import os
import subprocess
import sys
import textwrap
from pathlib import Path


SOURCE_ROOT = Path("/home/trevor/dev/roar")


def _run_python(code: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(SOURCE_ROOT)}
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
        importlib.import_module("roar.services.execution.inject.sitecustomize")
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
        importlib.import_module("roar.services.execution.inject.sitecustomize")
        """
    )

    result = _run_python(code)

    assert result.returncode == 0, result.stderr
