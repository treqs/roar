from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from roar.application.git import (
    build_roar_git_tag_name,
    create_roar_git_tag,
    ensure_clean_git_repo,
    finalize_put_git,
    finalize_register_git,
    prepare_put_git,
    resolve_git_state,
    resolve_roar_git_context,
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


def test_resolve_git_state_returns_repo_root_and_commit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    state = resolve_git_state(repo)

    assert state.repo_root == repo
    assert len(state.commit) == 40


def test_ensure_clean_git_repo_rejects_dirty_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "file.txt").write_text("modified")

    with pytest.raises(ValueError, match="dirty"):
        ensure_clean_git_repo(repo, error_message="dirty")


def test_create_roar_git_tag_is_idempotent(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    state = resolve_git_state(repo)
    tag_name = build_roar_git_tag_name(state.commit)

    success_1, error_1 = create_roar_git_tag(repo, tag_name)
    success_2, error_2 = create_roar_git_tag(repo, tag_name)

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


def test_build_roar_git_tag_name_supports_short_form() -> None:
    commit = "abc123def456"

    assert build_roar_git_tag_name(commit) == "roar/abc123def456"
    assert build_roar_git_tag_name(commit, short=True) == "roar/abc123de"


def test_prepare_put_git_returns_git_state_and_expected_tag(tmp_path: Path) -> None:
    logger = MagicMock()
    git_state = MagicMock(commit="deadbeef", repo_root=tmp_path)

    with patch("roar.application.git.ensure_clean_git_repo", return_value=git_state):
        prepared = prepare_put_git(
            repo_root=tmp_path,
            dry_run=False,
            no_tag=False,
            logger=logger,
        )

    assert prepared.git_state is git_state
    assert prepared.git_commit == "deadbeef"
    assert prepared.expected_tag == "roar/deadbeef"
    assert prepared.warnings == ()


def test_prepare_put_git_returns_warning_when_git_preflight_fails(tmp_path: Path) -> None:
    logger = MagicMock()

    with patch(
        "roar.application.git.ensure_clean_git_repo",
        side_effect=RuntimeError("git unavailable"),
    ):
        prepared = prepare_put_git(
            repo_root=tmp_path,
            dry_run=False,
            no_tag=False,
            logger=logger,
        )

    assert prepared.git_state is None
    assert prepared.git_commit is None
    assert prepared.expected_tag is None
    assert prepared.warnings == ("Git operation failed: git unavailable",)


def test_finalize_put_git_returns_created_tag(tmp_path: Path) -> None:
    logger = MagicMock()
    git_state = MagicMock(repo_root=tmp_path)

    with patch(
        "roar.application.git.create_roar_git_tag",
        return_value=(True, None),
    ) as create_tag:
        created_tag, warnings = finalize_put_git(
            result_success=True,
            result_dry_run=False,
            no_tag=False,
            git_commit="deadbeef",
            expected_tag="roar/deadbeef",
            git_state=git_state,
            repo_root=tmp_path,
            logger=logger,
        )

    assert created_tag == "roar/deadbeef"
    assert warnings == []
    create_tag.assert_called_once_with(tmp_path, "roar/deadbeef")


def test_finalize_register_git_creates_tag_when_enabled(tmp_path: Path) -> None:
    logger = MagicMock()

    with patch(
        "roar.application.git.create_roar_git_tag",
        return_value=(True, None),
    ) as create_tag:
        finalize_register_git(
            result_success=True,
            dry_run=False,
            git_tag_name="roar/deadbeef",
            git_tag_repo_root=tmp_path,
            logger=logger,
        )

    create_tag.assert_called_once_with(tmp_path, "roar/deadbeef")


def test_resolve_roar_git_context_uses_shared_transfer_resolution() -> None:
    logger = MagicMock()

    with patch("roar.application.git.resolve_git_context") as resolve_git_context:
        resolve_git_context.return_value.repo = "file:///repo"
        resolve_git_context.return_value.commit = "deadbeef" * 5
        resolve_git_context.return_value.branch = "main"

        ctx = resolve_roar_git_context(Path("/tmp/repo"), logger=logger)

    assert ctx is resolve_git_context.return_value
    resolve_git_context.assert_called_once_with(Path("/tmp/repo"), None)
    logger.debug.assert_any_call("Resolving git context from %s", Path("/tmp/repo"))
