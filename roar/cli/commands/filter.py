"""
roar filter — manage project-level path filters in .roarconfig.

Usage:
    roar filter list                    # show current ignore_paths
    roar filter add <pattern> ...       # add patterns
    roar filter remove <pattern> ...    # remove patterns
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


def _roarconfig_path() -> Path:
    """Return .roarconfig in the current working directory."""
    return Path.cwd() / ".roarconfig"


def _load_roarconfig(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _get_ignore_paths(data: dict) -> list[str]:
    return list(data.get("filters", {}).get("ignore_paths", []))


def _write_roarconfig(path: Path, data: dict) -> None:
    """Write .roarconfig as TOML.

    We only support the [filters] section for now, so the writer is simple.
    """
    lines: list[str] = []
    filters = data.get("filters", {})
    ignore_paths = filters.get("ignore_paths", [])

    # Preserve any non-filters top-level sections by round-tripping.
    # For now, only [filters].ignore_paths is managed; other sections
    # are dropped on write (acceptable for the initial implementation).
    if ignore_paths:
        lines.append("[filters]")
        lines.append("ignore_paths = [")
        for pattern in ignore_paths:
            lines.append(f'    "{pattern}",')
        lines.append("]")
        lines.append("")

    path.write_text("\n".join(lines))


@click.group("filter")
def filter_group() -> None:
    """Manage project-level path filters (.roarconfig).

    Patterns in ignore_paths are matched against file paths observed
    by the tracer.  Absolute patterns (starting with /) match as
    prefixes; relative patterns match as substrings anywhere in the
    path.  A trailing / means "this directory and everything under it".

    \b
    Examples:
        roar filter add wandb/ .cache/wandb/ /run/udev/
        roar filter remove wandb/
        roar filter list
    """


@filter_group.command("list")
def filter_list() -> None:
    """Show current ignore_paths from .roarconfig."""
    path = _roarconfig_path()
    data = _load_roarconfig(path)
    patterns = _get_ignore_paths(data)
    if not patterns:
        click.echo("No ignore_paths configured.")
        click.echo("Add patterns with: roar filter add <pattern>")
        return
    click.echo(f".roarconfig ({path}):")
    for pattern in patterns:
        click.echo(f"  {pattern}")


@filter_group.command("add")
@click.argument("patterns", nargs=-1, required=True)
def filter_add(patterns: tuple[str, ...]) -> None:
    """Add patterns to ignore_paths in .roarconfig."""
    path = _roarconfig_path()
    data = _load_roarconfig(path)
    current = _get_ignore_paths(data)
    added = []
    for pattern in patterns:
        if pattern not in current:
            current.append(pattern)
            added.append(pattern)
    if not added:
        click.echo("All patterns already present.")
        return
    data.setdefault("filters", {})["ignore_paths"] = current
    _write_roarconfig(path, data)
    for pattern in added:
        click.echo(f"  + {pattern}")
    click.echo(f"Updated {path}")


@filter_group.command("remove")
@click.argument("patterns", nargs=-1, required=True)
def filter_remove(patterns: tuple[str, ...]) -> None:
    """Remove patterns from ignore_paths in .roarconfig."""
    path = _roarconfig_path()
    data = _load_roarconfig(path)
    current = _get_ignore_paths(data)
    removed = []
    for pattern in patterns:
        if pattern in current:
            current.remove(pattern)
            removed.append(pattern)
    if not removed:
        click.echo("None of the specified patterns were found.")
        sys.exit(1)
    data.setdefault("filters", {})["ignore_paths"] = current
    _write_roarconfig(path, data)
    for pattern in removed:
        click.echo(f"  - {pattern}")
    click.echo(f"Updated {path}")
