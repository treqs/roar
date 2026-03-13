from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from roar.application.publish.git import (
    build_publish_tag_name,
    create_publish_git_tag,
    ensure_clean_publish_repo,
    resolve_publish_git_context,
    resolve_publish_git_state,
)


def _init_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "file.txt").write_text("content")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def test_resolve_publish_git_state_returns_repo_root_and_commit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    state = resolve_publish_git_state(repo)

    assert state.repo_root == repo
    assert len(state.commit) == 40


def test_ensure_clean_publish_repo_rejects_dirty_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "file.txt").write_text("modified")

    with pytest.raises(ValueError, match="dirty"):
        ensure_clean_publish_repo(repo, error_message="dirty")


def test_create_publish_git_tag_is_idempotent(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    state = resolve_publish_git_state(repo)
    tag_name = build_publish_tag_name(state.commit)

    success_1, error_1 = create_publish_git_tag(repo, tag_name)
    success_2, error_2 = create_publish_git_tag(repo, tag_name)

    assert (success_1, error_1) == (True, None)
    assert (success_2, error_2) == (True, None)
    result = subprocess.run(
        ["git", "tag", "-l", tag_name],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert tag_name in result.stdout


def test_build_publish_tag_name_supports_short_form() -> None:
    commit = "abc123def456"

    assert build_publish_tag_name(commit) == "roar/abc123def456"
    assert build_publish_tag_name(commit, short=True) == "roar/abc123de"


def test_resolve_publish_git_context_uses_shared_transfer_resolution() -> None:
    logger = MagicMock()

    with patch("roar.application.publish.git.resolve_git_context") as resolve_git_context:
        resolve_git_context.return_value.repo = "file:///repo"
        resolve_git_context.return_value.commit = "deadbeef" * 5
        resolve_git_context.return_value.branch = "main"

        ctx = resolve_publish_git_context(Path("/tmp/repo"), logger=logger)

    assert ctx is resolve_git_context.return_value
    resolve_git_context.assert_called_once_with(Path("/tmp/repo"), None)
    logger.debug.assert_any_call("Resolving git context from %s", Path("/tmp/repo"))
