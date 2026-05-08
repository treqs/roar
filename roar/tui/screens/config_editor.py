"""Project-config editor — flat list of keys, edit via type-aware sub-modals.

Reads/writes go through the same `config_list` / `config_get` / `config_set`
trio the CLI uses, so type parsing, range/enum validation, and TOML
preservation all happen in one place.

Bools toggle straight on Enter (no form). Enum-like strings (`tracer.default`,
`hash.primary`, `logging.level`) open a small select; everything else opens a
text-input form whose raw value is fed to `config_set` for parsing.
"""

from __future__ import annotations

from typing import ClassVar, Literal, get_args

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static

from ...core.tracer_modes import VALID_TRACER_MODES
from ...integrations.config import config_get, config_list, config_set
from ...integrations.config.access import VALID_HASH_ALGORITHMS
from ...integrations.config.schema import LogLevel

# Keys whose accepted values come from existing constants/Literals — no new
# config-side API needed. Anything not listed here is treated as freeform.
ENUM_CHOICES: dict[str, tuple[str, ...]] = {
    "tracer.default": tuple(sorted(VALID_TRACER_MODES)),
    "hash.primary": tuple(sorted(VALID_HASH_ALGORITHMS)),
    "logging.level": get_args(LogLevel),
}


def _value_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(str(x) for x in value)
    return str(value)


class ConfigEditorScreen(ModalScreen[None]):
    """Browse + edit project config keys, with Ctrl+S filter mode."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close", show=False),
        Binding("enter", "edit_selected", "Edit", priority=True),
        Binding("ctrl+s", "enter_search", "Search", priority=True),
        Binding("ctrl+g", "abort_search", show=False, priority=True),
        Binding("up", "list_step(-1)", show=False, priority=True),
        Binding("down", "list_step(1)", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    ConfigEditorScreen { align: center middle; }
    #config-box {
        width: 90%;
        max-width: 130;
        height: 80%;
        background: $surface;
        border: heavy $accent;
        padding: 1 2;
    }
    #config-title { color: $accent; padding-bottom: 1; }
    #config-search { display: none; margin-bottom: 1; }
    #config-search.-active { display: block; }
    #config-list { height: 1fr; }
    #config-detail { color: $text-muted; padding-top: 1; padding-bottom: 0; }
    #config-keys { color: $text-muted; padding-top: 1; }
    """

    BROWSE_KEYS = "Enter edit · Ctrl+S search · Esc close"
    SEARCH_KEYS = "Type to filter · ↑↓ navigate · Enter edit · Ctrl+G abort"

    def __init__(self, start_dir: str) -> None:
        super().__init__()
        self.start_dir = start_dir
        # (key, type, default, description)
        self._entries: list[tuple[str, type, object, str]] = []
        self._mode: Literal["browse", "search"] = "browse"
        self._search_query: str = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="config-box"):
            yield Static("Project config (.roar/config.toml)", id="config-title")
            yield Input(placeholder="filter…", id="config-search")
            yield ListView(id="config-list")
            yield Static("", id="config-detail")
            yield Static(self.BROWSE_KEYS, id="config-keys")

    async def on_mount(self) -> None:
        await self._refresh()
        self.query_one("#config-list", ListView).focus()

    async def _refresh(self) -> None:
        listing = config_list()
        self._entries = sorted(
            (
                (key, info["type"], info.get("default"), info.get("description", ""))
                for key, info in listing.items()
            ),
            key=lambda row: row[0],
        )
        view = self.query_one("#config-list", ListView)
        # Try to keep the cursor on the same key across filter changes —
        # most natural when the user is narrowing the list to find a key.
        prev_key = None
        prev_idx = view.index
        if prev_idx is not None and 0 <= prev_idx < len(self._visible_entries()):
            prev_key = self._visible_entries()[prev_idx][0]
        visible = self._visible_entries()
        await view.clear()
        items: list[ListItem] = []
        for key, _t, _default, _desc in visible:
            current = config_get(key, start_dir=self.start_dir)
            items.append(ListItem(Label(f"{key} = {_value_str(current)}")))
        if items:
            await view.extend(items)
        if not visible:
            self._update_detail()
            return
        target_idx = 0
        if prev_key is not None:
            for i, (key, *_rest) in enumerate(visible):
                if key == prev_key:
                    target_idx = i
                    break
        # Force the reactive watcher to fire so the row gets `-highlight`
        # even when target_idx happens to match the previous value.
        view.index = None
        view.index = target_idx
        self._update_detail()

    def _visible_entries(self) -> list[tuple[str, type, object, str]]:
        """Apply the current search filter to `_entries` (case-insensitive)."""
        query = self._search_query.strip().lower()
        if not query:
            return self._entries
        return [row for row in self._entries if query in row[0].lower()]

    # --- detail line under the list ------------------------------------------

    def on_list_view_highlighted(self, event) -> None:
        self._update_detail()

    def _update_detail(self) -> None:
        view = self.query_one("#config-list", ListView)
        idx = view.index
        detail = self.query_one("#config-detail", Static)
        visible = self._visible_entries()
        if idx is None or not (0 <= idx < len(visible)):
            detail.update("")
            return
        _key, type_, default, description = visible[idx]
        type_name = getattr(type_, "__name__", str(type_))
        detail.update(
            f"{description}\n[dim]type: {type_name} · default: {_value_str(default)}[/dim]"
        )

    # --- search-mode actions --------------------------------------------------

    async def action_enter_search(self) -> None:
        if self._mode == "search":
            return
        self._mode = "search"
        bar = self.query_one("#config-search", Input)
        bar.add_class("-active")
        bar.value = ""
        bar.focus()
        self._refresh_keys()

    async def action_abort_search(self) -> None:
        if self._mode != "search":
            return
        await self._exit_search()

    async def _exit_search(self) -> None:
        self._mode = "browse"
        self._search_query = ""
        bar = self.query_one("#config-search", Input)
        bar.remove_class("-active")
        bar.value = ""
        await self._refresh()
        self._refresh_keys()
        self.query_one("#config-list", ListView).focus()

    def _refresh_keys(self) -> None:
        keys = self.query_one("#config-keys", Static)
        keys.update(self.SEARCH_KEYS if self._mode == "search" else self.BROWSE_KEYS)

    async def action_list_step(self, delta: int) -> None:
        """↑/↓ priority binding — moves the list cursor while the search Input
        keeps focus. In browse mode we delegate to the focused widget so the
        ListView's own up/down handlers still work."""
        if self._mode != "search":
            view = self.query_one("#config-list", ListView)
            if view.has_focus:
                if delta < 0:
                    view.action_cursor_up()
                else:
                    view.action_cursor_down()
            return
        view = self.query_one("#config-list", ListView)
        visible = self._visible_entries()
        if not visible:
            return
        cur = view.index if view.index is not None else 0
        view.index = max(0, min(len(visible) - 1, cur + delta))
        self._update_detail()

    async def on_input_changed(self, event: Input.Changed) -> None:
        if self._mode != "search":
            return
        self._search_query = event.value
        await self._refresh()

    # --- actions --------------------------------------------------------------

    def action_close(self) -> None:
        self.dismiss(None)

    async def action_edit_selected(self) -> None:
        view = self.query_one("#config-list", ListView)
        idx = view.index
        visible = self._visible_entries()
        if idx is None or not (0 <= idx < len(visible)):
            return
        # If user is in search mode, edit-then-exit so they're back in the
        # full list with the just-edited key still selected.
        if self._mode == "search":
            await self._exit_search()
            for i, (k, *_rest) in enumerate(self._entries):
                if k == visible[idx][0]:
                    view.index = i
                    break
            visible = self._entries
            idx = view.index if view.index is not None else 0
        key, type_, default, description = visible[idx]
        current = config_get(key, start_dir=self.start_dir)

        # Bool: straight toggle, no form.
        if type_ is bool:
            new_str = "false" if current else "true"
            await self._apply_set(key, new_str)
            return

        # Enum-like string → select.
        if key in ENUM_CHOICES:
            choices = ENUM_CHOICES[key]
            current_str = _value_str(current)

            async def _on_pick(chosen: str | None) -> None:
                if chosen is not None:
                    await self._apply_set(key, chosen)

            self.app.push_screen(
                ConfigSelectScreen(key, current_str, choices, description), _on_pick
            )
            return

        # Otherwise → text input.
        current_str = _value_str(current)

        async def _on_value(new_str: str | None) -> None:
            if new_str is not None:
                await self._apply_set(key, new_str)

        self.app.push_screen(
            ConfigEditFormScreen(key, current_str, type_, description, default),
            _on_value,
        )

    async def _apply_set(self, key: str, value: str) -> None:
        try:
            config_set(key, value, start_dir=self.start_dir)
        except (ValueError, OSError) as exc:
            self.app.notify(str(exc), severity="error", timeout=8)
            return
        await self._refresh()


class ConfigEditFormScreen(ModalScreen["str | None"]):
    """Text-input form for non-bool, non-enum config values."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel"),
        Binding("enter", "save", priority=True),
    ]

    DEFAULT_CSS = """
    ConfigEditFormScreen { align: center middle; }
    #config-form-box {
        width: 75%;
        max-width: 110;
        height: auto;
        background: $surface;
        border: heavy $accent;
        padding: 1 2;
    }
    #config-form-title { color: $accent; padding-bottom: 1; }
    #config-form-meta  { color: $text-muted; padding-bottom: 1; }
    #config-form-keys  { color: $text-muted; padding-top: 1; }
    """

    def __init__(self, key: str, current: str, type_: type, description: str, default) -> None:
        super().__init__()
        self.key = key
        self.current = current
        self.type_ = type_
        self.description = description
        self.default = default

    def compose(self) -> ComposeResult:
        with Vertical(id="config-form-box"):
            yield Static(f"Edit {self.key}", id="config-form-title")
            type_name = getattr(self.type_, "__name__", str(self.type_))
            yield Static(
                f"{self.description}\n"
                f"[dim]type: {type_name} · default: {_value_str(self.default)}[/dim]",
                id="config-form-meta",
            )
            placeholder = "comma-separated values" if self.type_ is list else ""
            yield Input(
                value=self.current,
                placeholder=placeholder,
                id="config-form-input",
            )
            yield Static("Enter save · Esc cancel", id="config-form-keys")

    def on_mount(self) -> None:
        self.query_one("#config-form-input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        self.dismiss(self.query_one("#config-form-input", Input).value)


class ConfigSelectScreen(ModalScreen["str | None"]):
    """Pick one of N choices for an enum-like string config key."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel"),
        Binding("enter", "save", priority=True),
    ]

    DEFAULT_CSS = """
    ConfigSelectScreen { align: center middle; }
    #select-box {
        width: 60%;
        max-width: 80;
        height: auto;
        background: $surface;
        border: heavy $accent;
        padding: 1 2;
    }
    #select-title { color: $accent; padding-bottom: 1; }
    #select-desc  { color: $text-muted; padding-bottom: 1; }
    #select-list  { height: auto; max-height: 12; }
    #select-keys  { color: $text-muted; padding-top: 1; }
    """

    def __init__(self, key: str, current: str, choices: tuple[str, ...], description: str) -> None:
        super().__init__()
        self.key = key
        self.current = current
        self.choices = choices
        self.description = description

    def compose(self) -> ComposeResult:
        with Vertical(id="select-box"):
            yield Static(f"Set {self.key}", id="select-title")
            yield Static(self.description, id="select-desc")
            yield ListView(id="select-list")
            yield Static("Enter pick · Esc cancel", id="select-keys")

    async def on_mount(self) -> None:
        view = self.query_one("#select-list", ListView)
        items = [ListItem(Label(c)) for c in self.choices]
        await view.extend(items)
        try:
            view.index = self.choices.index(self.current)
        except ValueError:
            view.index = 0
        view.focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        view = self.query_one("#select-list", ListView)
        idx = view.index
        if idx is None or not (0 <= idx < len(self.choices)):
            self.dismiss(None)
            return
        self.dismiss(self.choices[idx])
