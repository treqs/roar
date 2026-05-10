"""Construct the user-facing error message for a dirty git working tree.

When `roar run`/`roar build` detects uncommitted changes, the error needs
to (a) teach *why* roar requires a clean tree, (b) show the exact
remediation commands using the actual changed files, (c) detect two
common ways users trip into this state:

  1. The dirty file is roar's own state directory (`.roar/`) — usually
     a fresh `roar init` whose `.gitignore` change isn't committed yet.
  2. The user is running `roar` from `$HOME` instead of a project
     directory.

The whole message is a single string raised as `ValueError(message)`,
which the CLI surface presents to the user.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

DOCS_URL = "https://glaas.ai/docs/why-clean-commits"
_MAX_LISTED_PATHS = 8
_MAX_NAMED_FOR_GIT_ADD = 5


def _parse_porcelain(status_output: str) -> list[str]:
    """Extract the path part from each `git status --porcelain` line.

    Porcelain v1 format is `XY path`, where X and Y are status codes and
    path may be quoted if it contains spaces. For renames the line is
    `XY old -> new`; we keep `new` since that's what the user works with.
    """
    paths: list[str] = []
    for line in status_output.splitlines():
        if not line:
            continue
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path:
            paths.append(path)
    return paths


def _all_under_roar_dir(paths: list[str]) -> bool:
    if not paths:
        return False
    return all(p == ".roar" or p.startswith(".roar/") for p in paths)


def _is_home_dir(repo_root: str | Path) -> bool:
    home = os.path.expanduser("~")
    if not home:
        return False
    try:
        return Path(repo_root).resolve() == Path(home).resolve()
    except OSError:
        return False


def _format_user_command(verb: str, args: list[str] | None) -> str:
    if not args:
        return f"roar {verb} <your-command>"
    return f"roar {verb} " + " ".join(shlex.quote(a) for a in args)


def _format_git_add(paths: list[str]) -> str:
    if len(paths) <= _MAX_NAMED_FOR_GIT_ADD:
        return "git add " + " ".join(shlex.quote(p) for p in paths)
    return "git add -A"


def _format_capped_list(paths: list[str]) -> list[str]:
    visible = paths[:_MAX_LISTED_PATHS]
    lines = [f"  {p}" for p in visible]
    if len(paths) > _MAX_LISTED_PATHS:
        lines.append(f"  ... and {len(paths) - _MAX_LISTED_PATHS} more")
    return lines


def format_dirty_tree_error(
    *,
    status_output: str,
    repo_root: str | Path,
    verb: str = "run",
    args: list[str] | None = None,
) -> str:
    paths = _parse_porcelain(status_output)
    user_cmd = _format_user_command(verb, args)

    if _is_home_dir(repo_root):
        return _format_home_dir_message(user_cmd)
    if _all_under_roar_dir(paths):
        return _format_roar_only_message(user_cmd)
    return _format_default_message(paths, user_cmd)


def _format_default_message(paths: list[str], user_cmd: str) -> str:
    lines = [
        "Run blocked: working tree is dirty.",
        "",
        "roar tags every run with the current git commit SHA so the lineage",
        'record answers "what code produced this artifact?" Uncommitted',
        "changes mean we can't record that honestly — same reason you wouldn't",
        "deploy uncommitted code to production.",
        "",
    ]
    if len(paths) > _MAX_NAMED_FOR_GIT_ADD:
        lines.append("Dirty files:")
        lines.extend(_format_capped_list(paths))
        lines.append("")
    lines.extend(
        [
            "To proceed:",
            f"  {_format_git_add(paths)}",
            '  git commit -m "<describe your changes>"',
            f"  {user_cmd}",
            "",
            f"Why: {DOCS_URL}",
        ]
    )
    return "\n".join(lines)


def _format_roar_only_message(user_cmd: str) -> str:
    return "\n".join(
        [
            "Run blocked: working tree is dirty.",
            "",
            "The only dirty path is roar's own state directory (.roar/).",
            "It should be in .gitignore so it doesn't trip future runs.",
            "",
            "Quickest fix:",
            "  echo '.roar/' >> .gitignore",
            "  git add .gitignore && git commit -m 'ignore .roar/'",
            f"  {user_cmd}",
            "",
            "Or re-run `roar init` — it will set this up for you.",
            "",
            f"Why: {DOCS_URL}",
        ]
    )


def _format_home_dir_message(user_cmd: str) -> str:
    return "\n".join(
        [
            "Run blocked: you're running roar from your home directory.",
            "",
            "roar tags every run with the current git commit SHA. Your home",
            "directory is rarely under clean version control, and is almost",
            "never the right place to track ML pipeline lineage.",
            "",
            "Switch to your project directory and run there:",
            "  cd <your project>",
            f"  {user_cmd}",
            "",
            f"Why: {DOCS_URL}",
        ]
    )
