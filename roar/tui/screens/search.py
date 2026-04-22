"""Global search modal — substring search over jobs (command) and artifacts (path/hash)."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static

from .. import data as tui_data
from ..data import SearchHit


class SearchScreen(ModalScreen[str | None]):
    """Dismisses with a `target_ref` string the main screen can focus, or None."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss_none", "Cancel"),
        Binding("enter", "submit", "Open"),
    ]

    DEFAULT_CSS = """
    SearchScreen { align: center middle; }
    #search-box {
        width: 80%;
        max-width: 120;
        height: 70%;
        background: $surface;
        border: heavy $primary;
        padding: 1 2;
    }
    #search-title { color: $accent; padding-bottom: 1; }
    #search-input { margin-bottom: 1; }
    #search-results { height: 1fr; }
    """

    def __init__(self, roar_dir: Path) -> None:
        super().__init__()
        self.roar_dir = roar_dir
        self._hits: list[SearchHit] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="search-box"):
            yield Static("Search jobs & artifacts", id="search-title")
            yield Input(placeholder="Type to filter commands, paths, or hashes…", id="search-input")
            yield ListView(id="search-results")

    def on_mount(self) -> None:
        self.query_one("#search-input", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh_results(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    def action_submit(self) -> None:
        results = self.query_one("#search-results", ListView)
        if not self._hits:
            self.app.bell()
            return
        idx = results.index or 0
        if 0 <= idx < len(self._hits):
            self.dismiss(self._hits[idx].target_ref)
        else:
            self.app.bell()

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def _refresh_results(self, query: str) -> None:
        results = self.query_one("#search-results", ListView)
        results.clear()
        self._hits = tui_data.search(self.roar_dir, query)
        for hit in self._hits:
            prefix = "⚙ " if hit.kind == "job" else "⬇ "
            results.append(ListItem(Label(f"{prefix}{hit.label}")))
        if self._hits:
            results.index = 0
