"""Tests for the git-readiness summary surfaced by `roar status` (P1-11).

Covers:
  - Porcelain line categorization (modified/untracked/added/deleted/
    renamed/conflict, with priority).
  - The four reported states (clean / dirty / not_a_repo / home_dir),
    each driven by a real git working tree under tmp_path.
  - The single-line render for each state.
  - `roar status` output integration: the `Git:` line shows up at the
    top with the right wording.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from roar.application.query.git_readiness import (
    GitReadinessSummary,
    _categorize_porcelain_line,
    collect_git_readiness,
)

# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


class TestCategorizePorcelainLine:
    @pytest.mark.parametrize(
        "line,bucket",
        [
            (" M file.py", "modified"),
            ("M  file.py", "modified"),
            ("MM file.py", "modified"),
            ("?? new.py", "untracked"),
            ("A  added.py", "added"),
            (" D removed.py", "deleted"),
            ("D  removed.py", "deleted"),
            ("R  old.py -> new.py", "renamed"),
            ("C  src.py -> copy.py", "renamed"),
            ("UU conflict.py", "conflict"),
            ("AA both-added.py", "conflict"),
            ("DD both-deleted.py", "conflict"),
            ("AU left-added.py", "conflict"),
            ("UA right-added.py", "conflict"),
        ],
    )
    def test_buckets(self, line: str, bucket: str) -> None:
        assert _categorize_porcelain_line(line) == bucket

    def test_conflict_beats_modified(self) -> None:
        # 'UU' has no 'M' but *would* be 'modified' under a naive
        # second-char check — confirm conflict wins.
        assert _categorize_porcelain_line("UU file") == "conflict"

    def test_unparseable_line_returns_none(self) -> None:
        assert _categorize_porcelain_line("") is None
        assert _categorize_porcelain_line("X") is None


# ---------------------------------------------------------------------------
# render_line
# ---------------------------------------------------------------------------


class TestRenderLine:
    def test_clean_with_branch_and_commit(self) -> None:
        line = GitReadinessSummary(
            state="clean", branch="main", short_commit="abc1234"
        ).render_line()
        assert line == "clean — main @ abc1234  ('roar run' ready)"

    def test_clean_without_branch(self) -> None:
        line = GitReadinessSummary(state="clean").render_line()
        assert line == "clean  ('roar run' ready)"

    def test_dirty_lists_only_nonzero_buckets(self) -> None:
        line = GitReadinessSummary(
            state="dirty", modified=3, untracked=1, added=0, deleted=0
        ).render_line()
        assert line == "dirty — 3 modified, 1 untracked  ('roar run' will be refused)"

    def test_dirty_user_facing_example(self) -> None:
        """Matches the proposed P1-11 wording verbatim."""
        line = GitReadinessSummary(state="dirty", modified=3, untracked=1).render_line()
        assert "dirty — 3 modified, 1 untracked" in line
        assert "'roar run' will be refused" in line

    def test_not_a_repo(self) -> None:
        # `roar run` works outside a repo now (commit-less lineage); the
        # line must not threaten a refusal, only note the missing commit tag.
        line = GitReadinessSummary(state="not_a_repo").render_line()
        assert line == "not a git repo  ('roar run' works; not commit-tagged)"
        assert "will be refused" not in line

    def test_home_dir(self) -> None:
        line = GitReadinessSummary(state="home_dir").render_line()
        assert line == "running from $HOME  ('roar run' will be refused)"


# ---------------------------------------------------------------------------
# collect_git_readiness over real git trees
# ---------------------------------------------------------------------------


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@t"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "t"],
        check=True,
        capture_output=True,
    )


def _commit_initial(repo: Path) -> None:
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "init"],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


class TestCollect:
    def test_clean_repo(self, tmp_path: Path, fake_home: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)
        _commit_initial(repo)
        summary = collect_git_readiness(repo)
        assert summary.state == "clean"
        assert summary.run_ready is True
        assert summary.short_commit
        # Branch may be 'master' or 'main' depending on git config — accept either.
        assert summary.branch in {"main", "master"}

    def test_dirty_with_categories(self, tmp_path: Path, fake_home: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)
        _commit_initial(repo)
        # Make: 1 modified, 2 untracked, 1 deleted, 1 added.
        (repo / "seed.txt").write_text("seed-changed\n")
        (repo / "new1.py").write_text("x")
        (repo / "new2.py").write_text("y")
        subprocess.run(
            ["git", "-C", str(repo), "rm", "-q", "seed.txt"],
            cwd=repo,
            check=False,
            capture_output=True,
        )
        # That `git rm` deletes both the file and stages the deletion;
        # rewrite it to reproduce the modified+deleted shape we want.
        (repo / "seed.txt").write_text("seed-changed\n")
        added_file = repo / "added.py"
        added_file.write_text("a")
        subprocess.run(
            ["git", "-C", str(repo), "add", str(added_file)],
            check=True,
            capture_output=True,
        )

        summary = collect_git_readiness(repo)
        assert summary.state == "dirty"
        assert summary.run_ready is False
        assert summary.untracked >= 2
        assert summary.added >= 1
        # `roar run' will be refused` stable in the rendered line.
        line = summary.render_line()
        assert line.startswith("dirty —")
        assert "'roar run' will be refused" in line

    def test_not_a_repo(self, tmp_path: Path, fake_home: Path) -> None:
        non_repo = tmp_path / "plain"
        non_repo.mkdir()
        summary = collect_git_readiness(non_repo)
        assert summary.state == "not_a_repo"
        assert summary.run_ready is False

    def test_home_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Make the temp home itself a git repo and probe that.
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        _git_init(home)
        _commit_initial(home)
        summary = collect_git_readiness(home)
        assert summary.state == "home_dir"
        assert summary.run_ready is False


# ---------------------------------------------------------------------------
# render_status integration
# ---------------------------------------------------------------------------


class TestStatusRendersGitLine:
    def test_status_includes_git_line_and_session_line(
        self, tmp_path: Path, fake_home: Path
    ) -> None:
        # Build a StatusSummary directly (avoids the DB integration). We
        # only care that render_status emits the Git: line and the new
        # single-line Session: section.
        from unittest.mock import patch

        from roar.application.query.git_readiness import GitReadinessSummary
        from roar.application.query.requests import StatusQueryRequest
        from roar.application.query.results import StatusSummary
        from roar.application.query.status import render_status

        summary = StatusSummary(
            dag_hash="0" * 64,
            build_steps=2,
            run_steps=3,
            git=GitReadinessSummary(state="dirty", modified=3, untracked=1),
        )
        with patch(
            "roar.application.query.status.build_status_summary",
            return_value=summary,
        ):
            out = render_status(StatusQueryRequest(roar_dir=tmp_path))

        assert out.startswith("Git:")
        assert "dirty — 3 modified, 1 untracked" in out
        assert "Session:" in out
        # Git: line precedes Session: line (readiness first).
        assert out.index("Git:") < out.index("Session:")

    def test_status_omits_git_section_when_unset(self, tmp_path: Path) -> None:
        """Older summaries without a `git` field still render."""
        from unittest.mock import patch

        from roar.application.query.requests import StatusQueryRequest
        from roar.application.query.results import StatusSummary
        from roar.application.query.status import render_status

        summary = StatusSummary(
            dag_hash="0" * 64, build_steps=0, run_steps=0
        )  # git defaults to None
        with patch(
            "roar.application.query.status.build_status_summary",
            return_value=summary,
        ):
            out = render_status(StatusQueryRequest(roar_dir=tmp_path))
        assert not out.startswith("Git:")
        assert out.startswith("Session:")
