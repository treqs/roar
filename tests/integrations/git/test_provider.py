"""Tests for git VCS provider behavior."""

import subprocess
from unittest.mock import patch

from roar.integrations.git.provider import GitVCSProvider


def test_get_info_returns_empty_when_git_binary_is_missing(tmp_path) -> None:
    provider = GitVCSProvider()

    with patch.object(provider, "is_available", return_value=False):
        info = provider.get_info(str(tmp_path))

    assert info.commit is None
    assert info.branch is None
    assert info.remote_url is None
    assert info.clean is True
    assert info.uncommitted_changes == []


def test_get_status_returns_clean_when_git_binary_is_missing(tmp_path) -> None:
    provider = GitVCSProvider()

    with patch(
        "roar.integrations.git.provider.subprocess.check_output",
        side_effect=FileNotFoundError("git"),
    ):
        clean, changes = provider.get_status(str(tmp_path))

    assert (clean, changes) == (True, [])


def test_get_status_suppresses_git_stderr_outside_a_repo(tmp_path, capfd) -> None:
    # roar runs outside a git repo now; probing status there must not leak
    # git's "fatal: not a git repository" chatter onto the user's terminal.
    provider = GitVCSProvider()

    clean, changes = provider.get_status(str(tmp_path))

    assert (clean, changes) == (True, [])
    captured = capfd.readouterr()
    assert "fatal:" not in captured.err
    assert "not a git repository" not in captured.err


def test_get_status_passes_devnull_stderr(tmp_path) -> None:
    provider = GitVCSProvider()

    with patch(
        "roar.integrations.git.provider.subprocess.check_output",
        return_value=b"",
    ) as mock_check:
        provider.get_status(str(tmp_path))

    assert mock_check.call_args.kwargs.get("stderr") is subprocess.DEVNULL
