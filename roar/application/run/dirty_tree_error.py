"""Construct the user-facing error message for a dirty git working tree.

When ``roar run`` / ``roar build`` detects uncommitted changes, the
error has to teach (a) *why* roar requires a clean tree, (b) the right
remediation for what's actually dirty, and (c) handle two common
fresh-install pitfalls:

* The dirty file is roar's own state directory (``.roar/``) — usually a
  fresh ``roar init`` whose ``.gitignore`` change isn't committed yet.
* The user is running ``roar`` from ``$HOME`` instead of a project dir.

The message is a single string raised as ``ValueError(message)``,
which the CLI surface presents.

The path-classifier (``dirty_tree_classify``) splits dirty paths into
three buckets — code changes, prior-roar outputs (path match in DB),
and unknown untracked files — so the message can recommend the right
fix per bucket. When buckets mix, the message segments accordingly.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any

from .dirty_tree_classify import DirtyClassification, classify_dirty_paths
from .gitignore_suggest import gitignore_lines

DOCS_URL = "https://glaas.ai/docs/why-clean-commits"
_MAX_LISTED_PATHS = 8
_MAX_NAMED_FOR_GIT_ADD = 5


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
    artifact_lookup: Any = None,
) -> str:
    """Build the user-facing dirty-tree refusal message.

    ``artifact_lookup`` is an optional object with a ``get_by_path``
    method (typically ``db_ctx.artifacts``). When provided, untracked
    paths that match a recorded artifact are classified as
    ``roar_outputs`` and the message recommends ``.gitignore`` for
    them. When ``None``, every untracked path is treated as unknown
    (the message offers both commit and gitignore).
    """
    user_cmd = _format_user_command(verb, args)

    if _is_home_dir(repo_root):
        return _format_home_dir_message(user_cmd)

    classification = classify_dirty_paths(status_output, repo_root, artifact_lookup)
    all_paths = classification.code + classification.roar_outputs + classification.unknown
    if _all_under_roar_dir(all_paths):
        return _format_roar_only_message(user_cmd)

    if classification.all_code:
        return _format_code_only_message(classification.code, user_cmd)
    if classification.all_roar_outputs:
        return _format_roar_outputs_only_message(classification.roar_outputs, user_cmd)
    if classification.all_unknown:
        return _format_unknown_only_message(classification.unknown, user_cmd)
    return _format_mixed_message(classification, user_cmd)


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------


def _format_code_only_message(paths: list[str], user_cmd: str) -> str:
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


def _format_roar_outputs_only_message(paths: list[str], user_cmd: str) -> str:
    lines = [
        "Run blocked: untracked outputs from earlier roar run(s).",
        "",
        "These match artifacts in the local roar DB — they look like",
        "products of past runs, not source code roar should pin commits to.",
        "",
        "Untracked outputs:",
    ]
    lines.extend(_format_capped_list(paths))
    lines.extend(
        [
            "",
            "Fix — gitignore them:",
        ]
    )
    lines.extend(f"  {sug}" for sug in gitignore_lines(paths))
    lines.extend(
        [
            "  git add .gitignore && git commit -m 'ignore roar outputs'",
            f"  {user_cmd}",
            "",
            "(Or `git add` + `git commit` them if they belong in the repo.)",
            "",
            f"Why: {DOCS_URL}",
        ]
    )
    return "\n".join(lines)


def _format_unknown_only_message(paths: list[str], user_cmd: str) -> str:
    lines = [
        "Run blocked: untracked files in the working tree.",
        "",
        "roar tags every run with the current git commit SHA. Untracked",
        "files mean the commit alone can't explain what was on disk at",
        "run time.",
        "",
        "Untracked files:",
    ]
    lines.extend(_format_capped_list(paths))
    lines.extend(
        [
            "",
            "If they're outputs you want kept around but not in the repo, gitignore them:",
        ]
    )
    lines.extend(f"  {sug}" for sug in gitignore_lines(paths))
    lines.extend(
        [
            "  git add .gitignore && git commit -m 'ignore outputs'",
            "",
            "If they belong in the repo, commit them:",
            f"  {_format_git_add(paths)}",
            '  git commit -m "<describe your changes>"',
            "",
            f"Then retry: {user_cmd}",
            "",
            f"Why: {DOCS_URL}",
        ]
    )
    return "\n".join(lines)


def _format_mixed_message(classification: DirtyClassification, user_cmd: str) -> str:
    lines = [
        "Run blocked: working tree is dirty.",
        "",
    ]

    if classification.code:
        lines.append("Code changes:")
        lines.extend(_format_capped_list(classification.code))
        lines.append("")
        lines.append("Fix:")
        lines.append(f"  {_format_git_add(classification.code)}")
        lines.append('  git commit -m "<describe your changes>"')
        lines.append("")

    if classification.roar_outputs:
        lines.append("Roar outputs (untracked):")
        lines.extend(_format_capped_list(classification.roar_outputs))
        lines.append("")
        lines.append("Fix — gitignore them:")
        lines.extend(f"  {sug}" for sug in gitignore_lines(classification.roar_outputs))
        lines.append("  git add .gitignore && git commit -m 'ignore roar outputs'")
        lines.append("")

    if classification.unknown:
        lines.append("Other untracked files:")
        lines.extend(_format_capped_list(classification.unknown))
        lines.append("")
        lines.append("Fix — gitignore or commit:")
        lines.extend(f"  {sug}" for sug in gitignore_lines(classification.unknown))
        lines.append("  git add .gitignore && git commit -m 'ignore outputs'")
        lines.append(f"  (or `{_format_git_add(classification.unknown)}` + commit)")
        lines.append("")

    lines.append(f"Then retry: {user_cmd}")
    lines.append("")
    lines.append(f"Why: {DOCS_URL}")
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
