"""Main TUI screen: full-width DAG tree on top, collapsible detail pane below."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import (
    ContentSwitcher,
    Footer,
    Header,
    Static,
    Tree,
)
from textual.widgets.tree import TreeNode

from .. import data as tui_data
from ..widgets.detail import ArtifactDetail, JobDetail

REFRESH_INTERVAL_SECONDS = 5.0

_STATE_STYLE = {
    "active": "cyan",
    "cached": "green",
    "stale": "yellow",
    "superseded": "dim",
}

_ARTIFACT_STATE_STYLE = {
    "active": "green",
    "stale": "yellow",
    "superseded": "dim",
    "orphaned": "red",
}


class MainScreen(Screen):
    """Home screen: full-width DAG tree, collapsible detail pane below."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("a", "toggle_artifacts", "Artifacts"),
        Binding("e", "toggle_expanded", "Expanded"),
        Binding("l", "app.push_log", "Log"),
        Binding("slash", "app.open_search", "Search"),
        Binding("exclamation_mark", "app.open_launcher", "Run"),
        Binding("left_square_bracket", "prev_session", "Prev session"),
        Binding("right_square_bracket", "next_session", "Next session"),
        Binding("q", "back", "Back/Quit"),
        Binding("escape", "back", show=False),
        Binding("tab", "toggle_focus", "Focus tree/detail", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    #session-info { padding: 0 1; background: $boost; color: $text; }
    Tree { padding: 0 1; }
    #dag-pane { height: 1fr; }
    #detail-pane { display: none; }
    #detail-pane.-visible { display: block; height: 1fr; border-top: solid $primary; }
    """

    def __init__(self, roar_dir: Path, cwd: Path) -> None:
        super().__init__()
        self.roar_dir = roar_dir
        self.cwd = cwd
        self.show_artifacts = True
        self.expanded = False
        self._reloading = False
        # session_ref is the hash of the session being viewed; None tracks the
        # active session (the default — most users only ever look at the active
        # one). Set by the session picker / `[ ]` navigation.
        self.session_ref: str | None = None
        # Cached session listing for `[`/`]` navigation; refreshed when the
        # picker opens or when we paginate past the cached edges.
        self._session_listing: list[tui_data.SessionListing] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="dag-pane"):
            yield Static("", id="session-info")
            yield Tree("DAG", id="dag-tree")
        with Vertical(id="detail-pane"):
            yield ContentSwitcher(
                Static("", id="empty-detail"),
                JobDetail(id="job-detail"),
                ArtifactDetail(id="artifact-detail"),
                initial="empty-detail",
                id="detail-switcher",
            )
        yield Footer()

    def on_mount(self) -> None:
        self._reload_dag()
        tree = self.query_one("#dag-tree", Tree)
        # Enter must reveal the detail pane without collapsing the step's artifact children.
        tree.auto_expand = False
        tree.focus()
        self.set_interval(REFRESH_INTERVAL_SECONDS, self._reload_dag)

    # --- actions --------------------------------------------------------------

    def action_back(self) -> None:
        """Close the detail pane if open; otherwise exit the app.

        `q` (and `escape`) drive this. `q` is the snappy choice — pure ASCII,
        no escape-sequence ambiguity — while `escape` still works for users
        who reach for it but pays the terminal's ESC-prefix disambiguation
        delay before firing.
        """
        pane = self.query_one("#detail-pane")
        if pane.has_class("-visible"):
            pane.remove_class("-visible")
            self.query_one("#dag-tree", Tree).focus()
        else:
            self.app.exit()

    def action_toggle_focus(self) -> None:
        if not self.query_one("#detail-pane").has_class("-visible"):
            return
        tree = self.query_one("#dag-tree", Tree)
        if tree.has_focus:
            self._focus_detail()
        else:
            tree.focus()

    def _focus_detail(self) -> None:
        switcher = self.query_one("#detail-switcher", ContentSwitcher)
        if switcher.current == "artifact-detail":
            self.query_one("#artifact-detail", ArtifactDetail).focus_body()
        elif switcher.current == "job-detail":
            self.query_one("#job-detail", JobDetail).focus_body()

    def action_prev_session(self) -> None:
        self._step_session(+1)  # newer-first list → +1 means older

    def action_next_session(self) -> None:
        self._step_session(-1)

    def _step_session(self, delta: int) -> None:
        listing = self._refresh_session_listing()
        if not listing:
            return
        current_hash = self._current_session_hash(listing)
        try:
            current_idx = next(
                i for i, s in enumerate(listing) if s.hash == current_hash
            )
        except StopIteration:
            current_idx = 0
        new_idx = current_idx + delta
        if new_idx < 0 or new_idx >= len(listing):
            return  # don't wrap — easy to lose your place
        target = listing[new_idx]
        # None tracks "the active session" — preserves the ACTIVE badge as the
        # active session shifts (e.g., a `roar run` in another terminal).
        self.session_ref = None if target.is_active else target.hash
        self._reload_dag()

    def _refresh_session_listing(self) -> list[tui_data.SessionListing]:
        self._session_listing = tui_data.list_sessions(self.roar_dir)
        return self._session_listing

    def _current_session_hash(self, listing: list[tui_data.SessionListing]) -> str | None:
        if self.session_ref is None:
            active = next((s for s in listing if s.is_active), None)
            return active.hash if active else None
        # Match by full hash or prefix.
        for session in listing:
            if session.hash == self.session_ref or session.hash.startswith(self.session_ref):
                return session.hash
        return None

    def action_toggle_artifacts(self) -> None:
        self.show_artifacts = not self.show_artifacts
        self._reload_dag()

    def action_toggle_expanded(self) -> None:
        self.expanded = not self.expanded
        self._reload_dag()

    # --- external nav hooks (used by search modal) ----------------------------

    def focus_target(self, ref: str) -> None:
        """Move the DAG cursor/detail to the entity referenced by `ref` (`@N`, path, or hash)."""
        tree = self.query_one("#dag-tree", Tree)
        for node in _iter_tree_nodes(tree.root):
            data = node.data
            if not data:
                continue
            if data.get("kind") == "step" and ref.startswith("@") and _step_ref(data) == ref:
                tree.select_node(node)
                tree.scroll_to_node(node)
                return
            if data.get("kind") == "artifact" and (
                ref == data.get("path") or (data.get("hash") and ref.startswith(data["hash"][:8]))
            ):
                tree.select_node(node)
                tree.scroll_to_node(node)
                return
        # Fallback: show directly without changing tree cursor
        self._show_by_ref(ref)

    # --- internals ------------------------------------------------------------

    def _reload_dag(self) -> None:
        info = self.query_one("#session-info", Static)
        tree = self.query_one("#dag-tree", Tree)
        saved_cursor = (
            dict(tree.cursor_node.data)
            if tree.cursor_node is not None and tree.cursor_node.data
            else None
        )
        try:
            session = tui_data.load_session(self.roar_dir, self.session_ref)
            dag = tui_data.load_dag(
                self.roar_dir,
                expanded=self.expanded,
                show_artifacts=self.show_artifacts,
                session_ref=self.session_ref,
            )
        except ValueError as exc:
            info.update(f"[red]{exc}[/red]")
            tree.clear()
            return

        info.update(self._format_session_info(session, dag))

        self._reloading = True
        try:
            tree.clear()
            tree.show_root = False
            artifacts_by_producer: dict[int, list] = {}
            for a in dag.artifacts:
                if a.producer_step is not None:
                    artifacts_by_producer.setdefault(int(a.producer_step), []).append(a)

            for node in dag.nodes:
                label = _step_label(node)
                tree_node = tree.root.add(
                    label,
                    data={
                        "kind": "step",
                        "step_number": node.step_number,
                        "job_type": "build" if node.is_build else "run",
                        "state": node.state if isinstance(node.state, str) else node.state.value,
                    },
                    expand=self.show_artifacts,
                )
                if self.show_artifacts:
                    for artifact in artifacts_by_producer.get(int(node.step_number), []):
                        tree_node.add_leaf(
                            _artifact_label(artifact),
                            data={
                                "kind": "artifact",
                                "path": artifact.path,
                                "hash": artifact.hash,
                            },
                        )

            tree.root.expand_all()
        finally:
            self._reloading = False

        # `move_cursor` depends on TreeNode._line which is only assigned after
        # the tree renders, so defer restoration until the next refresh tick.
        self.call_after_refresh(self._restore_cursor, tree, saved_cursor)

        # If detail pane is open, refresh its content too.
        if self.query_one("#detail-pane").has_class("-visible"):
            cursor = tree.cursor_node
            if cursor is not None and cursor.data:
                self._populate_detail(cursor.data)

    def _format_session_info(self, session, dag) -> str:
        if session is None:
            if self.session_ref is None:
                return "[yellow]No active session[/yellow]"
            return f"[yellow]No session matching {self.session_ref}[/yellow]"
        toggles = []
        if self.expanded:
            toggles.append("expanded")
        if self.show_artifacts:
            toggles.append("artifacts")
        toggle_str = f"  [{' '.join(toggles)}]" if toggles else ""
        # ACTIVE badge when viewing the active session; otherwise show "session"
        # so it's clear we're browsing history (the cue users will look for
        # before muscle-memory `!`-launching a tracked run).
        badge = "[green]ACTIVE[/green]" if self.session_ref is None else "session"
        return (
            f"{badge} [magenta]{session.hash[:12]}[/magenta]  "
            f"{dag.total_steps} steps, {dag.stale_count} stale"
            f"{toggle_str}"
        )

    def _restore_cursor(self, tree: Tree, saved: dict | None) -> None:
        # `move_cursor` (vs. `select_node`) shifts the cursor without firing
        # NodeSelected — important so reload doesn't auto-open the detail pane.
        if saved is not None:
            for node in _iter_tree_nodes(tree.root):
                if node.data and _cursor_matches(node.data, saved):
                    tree.move_cursor(node)
                    tree.scroll_to_node(node)
                    return
        # No saved cursor (first load) — land on the first step so Enter works immediately.
        if tree.root.children:
            tree.move_cursor(tree.root.children[0])

    def _show_by_ref(self, ref: str) -> None:
        if ref.startswith("@"):
            job = tui_data.load_job(
                self.roar_dir, self.cwd, ref, session_ref=self.session_ref
            )
            if job:
                self._populate_detail({"kind": "step", "step_number": int(ref.lstrip("@B"))})
                return
        artifact = tui_data.load_artifact_by_path(self.roar_dir, self.cwd, ref)
        if artifact is None and ref and not ref.startswith("/") and "/" not in ref:
            artifact = tui_data.load_artifact_by_hash(self.roar_dir, self.cwd, ref)
        if artifact:
            self.query_one("#artifact-detail", ArtifactDetail).update_artifact(artifact)
            self.query_one("#detail-switcher", ContentSwitcher).current = "artifact-detail"
            self._reveal_detail()

    def _populate_detail(self, data: dict) -> None:
        switcher = self.query_one("#detail-switcher", ContentSwitcher)
        if data.get("kind") == "step":
            ref = _step_ref(data)
            job = tui_data.load_job(
                self.roar_dir, self.cwd, ref, session_ref=self.session_ref
            )
            if job is None:
                switcher.current = "empty-detail"
                return
            self.query_one("#job-detail", JobDetail).update_job(job)
            switcher.current = "job-detail"
        elif data.get("kind") == "artifact":
            path = data.get("path")
            ahash = data.get("hash")
            artifact = None
            if path:
                artifact = tui_data.load_artifact_by_path(self.roar_dir, self.cwd, path)
            if artifact is None and ahash:
                artifact = tui_data.load_artifact_by_hash(self.roar_dir, self.cwd, ahash)
            if artifact is None:
                switcher.current = "empty-detail"
                return
            self.query_one("#artifact-detail", ArtifactDetail).update_artifact(artifact)
            switcher.current = "artifact-detail"

    def _reveal_detail(self) -> None:
        pane = self.query_one("#detail-pane")
        if not pane.has_class("-visible"):
            pane.add_class("-visible")

    # --- tree events ----------------------------------------------------------

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if self._reloading:
            return
        data = event.node.data
        if not data:
            return
        self._populate_detail(data)
        self._reveal_detail()
        self._focus_detail()

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if self._reloading:
            return
        if not self.query_one("#detail-pane").has_class("-visible"):
            return
        data = event.node.data
        if data:
            self._populate_detail(data)


def _step_ref(data: dict) -> str:
    prefix = "@B" if data.get("job_type") == "build" else "@"
    return f"{prefix}{data['step_number']}"


def _step_label(node) -> Text:
    state = node.state if isinstance(node.state, str) else node.state.value
    style = _STATE_STYLE.get(state, "")
    ref = f"@B{node.step_number}" if node.is_build else f"@{node.step_number}"
    t = Text()
    t.append(f"{ref}  ", style="bold")
    t.append(f"[{state}] ", style=style)
    cmd = (node.command or "").strip()
    if len(cmd) > 80:
        cmd = cmd[:77] + "…"
    t.append(cmd)
    if node.exit_code not in (None, 0):
        t.append(f"  ✗{node.exit_code}", style="red")
    return t


def _artifact_label(artifact) -> Text:
    state = artifact.state if isinstance(artifact.state, str) else artifact.state.value
    style = _ARTIFACT_STATE_STYLE.get(state, "")
    t = Text()
    t.append("⬇ ", style="dim")
    path = artifact.path or "-"
    if len(path) > 60:
        path = "…" + path[-59:]
    t.append(path)
    t.append(f"  [{state}]", style=style)
    if artifact.hash:
        t.append(f"  {artifact.hash[:8]}", style="magenta")
    return t


def _iter_tree_nodes(node: TreeNode):
    yield node
    for child in node.children:
        yield from _iter_tree_nodes(child)


def _cursor_matches(current: dict, saved: dict) -> bool:
    kind = current.get("kind")
    if kind != saved.get("kind"):
        return False
    if kind == "step":
        return (
            current.get("step_number") == saved.get("step_number")
            and current.get("job_type") == saved.get("job_type")
        )
    if kind == "artifact":
        if saved.get("hash") and current.get("hash") == saved["hash"]:
            return True
        return bool(saved.get("path")) and current.get("path") == saved["path"]
    return False
