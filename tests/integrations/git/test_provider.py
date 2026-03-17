"""Tests for git VCS provider behavior."""

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
