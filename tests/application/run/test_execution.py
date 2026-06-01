"""Tests for run/build execution helpers.

Focus: ``validate_git_clean`` resolves the working root and enforces a
clean tree *only* when inside a git repository. Outside a repo it returns
the cwd instead of erroring — roar runs there too, just without a commit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from roar.application.run.execution import validate_git_clean


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)


def test_returns_cwd_when_not_in_a_git_repo(tmp_path, monkeypatch) -> None:
    # The relaxation: no repo is no longer an error — roar captures
    # lineage from the working directory without a commit.
    monkeypatch.chdir(tmp_path)
    result = validate_git_clean()
    assert Path(result).resolve() == tmp_path.resolve()


def test_returns_repo_root_when_clean(tmp_path, monkeypatch) -> None:
    _git_init(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = validate_git_clean()
    assert Path(result).resolve() == tmp_path.resolve()


def test_raises_on_dirty_tree_inside_repo(tmp_path, monkeypatch) -> None:
    _git_init(tmp_path)
    (tmp_path / "train.py").write_text("print('hi')\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        validate_git_clean(verb="run", args=["python", "train.py"])
    assert str(excinfo.value).startswith("Run blocked:")
