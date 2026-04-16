"""Integration coverage for the ``from roar import require`` guard."""

from __future__ import annotations

import textwrap

import pytest

pytestmark = [pytest.mark.integration]


def test_roar_run_allows_nested_python_children_to_import_require(
    temp_git_repo,
    roar_cli,
    git_commit,
    python_exe,
):
    """A grandchild Python process should still see an ancestor running under Roar."""
    (temp_git_repo / "guarded_child.py").write_text(
        "from roar import require\nprint('CHILD_GUARD_OK', flush=True)\n"
    )
    (temp_git_repo / "intermediary.py").write_text(
        textwrap.dedent(
            """\
            import subprocess
            import sys

            result = subprocess.run(
                [sys.executable, "guarded_child.py"],
                capture_output=True,
                text=True,
                check=False,
            )
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
            raise SystemExit(result.returncode)
            """
        )
    )
    (temp_git_repo / "launcher.py").write_text(
        textwrap.dedent(
            """\
            import subprocess
            import sys

            result = subprocess.run(
                [sys.executable, "intermediary.py"],
                capture_output=True,
                text=True,
                check=False,
            )
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
            raise SystemExit(result.returncode)
            """
        )
    )
    git_commit("add nested require guard scripts")

    result = roar_cli("run", python_exe, "launcher.py", check=False)

    assert result.returncode == 0, (
        f"roar run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "CHILD_GUARD_OK" in result.stdout
