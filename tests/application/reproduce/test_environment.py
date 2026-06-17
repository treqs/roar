"""Tests for the reproduction code-source resolver."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from roar.application.reproduce import environment as env
from roar.application.reproduce.environment import resolve_code_source
from roar.core.interfaces.reproduction import PipelineInfo


def _make_pipeline(**kwargs):
    defaults = {
        "artifact_hash": "abc123",
        "git_repo": "https://github.com/user/repo.git",
        "git_commit": "deadbeefcafe",
        "build_steps": [],
        "run_steps": [],
        "total_steps": 0,
    }
    defaults.update(kwargs)
    return PipelineInfo(**defaults)


def _patch(**overrides):
    """Patch the resolver's git helpers; sensible defaults, override per test."""
    defaults = {
        "_git_toplevel": lambda cwd: None,
        "_git_origin": lambda root: None,
        "_commit_exists": lambda root, commit: False,
        "_is_dirty": lambda root: False,
        "_git_fetch": lambda root: None,
        "_add_worktree": lambda root, target, commit, *, presenter: Path(target) / "wt",
    }
    defaults.update(overrides)
    return [patch.object(env, name, fn) for name, fn in defaults.items()]


def _run(cwd, pipeline=None, **overrides):
    patches = _patch(**overrides)
    for p in patches:
        p.start()
    try:
        return resolve_code_source(cwd, pipeline or _make_pipeline(), presenter=MagicMock())
    finally:
        for p in patches:
            p.stop()


def test_matching_repo_clean_tree_uses_worktree(tmp_path):
    plan = _run(
        tmp_path,
        _git_toplevel=lambda cwd: str(tmp_path),
        _commit_exists=lambda root, commit: True,  # recorded commit is here
        _is_dirty=lambda root: False,
    )
    assert plan.kind == "worktree"
    assert plan.repo_dir == tmp_path / "reproduce" / "wt"


def test_matching_repo_dirty_tree_errors(tmp_path):
    with pytest.raises(ValueError, match="uncommitted changes"):
        _run(
            tmp_path,
            _git_toplevel=lambda cwd: str(tmp_path),
            _commit_exists=lambda root, commit: True,
            _is_dirty=lambda root: True,
        )


def test_inside_nonmatching_repo_errors(tmp_path):
    with pytest.raises(ValueError, match="doesn't match this lineage"):
        _run(
            tmp_path,
            _git_toplevel=lambda cwd: str(tmp_path),
            _git_origin=lambda root: "git@github.com:other/project.git",
            _commit_exists=lambda root, commit: False,  # commit not here, origin differs
        )


def test_matching_by_origin_fetches_then_worktrees(tmp_path):
    # origin matches but the commit isn't local yet; a fetch makes it appear.
    state = {"fetched": False}

    def fetch(root):
        state["fetched"] = True

    def commit_exists(root, commit):
        return state["fetched"]

    plan = _run(
        tmp_path,
        _git_toplevel=lambda cwd: str(tmp_path),
        _git_origin=lambda root: "git@github.com:user/repo.git",
        _commit_exists=commit_exists,
        _git_fetch=fetch,
    )
    assert state["fetched"] is True
    assert plan.kind == "worktree"


def test_not_in_repo_with_remote_clones(tmp_path):
    plan = _run(tmp_path, _git_toplevel=lambda cwd: None)
    assert plan.kind == "clone"
    assert plan.repo_dir is None


def test_no_repo_no_commit_reruns(tmp_path):
    plan = _run(
        tmp_path,
        _make_pipeline(git_repo="", git_commit=""),
        _git_toplevel=lambda cwd: None,
    )
    assert plan.kind == "rerun"
    assert plan.repo_dir == tmp_path / "reproduce" / "rerun"


def test_commit_recorded_no_remote_not_in_repo_errors(tmp_path):
    with pytest.raises(ValueError, match="no published remote"):
        _run(
            tmp_path,
            _make_pipeline(git_repo="", git_commit="deadbeefcafe"),
            _git_toplevel=lambda cwd: None,
        )
