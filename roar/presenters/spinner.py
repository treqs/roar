"""Simple threaded spinner for post-command processing feedback."""

from __future__ import annotations

import sys
import threading
import time


_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_INTERVAL = 0.08


class Spinner:
    """Context-manager spinner that writes to stderr.

    No-ops when stderr is not a TTY or when *quiet* is True.
    """

    def __init__(self, message: str = "", *, quiet: bool = False) -> None:
        self._message = message
        self._active = not quiet and hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

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
            # clear the spinner line
            sys.stderr.write("\r" + " " * (len(self._message) + 4) + "\r")
            sys.stderr.flush()

    # -- public api ----------------------------------------------------------

    def update(self, message: str) -> None:
        """Change the status text shown next to the spinner."""
        with self._lock:
            self._message = message

    # -- internals -----------------------------------------------------------

    def _spin(self) -> None:
        idx = 0
        while not self._stop_event.is_set():
            with self._lock:
                msg = self._message
            frame = _FRAMES[idx % len(_FRAMES)]
            sys.stderr.write(f"\r{frame} {msg}")
            sys.stderr.flush()
            idx += 1
            self._stop_event.wait(_INTERVAL)
