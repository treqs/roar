"""Implementation of ``roar init agents``.

Sets up agent-facing guidance for the current project and machine:

* writes ``~/.claude/skills/roar/SKILL.md`` (Claude-specific skill)
* appends a roar section to ``./AGENTS.md`` (cross-agent guidance), bracketed
  by managed markers so the section can be re-rendered idempotently without
  disturbing surrounding content.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import click

from roar.cli import __version__

# ---- template loading ----

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates" / "agents"
_SKILL_TEMPLATE_PATH = _TEMPLATE_DIR / "SKILL.md.tmpl"
_AGENTS_SECTION_TEMPLATE_PATH = _TEMPLATE_DIR / "agents_section.md.tmpl"

# Markers used to fence the managed AGENTS.md region.
AGENTS_BEGIN_MARKER = (
    "<!-- roar:begin (managed by `roar init agents` — edits inside this block "
    "will be overwritten) -->"
)
AGENTS_END_MARKER = "<!-- roar:end -->"


def render_skill(version: str = __version__) -> str:
    """Render the Claude skill markdown with the given version stamped in."""
    return _SKILL_TEMPLATE_PATH.read_text().format(version=version)


def render_agents_section() -> str:
    """Return the AGENTS.md section body (without surrounding markers)."""
    return _AGENTS_SECTION_TEMPLATE_PATH.read_text()


def render_agents_block(version: str = __version__) -> str:
    """Return the full marker-fenced AGENTS.md block."""
    body = render_agents_section()
    return (
        f"{AGENTS_BEGIN_MARKER}\n"
        f"<!-- roar version: {version} -->\n"
        f"{body.rstrip()}\n"
        f"{AGENTS_END_MARKER}\n"
    )


# ---- AGENTS.md edit logic ----


@dataclass(frozen=True)
class AgentsUpdate:
    new_content: str
    action: str  # "create", "append", "update", "noop"


def update_agents_file(existing: str | None, version: str = __version__) -> AgentsUpdate:
    """Compute the new AGENTS.md content given the existing content (or None).

    * If the file does not exist, create one with a brief header and the block.
    * If the file exists but has no roar block, append the block.
    * If a block exists and matches the new content, no-op.
    * If a block exists and differs, replace just the block.
    """
    block = render_agents_block(version)

    if existing is None:
        new = (
            "# AGENTS.md\n"
            "\n"
            "Guidance for AI coding agents working in this repo.\n"
            "See https://agents.md for the convention.\n"
            "\n"
            f"{block}"
        )
        return AgentsUpdate(new_content=new, action="create")

    begin_idx = existing.find(AGENTS_BEGIN_MARKER)
    end_idx = existing.find(AGENTS_END_MARKER)
    if begin_idx == -1 or end_idx == -1 or end_idx < begin_idx:
        sep = "" if existing.endswith("\n") else "\n"
        new = f"{existing}{sep}\n{block}"
        return AgentsUpdate(new_content=new, action="append")

    end_after = end_idx + len(AGENTS_END_MARKER)
    # Consume a trailing newline so we don't accumulate blank lines on each run.
    if end_after < len(existing) and existing[end_after] == "\n":
        end_after += 1
    new = existing[:begin_idx] + block + existing[end_after:]
    if new == existing:
        return AgentsUpdate(new_content=existing, action="noop")
    return AgentsUpdate(new_content=new, action="update")


# ---- skill file edit logic ----


def _has_managed_marker(text: str) -> bool:
    return "<!-- roar version:" in text


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SkillUpdate:
    new_content: str
    action: str  # "create", "update", "noop", "modified"


def update_skill_file(
    existing: str | None,
    *,
    version: str = __version__,
    force: bool = False,
) -> SkillUpdate:
    """Compute the new SKILL.md content given the existing content (or None)."""
    new = render_skill(version)
    if existing is None:
        return SkillUpdate(new_content=new, action="create")
    if existing == new:
        return SkillUpdate(new_content=existing, action="noop")
    if not _has_managed_marker(existing) and not force:
        return SkillUpdate(new_content=existing, action="modified")
    return SkillUpdate(new_content=new, action="update")


# ---- click command ----


def _skill_path() -> Path:
    return Path.home() / ".claude" / "skills" / "roar" / "SKILL.md"


def _apply_skill(*, dry_run: bool, force: bool) -> tuple[str, Path]:
    path = _skill_path()
    existing = path.read_text() if path.exists() else None
    update = update_skill_file(existing, force=force)
    if update.action == "modified":
        return ("modified", path)
    if update.action == "noop":
        return ("noop", path)
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(update.new_content)
    return (update.action, path)


def _apply_agents(*, project_dir: Path, dry_run: bool) -> tuple[str, Path]:
    path = project_dir / "AGENTS.md"
    existing = path.read_text() if path.exists() else None
    update = update_agents_file(existing)
    if update.action == "noop":
        return ("noop", path)
    if not dry_run:
        path.write_text(update.new_content)
    return (update.action, path)


def _check_skill() -> bool:
    """Return True iff the installed skill matches the rendered template."""
    path = _skill_path()
    if not path.exists():
        return False
    return _content_hash(path.read_text()) == _content_hash(render_skill())


def _check_agents(project_dir: Path) -> bool:
    """Return True iff AGENTS.md contains an up-to-date managed block."""
    path = project_dir / "AGENTS.md"
    if not path.exists():
        return False
    existing = path.read_text()
    update = update_agents_file(existing)
    return update.action == "noop"


@click.command("agents")
@click.option(
    "--skill/--no-skill",
    "do_skill",
    default=None,
    help="Install/refresh the user-global Claude skill at ~/.claude/skills/roar/SKILL.md.",
)
@click.option(
    "--project/--no-project",
    "do_project",
    default=None,
    help="Install/refresh the roar section in ./AGENTS.md (cross-agent guidance).",
)
@click.option("--dry-run", is_flag=True, help="Print what would change without writing.")
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite hand-edited skill files (AGENTS.md edits inside markers are always replaced).",
)
@click.option(
    "--check",
    is_flag=True,
    help="Exit nonzero if installed agent config is missing or out of date. Does not write.",
)
@click.option(
    "--path",
    "project_dir",
    default=None,
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    help="Project directory for AGENTS.md (default: current dir).",
)
def init_agents(
    do_skill: bool | None,
    do_project: bool | None,
    dry_run: bool,
    force: bool,
    check: bool,
    project_dir: Path | None,
) -> None:
    """Install agent-facing guidance for roar.

    By default installs both the user-global Claude skill and the project-level
    AGENTS.md section. Use --skill or --project to limit to one target.

    \b
    Examples:
        roar init agents                  # install both
        roar init agents --skill          # only the user-global skill
        roar init agents --project        # only ./AGENTS.md
        roar init agents --dry-run        # preview without writing
        roar init agents --check          # exit nonzero if out of date (CI/pre-commit)
    """
    project_dir = project_dir or Path.cwd()

    # Resolve which targets to act on. If neither flag is given, do both.
    # If only one flag is given, do just that one (others default to off).
    if do_skill is None and do_project is None:
        skill_on, project_on = True, True
    else:
        skill_on = bool(do_skill)
        project_on = bool(do_project)

    if check:
        problems: list[str] = []
        if skill_on and not _check_skill():
            problems.append(f"skill out of date or missing: {_skill_path()}")
        if project_on and not _check_agents(project_dir):
            problems.append(f"AGENTS.md out of date or missing: {project_dir / 'AGENTS.md'}")
        if problems:
            for p in problems:
                click.echo(f"out of date: {p}", err=True)
            raise click.exceptions.Exit(1)
        click.echo("agent config up to date.")
        return

    actions: list[str] = []

    if skill_on:
        action, path = _apply_skill(dry_run=dry_run, force=force)
        if action == "create":
            actions.append(f"{'would create' if dry_run else 'created'} {path}")
        elif action == "update":
            actions.append(f"{'would update' if dry_run else 'updated'} {path}")
        elif action == "noop":
            actions.append(f"skill already up to date: {path}")
        elif action == "modified":
            actions.append(
                f"skill has hand-edits, refusing to overwrite: {path} "
                "(re-run with --force to replace)"
            )

    if project_on:
        action, path = _apply_agents(project_dir=project_dir, dry_run=dry_run)
        if action == "create":
            actions.append(f"{'would create' if dry_run else 'created'} {path}")
        elif action == "append":
            actions.append(f"{'would append roar section to' if dry_run else 'appended to'} {path}")
        elif action == "update":
            actions.append(
                f"{'would update roar section in' if dry_run else 'updated roar section in'} {path}"
            )
        elif action == "noop":
            actions.append(f"AGENTS.md already up to date: {path}")

    for line in actions:
        click.echo(line)
