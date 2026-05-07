"""roar TUI — Textual App that hosts the main screen and modal overlays."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import App
from textual.binding import Binding, BindingType

from .screens.launcher import LauncherScreen
from .screens.log import LogScreen
from .screens.main import MainScreen
from .screens.search import SearchScreen
from .screens.session_picker import SessionPickerScreen


class TuiApp(App):
    """Top-level Textual app for `roar tui`."""

    TITLE = "roar"
    SUB_TITLE = "interactive lineage browser"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(self, roar_dir: Path, cwd: Path) -> None:
        super().__init__()
        self.roar_dir = roar_dir
        self.cwd = cwd

    def on_mount(self) -> None:
        self.push_screen(MainScreen(self.roar_dir, self.cwd))

    # --- global actions (wired via screen BINDINGS -> app.<action>) -----------

    def action_push_log(self) -> None:
        self.push_screen(LogScreen(self.roar_dir, self.cwd))

    def action_open_search(self) -> None:
        def _on_result(ref: str | None) -> None:
            if ref is None:
                return
            top = self._main_screen()
            if top is not None:
                top.focus_target(ref)

        self.push_screen(SearchScreen(self.roar_dir), _on_result)

    def action_open_launcher(self) -> None:
        def _on_result(message: str | None) -> None:
            if not message:
                return
            self.notify(message, markup=True, timeout=8)
            top = self._main_screen()
            if top is not None:
                top._reload_dag()

        self.push_screen(LauncherScreen(self.roar_dir, self.cwd), _on_result)

    def action_open_session_picker(self) -> None:
        def _on_result(listing) -> None:
            if listing is None:
                return  # cancelled
            top = self._main_screen()
            if top is None:
                return
            top.session_ref = None if listing.is_active else listing.hash
            top._reload_dag()

        top = self._main_screen()
        current_ref = top.session_ref if top is not None else None
        self.push_screen(SessionPickerScreen(self.roar_dir, current_ref), _on_result)

    def _main_screen(self) -> MainScreen | None:
        for screen in reversed(self.screen_stack):
            if isinstance(screen, MainScreen):
                return screen
        return None


def run(roar_dir: Path, cwd: Path) -> None:
    """Entry point called by `roar tui`."""
    TuiApp(roar_dir=roar_dir, cwd=cwd).run()
