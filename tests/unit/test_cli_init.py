from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from roar.cli import cli


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)


def _run_init(repo: Path, *args: str) -> object:
    runner = CliRunner()
    original_cwd = Path.cwd()
    try:
        os.chdir(repo)
        return runner.invoke(cli, ["init", *args])
    finally:
        os.chdir(original_cwd)


def test_init_creates_gitignore_when_missing(tmp_path: Path) -> None:
    """P0-8: with no .gitignore, init must create one. Otherwise the .roar/
    dir it just made dirties the worktree and the next `roar run` refuses."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    gitignore = repo / ".gitignore"
    assert not gitignore.exists()

    result = _run_init(repo)

    assert result.exit_code == 0, result.output
    assert gitignore.exists()
    assert ".roar/" in gitignore.read_text().splitlines()
    config_text = (repo / ".roar" / "config.toml").read_text(encoding="utf-8")
    assert "[scope]" in config_text
    assert 'mode = "anonymous"' in config_text
    assert "scope:      anonymous (public; no account)" in result.output
    assert "created .gitignore with .roar/ entry" in result.output


def test_init_appends_to_existing_gitignore_without_roar(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    gitignore = repo / ".gitignore"
    gitignore.write_text("__pycache__/\n*.pyc\n")

    result = _run_init(repo)

    assert result.exit_code == 0, result.output
    lines = gitignore.read_text().splitlines()
    assert "__pycache__/" in lines
    assert "*.pyc" in lines
    assert ".roar/" in lines
    assert "added .roar/ entry" in result.output


def test_init_idempotent_when_gitignore_already_has_roar_slash(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    gitignore = repo / ".gitignore"
    gitignore.write_text("foo\n.roar/\nbar\n")

    result = _run_init(repo)

    assert result.exit_code == 0, result.output
    assert gitignore.read_text() == "foo\n.roar/\nbar\n"
    assert "already excluded" in result.output


def test_init_idempotent_when_gitignore_already_has_roar_unslashed(tmp_path: Path) -> None:
    """`.roar` (no slash) is also a valid gitignore entry covering the dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    gitignore = repo / ".gitignore"
    gitignore.write_text(".roar\n")

    result = _run_init(repo)

    assert result.exit_code == 0, result.output
    assert gitignore.read_text() == ".roar\n"
    assert "already excluded" in result.output


def test_init_no_gitignore_flag_skips(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    result = _run_init(repo, "--no-gitignore")

    assert result.exit_code == 0, result.output
    assert not (repo / ".gitignore").exists()
    assert "skipped (--no-gitignore)" in result.output


def test_init_short_no_flag_still_skips(tmp_path: Path) -> None:
    """Back-compat: -n / --no remain aliases for --no-gitignore."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    result = _run_init(repo, "-n")

    assert result.exit_code == 0, result.output
    assert not (repo / ".gitignore").exists()


def test_init_substring_match_does_not_count_as_already_present(tmp_path: Path) -> None:
    """A .gitignore entry like `not.roar` shares a substring with `.roar`
    but is a different path. The check must be per-line, not substring."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    gitignore = repo / ".gitignore"
    gitignore.write_text("not.roar\n")

    result = _run_init(repo)

    assert result.exit_code == 0, result.output
    lines = gitignore.read_text().splitlines()
    assert "not.roar" in lines
    assert ".roar/" in lines
    assert "added .roar/ entry" in result.output


def test_init_appends_when_gitignore_missing_trailing_newline(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    gitignore = repo / ".gitignore"
    gitignore.write_text("foo")  # no trailing newline

    result = _run_init(repo)

    assert result.exit_code == 0, result.output
    assert gitignore.read_text() == "foo\n.roar/\n"


def test_init_outside_git_repo_does_not_touch_gitignore(tmp_path: Path) -> None:
    """If we're not inside a git repo, there's no shared root to write to."""
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()

    result = _run_init(plain_dir)

    assert result.exit_code == 0, result.output
    assert not (plain_dir / ".gitignore").exists()
    assert "(not in a git repo)" in result.output


def test_init_hints_visible_when_gate_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the hint gate forced on (simulating TTY), `hint:` lines appear."""
    from roar.cli import _format

    monkeypatch.setattr(_format, "hints_should_print", lambda: True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    result = _run_init(repo)

    assert result.exit_code == 0, result.output
    assert "hint: Get started:" in result.output
    assert "roar run python train.py" in result.output
    assert "roar dag" in result.output
    assert "roar register" in result.output
    assert "https://glaas.ai/docs" in result.output
    assert "roar config set hints.enabled false" in result.output


def test_init_hints_appear_in_non_tty_on_stderr(tmp_path: Path) -> None:
    """Hints fire in non-TTY contexts (agents, CI) so consumers see the
    nudges. They go to stderr — stdout stays clean for pipelines."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    result = _run_init(repo)

    assert result.exit_code == 0, result.output
    assert "Initialized roar in" in result.stdout
    assert "hint:" in result.stderr
    # Stdout stays free of hint chrome so consumers can pipe/redirect cleanly.
    assert "hint:" not in result.stdout


def test_init_hints_suppressed_when_hints_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with the TTY gate forced on, hints.enabled=false silences."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / ".roar").mkdir()
    (repo / ".roar" / "config.toml").write_text("[hints]\nenabled = false\n")

    result = _run_init(repo)

    assert result.exit_code == 0, result.output
    assert "hint:" not in result.output


def test_init_brand_header_visible_when_gate_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the brand/hint gate forced on (TTY), the first line is the
    subcommand-form brand banner: `🦖 roar init` (or `roar: roar init`
    when emoji is unavailable). The version is no longer in the banner."""
    from roar.cli import _format

    monkeypatch.setattr(_format, "hints_should_print", lambda: True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    result = _run_init(repo)

    assert result.exit_code == 0, result.output
    first = result.output.splitlines()[0]
    assert "roar init" in first
    assert first.startswith(("roar:", "🦖"))
    # Version moved to `roar --version`; brand banner no longer carries it.
    assert "roar v" not in first


def test_init_brand_header_appears_in_non_tty_on_stderr(tmp_path: Path) -> None:
    """Brand header fires in non-TTY contexts (agents, CI) on stderr,
    matching the new hints-on-stderr convention. Stdout still leads
    with the actual init summary so pipelines parse cleanly."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    result = _run_init(repo)

    assert result.exit_code == 0, result.output
    # Brand header on stderr (emoji plain-fallback because CliRunner is non-TTY).
    assert "roar init" in result.stderr
    # Stdout leads with the body, not the banner.
    assert result.stdout.startswith("Initialized roar in")


def test_init_path_uses_target_repo_for_gitignore_updates(tmp_path: Path) -> None:
    caller_repo = tmp_path / "caller-repo"
    target_repo = tmp_path / "target-repo"
    caller_repo.mkdir()
    target_repo.mkdir()

    _init_git_repo(caller_repo)
    _init_git_repo(target_repo)

    caller_gitignore = caller_repo / ".gitignore"
    target_gitignore = target_repo / ".gitignore"
    caller_gitignore.write_text(".roar/\n")
    target_gitignore.write_text("")

    runner = CliRunner()
    original_cwd = Path.cwd()
    try:
        os.chdir(caller_repo)
        result = runner.invoke(cli, ["init", "--path", str(target_repo), "-y"])
    finally:
        os.chdir(original_cwd)

    assert result.exit_code == 0, result.output
    assert "added .roar/ entry" in result.output
    assert "already excluded" not in result.output
    assert caller_gitignore.read_text() == ".roar/\n"
    assert ".roar/" in target_gitignore.read_text().splitlines()
    assert (target_repo / ".roar").is_dir()
