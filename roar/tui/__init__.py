"""Interactive terminal UI for roar."""

from __future__ import annotations

import os

# Textual reads ESCDELAY once at import time (default 100ms) and waits that
# long after Esc to disambiguate it from arrow/function-key escape sequences.
# 25ms still leaves plenty of headroom for arrow keys on local + typical SSH
# while keeping the close-pane action snappy. User-set ESCDELAY wins.
os.environ.setdefault("ESCDELAY", "25")

__all__ = ["run"]


def run(*args, **kwargs):  # pragma: no cover - thin shim
    from .app import run as _run

    return _run(*args, **kwargs)
