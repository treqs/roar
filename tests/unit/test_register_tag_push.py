"""Tests for the P1-23 tag-and-push step that runs before GLaaS register.

Covers:
  - Canonical-remote resolution (3-step rule from the design doc).
  - `push_roar_tags` shell-out: refspec-explicit, single-call, fail-closed.
  - Lineage commit enumeration: de-dup, session-commit excluded from jobs.
  - `ensure_roar_tags_pushed` end-to-end across the four operating modes:
    dry-run, tagging disabled, push="never", push="auto".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from roar.application.publish.git_remote import (
    GitRemoteError,
    push_roar_tags,
    resolve_canonical_remote,
)
from roar.application.publish.register_tag_push import (
    TagPushError,
    _distinct_job_commits,
    ensure_roar_tags_pushed,
)
from roar.application.publish.results import RegisterTagSummary
from roar.core.interfaces.lineage import LineageData
from roar.core.logging import NullLogger

# ---------------------------------------------------------------------------
# git fixture
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=str(repo), text=True, stderr=subprocess.STDOUT
    )


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    _init_repo(repo_path)
    return repo_path


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    remote_path = tmp_path / "remote.git"
    subprocess.check_output(
        ["git", "init", "--bare", "-q", str(remote_path)], stderr=subprocess.STDOUT
    )
    return remote_path


# ---------------------------------------------------------------------------
# resolve_canonical_remote
# ---------------------------------------------------------------------------


class TestResolveCanonicalRemote:
    def test_no_remote_raises(self, repo: Path) -> None:
        with pytest.raises(GitRemoteError) as exc:
            resolve_canonical_remote(repo, None)
        assert "No git remote" in str(exc.value)
        # Hint the user about the opt-out so they're not stuck.
        assert "git.push_tags_on_register" in str(exc.value)

    def test_single_remote_used_when_no_override(self, repo: Path, bare_remote: Path) -> None:
        _git(repo, "remote", "add", "origin", str(bare_remote))
        assert resolve_canonical_remote(repo, None) == "origin"

    def test_multiple_remotes_require_explicit_config(
        self, repo: Path, bare_remote: Path, tmp_path: Path
    ) -> None:
        other_remote = tmp_path / "other.git"
        subprocess.check_output(
            ["git", "init", "--bare", "-q", str(other_remote)], stderr=subprocess.STDOUT
        )
        _git(repo, "remote", "add", "origin", str(bare_remote))
        _git(repo, "remote", "add", "upstream", str(other_remote))
        with pytest.raises(GitRemoteError) as exc:
            resolve_canonical_remote(repo, None)
        assert "Multiple git remotes" in str(exc.value)
        assert "git.remote" in str(exc.value)

    def test_configured_remote_must_exist(self, repo: Path, bare_remote: Path) -> None:
        _git(repo, "remote", "add", "origin", str(bare_remote))
        with pytest.raises(GitRemoteError) as exc:
            resolve_canonical_remote(repo, "upstream")
        assert "not a remote" in str(exc.value)

    def test_configured_remote_wins_over_single_remote(
        self, repo: Path, bare_remote: Path, tmp_path: Path
    ) -> None:
        other_remote = tmp_path / "other.git"
        subprocess.check_output(
            ["git", "init", "--bare", "-q", str(other_remote)], stderr=subprocess.STDOUT
        )
        _git(repo, "remote", "add", "origin", str(bare_remote))
        _git(repo, "remote", "add", "upstream", str(other_remote))
        assert resolve_canonical_remote(repo, "upstream") == "upstream"


# ---------------------------------------------------------------------------
# push_roar_tags
# ---------------------------------------------------------------------------


class TestPushRoarTags:
    def test_pushes_specific_refspecs(self, repo: Path, bare_remote: Path) -> None:
        _git(repo, "remote", "add", "origin", str(bare_remote))
        _git(repo, "tag", "roar/abc123")
        _git(repo, "tag", "roar/def456")
        _git(repo, "tag", "unrelated-tag")  # MUST NOT be pushed

        pushed = push_roar_tags(repo, "origin", ["roar/abc123", "roar/def456"])
        assert pushed == ["roar/abc123", "roar/def456"]

        remote_refs = subprocess.check_output(
            ["git", "ls-remote", "--tags", str(bare_remote)], text=True
        )
        assert "roar/abc123" in remote_refs
        assert "roar/def456" in remote_refs
        assert "unrelated-tag" not in remote_refs

    def test_dedups_input(self, repo: Path, bare_remote: Path) -> None:
        _git(repo, "remote", "add", "origin", str(bare_remote))
        _git(repo, "tag", "roar/abc123")
        pushed = push_roar_tags(repo, "origin", ["roar/abc123", "roar/abc123"])
        assert pushed == ["roar/abc123"]

    def test_empty_tag_list_is_noop(self, repo: Path, bare_remote: Path) -> None:
        _git(repo, "remote", "add", "origin", str(bare_remote))
        assert push_roar_tags(repo, "origin", []) == []

    def test_push_failure_raises_with_git_output(self, repo: Path, bare_remote: Path) -> None:
        _git(repo, "remote", "add", "origin", str(bare_remote))
        # The tag doesn't exist locally — git push will reject it.
        with pytest.raises(GitRemoteError) as exc:
            push_roar_tags(repo, "origin", ["roar/does-not-exist"])
        msg = str(exc.value)
        assert "Failed to push roar tags" in msg
        assert "origin" in msg


# ---------------------------------------------------------------------------
# _distinct_job_commits
# ---------------------------------------------------------------------------


class TestDistinctJobCommits:
    def test_excludes_session_commit_and_dedupes(self) -> None:
        lineage = LineageData(
            jobs=[
                {"git_commit": "aaa"},  # same as session — drop
                {"git_commit": "bbb"},
                {"git_commit": "ccc"},
                {"git_commit": "bbb"},  # dup — drop
            ]
        )
        result = _distinct_job_commits(lineage, "aaa")
        assert result == ["bbb", "ccc"]

    def test_ignores_missing_or_blank_commits(self) -> None:
        lineage = LineageData(
            jobs=[
                {"git_commit": ""},
                {"git_commit": None},
                {},  # no git_commit key
                {"git_commit": "xxx"},
            ]
        )
        assert _distinct_job_commits(lineage, "aaa") == ["xxx"]

    def test_none_lineage_returns_empty(self) -> None:
        assert _distinct_job_commits(None, "aaa") == []


# ---------------------------------------------------------------------------
# ensure_roar_tags_pushed (end-to-end across modes)
# ---------------------------------------------------------------------------


def _logger() -> NullLogger:
    return NullLogger()


class TestEnsureRoarTagsPushed:
    def test_dry_run_short_circuits(self, repo: Path) -> None:
        result = ensure_roar_tags_pushed(
            repo_root=repo,
            session_commit="abc",
            session_tag_name="roar/abc",
            lineage=None,
            dry_run=True,
            tagging_enabled=True,
            configured_remote=None,
            push_mode="auto",
            logger=_logger(),
        )
        assert result.push_skipped_reason == "dry_run"
        assert result.session_tag is None
        # Importantly: no local tag was created either.
        tags = _git(repo, "tag", "-l").strip()
        assert tags == ""

    def test_tagging_disabled_short_circuits(self, repo: Path) -> None:
        result = ensure_roar_tags_pushed(
            repo_root=repo,
            session_commit="abc",
            session_tag_name=None,
            lineage=None,
            dry_run=False,
            tagging_enabled=False,
            configured_remote=None,
            push_mode="auto",
            logger=_logger(),
        )
        assert result.push_skipped_reason == "tagging_disabled"

    def test_never_creates_but_does_not_push(self, repo: Path, bare_remote: Path) -> None:
        _git(repo, "remote", "add", "origin", str(bare_remote))
        head = _git(repo, "rev-parse", "HEAD").strip()
        lineage = LineageData(jobs=[{"git_commit": head}])

        result = ensure_roar_tags_pushed(
            repo_root=repo,
            session_commit=head,
            session_tag_name=f"roar/{head[:8]}",
            lineage=lineage,
            dry_run=False,
            tagging_enabled=True,
            configured_remote=None,
            push_mode="never",
            logger=_logger(),
        )
        assert result.push_skipped_reason == "never_config"
        assert result.session_tag == f"roar/{head[:8]}"
        assert result.remote is None
        # Tag exists locally
        local_tags = _git(repo, "tag", "-l").strip()
        assert f"roar/{head[:8]}" in local_tags
        # ...but NOT on remote
        remote_refs = subprocess.check_output(
            ["git", "ls-remote", "--tags", str(bare_remote)], text=True
        )
        assert "roar/" not in remote_refs

    def test_auto_creates_and_pushes_single_commit(self, repo: Path, bare_remote: Path) -> None:
        _git(repo, "remote", "add", "origin", str(bare_remote))
        head = _git(repo, "rev-parse", "HEAD").strip()
        result = ensure_roar_tags_pushed(
            repo_root=repo,
            session_commit=head,
            session_tag_name=f"roar/{head[:8]}",
            lineage=None,
            dry_run=False,
            tagging_enabled=True,
            configured_remote=None,
            push_mode="auto",
            logger=_logger(),
        )
        assert result.session_tag == f"roar/{head[:8]}"
        assert result.job_tags == ()
        assert result.remote == "origin"
        remote_refs = subprocess.check_output(
            ["git", "ls-remote", "--tags", str(bare_remote)], text=True
        )
        assert f"roar/{head[:8]}" in remote_refs

    def test_auto_pushes_session_plus_job_tags(self, repo: Path, bare_remote: Path) -> None:
        _git(repo, "remote", "add", "origin", str(bare_remote))
        # Make a second commit to use as a "previous job" commit.
        (repo / "b.txt").write_text("b\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "second")
        head = _git(repo, "rev-parse", "HEAD").strip()
        earlier = _git(repo, "rev-parse", "HEAD~1").strip()

        lineage = LineageData(
            jobs=[
                {"git_commit": earlier},
                {"git_commit": head},  # same as session — should not duplicate
            ]
        )
        result = ensure_roar_tags_pushed(
            repo_root=repo,
            session_commit=head,
            session_tag_name=f"roar/{head[:8]}",
            lineage=lineage,
            dry_run=False,
            tagging_enabled=True,
            configured_remote=None,
            push_mode="auto",
            logger=_logger(),
        )
        assert result.session_tag == f"roar/{head[:8]}"
        assert result.job_tags == (f"roar/{earlier[:8]}",)
        assert result.remote == "origin"
        remote_refs = subprocess.check_output(
            ["git", "ls-remote", "--tags", str(bare_remote)], text=True
        )
        assert f"roar/{head[:8]}" in remote_refs
        assert f"roar/{earlier[:8]}" in remote_refs

    def test_auto_fail_closed_when_no_remote(self, repo: Path) -> None:
        head = _git(repo, "rev-parse", "HEAD").strip()
        with pytest.raises(TagPushError) as exc:
            ensure_roar_tags_pushed(
                repo_root=repo,
                session_commit=head,
                session_tag_name=f"roar/{head[:8]}",
                lineage=None,
                dry_run=False,
                tagging_enabled=True,
                configured_remote=None,
                push_mode="auto",
                logger=_logger(),
            )
        msg = str(exc.value)
        assert "No git remote" in msg

    def test_unreachable_commit_in_lineage_fails_closed(
        self, repo: Path, bare_remote: Path
    ) -> None:
        """If a job references a commit that's no longer in the repo, the
        local tag-creation step fails — we never proceed to push or GLaaS.

        With a remote set up, this proves we fail *before* push (no remote
        write, no GLaaS write)."""
        _git(repo, "remote", "add", "origin", str(bare_remote))
        head = _git(repo, "rev-parse", "HEAD").strip()
        lineage = LineageData(jobs=[{"git_commit": "deadbeef" * 5}])
        with pytest.raises(TagPushError) as exc:
            ensure_roar_tags_pushed(
                repo_root=repo,
                session_commit=head,
                session_tag_name=f"roar/{head[:8]}",
                lineage=lineage,
                dry_run=False,
                tagging_enabled=True,
                configured_remote=None,
                push_mode="auto",
                logger=_logger(),
            )
        msg = str(exc.value)
        assert "Failed to create roar tag" in msg
        assert "deadbeef" in msg
        # Nothing should have been pushed to the remote.
        remote_refs = subprocess.check_output(
            ["git", "ls-remote", "--tags", str(bare_remote)], text=True
        )
        assert "roar/" not in remote_refs


# ---------------------------------------------------------------------------
# RegisterTagSummary CLI rendering smoke
# ---------------------------------------------------------------------------


class TestCliRender:
    def test_render_multi_commit(self, capsys: pytest.CaptureFixture[str]) -> None:
        from roar.cli.commands.register import _render_tag_summary

        _render_tag_summary(
            RegisterTagSummary(
                session_tag="roar/abc12345",
                job_tags=("roar/def67890", "roar/9abcde01"),
                remote="origin",
            )
        )
        out = capsys.readouterr().out
        assert "Tagged session commit as roar/abc12345" in out
        assert "Tagged 2 additional job commits: roar/def67890, roar/9abcde01" in out
        assert "Pushed 3 tags to origin" in out

    def test_render_single_commit(self, capsys: pytest.CaptureFixture[str]) -> None:
        from roar.cli.commands.register import _render_tag_summary

        _render_tag_summary(RegisterTagSummary(session_tag="roar/abc12345", remote="origin"))
        out = capsys.readouterr().out
        assert "Tagged session commit as roar/abc12345" in out
        assert "additional job" not in out
        assert "Pushed 1 tag to origin" in out

    def test_render_never_mode_warns_loudly(self, capsys: pytest.CaptureFixture[str]) -> None:
        from roar.cli.commands.register import _render_tag_summary

        _render_tag_summary(
            RegisterTagSummary(
                session_tag="roar/abc12345",
                push_skipped_reason="never_config",
            )
        )
        captured = capsys.readouterr()
        # The warning goes to stderr.
        assert "NOT pushed" in captured.err
        assert "will not resolve" in captured.err

    def test_render_none_is_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        from roar.cli.commands.register import _render_tag_summary

        _render_tag_summary(None)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
