"""Tests for the dirty-tree error message formatter (P0-9).

Covers all four shapes the message can take and the parser that backs
them:

  - Default message (a few dirty files).
  - Default message with cap (many dirty files → list capped + `git add -A`).
  - All-`.roar/` special case (tells user to gitignore it).
  - `$HOME` special case (tells user to leave home).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from roar.application.run.dirty_tree_error import (
    DOCS_URL,
    _all_under_roar_dir,
    _format_git_add,
    _format_user_command,
    _is_home_dir,
    _parse_porcelain,
    format_dirty_tree_error,
)

# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


class TestParsePorcelain:
    def test_modified_and_untracked(self) -> None:
        out = " M aggregate.py\n?? generate.py\nA  clean.sh\n"
        assert _parse_porcelain(out) == ["aggregate.py", "generate.py", "clean.sh"]

    def test_rename_takes_new_path(self) -> None:
        out = "R  old.py -> new.py\n"
        assert _parse_porcelain(out) == ["new.py"]

    def test_quoted_path_is_unquoted(self) -> None:
        out = '?? "name with space.py"\n'
        assert _parse_porcelain(out) == ["name with space.py"]

    def test_blank_lines_ignored(self) -> None:
        assert _parse_porcelain("\n\n") == []

    def test_empty_input_returns_empty(self) -> None:
        assert _parse_porcelain("") == []


# ---------------------------------------------------------------------------
# helpers
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
        # `.roar.bak` is not roar's state dir.
        assert not _all_under_roar_dir([".roar.bak"])

    def test_is_home_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _is_home_dir(tmp_path)
        assert not _is_home_dir(tmp_path / "project")


class TestFormatHelpers:
    def test_format_git_add_named_when_few(self) -> None:
        cmd = _format_git_add(["a.py", "b.py", "c.py"])
        assert cmd == "git add a.py b.py c.py"

    def test_format_git_add_uses_dash_A_when_many(self) -> None:
        cmd = _format_git_add([f"f{i}.py" for i in range(10)])
        assert cmd == "git add -A"

    def test_format_git_add_quotes_paths_with_spaces(self) -> None:
        cmd = _format_git_add(["a b.py"])
        assert cmd == "git add 'a b.py'"

    def test_format_user_command_run(self) -> None:
        out = _format_user_command("run", ["python", "script.py"])
        assert out == "roar run python script.py"

    def test_format_user_command_build(self) -> None:
        out = _format_user_command("build", ["./build.sh"])
        assert out == "roar build ./build.sh"

    def test_format_user_command_quotes_args_with_spaces(self) -> None:
        out = _format_user_command("run", ["python", "script with space.py"])
        assert "'script with space.py'" in out

    def test_format_user_command_handles_empty(self) -> None:
        # Defensive — should produce something usable, not crash.
        out = _format_user_command("run", None)
        assert "roar run" in out


# ---------------------------------------------------------------------------
# end-to-end message shape
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A path that is *not* the home directory, so the home-dir branch
    doesn't fire by accident in tests."""
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "project"
    project.mkdir()
    return project


class TestDefaultMessage:
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
            status_output=" M aggregate.py\n?? clean.sh\n M generate.py\n",
            repo_root=project_root,
            verb="run",
            args=["python3", "generate.py"],
        )
        assert "git add aggregate.py clean.sh generate.py" in out
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

    def test_few_files_skip_separate_list(self, project_root: Path) -> None:
        """When ≤5 files are dirty, the `git add` line names them — no
        separate 'Dirty files:' block needed."""
        out = format_dirty_tree_error(
            status_output=" M a.py\n M b.py\n",
            repo_root=project_root,
            verb="run",
            args=["python", "a.py"],
        )
        assert "Dirty files:" not in out

    def test_many_files_capped_list_and_dash_A(self, project_root: Path) -> None:
        many = "\n".join(f"?? f{i}.py" for i in range(15))
        out = format_dirty_tree_error(
            status_output=many,
            repo_root=project_root,
            verb="run",
            args=["python", "x.py"],
        )
        assert "Dirty files:" in out
        # Cap is 8 visible + "... and N more"
        assert "... and 7 more" in out
        # Recovery uses `git add -A` not enumerated paths
        assert "git add -A" in out


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
        assert "roar run python script.py" in out
        assert DOCS_URL in out


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
        assert "roar run python script.py" in out
        assert DOCS_URL in out

    def test_home_dir_takes_priority_over_roar_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If $HOME is the repo root AND the only dirty file is .roar/,
        the home-dir message wins because the user's bigger problem is
        running from $HOME."""
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
    def test_build_verb_renders_in_recovery_command(self, tmp_path: Path) -> None:
        out = format_dirty_tree_error(
            status_output=" M Makefile\n",
            repo_root=tmp_path / "project",
            verb="build",
            args=["./build.sh"],
        )
        assert "roar build ./build.sh" in out
