"""Tests for ``roar init agents``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

# The require guard is imported transitively by `roar.cli`; bypass it in tests.
os.environ["ROAR_GUARD"] = "0"

from roar.cli import cli
from roar.cli.commands.init_agents import (
    AGENTS_BEGIN_MARKER,
    AGENTS_END_MARKER,
    render_agents_block,
    render_skill,
    update_agents_file,
    update_skill_file,
)

# ---------------------------------------------------------------------------
# Pure-function tests (no filesystem)
# ---------------------------------------------------------------------------


class TestRenderSkill:
    def test_includes_version_marker(self):
        out = render_skill("9.9.9")
        assert "<!-- roar version: 9.9.9 -->" in out


class TestUpdateSkillFile:
    def test_create_when_missing(self):
        result = update_skill_file(None, version="1.0.0")
        assert result.action == "create"
        assert "<!-- roar version: 1.0.0 -->" in result.new_content

    def test_noop_when_identical(self):
        rendered = render_skill("1.0.0")
        result = update_skill_file(rendered, version="1.0.0")
        assert result.action == "noop"

    def test_modified_when_no_marker_and_no_force(self):
        result = update_skill_file("hand-written content", version="1.0.0")
        assert result.action == "modified"
        assert result.new_content == "hand-written content"

    def test_force_overwrites_modified(self):
        result = update_skill_file("hand-written content", version="1.0.0", force=True)
        assert result.action == "update"
        assert "<!-- roar version: 1.0.0 -->" in result.new_content

    def test_update_when_managed_but_old_version(self):
        old = render_skill("0.9.0")
        result = update_skill_file(old, version="1.0.0")
        assert result.action == "update"
        assert "<!-- roar version: 1.0.0 -->" in result.new_content


class TestUpdateAgentsFile:
    def test_create_when_missing(self):
        result = update_agents_file(None)
        assert result.action == "create"
        assert AGENTS_BEGIN_MARKER in result.new_content
        assert AGENTS_END_MARKER in result.new_content
        assert result.new_content.startswith("# AGENTS.md\n")

    def test_append_when_no_block(self):
        existing = "# AGENTS.md\n\nExisting project guidance.\n"
        result = update_agents_file(existing)
        assert result.action == "append"
        assert result.new_content.startswith(existing)
        assert AGENTS_BEGIN_MARKER in result.new_content

    def test_noop_when_block_matches(self):
        existing = f"# AGENTS.md\n\nFoo.\n\n{render_agents_block()}"
        result = update_agents_file(existing)
        assert result.action == "noop"
        assert result.new_content == existing

    def test_update_replaces_only_the_block(self):
        old_block = render_agents_block("0.0.0")
        existing = f"# AGENTS.md\n\nProject conventions.\n\n{old_block}\nMore stuff after.\n"
        result = update_agents_file(existing)
        assert result.action == "update"
        # Surrounding content is preserved.
        assert "Project conventions." in result.new_content
        assert "More stuff after." in result.new_content
        # Block contents are refreshed.
        assert "<!-- roar version: 0.0.0 -->" not in result.new_content

    def test_idempotent_re_runs(self):
        """Calling update twice in a row should not accumulate blank lines."""
        first = update_agents_file(None).new_content
        second = update_agents_file(first).new_content
        third = update_agents_file(second).new_content
        assert second == third

    def test_handles_existing_without_trailing_newline(self):
        existing = "# AGENTS.md\n\nNo trailing newline"
        result = update_agents_file(existing)
        assert result.action == "append"
        # Should have a clean separator between existing content and the block.
        assert "newline\n\n<!-- roar:begin" in result.new_content


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME at a temp dir so we don't touch the real ~/.claude."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    p = tmp_path / "project"
    p.mkdir()
    return p


class TestCli:
    def test_default_installs_both(self, isolated_home: Path, project_dir: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "agents", "--path", str(project_dir)])
        assert result.exit_code == 0, result.output
        skill = isolated_home / ".claude" / "skills" / "roar" / "SKILL.md"
        agents = project_dir / "AGENTS.md"
        assert skill.exists()
        assert agents.exists()
        assert AGENTS_BEGIN_MARKER in agents.read_text()
        assert "<!-- roar version:" in skill.read_text()

    def test_skill_only(self, isolated_home: Path, project_dir: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "agents", "--skill", "--path", str(project_dir)])
        assert result.exit_code == 0, result.output
        assert (isolated_home / ".claude" / "skills" / "roar" / "SKILL.md").exists()
        assert not (project_dir / "AGENTS.md").exists()

    def test_project_only(self, isolated_home: Path, project_dir: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "agents", "--project", "--path", str(project_dir)])
        assert result.exit_code == 0, result.output
        assert not (isolated_home / ".claude" / "skills" / "roar" / "SKILL.md").exists()
        assert (project_dir / "AGENTS.md").exists()

    def test_dry_run_writes_nothing(self, isolated_home: Path, project_dir: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "agents", "--dry-run", "--path", str(project_dir)])
        assert result.exit_code == 0, result.output
        assert "would" in result.output.lower()
        assert not (isolated_home / ".claude" / "skills" / "roar" / "SKILL.md").exists()
        assert not (project_dir / "AGENTS.md").exists()

    def test_check_fails_when_missing(self, isolated_home: Path, project_dir: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "agents", "--check", "--path", str(project_dir)])
        assert result.exit_code == 1
        assert "out of date" in result.output

    def test_check_passes_after_install(self, isolated_home: Path, project_dir: Path):
        runner = CliRunner()
        runner.invoke(cli, ["init", "agents", "--path", str(project_dir)])
        result = runner.invoke(cli, ["init", "agents", "--check", "--path", str(project_dir)])
        assert result.exit_code == 0, result.output
        assert "up to date" in result.output

    def test_idempotent_second_run_is_noop(self, isolated_home: Path, project_dir: Path):
        runner = CliRunner()
        runner.invoke(cli, ["init", "agents", "--path", str(project_dir)])
        before = (project_dir / "AGENTS.md").read_text()
        result = runner.invoke(cli, ["init", "agents", "--path", str(project_dir)])
        assert result.exit_code == 0, result.output
        after = (project_dir / "AGENTS.md").read_text()
        assert before == after

    def test_preserves_user_content_in_agents_md(self, isolated_home: Path, project_dir: Path):
        agents_file = project_dir / "AGENTS.md"
        agents_file.write_text("# AGENTS.md\n\nMy custom rules.\n")
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "agents", "--project", "--path", str(project_dir)])
        assert result.exit_code == 0, result.output
        content = agents_file.read_text()
        assert "My custom rules." in content
        assert AGENTS_BEGIN_MARKER in content

    def test_refuses_to_overwrite_modified_skill(self, isolated_home: Path, project_dir: Path):
        skill_path = isolated_home / ".claude" / "skills" / "roar" / "SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text("hand-edited skill")
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "agents", "--skill", "--path", str(project_dir)])
        assert result.exit_code == 0
        assert "hand-edits" in result.output
        # Untouched.
        assert skill_path.read_text() == "hand-edited skill"

    def test_force_overwrites_modified_skill(self, isolated_home: Path, project_dir: Path):
        skill_path = isolated_home / ".claude" / "skills" / "roar" / "SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text("hand-edited skill")
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "agents", "--skill", "--force", "--path", str(project_dir)],
        )
        assert result.exit_code == 0, result.output
        assert "<!-- roar version:" in skill_path.read_text()


class TestBackwardCompat:
    """Ensure the existing `roar init` (no subcommand) still works."""

    def test_init_no_subcommand(self, project_dir: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--no", "--path", str(project_dir)],
        )
        assert result.exit_code == 0, result.output
        assert (project_dir / ".roar").is_dir()
        assert (project_dir / ".roar" / "config.toml").exists()
