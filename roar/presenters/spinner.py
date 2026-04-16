"""Threaded spinner for post-command processing feedback.

Supports two frame sets — clock emojis (for UTF-8 terminals) and the classic
ANSI braille cycle — and an optional live "N/M" counter appended to the label.
All output goes to stderr; no-ops when stderr isn't a TTY or when quiet.
"""

from __future__ import annotations

import sys
import threading

# Full 12-position hour clock cycle.
CLOCK_FRAMES = "🕛🕐🕑🕒🕓🕔🕕🕖🕗🕘🕙🕚"
BRAILLE_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_DEFAULT_INTERVAL = 0.1


class Spinner:
    """Context-manager spinner that writes to stderr.

    Keyword arguments:
      message   -- status text shown after the frame
      frames    -- iterable of single-char-wide frames (emoji OK)
      interval  -- seconds between frames
      prefix    -- text shown before the frame (e.g. "🦖 " or "roar: ")
      quiet     -- if True, becomes a no-op
      total     -- if set, renders a live "N/total" counter after the message
    """

    def __init__(
        self,
        message: str = "",
        *,
        frames: str = BRAILLE_FRAMES,
        interval: float = _DEFAULT_INTERVAL,
        prefix: str = "",
        quiet: bool = False,
        total: int | None = None,
    ) -> None:
        self._message = message
        self._frames = frames
        self._interval = interval
        self._prefix = prefix
        self._total = total
        self._count = 0
        self._active = not quiet and hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_line_len = 0

    # -- context manager -----------------------------------------------------

    def __enter__(self) -> Spinner:
        if self._active:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
        if self._active:
            # Clear the spinner line.
            sys.stderr.write("\r" + " " * self._last_line_len + "\r")
            sys.stderr.flush()

    # -- public api ----------------------------------------------------------

    def update(self, message: str) -> None:
        """Change the status text shown next to the spinner."""
        with self._lock:
            self._message = message

    def advance(self, delta: int = 1) -> None:
        """Increment the N of the N/total counter."""
        with self._lock:
            self._count += delta

    def set_count(self, count: int) -> None:
        with self._lock:
            self._count = count

    # -- internals -----------------------------------------------------------

    def _spin(self) -> None:
        idx = 0
        while not self._stop_event.is_set():
            with self._lock:
                msg = self._message
                count = self._count
                total = self._total
            frame = self._frames[idx % len(self._frames)]
            suffix = f" {count}/{total}" if total is not None else ""
            line = f"{self._prefix}{msg} {frame}{suffix}"
            # Pad to clear any residue from a longer previous line.
            pad = max(0, self._last_line_len - len(line))
            sys.stderr.write(f"\r{line}{' ' * pad}")
            sys.stderr.flush()
            self._last_line_len = len(line)
            idx += 1
            self._stop_event.wait(self._interval)
