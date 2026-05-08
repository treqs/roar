"""Label editor modal — list / add / edit / delete user labels for one entity.

User-origin labels only for v1; system labels are visible read-only behind a
toggle (project / org / global scopes aren't wired into roar yet). All writes
route through the same `LabelService` the CLI uses; deletion is via a small
sibling method (`delete_keys`) added alongside.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static

from ...application.label_rendering import flatten_label_metadata
from ...application.labels import LabelService, parse_label_pairs
from ...application.query.label import sync_labels
from ...application.query.requests import LabelSyncRequest
from ...application.system_labels import is_reserved_system_label_path
from ...db.context import create_database_context


class LabelEditorScreen(ModalScreen[None]):
    """List labels for an entity. `Enter` edits, `a` adds, `d` deletes, `p` syncs."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close", show=False),
        Binding("a", "add", "Add"),
        Binding("d", "delete", "Delete"),
        Binding("t", "toggle_system", "Toggle system"),
        Binding("p", "sync", "Sync to remote"),
        Binding("enter", "edit_selected", "Edit", priority=True),
    ]

    DEFAULT_CSS = """
    LabelEditorScreen { align: center middle; }
    #label-box {
        width: 80%;
        max-width: 120;
        height: 70%;
        background: $surface;
        border: heavy $accent;
        padding: 1 2;
    }
    #label-title { color: $accent; padding-bottom: 1; }
    #label-list { height: 1fr; }
    #label-list ListItem.-system Label { color: $text-muted; }
    #label-keys { color: $text-muted; padding-top: 1; }
    """

    BASE_KEYS = ("Enter edit", "a add", "d delete")
    TAIL_KEYS = ("p sync", "Esc close")

    def __init__(
        self,
        roar_dir: Path,
        cwd: Path,
        entity_type: str,  # "job" or "artifact"
        target: str,  # job_uid or artifact hash
        display_name: str,
    ) -> None:
        super().__init__()
        self.roar_dir = roar_dir
        self.cwd = cwd
        self.entity_type = entity_type
        self.target = target
        self.display_name = display_name
        self._show_system: bool = False
        self._entries: list[tuple[str, str, bool]] = []  # (key, value_str, is_system)

    def compose(self) -> ComposeResult:
        with Vertical(id="label-box"):
            yield Static(
                f"Labels — {self.entity_type} {self.display_name}",
                id="label-title",
            )
            yield ListView(id="label-list")
            yield Static("", id="label-keys")

    async def on_mount(self) -> None:
        await self._refresh()
        self.query_one("#label-list", ListView).focus()

    # --- list rendering -------------------------------------------------------

    def _load_entries(self) -> list[tuple[str, str, bool]]:
        with create_database_context(self.roar_dir) as db_ctx:
            service = LabelService(db_ctx, self.cwd)
            resolved = service.resolve_target(self.entity_type, self.target)
            metadata = service.current_metadata(resolved)
        rows: list[tuple[str, str, bool]] = []
        for key, value in flatten_label_metadata(metadata):
            is_system = is_reserved_system_label_path(key)
            if is_system and not self._show_system:
                continue
            rows.append((key, value, is_system))
        return rows

    def _refresh_hint(self) -> None:
        """Build the key-hint footer.

        `t` is always shown so the toggle state is discoverable even on
        entities that happen to have zero system labels right now (artifacts
        don't accumulate them today, but that's a data state, not a fixed
        rule — system label production may broaden in future).
        """
        parts = list(self.BASE_KEYS)
        label = "t hide system" if self._show_system else "t show system"
        parts.append(label)
        parts.extend(self.TAIL_KEYS)
        self.query_one("#label-keys", Static).update(" · ".join(parts))

    async def _refresh(self) -> None:
        view = self.query_one("#label-list", ListView)
        prev_idx = view.index
        self._entries = self._load_entries()
        self._refresh_hint()
        # Both `clear()` and `extend()` are async (return AwaitRemove /
        # AwaitMount). Awaiting them ensures the DOM has actually flushed
        # before we try to set `index`, otherwise `index` pointer-arithmetics
        # against a stale `_nodes` and the highlight goes to the wrong row.
        await view.clear()
        items = []
        for key, value, is_system in self._entries:
            item = ListItem(Label(f"{key} = {value}"))
            if is_system:
                item.add_class("-system")
            items.append(item)
        if items:
            await view.extend(items)
        if not self._entries:
            return
        target_idx = (
            min(prev_idx, len(self._entries) - 1) if prev_idx is not None else 0
        )
        # `index` is a reactive that only invokes its watcher on a *change*,
        # so setting it back to the same value as before the rebuild is a
        # no-op and the new row never gets `-highlight`. Toggle through None
        # to force the watcher.
        view.index = None
        view.index = target_idx

    # --- actions --------------------------------------------------------------

    def action_close(self) -> None:
        self.dismiss(None)

    async def action_toggle_system(self) -> None:
        self._show_system = not self._show_system
        await self._refresh()

    def action_add(self) -> None:
        async def _on_result(result: tuple[str, str] | None) -> None:
            if result is not None:
                await self._apply_set(*result)

        self.app.push_screen(LabelEditFormScreen(), _on_result)

    def action_edit_selected(self) -> None:
        idx = self.query_one("#label-list", ListView).index
        if idx is None or not (0 <= idx < len(self._entries)):
            return
        key, value, is_system = self._entries[idx]
        if is_system:
            self.app.notify("System labels are read-only.", severity="warning")
            return

        async def _on_result(result: tuple[str, str] | None) -> None:
            if result is not None:
                # If user changed the key, drop the old one so we don't leave
                # an orphan; the merge of set_metadata wouldn't remove it.
                new_key, new_value = result
                if new_key != key:
                    await self._apply_delete(key)
                await self._apply_set(new_key, new_value)

        self.app.push_screen(LabelEditFormScreen(key, value), _on_result)

    def action_delete(self) -> None:
        idx = self.query_one("#label-list", ListView).index
        if idx is None or not (0 <= idx < len(self._entries)):
            return
        key, _value, is_system = self._entries[idx]
        if is_system:
            self.app.notify("System labels are read-only.", severity="warning")
            return

        async def _on_result(confirmed: bool | None) -> None:
            if confirmed:
                await self._apply_delete(key)

        self.app.push_screen(ConfirmScreen(f"Delete label `{key}`?"), _on_result)

    def action_sync(self) -> None:
        try:
            sync_labels(
                LabelSyncRequest(
                    roar_dir=self.roar_dir,
                    cwd=self.cwd,
                    entity_type=self.entity_type,
                    target=self.target,
                )
            )
        except Exception as exc:  # noqa: BLE001 — surface any backend failure
            self.app.notify(f"Sync failed: {exc}", severity="error", timeout=8)
            return
        self.app.notify("Labels synced.", severity="information")

    # --- mutations ------------------------------------------------------------

    async def _apply_set(self, key: str, value: str) -> None:
        if is_reserved_system_label_path(key):
            self.app.notify(
                f"`{key}` is a reserved system path.", severity="error"
            )
            return
        try:
            patch = parse_label_pairs((f"{key}={value}",))
        except ValueError as exc:
            self.app.notify(str(exc), severity="error")
            return
        try:
            with create_database_context(self.roar_dir) as db_ctx:
                service = LabelService(db_ctx, self.cwd)
                resolved = service.resolve_target(self.entity_type, self.target)
                service.set_metadata(resolved, patch)
        except ValueError as exc:
            self.app.notify(str(exc), severity="error")
            return
        await self._refresh()

    async def _apply_delete(self, key: str) -> None:
        try:
            with create_database_context(self.roar_dir) as db_ctx:
                service = LabelService(db_ctx, self.cwd)
                resolved = service.resolve_target(self.entity_type, self.target)
                service.delete_keys(resolved, [key])
        except ValueError as exc:
            self.app.notify(str(exc), severity="error")
            return
        await self._refresh()


class LabelEditFormScreen(ModalScreen["tuple[str, str] | None"]):
    """Inline form for adding or editing a single label.

    Returns ``(key, value)`` on submit, ``None`` on cancel. Validation of the
    key (reserved-path rejection, parse errors) is done by the parent screen
    on receipt — this widget is purely a text form.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "save", "Save", priority=True),
    ]

    DEFAULT_CSS = """
    LabelEditFormScreen { align: center middle; }
    #form-box {
        width: 70%;
        max-width: 100;
        height: auto;
        background: $surface;
        border: heavy $accent;
        padding: 1 2;
    }
    #form-title { color: $accent; padding-bottom: 1; }
    #form-key { margin-bottom: 1; }
    #form-keys { color: $text-muted; padding-top: 1; }
    """

    def __init__(self, key: str = "", value: str = "") -> None:
        super().__init__()
        self._initial_key = key
        self._initial_value = value

    def compose(self) -> ComposeResult:
        with Vertical(id="form-box"):
            verb = "Edit label" if self._initial_key else "Add label"
            yield Static(verb, id="form-title")
            yield Input(
                value=self._initial_key,
                placeholder="key (dotted path, e.g. experiment.tag)",
                id="form-key",
            )
            yield Input(
                value=self._initial_value, placeholder="value", id="form-value"
            )
            yield Static("Enter save · Esc cancel", id="form-keys")

    def on_mount(self) -> None:
        # Editing an existing label: cursor lands in the value field so the
        # common case (tweaking a value) needs zero navigation.
        focus_id = "form-value" if self._initial_key else "form-key"
        self.query_one(f"#{focus_id}", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        key = self.query_one("#form-key", Input).value.strip()
        value = self.query_one("#form-value", Input).value
        if not key:
            self.app.bell()
            return
        self.dismiss((key, value))


class ConfirmScreen(ModalScreen[bool]):
    """Yes/No confirm modal. `y`/`Enter` → True, `n`/`Esc` → False."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "no", "No"),
        Binding("n", "no", show=False),
        Binding("y", "yes", show=False),
        Binding("enter", "yes", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    #confirm-box {
        width: 60%;
        max-width: 80;
        height: auto;
        background: $surface;
        border: heavy $warning;
        padding: 1 2;
    }
    #confirm-prompt { color: $warning; padding-bottom: 1; }
    #confirm-keys { color: $text-muted; padding-top: 1; }
    """

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self._prompt, id="confirm-prompt")
            yield Static("y / Enter — yes  ·  n / Esc — no", id="confirm-keys")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)
