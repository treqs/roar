"""Modal session picker — pick a project session to view in the main screen."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, ListItem, ListView, Static

from .. import data as tui_data
from ..data import SessionListing


def _fmt_dt(ts: float | None) -> str:
    if ts is None or ts == 0:
        return "?"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "?"


class SessionPickerScreen(ModalScreen["SessionListing | None"]):
    """Returns the chosen `SessionListing`, or `None` on cancel."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "cancel", "Cancel", show=False),
        Binding("home", "jump_first", "First", show=False, priority=True),
        Binding("end", "jump_last", "Last", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    SessionPickerScreen { align: center middle; }
    #session-box {
        width: 80%;
        max-width: 120;
        height: 70%;
        background: $surface;
        border: heavy $primary;
        padding: 1 2;
    }
    #session-title { color: $accent; padding-bottom: 1; }
    #session-list { height: 1fr; }
    #session-list ListItem.-active-session > Label { color: $accent; text-style: bold; }
    """

    def __init__(self, roar_dir: Path, current_ref: str | None) -> None:
        super().__init__()
        self.roar_dir = roar_dir
        self.current_ref = current_ref
        self._listings: list[SessionListing] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="session-box"):
            yield Static("Sessions", id="session-title")
            yield ListView(id="session-list")

    def on_mount(self) -> None:
        self._listings = tui_data.list_sessions(self.roar_dir)
        view = self.query_one("#session-list", ListView)
        for listing in self._listings:
            badge = "●" if listing.is_active else " "
            jobs = f"{listing.job_count} job{'s' if listing.job_count != 1 else ''}"
            label = f"{badge}  {listing.short_hash}  {_fmt_dt(listing.created_at)}   {jobs}"
            item = ListItem(Label(label))
            if listing.is_active:
                item.add_class("-active-session")
            view.append(item)
        view.index = self._initial_index()
        view.focus()

    def _initial_index(self) -> int:
        # Land on the currently-displayed session if we can find it; otherwise
        # the active one (top of the list).
        ref = self.current_ref
        for i, listing in enumerate(self._listings):
            if ref is None and listing.is_active:
                return i
            if ref is not None and (
                listing.hash == ref or listing.hash.startswith(ref)
            ):
                return i
        return 0

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # ListView consumes Enter via its own `select_cursor` binding and
        # surfaces the choice via this message — listen here instead of
        # binding Enter on the screen.
        idx = event.list_view.index or 0
        if 0 <= idx < len(self._listings):
            self.dismiss(self._listings[idx])
        else:
            self.app.bell()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_jump_first(self) -> None:
        view = self.query_one("#session-list", ListView)
        if self._listings:
            view.index = 0

    def action_jump_last(self) -> None:
        view = self.query_one("#session-list", ListView)
        if self._listings:
            view.index = len(self._listings) - 1
