"""Detached command launching via tmux.

The TUI requires tmux — if tmux is missing, the launcher surfaces a clear error
instead of falling back. If the TUI is itself running inside a tmux session,
new windows open in that session; otherwise a dedicated `roar-runs` session is
created (detached) and windows are placed there.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass


class TmuxError(RuntimeError):
    """Raised when tmux is missing or a tmux operation fails."""


@dataclass(frozen=True)
class TmuxLaunch:
    """Result of a successful detached launch."""

    session: str
    window_index: str
    window_name: str

    @property
    def target(self) -> str:
        """Tmux target ref like `roar-runs:3`."""
        return f"{self.session}:{self.window_index}"

    @property
    def attach_hint(self) -> str:
        """Human hint for reaching the running window."""
        if os.environ.get("TMUX"):
            return f"Prefix-w  (window '{self.window_name}' in this session)"
        return f"tmux attach -t {self.session}"


ROAR_RUNS_SESSION = "roar-runs"


def tmux_available() -> bool:
    """Return True iff the `tmux` binary is on PATH."""
    return shutil.which("tmux") is not None


def _run_tmux(args: list[str]) -> subprocess.CompletedProcess[str]:
    if not tmux_available():
        raise TmuxError("tmux is not installed. Install tmux to use the launcher.")
    try:
        return subprocess.run(
            ["tmux", *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip() or "tmux command failed"
        raise TmuxError(stderr) from exc
    except FileNotFoundError as exc:
        raise TmuxError("tmux is not installed. Install tmux to use the launcher.") from exc


def _ensure_session(session: str) -> None:
    try:
        _run_tmux(["has-session", "-t", session])
    except TmuxError:
        _run_tmux(["new-session", "-d", "-s", session])


def launch_roar_run(
    user_command: str,
    *,
    cwd: str,
    window_name: str | None = None,
) -> TmuxLaunch:
    """Launch `roar run <user_command>` in a detached tmux window.

    If the caller is already inside tmux (`$TMUX` set) the new window is created
    in the current session; otherwise we place it in the `roar-runs` session
    (creating it detached if needed).
    """
    if not user_command.strip():
        raise TmuxError("Empty command.")
    if not tmux_available():
        raise TmuxError("tmux is not installed. Install tmux to use the launcher.")

    # Build `roar run` command. We launch it via a login-ish shell to preserve
    # the user's PATH and env; the user's command is quoted safely.
    roar_cmd = f"roar run {user_command}"
    wrapped = (
        f"cd {shlex.quote(cwd)}; "
        f"{roar_cmd}; "
        f"echo; echo '[roar tui: command exited — press any key]'; read -n1"
    )

    in_tmux = bool(os.environ.get("TMUX"))
    if in_tmux:
        target_session = None  # current session
    else:
        _ensure_session(ROAR_RUNS_SESSION)
        target_session = ROAR_RUNS_SESSION

    name = window_name or _default_window_name(user_command)

    args = ["new-window", "-d", "-P", "-F", "#{session_name}\t#{window_index}\t#{window_name}"]
    if target_session is not None:
        args += ["-t", f"{target_session}:"]
    args += ["-n", name, wrapped]

    result = _run_tmux(args)
    parts = result.stdout.strip().split("\t")
    if len(parts) != 3:
        raise TmuxError(f"Unexpected tmux response: {result.stdout!r}")
    return TmuxLaunch(session=parts[0], window_index=parts[1], window_name=parts[2])


def _default_window_name(user_command: str, *, max_len: int = 20) -> str:
    head = user_command.strip().split(None, 1)[0] if user_command.strip() else "cmd"
    name = f"roar/{head}"
    if len(name) > max_len:
        name = name[:max_len]
    return name
