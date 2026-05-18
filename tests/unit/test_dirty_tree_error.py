"""Tests for the bucketed dirty-tree error message formatter.

Covers the five message shapes:

  * code-only — all paths are tracked-modified.
  * roar_outputs-only — all paths are untracked + match an artifact in
    the local DB (via the injected ``artifact_lookup``).
  * unknown-only — all paths are untracked + don't match any artifact.
  * mixed — any combination of the above three.
  * special cases — all-``.roar/`` and ``$HOME`` (preserved from the
    pre-bucketing code).

Plus the porcelain parser (now living in ``dirty_tree_classify``) and
the format helpers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from roar.application.run.dirty_tree_classify import (
    _is_tracked_modification,
    _parse_porcelain,
    classify_dirty_paths,
)
from roar.application.run.dirty_tree_error import (
    DOCS_URL,
    _all_under_roar_dir,
    _format_git_add,
    _format_user_command,
    _is_home_dir,
    format_dirty_tree_error,
)

# ---------------------------------------------------------------------------
# parser (now lives in dirty_tree_classify, returns (code, path) pairs)
# ---------------------------------------------------------------------------


class TestParsePorcelain:
    def test_modified_and_untracked(self) -> None:
        out = " M aggregate.py\n?? generate.py\nA  clean.sh\n"
        assert _parse_porcelain(out) == [
            (" M", "aggregate.py"),
            ("??", "generate.py"),
            ("A ", "clean.sh"),
        ]

    def test_rename_takes_new_path(self) -> None:
        pairs = _parse_porcelain("R  old.py -> new.py\n")
        assert [p[1] for p in pairs] == ["new.py"]

    def test_quoted_path_is_unquoted(self) -> None:
        pairs = _parse_porcelain('?? "name with space.py"\n')
        assert [p[1] for p in pairs] == ["name with space.py"]

    def test_blank_lines_ignored(self) -> None:
        assert _parse_porcelain("\n\n") == []

    def test_empty_input_returns_empty(self) -> None:
        assert _parse_porcelain("") == []


class TestTrackedModification:
    def test_untracked(self) -> None:
        assert not _is_tracked_modification("??")

    def test_modified(self) -> None:
        assert _is_tracked_modification(" M")

    def test_added(self) -> None:
        assert _is_tracked_modification("A ")

    def test_rename(self) -> None:
        assert _is_tracked_modification("R ")


# ---------------------------------------------------------------------------
# classifier
# ---------------------------------------------------------------------------


class _FakeLookup:
    """Stub artifact_lookup. Paths in ``known`` are treated as known."""

    def __init__(self, known: list[str]) -> None:
        self._known = set(known)

    def get_by_path(self, abs_path: str) -> Any:
        return {"id": "stub"} if abs_path in self._known else None


def test_classify_buckets_code_outputs_and_unknown(tmp_path: Path) -> None:
    status = " M train.py\n?? model.pkl\n?? log.txt\n"
    known_model = str((tmp_path / "model.pkl").resolve())
    classification = classify_dirty_paths(status, tmp_path, _FakeLookup([known_model]))

    assert classification.code == ["train.py"]
    assert classification.roar_outputs == ["model.pkl"]
    assert classification.unknown == ["log.txt"]


def test_classify_without_lookup_routes_all_untracked_to_unknown(tmp_path: Path) -> None:
    status = " M train.py\n?? model.pkl\n"
    classification = classify_dirty_paths(status, tmp_path, None)

    assert classification.code == ["train.py"]
    assert classification.roar_outputs == []
    assert classification.unknown == ["model.pkl"]


def test_classify_lookup_failures_treat_as_unknown(tmp_path: Path) -> None:
    class _ExplodingLookup:
        def get_by_path(self, _abs_path: str) -> Any:
            raise RuntimeError("DB unreachable")

    classification = classify_dirty_paths("?? model.pkl\n", tmp_path, _ExplodingLookup())
    assert classification.unknown == ["model.pkl"]
    assert classification.roar_outputs == []


# ---------------------------------------------------------------------------
# format helpers (unchanged behavior)
# ---------------------------------------------------------------------------


class TestSpecialCaseDetection:
    def test_all_under_roar_dir_true(self) -> None:
        assert _all_under_roar_dir([".roar/state.json", ".roar/db.sqlite"])

    def test_all_under_roar_dir_root_only(self) -> None:
        assert _all_under_roar_dir([".roar"])

    def test_all_under_roar_dir_mixed_returns_false(self) -> None:
        assert not _all_under_roar_dir([".roar/state.json", "script.py"])

    def test_empty_paths_not_special(self) -> None:
        assert not _all_under_roar_dir([])

    def test_lookalike_does_not_match(self) -> None:
        assert not _all_under_roar_dir([".roar.bak"])

    def test_is_home_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _is_home_dir(tmp_path)
        assert not _is_home_dir(tmp_path / "project")


class TestFormatHelpers:
    def test_format_git_add_named_when_few(self) -> None:
        assert _format_git_add(["a.py", "b.py", "c.py"]) == "git add a.py b.py c.py"

    def test_format_git_add_uses_dash_A_when_many(self) -> None:
        assert _format_git_add([f"f{i}.py" for i in range(10)]) == "git add -A"

    def test_format_git_add_quotes_paths_with_spaces(self) -> None:
        assert _format_git_add(["a b.py"]) == "git add 'a b.py'"

    def test_format_user_command_run(self) -> None:
        assert _format_user_command("run", ["python", "script.py"]) == "roar run python script.py"

    def test_format_user_command_build(self) -> None:
        assert _format_user_command("build", ["./build.sh"]) == "roar build ./build.sh"

    def test_format_user_command_handles_empty(self) -> None:
        out = _format_user_command("run", None)
        assert "roar run" in out


# ---------------------------------------------------------------------------
# end-to-end message shape — one test class per variant
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "project"
    project.mkdir()
    return project


class TestCodeOnlyMessage:
    def test_lead_with_principle(self, project_root: Path) -> None:
        out = format_dirty_tree_error(
            status_output=" M script.py\n",
            repo_root=project_root,
            verb="run",
            args=["python", "script.py"],
        )
        assert out.startswith("Run blocked: working tree is dirty.")
        assert "git commit SHA" in out
        assert "lineage" in out

    def test_recovery_block_uses_actual_files_and_command(self, project_root: Path) -> None:
        out = format_dirty_tree_error(
            status_output=" M aggregate.py\n M generate.py\n",
            repo_root=project_root,
            verb="run",
            args=["python3", "generate.py"],
        )
        assert "git add aggregate.py generate.py" in out
        assert 'git commit -m "<describe your changes>"' in out
        assert "roar run python3 generate.py" in out

    def test_docs_url_present(self, project_root: Path) -> None:
        out = format_dirty_tree_error(
            status_output=" M a.py\n",
            repo_root=project_root,
            verb="run",
            args=["python", "a.py"],
        )
        assert DOCS_URL in out

    def test_many_code_files_capped_list_and_dash_A(self, project_root: Path) -> None:
        many = "\n".join(f" M f{i}.py" for i in range(15))
        out = format_dirty_tree_error(
            status_output=many,
            repo_root=project_root,
            verb="run",
            args=["python", "x.py"],
        )
        assert "Dirty files:" in out
        assert "... and 7 more" in out
        assert "git add -A" in out


class TestRoarOutputsOnlyMessage:
    def test_recommends_gitignore_for_known_outputs(self, project_root: Path) -> None:
        known = str((project_root / "model.pkl").resolve())
        out = format_dirty_tree_error(
            status_output="?? model.pkl\n",
            repo_root=project_root,
            verb="run",
            args=["python", "train.py"],
            artifact_lookup=_FakeLookup([known]),
        )
        assert "untracked outputs from earlier roar run" in out
        assert "echo 'model.pkl' >> .gitignore" in out
        assert "git add .gitignore" in out
        assert "roar run python train.py" in out
        # "or commit them" remains a fallback path
        assert "if they belong in the repo" in out

    def test_pattern_suggestion_at_threshold(self, project_root: Path) -> None:
        """Three same-extension paths trigger a `*.<ext>` suggestion."""
        known = [str((project_root / f"model_{i}.pkl").resolve()) for i in range(3)]
        out = format_dirty_tree_error(
            status_output="?? model_0.pkl\n?? model_1.pkl\n?? model_2.pkl\n",
            repo_root=project_root,
            verb="run",
            args=["python", "train.py"],
            artifact_lookup=_FakeLookup(known),
        )
        assert "echo '*.pkl' >> .gitignore" in out
        assert "(covers all 3)" in out


class TestUnknownOnlyMessage:
    def test_offers_both_gitignore_and_commit(self, project_root: Path) -> None:
        out = format_dirty_tree_error(
            status_output="?? log.txt\n",
            repo_root=project_root,
            verb="run",
            args=["python", "x.py"],
        )
        assert "untracked files in the working tree" in out
        assert "gitignore them" in out
        assert "If they belong in the repo, commit them" in out
        assert "git add log.txt" in out
        assert "roar run python x.py" in out


class TestMixedMessage:
    def test_segmented_blocks(self, project_root: Path) -> None:
        known = str((project_root / "model.pkl").resolve())
        out = format_dirty_tree_error(
            status_output=" M train.py\n?? model.pkl\n?? log.txt\n",
            repo_root=project_root,
            verb="run",
            args=["python3", "train.py"],
            artifact_lookup=_FakeLookup([known]),
        )
        # All three sections present.
        assert "Code changes:" in out
        assert "Roar outputs (untracked):" in out
        assert "Other untracked files:" in out
        # Code goes through `git add` + commit.
        assert "git add train.py" in out
        # Roar outputs go through gitignore suggestion.
        assert "echo 'model.pkl' >> .gitignore" in out
        # Trailing retry uses the user's actual command.
        assert "roar run python3 train.py" in out


class TestRoarOnlyMessage:
    def test_roar_only_special_case(self, project_root: Path) -> None:
        out = format_dirty_tree_error(
            status_output="?? .roar/state.json\n?? .roar/roar.db\n",
            repo_root=project_root,
            verb="run",
            args=["python", "script.py"],
        )
        assert "roar's own state directory" in out
        assert "echo '.roar/' >> .gitignore" in out
        assert "roar init" in out


class TestHomeDirMessage:
    def test_home_dir_special_case(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        out = format_dirty_tree_error(
            status_output=" M something.txt\n",
            repo_root=home,
            verb="run",
            args=["python", "script.py"],
        )
        assert "running roar from your home directory" in out
        assert "Switch to your project directory" in out

    def test_home_dir_takes_priority_over_roar_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        out = format_dirty_tree_error(
            status_output="?? .roar/state.json\n",
            repo_root=home,
            verb="run",
            args=["python", "x.py"],
        )
        assert "running roar from your home directory" in out


class TestVerbHandling:
    def test_build_verb_renders_in_recovery_command(self, project_root: Path) -> None:
        out = format_dirty_tree_error(
            status_output=" M Makefile\n",
            repo_root=project_root,
            verb="build",
            args=["./build.sh"],
        )
        assert "roar build ./build.sh" in out


# ---------------------------------------------------------------------------
# regression: validate_git_clean's DB-context plumbing must propagate the
# ValueError raised on a dirty tree, not turn it into a generator error.
# ---------------------------------------------------------------------------


class TestValidateGitCleanPropagation:
    """The dirty-tree refusal raises ``ValueError``. The DB-context helper
    that wraps the call must not swallow it (an earlier version turned
    ``ValueError`` from inside a ``@contextmanager`` ``with`` block into
    ``RuntimeError: generator didn't stop after throw()``)."""

    def test_dirty_tree_raises_valueerror_with_roar_dir_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess

        from roar.application.run.execution import validate_git_clean

        # Make the home-dir branch not fire.
        home = tmp_path / "fake-home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))

        repo = tmp_path / "project"
        repo.mkdir()
        roar_dir = repo / ".roar"
        roar_dir.mkdir()
        monkeypatch.chdir(repo)

        # Stub git: rev-parse returns the repo path; status returns one
        # untracked file. validate_git_clean must surface the bucketed
        # message as a plain ValueError, not a contextmanager RuntimeError.
        original = subprocess.check_output

        def fake_check_output(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
            if cmd[:2] == ["git", "rev-parse"]:
                return str(repo) + "\n"
            if cmd[:2] == ["git", "status"]:
                return "?? stray.txt\n"
            return original(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "check_output", fake_check_output)

        with pytest.raises(ValueError, match="Run blocked"):
            validate_git_clean(verb="run", args=["echo"], roar_dir=roar_dir)

    def test_worktree_modified_path_is_not_truncated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Porcelain emits ` M path` (X column is a space) for worktree-only
        modifications. An earlier version `.strip()`-ed the subprocess
        output, eating the leading space and chopping the first character
        off the path (`train.py` → `rain.py`)."""
        import subprocess

        from roar.application.run.execution import validate_git_clean

        home = tmp_path / "fake-home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))

        repo = tmp_path / "project"
        repo.mkdir()
        monkeypatch.chdir(repo)

        original = subprocess.check_output

        def fake_check_output(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
            if cmd[:2] == ["git", "rev-parse"]:
                return str(repo) + "\n"
            if cmd[:2] == ["git", "status"]:
                return " M train.py\n"  # X=space (worktree-modified-only)
            return original(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "check_output", fake_check_output)

        with pytest.raises(ValueError) as exc_info:
            validate_git_clean(verb="run", args=["echo"])

        message = str(exc_info.value)
        # Check the action line specifically. The earlier bug rendered
        # `git add rain.py` here instead of `git add train.py`.
        assert "git add train.py" in message
        assert "git add rain.py" not in message
