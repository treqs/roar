"""Launcher modal — launches `roar run <cmd>` in a detached tmux window.

History source: distinct `command` strings from prior jobs in the active
session. Type to filter; Enter to launch.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static

from .. import data as tui_data
from ..tmux import TmuxError, launch_roar_run, tmux_available


class LauncherScreen(ModalScreen[str | None]):
    """Dismisses with a short status message (success or error) to show in the main footer."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss_none", "Cancel"),
        Binding("ctrl+r", "use_history", "Use Highlighted"),
        Binding("enter", "submit", "Launch"),
    ]

    DEFAULT_CSS = """
    LauncherScreen { align: center middle; }
    #launcher-box {
        width: 80%;
        max-width: 120;
        height: 70%;
        background: $surface;
        border: heavy $success;
        padding: 1 2;
    }
    #launcher-title { color: $success; padding-bottom: 1; }
    #launcher-hint  { color: $text-muted; padding-bottom: 1; }
    #launcher-input { margin-bottom: 1; }
    #launcher-history { height: 1fr; }
    """

    def __init__(self, roar_dir: Path, cwd: Path) -> None:
        super().__init__()
        self.roar_dir = roar_dir
        self.cwd = cwd
        self._history: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="launcher-box"):
            yield Static("Launch command (runs in a detached tmux window)", id="launcher-title")
            yield Static(
                "Type a command, or filter history below. Enter launches. Ctrl+R copies highlighted history to the input.",
                id="launcher-hint",
            )
            yield Input(placeholder="python train.py --lr 0.001", id="launcher-input")
            yield ListView(id="launcher-history")

    def on_mount(self) -> None:
        if not tmux_available():
            self.dismiss("[red]tmux is not installed — launcher requires tmux[/red]")
            return
        self._history = tui_data.load_command_history(self.roar_dir)
        self._refresh_history("")
        self.query_one("#launcher-input", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh_history(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    def action_use_history(self) -> None:
        hist = self.query_one("#launcher-history", ListView)
        if hist.index is None:
            return
        filtered = self._filtered_history(self.query_one("#launcher-input", Input).value)
        if 0 <= hist.index < len(filtered):
            input_widget = self.query_one("#launcher-input", Input)
            input_widget.value = filtered[hist.index]
            input_widget.cursor_position = len(input_widget.value)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        cmd = self.query_one("#launcher-input", Input).value.strip()
        if not cmd:
            self.app.bell()
            return
        try:
            launch = launch_roar_run(cmd, cwd=str(self.cwd))
        except TmuxError as exc:
            self.dismiss(f"[red]tmux: {exc}[/red]")
            return
        self.dismiss(f"[green]launched[/green] {launch.target}  —  {launch.attach_hint}")

    # --- history list ---------------------------------------------------------

    def _filtered_history(self, needle: str) -> list[str]:
        n = needle.strip().lower()
        if not n:
            return self._history
        return [cmd for cmd in self._history if n in cmd.lower()]

    def _refresh_history(self, needle: str) -> None:
        hist = self.query_one("#launcher-history", ListView)
        hist.clear()
        for cmd in self._filtered_history(needle):
            hist.append(ListItem(Label(cmd)))
        if self._filtered_history(needle):
            hist.index = 0
