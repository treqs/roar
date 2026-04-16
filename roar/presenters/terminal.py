"""Centralized terminal capability detection for roar presenters.

Single source of truth for "is this stream a TTY", "should we use color",
"can we print emoji", and "how wide is the terminal". Used by the run
presenter, spinner, and anyone else that wants to render nicely.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from typing import IO


@dataclass(frozen=True)
class TerminalCaps:
    """What the current terminal can do, as seen through `stream`."""

    is_tty: bool
    can_color: bool
    can_emoji: bool
    width: int

    @property
    def pipe_mode(self) -> bool:
        """True when the stream is not a TTY (piped, redirected, captured)."""
        return not self.is_tty


def detect(stream: IO | None = None) -> TerminalCaps:
    """Sniff capabilities of *stream* (defaults to stderr)."""
    s = stream if stream is not None else sys.stderr
    is_tty = bool(getattr(s, "isatty", lambda: False)())

    # Standard NO_COLOR convention (https://no-color.org/).
    no_color_env = os.environ.get("NO_COLOR", "") != ""
    can_color = is_tty and not no_color_env

    enc = (getattr(s, "encoding", "") or "").lower()
    lang = os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    can_emoji = is_tty and ("utf" in enc or "UTF-8" in lang.upper())

    try:
        width = shutil.get_terminal_size((80, 24)).columns
    except OSError:
        width = 80
    width = max(40, width)

    return TerminalCaps(is_tty=is_tty, can_color=can_color, can_emoji=can_emoji, width=width)


# ---- ANSI helpers -----------------------------------------------------------

_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "italic": "\033[3m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "gray": "\033[90m",
}


def style(text: str, *codes: str, enabled: bool = True) -> str:
    """Wrap *text* in the given ANSI styles, or return it unchanged if disabled."""
    if not enabled or not codes:
        return text
    prefix = "".join(_ANSI.get(c, "") for c in codes)
    return f"{prefix}{text}{_ANSI['reset']}" if prefix else text
