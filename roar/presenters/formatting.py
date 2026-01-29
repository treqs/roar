"""
Shared formatting utilities for roar CLI output.

This module consolidates formatting functions that were previously
duplicated across multiple command files. All formatting is done
in a consistent, human-readable style.

Locations consolidated from:
- format_duration: show.py, dag.py, log.py, history.py, reproduce.py
- format_timestamp: show.py, dag.py, log.py
- format_size: console.py, run_report.py
- relativize_path: show.py (7x), status.py, clean.py, rm.py, log.py, verify.py
- extract_blake3_hash: show.py (4x), status.py
- truncate_string: show.py, dag.py, reproduce.py
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any


def format_duration(seconds: float | None) -> str:
    """Format duration in human-readable format.

    Args:
        seconds: Duration in seconds, or None

    Returns:
        Human-readable duration string

    Examples:
        >>> format_duration(None)
        '?'
        >>> format_duration(45.5)
        '45.5s'
        >>> format_duration(125)
        '2m 5s'
        >>> format_duration(3725)
        '1h 2m'
    """
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        mins = int(seconds // 60)
        secs = seconds % 60
        return f"{mins}m {secs:.0f}s"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"


def format_timestamp(ts: float | None) -> str:
    """Format a Unix timestamp for display.

    Args:
        ts: Unix timestamp (seconds since epoch), or None

    Returns:
        Formatted datetime string in "YYYY-MM-DD HH:MM:SS" format

    Examples:
        >>> format_timestamp(None)
        '?'
        >>> format_timestamp(1704067200.0)  # 2024-01-01 00:00:00 UTC
        '2024-01-01 00:00:00'  # Depends on local timezone
    """
    if ts is None:
        return "?"
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_size(size_bytes: int | None) -> str:
    """Format byte size as human-readable string.

    Args:
        size_bytes: Size in bytes, or None

    Returns:
        Human-readable size string with appropriate unit

    Examples:
        >>> format_size(None)
        '?'
        >>> format_size(500)
        '500B'
        >>> format_size(2048)
        '2.0KB'
        >>> format_size(1536000)
        '1.5MB'
        >>> format_size(2147483648)
        '2.0GB'
    """
    if size_bytes is None:
        return "?"

    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f}MB"
    else:
        return f"{size_bytes / 1024 / 1024 / 1024:.1f}GB"


def truncate_string(s: str, max_len: int = 50, suffix: str = "...") -> str:
    """Truncate a string with ellipsis if too long.

    Args:
        s: String to truncate
        max_len: Maximum length including suffix
        suffix: String to append when truncating

    Returns:
        Original string if short enough, otherwise truncated with suffix

    Examples:
        >>> truncate_string("short", 10)
        'short'
        >>> truncate_string("this is a very long string", 15)
        'this is a ve...'
    """
    if len(s) <= max_len:
        return s
    return s[: max_len - len(suffix)] + suffix


def truncate_command(cmd: str, max_len: int = 50) -> str:
    """Truncate a command string for display.

    This is a convenience wrapper around truncate_string specifically
    for command strings.

    Args:
        cmd: Command string to truncate
        max_len: Maximum length including ellipsis

    Returns:
        Truncated command string

    Examples:
        >>> truncate_command("python train.py --epochs 100 --batch-size 32", 30)
        'python train.py --epochs 1...'
    """
    return truncate_string(cmd, max_len, "...")


def relativize_path(path: str | Path, cwd: Path | None = None) -> str:
    """Make a path relative to cwd if it makes the path shorter.

    This function tries to make paths relative for cleaner display,
    but only if the relative path doesn't go up directories (no ..)
    and is actually shorter than the absolute path.

    Args:
        path: Path to relativize (string or Path object)
        cwd: Current working directory (defaults to Path.cwd())

    Returns:
        Relative path string if shorter and doesn't use .., otherwise original

    Examples:
        >>> # If cwd is /home/user/project
        >>> relativize_path("/home/user/project/data/file.txt")
        'data/file.txt'
        >>> relativize_path("/home/other/file.txt")  # Would need ..
        '/home/other/file.txt'
    """
    if cwd is None:
        cwd = Path.cwd()

    path_obj = Path(path)

    try:
        rel = path_obj.relative_to(cwd)
        rel_str = str(rel)

        # Don't use relative path if it goes up directories
        if rel_str.startswith(".."):
            return str(path)

        # Use relative if it's shorter or same length
        if len(rel_str) <= len(str(path)):
            return rel_str

        return str(path)
    except ValueError:
        # Path is not relative to cwd
        return str(path)


def extract_blake3_hash(
    hashes: list[dict[str, Any]] | None,
    fallback_id: str | None = None,
) -> str | None:
    """Extract blake3 hash from a list of hash dictionaries.

    Roar stores multiple hashes per artifact. This function extracts
    the blake3 hash, which is the primary hash algorithm used.

    Args:
        hashes: List of hash dicts with 'algorithm' and 'digest' keys
        fallback_id: Fallback value if no blake3 hash found

    Returns:
        blake3 hash digest, or first hash digest, or fallback

    Examples:
        >>> hashes = [
        ...     {"algorithm": "sha256", "digest": "abc123"},
        ...     {"algorithm": "blake3", "digest": "def456"},
        ... ]
        >>> extract_blake3_hash(hashes)
        'def456'
        >>> extract_blake3_hash([{"algorithm": "sha256", "digest": "abc"}])
        'abc'
        >>> extract_blake3_hash(None, "fallback")
        'fallback'
    """
    if not hashes:
        return fallback_id

    # Look for blake3 first
    for h in hashes:
        if h.get("algorithm") == "blake3":
            return h.get("digest")

    # Fall back to first hash if no blake3
    if hashes:
        return hashes[0].get("digest")

    return fallback_id


def format_exit_code(exit_code: int | None) -> str:
    """Format an exit code for display.

    Args:
        exit_code: Process exit code, or None

    Returns:
        Formatted exit code with success/failure indicator

    Examples:
        >>> format_exit_code(0)
        '0 (success)'
        >>> format_exit_code(1)
        '1 (failure)'
        >>> format_exit_code(None)
        '?'
    """
    if exit_code is None:
        return "?"
    if exit_code == 0:
        return "0 (success)"
    return f"{exit_code} (failure)"


def format_step_reference(step_num: int, is_build: bool = False) -> str:
    """Format a DAG step reference.

    Args:
        step_num: Step number
        is_build: Whether this is a build step

    Returns:
        Formatted step reference (@N or @BN)

    Examples:
        >>> format_step_reference(1)
        '@1'
        >>> format_step_reference(2, is_build=True)
        '@B2'
    """
    prefix = "@B" if is_build else "@"
    return f"{prefix}{step_num}"


def format_hash_prefix(hash_str: str | None, length: int = 8) -> str:
    """Format a hash string as a prefix for display.

    Args:
        hash_str: Full hash string, or None
        length: Number of characters to show

    Returns:
        Hash prefix or "?" if None

    Examples:
        >>> format_hash_prefix("abc123def456")
        'abc123de'
        >>> format_hash_prefix(None)
        '?'
    """
    if hash_str is None:
        return "?"
    return hash_str[:length]


def format_file_list(
    files: list[str],
    cwd: Path | None = None,
    max_items: int = 5,
    prefix: str = "  ",
) -> list[str]:
    """Format a list of file paths for display.

    Args:
        files: List of file paths
        cwd: Working directory for relativization
        max_items: Maximum number of items to show
        prefix: Prefix for each line

    Returns:
        List of formatted lines

    Examples:
        >>> format_file_list(["/home/user/a.txt", "/home/user/b.txt"], max_items=1)
        ['  a.txt', '  ... and 1 more']
    """
    lines = []

    for path in files[:max_items]:
        rel_path = relativize_path(path, cwd)
        lines.append(f"{prefix}{rel_path}")

    remaining = len(files) - max_items
    if remaining > 0:
        lines.append(f"{prefix}... and {remaining} more")

    return lines
