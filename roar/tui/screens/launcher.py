"""Launcher modal — launches `roar run <cmd>` in a detached tmux window.

Two modes share one input field:

- **input** (default): type a command. Enter launches it as
  ``roar run <cmd>``. The "$ roar run" prefix is shown above the input
  so it's clear the wrapper is implicit and not user-editable.
- **search** (Ctrl+R): the input becomes a search query, the history
  list filters live, ↑/↓ navigate matches, Enter copies the match
  back into the input (and returns to input mode), Ctrl+G aborts and
  restores the prior input.

History is sourced from every project session, distinct, newest first.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static

from .. import data as tui_data
from ..tmux import TmuxError, launch_roar_run, tmux_available

Mode = Literal["input", "search"]


class LauncherScreen(ModalScreen[str | None]):
    """Dismisses with a short status message (success or error) to show in the main footer."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss_none", "Cancel"),
        Binding("ctrl+r", "enter_search", "Search hist", priority=True),
        Binding("ctrl+g", "abort_search", "Abort search", show=False, priority=True),
        Binding("ctrl+y", "yank", show=False, priority=True),
        Binding("up", "history_step(-1)", show=False, priority=True),
        Binding("down", "history_step(1)", show=False, priority=True),
        # Priority so Enter from the focused ListView (which has its own
        # `select_cursor` binding) routes to our dispatcher.
        Binding("enter", "submit", "Launch", priority=True),
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
    #launcher-prefix { color: $accent; padding-bottom: 0; }
    #launcher-input { margin-bottom: 1; }
    #launcher-history { height: 1fr; }
    #launcher-keys {
        color: $text-muted;
        padding-top: 1;
        padding-bottom: 0;
    }
    """

    INPUT_KEYS = "↓ history · Ctrl+R search · Ctrl+Y paste · Enter launch · Esc cancel"
    SEARCH_KEYS = "Type to filter · ↑↓ navigate · Enter use · Ctrl+G abort"

    def __init__(
        self, roar_dir: Path, cwd: Path, initial_command: str = ""
    ) -> None:
        super().__init__()
        self.roar_dir = roar_dir
        self.cwd = cwd
        self._history: list[str] = []
        self._initial_command = initial_command
        self._mode: Mode = "input"
        # In input mode tracks last user-typed command (for "up" recall);
        # in search mode tracks the value to restore on Ctrl+G abort.
        self._stashed_command: str = ""
        self._suppress_filter: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(id="launcher-box"):
            yield Static("$ roar run", id="launcher-title")
            yield Static("(prefix is automatic — type the rest)", id="launcher-prefix")
            yield Input(
                placeholder="python train.py --lr 0.001", id="launcher-input"
            )
            yield ListView(id="launcher-history")
            yield Static(self.INPUT_KEYS, id="launcher-keys")

    def on_mount(self) -> None:
        if not tmux_available():
            self.dismiss("[red]tmux is not installed — launcher requires tmux[/red]")
            return
        self._history = tui_data.load_command_history(self.roar_dir)
        self._refresh_history("")
        input_widget = self.query_one("#launcher-input", Input)
        if self._initial_command:
            input_widget.value = self._initial_command
            input_widget.cursor_position = len(input_widget.value)
        input_widget.focus()

    # --- events ---------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._suppress_filter:
            self._suppress_filter = False
            return
        # Filter live in both modes — the only difference is what Enter does.
        self._refresh_history(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    # --- mode actions ---------------------------------------------------------

    def action_enter_search(self) -> None:
        if self._mode == "search":
            return  # already searching
        input_widget = self.query_one("#launcher-input", Input)
        self._stashed_command = input_widget.value
        self._mode = "search"
        self._set_input_quietly("")
        self._refresh_chrome()

    def action_abort_search(self) -> None:
        if self._mode != "search":
            return
        self._mode = "input"
        self._set_input_quietly(self._stashed_command)
        self._refresh_chrome()

    def _accept_search_match(self) -> None:
        """Search → input mode, with the highlighted match copied to input."""
        hist = self.query_one("#launcher-history", ListView)
        input_widget = self.query_one("#launcher-input", Input)
        filtered = self._filtered_history(input_widget.value)
        idx = hist.index if hist.index is not None else 0
        chosen = filtered[idx] if 0 <= idx < len(filtered) else self._stashed_command
        self._mode = "input"
        self._set_input_quietly(chosen)
        self._refresh_chrome()

    def _set_input_quietly(self, value: str) -> None:
        """Update the input without triggering filter reset semantics."""
        self._suppress_filter = True
        input_widget = self.query_one("#launcher-input", Input)
        input_widget.value = value
        input_widget.cursor_position = len(value)
        self._refresh_history(value)

    def _refresh_chrome(self) -> None:
        title = self.query_one("#launcher-title", Static)
        prefix = self.query_one("#launcher-prefix", Static)
        keys = self.query_one("#launcher-keys", Static)
        if self._mode == "search":
            title.update("[$accent]search history:[/]")
            prefix.update("(↑↓ to navigate matches; Enter to use; Ctrl+G to abort)")
            keys.update(self.SEARCH_KEYS)
        else:
            title.update("$ roar run")
            prefix.update("(prefix is automatic — type the rest)")
            keys.update(self.INPUT_KEYS)

    # --- existing actions -----------------------------------------------------

    def action_history_step(self, delta: int) -> None:
        """↑/↓ behavior, dispatched by mode + focus.

        - search mode: move the filtered list cursor.
        - input mode + input focused: ↓ steps into the history list (cursor
          at first item); ↑ is a no-op.
        - input mode + list focused: arrows move the list cursor; ↑ at the
          top steps back to the input.
        """
        if self._mode == "search":
            self._move_list(delta)
            return
        inp = self.query_one("#launcher-input", Input)
        hist = self.query_one("#launcher-history", ListView)
        if inp.has_focus:
            if delta < 0:
                return  # up from input: nothing
            if not self._filtered_history(inp.value):
                return
            hist.focus()
            if hist.index is None:
                hist.index = 0
            return
        if hist.has_focus:
            items = self._filtered_history(inp.value)
            if not items:
                inp.focus()
                return
            cur = hist.index if hist.index is not None else 0
            new_idx = cur + delta
            if new_idx < 0:
                inp.focus()
                return
            hist.index = max(0, min(len(items) - 1, new_idx))

    def _move_list(self, delta: int) -> None:
        hist = self.query_one("#launcher-history", ListView)
        items = self._filtered_history(
            self.query_one("#launcher-input", Input).value
        )
        if not items:
            return
        cur = hist.index if hist.index is not None else 0
        hist.index = max(0, min(len(items) - 1, cur + delta))

    def action_yank(self) -> None:
        self.query_one("#launcher-input", Input).action_paste()

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        if self._mode == "search":
            # Enter in search mode promotes the highlighted match into the
            # input box and returns to input mode — doesn't launch yet.
            self._accept_search_match()
            return
        inp = self.query_one("#launcher-input", Input)
        hist = self.query_one("#launcher-history", ListView)
        if hist.has_focus:
            # Enter on the history list copies the selection into the input
            # and returns focus there so the user can edit before launching.
            items = self._filtered_history(inp.value)
            idx = hist.index if hist.index is not None else 0
            if 0 <= idx < len(items):
                self._set_input_quietly(items[idx])
            inp.focus()
            return
        cmd = inp.value.strip()
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
        filtered = self._filtered_history(needle)
        for cmd in filtered:
            hist.append(ListItem(Label(cmd)))
        if filtered:
            hist.index = 0
