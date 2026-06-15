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


# --- canonical remote resolution (real temp repos) ---------------------------
# A repo whose only remote isn't named "origin" (e.g. "treqs") must still record
# its remote URL as git_repo, not fall back to a local file:// path.


def _git(repo, *args: str) -> None:
    subprocess.check_call(
        ["git", *args], cwd=str(repo), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def _repo(tmp_path, remotes: dict[str, str]):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    for name, url in remotes.items():
        _git(repo, "remote", "add", name, url)
    return repo


def test_resolve_remote_name_falls_back_to_sole_remote(tmp_path) -> None:
    repo = _repo(tmp_path, {"treqs": "https://github.com/treqs/nanochat.git"})
    assert GitVCSProvider().resolve_remote_name(str(repo)) == "treqs"


def test_resolve_remote_name_prefers_origin(tmp_path) -> None:
    repo = _repo(tmp_path, {"treqs": "https://x/a.git", "origin": "https://x/o.git"})
    assert GitVCSProvider().resolve_remote_name(str(repo)) == "origin"


def test_resolve_remote_name_honors_configured(tmp_path) -> None:
    repo = _repo(tmp_path, {"treqs": "https://x/a.git", "origin": "https://x/o.git"})
    assert GitVCSProvider().resolve_remote_name(str(repo), configured="treqs") == "treqs"


def test_resolve_remote_name_ambiguous_returns_none(tmp_path) -> None:
    repo = _repo(tmp_path, {"a": "https://x/a.git", "b": "https://x/b.git"})
    assert GitVCSProvider().resolve_remote_name(str(repo)) is None


def test_get_remote_url_uses_sole_non_origin_remote(tmp_path) -> None:
    url = "https://github.com/treqs/nanochat.git"
    repo = _repo(tmp_path, {"treqs": url})
    assert GitVCSProvider().get_remote_url(str(repo)) == url


def test_get_info_records_non_origin_remote_url(tmp_path) -> None:
    url = "https://github.com/treqs/nanochat.git"
    repo = _repo(tmp_path, {"treqs": url})
    assert GitVCSProvider().get_info(str(repo)).remote_url == url


def test_resolve_git_context_records_url_not_file_uri_for_non_origin_remote(tmp_path) -> None:
    from roar.integrations.git.context import resolve_git_context

    url = "https://github.com/treqs/nanochat.git"
    repo = _repo(tmp_path, {"treqs": url})
    (repo / "a.txt").write_text("a\n")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")

    ctx = resolve_git_context(repo)
    assert ctx.repo == url
    assert not str(ctx.repo).startswith("file://")
