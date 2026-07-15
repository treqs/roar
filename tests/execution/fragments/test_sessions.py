from __future__ import annotations

from pathlib import Path

import pytest

from roar.execution.fragments.sessions import resolve_project_roar_dir


def test_resolves_project_root_from_subdirectory(tmp_path: Path) -> None:
    (tmp_path / ".roar").mkdir()
    nested = tmp_path / "jobs" / "nested"
    nested.mkdir(parents=True)

    assert resolve_project_roar_dir(environ={}, cwd=nested) == tmp_path / ".roar"


def test_project_dir_env_override_wins(tmp_path: Path) -> None:
    (tmp_path / ".roar").mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()

    resolved = resolve_project_roar_dir(environ={"ROAR_PROJECT_DIR": str(other)}, cwd=tmp_path)

    assert resolved == other / ".roar"


def test_falls_back_to_cwd_when_no_project_found(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()

    assert resolve_project_roar_dir(environ={}, cwd=nested) == nested / ".roar"


def test_walk_stops_at_git_repository_boundary(tmp_path: Path) -> None:
    # A .roar above an unrelated git repo must not be adopted.
    (tmp_path / ".roar").mkdir()
    repo = tmp_path / "unrelated-repo"
    (repo / ".git").mkdir(parents=True)
    inner = repo / "src"
    inner.mkdir()

    assert resolve_project_roar_dir(environ={}, cwd=inner) == inner / ".roar"


def test_git_root_with_roar_dir_is_used(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".roar").mkdir()
    nested = tmp_path / "src"
    nested.mkdir()

    assert resolve_project_roar_dir(environ={}, cwd=nested) == tmp_path / ".roar"


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_project_dir_env_is_ignored(tmp_path: Path, value: str) -> None:
    (tmp_path / ".roar").mkdir()
    nested = tmp_path / "sub"
    nested.mkdir()

    resolved = resolve_project_roar_dir(environ={"ROAR_PROJECT_DIR": value}, cwd=nested)

    assert resolved == tmp_path / ".roar"
